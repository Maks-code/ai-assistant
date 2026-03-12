from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass
class ActionRecord:
    action_id: str
    action_type: str
    process: str
    title: str
    details: str
    requester: str
    status: str
    created_at: str
    is_anonymous: bool = False
    topic_id: str = ""
    topic_name: str = ""
    pu_name: str = ""
    routing_block_id: str = ""
    routing_block: str = ""
    recipient_email: str = ""
    gap_signature: str = ""
    occurrences: int = 0
    last_seen_at: str = ""


class ActionsStore:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self.file_path.write_text("", encoding="utf-8")

    def create_action(
        self,
        action_type: str,
        process: str,
        title: str,
        details: str,
        requester: str,
        *,
        status: str = "Черновик (локально)",
        is_anonymous: bool = False,
        topic_id: str = "",
        topic_name: str = "",
        pu_name: str = "",
        routing_block_id: str = "",
        routing_block: str = "",
        recipient_email: str = "",
        gap_signature: str = "",
        occurrences: int = 0,
        last_seen_at: str = "",
    ) -> ActionRecord:
        created_at = datetime.now(timezone.utc).isoformat()
        normalized_occurrences = max(0, int(occurrences))
        if action_type == "kb_gap_from_chat" and normalized_occurrences < 1:
            normalized_occurrences = 1
        normalized_last_seen = (last_seen_at or "").strip() or (created_at if normalized_occurrences else "")

        record = ActionRecord(
            action_id=f"ACT-{uuid4().hex[:8].upper()}",
            action_type=action_type,
            process=process,
            title=title,
            details=details,
            requester=requester or "Не указан",
            status=status,
            created_at=created_at,
            is_anonymous=is_anonymous,
            topic_id=topic_id,
            topic_name=topic_name,
            pu_name=pu_name,
            routing_block_id=routing_block_id,
            routing_block=routing_block,
            recipient_email=recipient_email,
            gap_signature=gap_signature.strip(),
            occurrences=normalized_occurrences,
            last_seen_at=normalized_last_seen,
        )
        self._append_record(record)
        return record

    def list_actions(self, limit: int = 50) -> list[dict[str, Any]]:
        items = list(reversed(self._read_all_records()))
        return items[: max(limit, 0)]

    def update_action_status(
        self,
        action_id: str,
        status: str,
        status_comment: str = "",
    ) -> dict[str, Any] | None:
        items = self._read_all_records()
        if not items:
            return None

        now_iso = datetime.now(timezone.utc).isoformat()
        updated_item: dict[str, Any] | None = None

        for item in items:
            if str(item.get("action_id", "")).strip() != action_id:
                continue

            item["status"] = status
            item["updated_at"] = now_iso
            if status_comment:
                item["status_comment"] = status_comment
            else:
                item.pop("status_comment", None)

            if status == "Закрыто":
                item["closed_at"] = now_iso
            else:
                item.pop("closed_at", None)

            updated_item = dict(item)
            break

        if not updated_item:
            return None

        self._write_all_records(items)
        return updated_item

    def upsert_kb_gap(
        self,
        *,
        question: str,
        signature: str,
        routing_block: str,
        recipient_email: str,
        window_hours: int = 24,
    ) -> tuple[dict[str, Any], bool]:
        signature_value = signature.strip()
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        window_start = now_dt - timedelta(hours=max(1, int(window_hours)))

        items = self._read_all_records()
        matched_item: dict[str, Any] | None = None
        matched_index = -1

        for idx, item in enumerate(items):
            if str(item.get("action_type", "")).strip() != "kb_gap_from_chat":
                continue
            if str(item.get("gap_signature", "")).strip() != signature_value:
                continue

            reference_dt = self._parse_record_dt(item.get("last_seen_at")) or self._parse_record_dt(item.get("created_at"))
            if not reference_dt or reference_dt < window_start:
                continue

            matched_item = item
            matched_index = idx

        if matched_item is not None:
            current_occurrences = int(matched_item.get("occurrences", 1) or 1)
            matched_item["occurrences"] = current_occurrences + 1
            matched_item["last_seen_at"] = now_iso
            matched_item["updated_at"] = now_iso
            matched_item["details"] = question
            matched_item["status"] = "Новая"
            items[matched_index] = matched_item
            self._write_all_records(items)
            return dict(matched_item), False

        record = self.create_action(
            action_type="kb_gap_from_chat",
            process="Поддержка базы знаний",
            title="Нет ответа в базе знаний по запросу",
            details=question,
            requester="Система",
            status="Новая",
            routing_block="Администратор базы знаний" if not routing_block.strip() else routing_block.strip(),
            recipient_email=recipient_email.strip(),
            gap_signature=signature_value,
            occurrences=1,
            last_seen_at=now_iso,
        )
        return {
            "action_id": record.action_id,
            "action_type": record.action_type,
            "process": record.process,
            "title": record.title,
            "details": record.details,
            "requester": record.requester,
            "status": record.status,
            "created_at": record.created_at,
            "is_anonymous": record.is_anonymous,
            "topic_id": record.topic_id,
            "topic_name": record.topic_name,
            "pu_name": record.pu_name,
            "routing_block_id": record.routing_block_id,
            "routing_block": record.routing_block,
            "recipient_email": record.recipient_email,
            "gap_signature": record.gap_signature,
            "occurrences": record.occurrences,
            "last_seen_at": record.last_seen_at,
        }, True

    def _append_record(self, record: ActionRecord) -> None:
        payload = {
            "action_id": record.action_id,
            "action_type": record.action_type,
            "process": record.process,
            "title": record.title,
            "details": record.details,
            "requester": record.requester,
            "status": record.status,
            "created_at": record.created_at,
            "is_anonymous": record.is_anonymous,
            "topic_id": record.topic_id,
            "topic_name": record.topic_name,
            "pu_name": record.pu_name,
            "routing_block_id": record.routing_block_id,
            "routing_block": record.routing_block,
            "recipient_email": record.recipient_email,
            "gap_signature": record.gap_signature,
            "occurrences": record.occurrences,
            "last_seen_at": record.last_seen_at,
            "integration_note": "MVP: действие зарегистрировано локально, в корпоративные ИС не отправляется.",
        }
        with self.file_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _read_all_records(self) -> list[dict[str, Any]]:
        if not self.file_path.exists():
            return []

        lines = self.file_path.read_text(encoding="utf-8").splitlines()
        items: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                items.append(payload)
        return items

    def _write_all_records(self, items: list[dict[str, Any]]) -> None:
        with self.file_path.open("w", encoding="utf-8") as file:
            for item in items:
                file.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _parse_record_dt(self, value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
