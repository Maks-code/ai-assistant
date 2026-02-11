#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class EvalCaseResult:
    case_id: str
    ok: bool
    skipped: bool
    reasons: list[str]
    no_exact_match: bool
    source_paths: list[str]
    answer_preview: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local eval suite for the assistant.")
    parser.add_argument("--cases", default="eval/cases.json", help="Path to JSON eval cases file.")
    parser.add_argument("--output", default="eval/reports/latest.json", help="Path for report output JSON.")
    parser.add_argument("--mode", choices=["http", "local"], default="local", help="How to call the app.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="API URL (for --mode http).")
    parser.add_argument("--stub-llm", action="store_true", help="In local mode, stub LLM calls for deterministic/no-network eval.")
    parser.add_argument("--fail-on-errors", action="store_true", help="Return non-zero exit code if any case fails.")
    return parser.parse_args()


class AskClient:
    def __init__(self, mode: str, base_url: str, stub_llm: bool) -> None:
        self.mode = mode
        self.base_url = base_url.rstrip("/")
        self.stub_llm = stub_llm
        self._local_client = None

        if mode == "local":
            self._init_local_client(stub_llm)

    def _init_local_client(self, stub_llm: bool) -> None:
        from fastapi.testclient import TestClient

        import app.main as main_app

        if stub_llm:
            # Disable network dependence for eval; we validate retrieval/source behavior here.
            main_app.llm_service.enable_rerank = False
            main_app.llm_service.client = None
            main_app.llm_service.generate_answer = lambda question, context_results, intent="procedure": "STUB_ANSWER"

        self._local_client = TestClient(main_app.app)
        self._local_client.post("/api/reindex")

    def ask(self, question: str, session_id: str) -> dict[str, Any]:
        payload = {"question": question, "session_id": session_id}

        if self.mode == "local":
            assert self._local_client is not None
            response = self._local_client.post("/api/ask", json=payload)
            return {"status": response.status_code, "body": response.json()}

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/ask",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return {"status": response.status, "body": json.loads(body)}
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8") if error.fp else ""
            parsed = {}
            if body:
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = {"detail": body}
            return {"status": error.code, "body": parsed}



def source_process(relative_path: str) -> str:
    return relative_path.split("/", 1)[0] if "/" in relative_path else "Общее"



def run_case(client: AskClient, case: dict[str, Any], idx: int) -> EvalCaseResult:
    case_id = str(case.get("id") or f"case_{idx}")
    session_id = str(case.get("session_id") or f"eval-{case_id}")

    if client.stub_llm and case.get("skip_when_stub_llm"):
        return EvalCaseResult(
            case_id=case_id,
            ok=True,
            skipped=True,
            reasons=["skipped in stub-llm mode"],
            no_exact_match=True,
            source_paths=[],
            answer_preview="",
        )

    for pre_step in case.get("pre_steps", []):
        pre_question = str(pre_step["question"])
        pre_session = str(pre_step.get("session_id") or session_id)
        client.ask(pre_question, pre_session)

    response = client.ask(str(case["question"]), session_id)
    status = response["status"]
    body = response["body"]

    reasons: list[str] = []
    if status != 200:
        detail = body.get("detail") if isinstance(body, dict) else str(body)
        reasons.append(f"HTTP {status}: {detail}")
        return EvalCaseResult(
            case_id=case_id,
            ok=False,
            skipped=False,
            reasons=reasons,
            no_exact_match=True,
            source_paths=[],
            answer_preview="",
        )

    no_exact_match = bool(body.get("no_exact_match", True))
    answer = str(body.get("answer", ""))
    sources = body.get("sources", []) or []
    source_paths = [str(item.get("relative_path", "")) for item in sources]

    expected_no_exact_match = case.get("expected_no_exact_match")
    if expected_no_exact_match is not None and bool(expected_no_exact_match) != no_exact_match:
        reasons.append(f"expected_no_exact_match={expected_no_exact_match}, got={no_exact_match}")

    expected_source_any = case.get("expected_source_any") or []
    if expected_source_any:
        if not any(any(token in path for token in expected_source_any) for path in source_paths):
            reasons.append(f"no source matched any of {expected_source_any}")

    expected_source_all = case.get("expected_source_all") or []
    if expected_source_all:
        for token in expected_source_all:
            if not any(token in path for path in source_paths):
                reasons.append(f"no source matched required token '{token}'")

    expected_process = case.get("expected_process")
    if expected_process:
        if not any(source_process(path) == expected_process for path in source_paths):
            reasons.append(f"no source from expected process '{expected_process}'")

    if case.get("expected_single_process") and source_paths:
        processes = {source_process(path) for path in source_paths}
        if len(processes) > 1:
            reasons.append(f"expected single process in sources, got {sorted(processes)}")

    min_sources = case.get("min_sources")
    if min_sources is not None and len(source_paths) < int(min_sources):
        reasons.append(f"expected at least {min_sources} sources, got {len(source_paths)}")

    max_sources = case.get("max_sources")
    if max_sources is not None and len(source_paths) > int(max_sources):
        reasons.append(f"expected at most {max_sources} sources, got {len(source_paths)}")

    contains_any = case.get("contains_in_answer_any") or []
    if contains_any and not any(token.lower() in answer.lower() for token in contains_any):
        reasons.append(f"answer does not contain any of {contains_any}")

    forbid_any = case.get("forbid_in_answer") or []
    for token in forbid_any:
        if token.lower() in answer.lower():
            reasons.append(f"answer contains forbidden token '{token}'")

    return EvalCaseResult(
        case_id=case_id,
        ok=not reasons,
        skipped=False,
        reasons=reasons,
        no_exact_match=no_exact_match,
        source_paths=source_paths,
        answer_preview=answer[:220],
    )



def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("cases file must be a JSON array")
    return payload



def build_report(results: list[EvalCaseResult], mode: str, cases_path: str) -> dict[str, Any]:
    total = len(results)
    skipped = sum(1 for item in results if item.skipped)
    executed = total - skipped
    passed = sum(1 for item in results if item.ok and not item.skipped)
    failed = sum(1 for item in results if (not item.ok) and (not item.skipped))
    no_exact = sum(1 for item in results if item.no_exact_match and not item.skipped)

    report_results: list[dict[str, Any]] = []
    for item in results:
        report_results.append(
            {
                "id": item.case_id,
                "ok": item.ok,
                "skipped": item.skipped,
                "reasons": item.reasons,
                "no_exact_match": item.no_exact_match,
                "source_paths": item.source_paths,
                "answer_preview": item.answer_preview,
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "cases_path": cases_path,
        "summary": {
            "total": total,
            "executed": executed,
            "skipped": skipped,
            "passed": passed,
            "failed": failed,
            "pass_rate": round((passed / executed) if executed else 0.0, 4),
            "fallback_rate": round((no_exact / executed) if executed else 0.0, 4),
        },
        "results": report_results,
    }



def main() -> int:
    args = parse_args()
    cases_path = Path(args.cases)
    output_path = Path(args.output)

    cases = load_cases(cases_path)
    client = AskClient(mode=args.mode, base_url=args.base_url, stub_llm=args.stub_llm)

    results: list[EvalCaseResult] = []
    for idx, case in enumerate(cases, start=1):
        results.append(run_case(client, case, idx))

    report = build_report(results, mode=args.mode, cases_path=str(cases_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = report["summary"]
    print(
        f"Eval done: total={summary['total']} passed={summary['passed']} failed={summary['failed']} "
        f"pass_rate={summary['pass_rate']} fallback_rate={summary['fallback_rate']}"
    )
    print(f"Report: {output_path}")

    if args.fail_on_errors and summary["failed"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
