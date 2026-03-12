from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.appeals_catalog import APPEAL_BLOCKS, APPEAL_TOPICS, list_topics_for_ui, resolve_topic
from app.actions import ActionsStore
from app.config import load_settings
from app.dialog_state import DialogStateStore
from app.kb import KnowledgeBaseIndex, RetrievalResult
from app.llm import FALLBACK_EN, FALLBACK_RU, LLMService
from app.logging_utils import RequestLogger
from app.profile import (
    ALLOWED_DIVISIONS,
    BRANCH_SUBDIVISIONS,
    ProfileStore,
    UserProfile,
)


settings = load_settings()
kb_index = KnowledgeBaseIndex(settings.kb_root)
kb_lock = Lock()
llm_service = LLMService(
    settings.openai_api_key,
    settings.openai_model,
    enable_rerank=settings.enable_llm_rerank,
    rerank_candidates=settings.rerank_candidates,
)
request_logger = RequestLogger(settings.log_file)
actions_store = ActionsStore(settings.log_file.parent / "actions.log")
profile_store = ProfileStore(settings.log_file.parent / "profile.json")
dialog_state = DialogStateStore(ttl_minutes=20)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    session_id: str | None = None


class AskResponse(BaseModel):
    answer: str
    sources: list[dict]
    no_exact_match: bool


class ActionCreateRequest(BaseModel):
    action_type: str = Field(..., min_length=2)
    process: str = Field(..., min_length=2)
    title: str = Field(..., min_length=2)
    details: str = Field(..., min_length=2)
    requester: str = Field(default="")


class AnonymousAppealCreateRequest(BaseModel):
    topic_id: str = Field(..., min_length=3, max_length=80)
    details: str = Field(..., min_length=3, max_length=2000)
    pu_name: str = Field(default="", max_length=120)


class AppealStatusUpdateRequest(BaseModel):
    status: str = Field(..., min_length=2, max_length=64)
    comment: str = Field(default="", max_length=500)


class AdminFallbackStatusUpdateRequest(BaseModel):
    status: str = Field(..., min_length=2, max_length=64)
    comment: str = Field(default="", max_length=500)


class DialogClearRequest(BaseModel):
    session_id: str | None = None


class ProfileRequest(BaseModel):
    full_name: str = Field(..., min_length=3, max_length=120)
    division: str = Field(..., min_length=2, max_length=64)
    subdivision: str | None = Field(default=None, max_length=64)
    subdivision_type: str | None = Field(default=None, max_length=64)
    job_title: str = Field(..., min_length=2, max_length=120)
    email: str = Field(..., min_length=5, max_length=160)


PARTICIPANT_QUERY_RE = re.compile(
    r"(участник\w*|актор\w*|роль\w*|заявител\w*|заказчик\w*|исполнител\w*|participant\w*|actor\w*|role\w*)",
    re.IGNORECASE,
)

RESPONSIBILITY_QUERY_RE = re.compile(
    r"((кто|какой\s+участник)\s+"
    r"(должен|отвеча\w*|дела\w*|созда\w*|заполня\w*|назнача\w*|принима\w*|акцепт\w*|подтвержда\w*|согласовыва\w*|согласу\w*|веде\w*|оформля\w*|курир\w*|подписыва\w*|подпис\w*)"
    r"|кто\s+за\s+что|who\s+(should|is\s+responsible|approves|assigns|creates|fills|confirms))",
    re.IGNORECASE,
)

ACTION_HINT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("доступ", re.compile(r"доступ\w*", re.IGNORECASE)),
    ("справоч", re.compile(r"справоч\w*", re.IGNORECASE)),
    ("акцепт", re.compile(r"акцепт\w*", re.IGNORECASE)),
    ("назнач", re.compile(r"назнач\w*|водител\w*|\bтс\b|транспорт\w*", re.IGNORECASE)),
    ("заявк", re.compile(r"заявк\w*|задани\w*", re.IGNORECASE)),
    ("факт", re.compile(r"фактич\w*|путев\w*", re.IGNORECASE)),
    ("ретрансляц", re.compile(r"ретрансляц\w*|глонасс\w*", re.IGNORECASE)),
    ("пп", re.compile(r"производствен\w*\s+программ\w*|\bпп\b", re.IGNORECASE)),
    ("журнал", re.compile(r"журнал\w*|раздел\w*", re.IGNORECASE)),
    ("ид", re.compile(r"исполнительн\w*\s+документац\w*|аоср|акт\w*", re.IGNORECASE)),
    ("сэд", re.compile(r"\bсэд\b|документооборот\w*|соглас\w*|подпис\w*", re.IGNORECASE)),
    ("замечан", re.compile(r"замечан\w*|доработк\w*", re.IGNORECASE)),
]

ACTION_ROLE_WEIGHTS: dict[str, dict[str, float]] = {
    "доступ": {
        "ЦДС": 2.0,
        "Администратор/координатор ЦУС": 2.3,
    },
    "справоч": {
        "Специалист по транспорту филиала/ГО": 2.0,
        "Подрядная организация": 0.5,
        "Координатор объекта": 0.9,
        "ЦДС": 0.75,
    },
    "акцепт": {
        "Специалист по транспорту филиала/ГО": 2.4,
        "Подрядная организация": 0.45,
        "Согласующее/подписывающее лицо": 1.35,
    },
    "назнач": {
        "Подрядная организация": 1.8,
        "Ответственный сотрудник ПУ": 1.15,
        "Инженер строительного контроля": 1.05,
    },
    "заявк": {
        "Ответственный сотрудник ПУ": 1.5,
    },
    "ретрансляц": {
        "Ответственный сотрудник ПУ": 1.2,
        "ЦДС": 1.1,
    },
    "журнал": {
        "Инженер строительного контроля": 1.9,
        "Подрядная организация": 1.3,
        "Координатор объекта": 1.1,
    },
    "ид": {
        "Инженер строительного контроля": 1.8,
        "Подрядная организация": 1.25,
        "Координатор объекта": 1.1,
        "Согласующее/подписывающее лицо": 1.05,
    },
    "сэд": {
        "Согласующее/подписывающее лицо": 1.9,
        "Координатор объекта": 1.2,
        "Администратор/координатор ЦУС": 1.1,
    },
    "замечан": {
        "Инженер строительного контроля": 1.45,
        "Подрядная организация": 1.2,
        "Координатор объекта": 1.1,
    },
}

ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ЦДС", re.compile(r"(\bцдс\b|участник\s*1)", re.IGNORECASE)),
    (
        "Специалист по транспорту филиала/ГО",
        re.compile(
            r"(специалист\w*\s+по\s+транспорт\w+[\s\S]{0,40}(филиал\w*|го)"
            r"|(филиал\w*|го)[\s\S]{0,40}специалист\w*\s+по\s+транспорт\w+"
            r"|участник\s*2|участник\s*5)",
            re.IGNORECASE,
        ),
    ),
    (
        "Ответственный сотрудник ПУ",
        re.compile(
            r"(ответствен\w*\s+сотрудник\w*[\s\S]{0,16}\bпу\b"
            r"|сотрудник\w*[\s\S]{0,16}\bпу\b[\s\S]{0,24}ответствен\w*"
            r"|специалист\w*[\s\S]{0,12}\bпу\b|участник\s*3)",
            re.IGNORECASE,
        ),
    ),
    (
        "Подрядная организация",
        re.compile(
            r"(подрядн\w*\s+организац\w*[\s\S]{0,20}транспорт\w*"
            r"|участник\s*4"
            r"|контрагент\w*[\s\S]{0,24}(назнач\w*|подтвержд\w*|отклон\w*)"
            r"|(назнач\w*|подтвержд\w*|отклон\w*)[\s\S]{0,24}контрагент\w*"
            r"|подрядчик\w*)",
            re.IGNORECASE,
        ),
    ),
    (
        "Администратор/координатор ЦУС",
        re.compile(
            r"(администратор\w*[\s\S]{0,24}\bцус\b"
            r"|координатор\w*[\s\S]{0,24}\bцус\b"
            r"|доступ\w*[\s\S]{0,24}\bцус\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "Координатор объекта",
        re.compile(
            r"(координатор\w*[\s\S]{0,24}объект\w*"
            r"|ответствен\w*[\s\S]{0,20}объект\w*"
            r"|\bпто\b|техзаказчик\w*)",
            re.IGNORECASE,
        ),
    ),
    (
        "Инженер строительного контроля",
        re.compile(
            r"(инженер\w*[\s\S]{0,30}(строительн\w*\s+контрол\w*|стройконтрол\w*)"
            r"|строительн\w*\s+контрол\w*)",
            re.IGNORECASE,
        ),
    ),
    (
        "Представитель подрядной организации",
        re.compile(
            r"(представител\w*[\s\S]{0,22}подрядн\w*\s+организац\w*"
            r"|представител\w*[\s\S]{0,16}подрядчик\w*)",
            re.IGNORECASE,
        ),
    ),
    (
        "Согласующее/подписывающее лицо",
        re.compile(
            r"(согласующ\w*|подписыва\w*|подписант\w*|подписани\w*|подписание\s+эцп|\bэ[цп]\b)",
            re.IGNORECASE,
        ),
    ),
]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
APPEAL_ALLOWED_STATUSES = (
    "Зарегистрировано",
    "В работе",
    "Нужны уточнения",
    "Закрыто",
)
ADMIN_FALLBACK_ALLOWED_STATUSES = (
    "Новая",
    "В работе",
    "Закрыто",
)
KB_GAP_ROUTING_BLOCK = "Администратор базы знаний"
KB_GAP_RECIPIENT_EMAIL = "admin_kb@mail.ru"
KB_GAP_DEDUP_HOURS = 24
KB_GAP_STOPWORDS = {
    "как",
    "что",
    "где",
    "когда",
    "чтобы",
    "или",
    "для",
    "это",
    "нет",
    "база",
    "базе",
    "базы",
    "знаний",
    "информация",
    "инфы",
    "процесс",
    "процесса",
    "процессе",
}
AVAILABLE_PU_NAMES = tuple(
    sorted(
        {
            subdivision.strip()
            for subdivisions in BRANCH_SUBDIVISIONS.values()
            for subdivision in subdivisions
            if subdivision.strip().lower().startswith("пу ")
        }
    )
)
AVAILABLE_PU_LOOKUP = {item.casefold(): item for item in AVAILABLE_PU_NAMES}


def _profile_to_dict(profile: UserProfile) -> dict[str, str]:
    return {
        "full_name": profile.full_name,
        "division": profile.division,
        "subdivision": profile.subdivision or "",
        # Keep old key for backward compatibility with existing frontend cache.
        "subdivision_type": profile.subdivision or "",
        "job_title": profile.job_title,
        "email": profile.email,
    }


def _normalize_profile_payload(payload: ProfileRequest) -> UserProfile:
    full_name = re.sub(r"\s+", " ", payload.full_name).strip()
    division = payload.division.strip()
    subdivision = (payload.subdivision or payload.subdivision_type or "").strip()
    job_title = re.sub(r"\s+", " ", payload.job_title).strip()
    email = payload.email.strip().lower()

    if division not in ALLOWED_DIVISIONS:
        raise HTTPException(status_code=400, detail="Некорректное подразделение.")

    if division == "ЦА":
        subdivision = ""
    else:
        allowed_subdivisions = BRANCH_SUBDIVISIONS.get(division, ())
        if subdivision not in allowed_subdivisions:
            raise HTTPException(
                status_code=400,
                detail=f"Для '{division}' укажите корректное ПУ/АУП.",
            )

    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Некорректный email.")

    return UserProfile(
        full_name=full_name,
        division=division,
        subdivision=subdivision or None,
        job_title=job_title,
        email=email,
    )


def detect_language(question: str) -> str:
    en_count = len(re.findall(r"[A-Za-z]", question))
    ru_count = len(re.findall(r"[А-Яа-яЁё]", question))
    if en_count > max(ru_count * 1.2, 3):
        return "en"
    return "ru"


def is_participant_question(question: str) -> bool:
    return bool(PARTICIPANT_QUERY_RE.search(question))


def is_responsibility_question(question: str) -> bool:
    return bool(RESPONSIBILITY_QUERY_RE.search(question))


def extract_action_hints(question: str) -> set[str]:
    hints: set[str] = set()
    for stem, pattern in ACTION_HINT_PATTERNS:
        if pattern.search(question):
            hints.add(stem)
    return hints


def extract_participants_from_results(
    results: list[RetrievalResult],
    process_hint: str | None = None,
    action_hints: set[str] | None = None,
    min_relative_score: float = 0.55,
    max_items: int = 10,
) -> list[str]:
    score_by_label: Counter[str] = Counter()

    for rank, result in enumerate(results):
        chunk = result.chunk
        if process_hint and chunk.process != process_hint:
            continue

        rank_bonus = max(0.2, 1.6 - (rank * 0.06))
        chunk_weight = (result.score * 0.35) + (result.coverage * 2.2) + rank_bonus
        chunk_text_lower = chunk.text.lower()

        if action_hints:
            action_hits = sum(1 for stem in action_hints if stem in chunk_text_lower)
            if action_hits == 0:
                chunk_weight *= 0.35
            else:
                chunk_weight *= 1.0 + min(0.4, action_hits * 0.12)

        for label, pattern in ROLE_PATTERNS:
            if pattern.search(chunk.text):
                label_weight = 1.0
                if action_hints:
                    for stem in action_hints:
                        role_weights = ACTION_ROLE_WEIGHTS.get(stem)
                        if not role_weights:
                            continue
                        label_weight *= role_weights.get(label, 0.95)
                label_weight = min(max(label_weight, 0.35), 2.8)
                score_by_label[label] += chunk_weight * label_weight

    ordered = score_by_label.most_common(max_items)
    if not ordered:
        return []

    top_score = ordered[0][1]
    min_score = max(1.2, top_score * min_relative_score)
    filtered = [label for label, score in ordered if score >= min_score]
    return filtered[:max_items]


def build_participants_answer(language: str, participants: list[str], process_hint: str | None) -> str:
    subject = process_hint.replace("_", " ") if process_hint else "указанном процессе"
    if language == "en":
        lines = "\n".join(f"- {item}" for item in participants)
        return f"Found process participant roles in {subject}:\n{lines}"

    lines = "\n".join(f"- {item}" for item in participants)
    return f"В базе знаний найдены участники (роли) заявочного процесса в {subject}:\n{lines}"


def build_responsibility_answer(language: str, participants: list[str], process_hint: str | None) -> str:
    subject = process_hint.replace("_", " ") if process_hint else "указанном процессе"
    top = participants[:2]
    if language == "en":
        lines = "\n".join(f"- {item}" for item in top)
        return f"According to the knowledge base, for {subject} this is handled by:\n{lines}"

    lines = "\n".join(f"- {item}" for item in top)
    if len(top) == 1:
        return f"По базе знаний в {subject} это выполняет:\n{lines}"
    return f"По базе знаний в {subject} это обычно выполняют:\n{lines}"


def normalize_session_id(session_id: str | None) -> str:
    value = (session_id or "").strip()
    if not value:
        return "local-default"
    if len(value) > 96:
        return value[:96]
    return value


def _source_extension(source: dict) -> str:
    return Path(str(source.get("relative_path", ""))).suffix.lower()


def _source_process(source: dict) -> str:
    relative_path = str(source.get("relative_path", ""))
    return relative_path.split("/", 1)[0] if "/" in relative_path else "Общее"


def _serialize_action_for_ui(action: dict) -> dict:
    payload = dict(action)
    if payload.get("is_anonymous"):
        payload.pop("requester", None)
    return payload


def _normalize_pu_name(raw_value: str | None) -> str:
    value = (raw_value or "").strip()
    if not value:
        return ""
    return AVAILABLE_PU_LOOKUP.get(value.casefold(), "")


def _build_gap_signature(question: str) -> str:
    tokens = [token.casefold() for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", question)]
    filtered: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if len(token) < 3:
            continue
        if token in KB_GAP_STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        filtered.append(token)
        if len(filtered) >= 24:
            break

    if filtered:
        return " ".join(filtered)[:280]

    normalized = re.sub(r"\s+", " ", question.casefold()).strip()
    return normalized[:280]


def _register_kb_gap_from_chat(question: str) -> None:
    signature = _build_gap_signature(question)
    if not signature:
        return
    actions_store.upsert_kb_gap(
        question=question,
        signature=signature,
        routing_block=KB_GAP_ROUTING_BLOCK,
        recipient_email=KB_GAP_RECIPIENT_EMAIL,
        window_hours=KB_GAP_DEDUP_HOURS,
    )


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _anonymous_appeals(limit: int = 2000) -> list[dict]:
    items = actions_store.list_actions(limit=limit)
    return [item for item in items if item.get("action_type") == "anonymous_appeal"]


def _kb_gap_actions(limit: int = 5000) -> list[dict]:
    items = actions_store.list_actions(limit=limit)
    return [item for item in items if item.get("action_type") == "kb_gap_from_chat"]


def _dominant_context_process(context_results: list[RetrievalResult]) -> str | None:
    if not context_results:
        return None

    counts: Counter[str] = Counter(item.chunk.process for item in context_results)
    top_process, top_count = counts.most_common(1)[0]
    total = sum(counts.values())
    share = top_count / max(total, 1)

    if len(counts) == 1:
        return top_process
    if top_count >= 2 and share >= 0.6:
        return top_process
    return None


def _dedupe_sources(sources: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for source in sources:
        key = str(source.get("relative_path", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(source)
    return result


GENERIC_SOURCE_QUERY_TOKENS = {
    "систем",
    "ис",
    "инструкц",
    "документ",
    "документа",
    "файл",
    "процесс",
    "процес",
    "порядок",
    "вопрос",
    "информац",
    "ответ",
}

SEMANTIC_QUERY_EXPANSIONS: dict[str, set[str]] = {
    "ретрансляц": {"повторн", "отправк", "телематич", "данных"},
    "глонасс": {"телематич", "данных", "пробег"},
    "доступ": {"заявк", "форм"},
}


def _semantic_query_tokens(question: str, process_hint: str | None) -> set[str]:
    tokens = set(kb_index.query_tokens(question))
    process_tokens = kb_index.process_token_set(process_hint)
    result: set[str] = set()
    for token in tokens:
        if token.startswith("syn:"):
            continue
        if token in process_tokens:
            continue
        if token in GENERIC_SOURCE_QUERY_TOKENS:
            continue
        if len(token) < 3:
            continue
        result.add(token)

    expanded: set[str] = set()
    for token in result:
        for stem, additions in SEMANTIC_QUERY_EXPANSIONS.items():
            if token.startswith(stem):
                expanded.update(additions)
    result.update(expanded)

    return result


def _rank_related_sources_by_question(
    question: str,
    process_hint: str | None,
    related_sources: list[dict],
) -> list[dict]:
    semantic_tokens = _semantic_query_tokens(question, process_hint)
    if not semantic_tokens:
        return []

    scored: list[tuple[float, int, dict]] = []
    for source in related_sources:
        relative_path = str(source.get("relative_path", ""))
        record = kb_index.documents.get(relative_path)
        if not record:
            continue

        overlap = semantic_tokens & record.metadata_tokens
        if not overlap:
            continue

        score = sum(kb_index.token_idf.get(token, 1.0) for token in overlap)
        overlap_count = len(overlap)
        if overlap_count < 1:
            continue
        if overlap_count == 1 and score < 0.75:
            continue

        scored.append((score, overlap_count, source))

    scored.sort(key=lambda item: (item[1], item[0]), reverse=True)
    return [item[2] for item in scored]


def _role_query_process_fallback_sources(question: str, process_hint: str | None) -> list[dict]:
    if not process_hint:
        return []

    docs = kb_index.list_documents(query=None, process=process_hint, forms_only=False)
    non_md_docs = [doc for doc in docs if Path(str(doc.get("relative_path", ""))).suffix.lower() != ".md"]
    if not non_md_docs:
        return []

    q = question.lower()
    asks_access = "доступ" in q
    semantic_tokens = _semantic_query_tokens(question, process_hint)

    def is_instruction(doc: dict) -> bool:
        name = str(doc.get("file_name", "")).lower()
        return ("инструкц" in name) or ("руководств" in name) or ("manual" in name)

    def is_form(doc: dict) -> bool:
        return bool(doc.get("is_form"))

    scored: list[tuple[float, dict]] = []
    for doc in non_md_docs:
        path = str(doc.get("relative_path", ""))
        record = kb_index.documents.get(path)
        metadata_tokens = record.metadata_tokens if record else set()
        name_lower = str(doc.get("file_name", "")).lower()

        score = 0.0
        if is_instruction(doc):
            score += 2.0
        if not is_form(doc):
            score += 1.2
        if is_form(doc) and asks_access:
            score += 3.0
        if is_form(doc) and not asks_access:
            score -= 1.2

        overlap = semantic_tokens & metadata_tokens
        if overlap:
            score += 2.4 + (0.6 * len(overlap))

        # Do not suggest highly specific telematics docs outside matching queries.
        if ("телемат" in name_lower or "ретрансля" in name_lower or "повторн" in name_lower) and not (
            "телемат" in q or "ретрансля" in q or "повторн" in q or "глонасс" in q
        ):
            score -= 2.6

        scored.append((score, doc))

    scored.sort(key=lambda item: item[0], reverse=True)
    top_limit = 1 if not asks_access else 2

    output: list[dict] = []
    for _, doc in scored[:top_limit]:
        output.append(
            {
                "file_name": doc["file_name"],
                "relative_path": doc["relative_path"],
                "url": doc["url"],
                "download_url": doc["download_url"],
                "pages": [],
                "sections": [],
                "source_type": "related",
            }
        )
    return output


def _build_source_from_path(relative_path: str, source_type: str) -> dict | None:
    record = kb_index.documents.get(relative_path)
    if not record:
        return None

    return {
        "file_name": record.file_name,
        "relative_path": record.relative_path,
        "url": kb_index._build_file_url(record.relative_path),
        "download_url": kb_index._build_file_url(record.relative_path, download=True),
        "pages": [],
        "sections": [],
        "source_type": source_type,
    }


def _rank_sources_from_results(
    question: str,
    process_hint: str | None,
    results: list[RetrievalResult],
    source_type: str,
    exclude_paths: set[str] | None = None,
    require_metadata_overlap: bool = False,
    require_semantic_overlap: bool = False,
) -> list[dict]:
    semantic_tokens = _semantic_query_tokens(question, process_hint)
    exclude_paths = exclude_paths or set()

    score_by_path: dict[str, float] = {}
    pages_by_path: dict[str, set[int]] = {}
    sections_by_path: dict[str, set[str]] = {}

    for result in results:
        chunk = result.chunk
        relative_path = chunk.relative_path
        if relative_path in exclude_paths:
            continue
        if Path(relative_path).suffix.lower() == ".md":
            continue
        if process_hint and chunk.process != process_hint:
            continue

        record = kb_index.documents.get(relative_path)
        if not record:
            continue

        metadata_overlap: set[str] = set()
        if semantic_tokens:
            metadata_overlap = semantic_tokens & record.metadata_tokens
            if require_metadata_overlap and not metadata_overlap:
                continue

        score = (result.score * 0.9) + (result.coverage * 2.4)
        if semantic_tokens:
            chunk_tokens = kb_index.query_tokens(chunk.text)
            overlap = semantic_tokens & chunk_tokens
            if require_semantic_overlap and not overlap:
                continue
            if overlap:
                score += 1.0 + (0.4 * len(overlap))
            else:
                score *= 0.08
            if metadata_overlap:
                score += 1.2 + (0.45 * len(metadata_overlap))

        if chunk.page is not None:
            score += 0.2

        previous = score_by_path.get(relative_path, 0.0)
        score_by_path[relative_path] = max(previous, score)

        if chunk.page is not None:
            pages_by_path.setdefault(relative_path, set()).add(chunk.page)
        if chunk.section:
            sections_by_path.setdefault(relative_path, set()).add(chunk.section)

    ranked_paths = sorted(score_by_path.items(), key=lambda item: item[1], reverse=True)
    output: list[dict] = []
    for relative_path, score in ranked_paths:
        if semantic_tokens and score < 1.25:
            continue

        source = _build_source_from_path(relative_path, source_type=source_type)
        if not source:
            continue
        source["pages"] = sorted(pages_by_path.get(relative_path, set()))
        source["sections"] = sorted(sections_by_path.get(relative_path, set()))
        output.append(source)

    return output


def select_display_sources(
    question: str,
    process_hint: str | None,
    ranked_results: list[RetrievalResult],
    context_results: list[RetrievalResult],
    context_sources: list[dict],
    related_sources: list[dict],
) -> list[dict]:
    intent = kb_index.classify_query_intent(question)
    if is_participant_question(question) or is_responsibility_question(question):
        intent = "procedure"
    target_process = process_hint or _dominant_context_process(context_results)

    context_non_md = _rank_sources_from_results(
        question=question,
        process_hint=target_process,
        results=context_results,
        source_type="context",
        require_metadata_overlap=False,
        require_semantic_overlap=(intent != "documents"),
    )

    # Default mode: show only documents that were actually used in answer context.
    if context_non_md and intent != "documents":
        return context_non_md[:4]

    # For role/participant questions keep at least one context document,
    # even when semantic filtering is too strict for short queries.
    if (is_participant_question(question) or is_responsibility_question(question)) and not context_non_md:
        role_context_fallback = _rank_sources_from_results(
            question=question,
            process_hint=target_process,
            results=context_results,
            source_type="context",
            require_metadata_overlap=False,
            require_semantic_overlap=False,
        )
        if role_context_fallback:
            return _dedupe_sources(role_context_fallback)[:2]

    context_paths = {str(source.get("relative_path", "")) for source in context_non_md}
    ranked_from_retrieval = _rank_sources_from_results(
        question=question,
        process_hint=target_process,
        results=ranked_results,
        source_type="related",
        exclude_paths=context_paths,
        require_metadata_overlap=(intent != "documents"),
        require_semantic_overlap=(intent != "documents"),
    )

    if context_non_md and intent == "documents":
        combined = _dedupe_sources([*context_non_md, *ranked_from_retrieval])
        return combined[:6]

    if ranked_from_retrieval:
        return _dedupe_sources(ranked_from_retrieval)[:4]

    related_non_md = [source for source in related_sources if _source_extension(source) != ".md"]
    if target_process:
        process_related = [
            source for source in related_non_md if _source_process(source) == target_process
        ]
        if process_related:
            related_non_md = process_related

    ranked_related = _rank_related_sources_by_question(question, target_process, related_non_md)
    if ranked_related:
        return _dedupe_sources(ranked_related)[:4]

    # Fallback: suggest available non-markdown docs from the detected process.
    if is_participant_question(question) or is_responsibility_question(question):
        role_process_fallback = _role_query_process_fallback_sources(question, target_process)
        if role_process_fallback:
            return role_process_fallback

    if intent != "documents":
        return []

    process_for_fallback = target_process or process_hint
    if process_for_fallback:
        process_docs = kb_index.list_documents(query=question, process=process_for_fallback, forms_only=False)
        fallback_docs = [
            {
                "file_name": doc["file_name"],
                "relative_path": doc["relative_path"],
                "url": doc["url"],
                "download_url": doc["download_url"],
                "pages": [],
                "sections": [],
                "source_type": "related",
            }
            for doc in process_docs
            if Path(str(doc["relative_path"])).suffix.lower() != ".md"
        ]
        return _dedupe_sources(fallback_docs)[:4]

    return []



def build_fallback_answer(language: str, related_docs: list[dict], reason: str = "missing") -> str:
    ambiguous_suffix_en = " Please specify the process or system first."
    ambiguous_suffix_ru = " Уточните, пожалуйста, процесс или систему (например: ЕКТП, ЦУС, обучение/медосмотр)."

    if language == "en":
        prefix = FALLBACK_EN if reason == "missing" else "The request is ambiguous across multiple processes."
        if related_docs:
            return (
                f"{prefix}\n\n"
                "Please clarify the request (process, role, system, or document name), "
                "or review the related documents from the source list."
                + (ambiguous_suffix_en if reason == "ambiguous" else "")
            )
        return (
            f"{prefix}\n\n"
            "Please clarify the request (process, role, system, or document name)."
            + (ambiguous_suffix_en if reason == "ambiguous" else "")
        )

    prefix = FALLBACK_RU if reason == "missing" else "Запрос неоднозначен и может относиться к нескольким процессам."
    if related_docs:
        return (
            f"{prefix}\n\n"
            "Уточните вопрос (процесс, роль, систему или название документа) "
            "или откройте релевантные документы из блока источников."
            + (ambiguous_suffix_ru if reason == "ambiguous" else "")
        )

    return (
        f"{prefix}\n\n"
        "Уточните вопрос (процесс, роль, систему или название документа)."
        + (ambiguous_suffix_ru if reason == "ambiguous" else "")
    )


app = FastAPI(title="Corporate AI Assistant MVP")


@app.on_event("startup")
def startup_event() -> None:
    settings.kb_root.mkdir(parents=True, exist_ok=True)
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    with kb_lock:
        kb_index.build()


@app.get("/")
def root() -> FileResponse:
    index_path = settings.base_dir / "static" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="UI file not found")
    return FileResponse(index_path)


@app.get("/api/health")
def health() -> dict[str, str | int]:
    with kb_lock:
        chunk_count = len(kb_index.chunks)
        document_count = len(kb_index.documents)

    return {
        "status": "ok",
        "documents": document_count,
        "chunks": chunk_count,
    }


@app.post("/api/reindex")
def reindex() -> dict[str, int | str]:
    with kb_lock:
        kb_index.build()
        documents = len(kb_index.documents)
        chunks = len(kb_index.chunks)

    return {"status": "reindexed", "documents": documents, "chunks": chunks}


@app.post("/api/ask", response_model=AskResponse)
def ask(payload: AskRequest) -> AskResponse:
    raw_question = payload.question.strip()
    if not raw_question:
        raise HTTPException(status_code=400, detail="Question is empty")

    session_id = normalize_session_id(payload.session_id)
    effective_question, _ = dialog_state.merge_with_pending(session_id, raw_question)
    language = detect_language(effective_question)

    with kb_lock:
        process_hint = kb_index.detect_process_hint(effective_question)
        lexical_results = kb_index.retrieve(
            effective_question,
            top_k=max(50, settings.rerank_candidates * 2),
        )
        reranked_results = llm_service.rerank_results(
            effective_question,
            lexical_results,
            top_n=settings.rerank_top_n,
        )

        intent = kb_index.classify_query_intent(effective_question)
        context_results = kb_index.select_context_results(effective_question, reranked_results, max_chunks=5)
        has_strong_context = kb_index.is_context_strong(context_results)
        process_ambiguous = kb_index.is_process_ambiguous(context_results)
        context_sources = kb_index.build_sources_from_results(context_results)

        exclude_paths = {item["relative_path"] for item in context_sources}
        related_sources = kb_index.find_related_documents(
            effective_question,
            limit=4,
            exclude_paths=exclude_paths,
        )
        display_sources = select_display_sources(
            question=effective_question,
            process_hint=process_hint,
            ranked_results=reranked_results,
            context_results=context_results,
            context_sources=context_sources,
            related_sources=related_sources,
        )

    final_sources = display_sources

    participant_query = is_participant_question(effective_question)
    responsibility_query = is_responsibility_question(effective_question)
    if participant_query or responsibility_query:
        role_scope_process = process_hint or _dominant_context_process(context_results)
        action_hints = extract_action_hints(effective_question)
        single_owner_hints = {"доступ", "справоч", "акцепт"}

        # For vague "кто должен..." questions without a concrete action we keep
        # fallback flow and request clarification instead of guessing a role.
        if responsibility_query and not action_hints and not participant_query:
            answer = build_fallback_answer(language, final_sources, reason="ambiguous")
            dialog_state.set_pending(session_id, effective_question)
            request_logger.log(raw_question, final_sources, answer=answer)
            return AskResponse(answer=answer, sources=final_sources, no_exact_match=True)
        else:
            participants = extract_participants_from_results(
                reranked_results[: min(len(reranked_results), 24)],
                process_hint=role_scope_process,
                action_hints=action_hints,
                min_relative_score=0.72 if responsibility_query else 0.45,
            )
        min_items = 1 if responsibility_query else 2
        if len(participants) >= min_items:
            if responsibility_query and (action_hints & single_owner_hints):
                participants = participants[:1]
            if responsibility_query:
                answer = build_responsibility_answer(language, participants, process_hint=role_scope_process)
            else:
                answer = build_participants_answer(language, participants, process_hint=role_scope_process)
            dialog_state.clear(session_id)
            request_logger.log(raw_question, final_sources, answer=answer)
            return AskResponse(answer=answer, sources=final_sources, no_exact_match=False)

    if not has_strong_context or (process_ambiguous and process_hint is None):
        reason = "ambiguous" if process_ambiguous and process_hint is None else "missing"
        answer = build_fallback_answer(language, final_sources, reason=reason)
        if reason == "ambiguous":
            dialog_state.set_pending(session_id, effective_question)
        if reason == "missing":
            _register_kb_gap_from_chat(effective_question)
        request_logger.log(raw_question, final_sources, answer=answer)
        return AskResponse(answer=answer, sources=final_sources, no_exact_match=True)

    try:
        answer = llm_service.generate_answer(effective_question, context_results, intent=intent)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"OpenAI API key is required: {exc}",
        ) from exc
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=502,
            detail=f"OpenAI request failed: {exc}",
        ) from exc

    no_exact_match = FALLBACK_RU.lower() in answer.lower() or FALLBACK_EN.lower() in answer.lower()
    if no_exact_match:
        _register_kb_gap_from_chat(effective_question)
    dialog_state.clear(session_id)
    request_logger.log(raw_question, final_sources, answer=answer)
    return AskResponse(answer=answer, sources=final_sources, no_exact_match=no_exact_match)


@app.post("/api/dialog/clear")
def clear_dialog_state(payload: DialogClearRequest) -> dict[str, str]:
    session_id = normalize_session_id(payload.session_id)
    dialog_state.clear(session_id)
    return {"status": "cleared"}


@app.get("/api/debug/retrieval")
def debug_retrieval(
    q: str = Query(..., min_length=2),
    limit: int = Query(default=12, ge=1, le=50),
) -> dict[str, object]:
    def serialize(results: list[RetrievalResult], size: int) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for result in results[:size]:
            chunk = result.chunk
            payload.append(
                {
                    "score": round(result.score, 4),
                    "coverage": round(result.coverage, 4),
                    "relative_path": chunk.relative_path,
                    "page": chunk.page,
                    "section": chunk.section,
                    "snippet": chunk.text[:260],
                }
            )
        return payload

    with kb_lock:
        lexical_results = kb_index.retrieve(q, top_k=max(40, settings.rerank_candidates))
        reranked_results = llm_service.rerank_results(q, lexical_results, top_n=max(limit, 10))
        context_results = kb_index.select_context_results(q, reranked_results, max_chunks=min(limit, 8))

    return {
        "query": q,
        "intent": kb_index.classify_query_intent(q),
        "lexical": serialize(lexical_results, limit),
        "reranked": serialize(reranked_results, limit),
        "context": serialize(context_results, min(limit, 8)),
    }


@app.get("/api/documents")
def list_documents(
    q: str | None = Query(default=None),
    process: str | None = Query(default=None),
    forms_only: int = Query(default=0),
) -> dict[str, list[dict]]:
    with kb_lock:
        items = kb_index.list_documents(query=q, process=process, forms_only=bool(forms_only))
    return {"documents": items}


@app.get("/api/actions")
def list_actions(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, list[dict]]:
    items = actions_store.list_actions(limit=limit)
    return {"actions": [_serialize_action_for_ui(item) for item in items]}


@app.post("/api/actions")
def create_action(payload: ActionCreateRequest) -> dict[str, object]:
    requester_value = payload.requester.strip()
    if not requester_value:
        saved_profile = profile_store.load_profile()
        if saved_profile and saved_profile.full_name:
            requester_value = saved_profile.full_name

    record = actions_store.create_action(
        action_type=payload.action_type.strip(),
        process=payload.process.strip(),
        title=payload.title.strip(),
        details=payload.details.strip(),
        requester=requester_value,
    )
    return {
        "status": "created",
        "message": "Действие зарегистрировано локально (MVP, без интеграции с корпоративными ИС).",
        "action": {
            "action_id": record.action_id,
            "action_type": record.action_type,
            "process": record.process,
            "title": record.title,
            "details": record.details,
            "requester": record.requester,
            "status": record.status,
            "created_at": record.created_at,
        },
    }


@app.get("/api/appeals/topics")
def list_anonymous_appeal_topics() -> dict[str, object]:
    return {
        "topics": list_topics_for_ui(),
        "pu_options": [{"pu_name": item} for item in AVAILABLE_PU_NAMES],
    }


@app.post("/api/appeals/anonymous")
def create_anonymous_appeal(payload: AnonymousAppealCreateRequest) -> dict[str, object]:
    topic = resolve_topic(payload.topic_id.strip())
    if not topic:
        raise HTTPException(status_code=400, detail="Выбрана некорректная тема обращения.")

    manual_pu_name_raw = payload.pu_name.strip()
    manual_pu_name = _normalize_pu_name(manual_pu_name_raw)
    if manual_pu_name_raw and not manual_pu_name:
        raise HTTPException(status_code=400, detail="Укажите корректное наименование ПУ из списка.")

    requester_value = ""
    profile_pu_name = ""
    saved_profile = profile_store.load_profile()
    if saved_profile and saved_profile.full_name:
        requester_value = saved_profile.full_name
    if saved_profile and saved_profile.subdivision:
        profile_pu_name = _normalize_pu_name(saved_profile.subdivision)

    pu_name = manual_pu_name or profile_pu_name
    if not pu_name:
        raise HTTPException(status_code=400, detail="Укажите наименование ПУ. Поле обязательно.")

    record = actions_store.create_action(
        action_type="anonymous_appeal",
        process="Анонимные обращения",
        title=topic["topic_name"],
        details=payload.details.strip(),
        requester=requester_value,
        status="Зарегистрировано",
        is_anonymous=True,
        topic_id=topic["topic_id"],
        topic_name=topic["topic_name"],
        pu_name=pu_name,
        routing_block_id=topic["block_id"],
        routing_block=topic["block_name"],
        recipient_email=topic["recipient_email"],
    )
    return {
        "status": "registered",
        "message": "Обращение зарегистрировано и направлено.",
        "action": {
            "action_id": record.action_id,
            "action_type": record.action_type,
            "process": record.process,
            "title": record.title,
            "details": record.details,
            "status": record.status,
            "created_at": record.created_at,
            "is_anonymous": record.is_anonymous,
            "topic_id": record.topic_id,
            "topic_name": record.topic_name,
            "pu_name": record.pu_name,
            "routing_block_id": record.routing_block_id,
            "routing_block": record.routing_block,
            "recipient_email": record.recipient_email,
        },
    }


@app.get("/api/appeals/manager")
def list_manager_appeals(
    block_id: str | None = Query(default=None),
    topic_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, object]:
    appeals = _anonymous_appeals(limit=2000)

    block_value = (block_id or "").strip()
    topic_value = (topic_id or "").strip()
    status_value = (status or "").strip()

    if block_value:
        appeals = [item for item in appeals if str(item.get("routing_block_id", "")).strip() == block_value]
    if topic_value:
        appeals = [item for item in appeals if str(item.get("topic_id", "")).strip() == topic_value]
    if status_value:
        appeals = [item for item in appeals if str(item.get("status", "")).strip() == status_value]

    from_dt = _parse_iso_datetime(date_from) if date_from else None
    to_dt = _parse_iso_datetime(date_to) if date_to else None
    if date_to and to_dt:
        to_dt = to_dt + timedelta(days=1)

    if from_dt or to_dt:
        filtered: list[dict] = []
        for item in appeals:
            created_at = _parse_iso_datetime(str(item.get("created_at", "")))
            if not created_at:
                continue
            if from_dt and created_at < from_dt:
                continue
            if to_dt and created_at >= to_dt:
                continue
            filtered.append(item)
        appeals = filtered

    topics = list_topics_for_ui()
    return {
        "appeals": [_serialize_action_for_ui(item) for item in appeals[:limit]],
        "filters": {
            "blocks": [
                {
                    "block_id": item.block_id,
                    "block_name": item.block_name,
                }
                for item in APPEAL_BLOCKS
            ],
            "topics": topics,
            "statuses": list(APPEAL_ALLOWED_STATUSES),
        },
    }


@app.post("/api/appeals/{action_id}/status")
def update_appeal_status(action_id: str, payload: AppealStatusUpdateRequest) -> dict[str, object]:
    status_value = payload.status.strip()
    if status_value not in APPEAL_ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail="Указан некорректный статус обращения.")

    action_id_value = action_id.strip()
    existing = next(
        (
            item
            for item in actions_store.list_actions(limit=5000)
            if str(item.get("action_id", "")).strip() == action_id_value
        ),
        None,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Обращение не найдено.")
    if existing.get("action_type") != "anonymous_appeal":
        raise HTTPException(status_code=400, detail="Статус можно менять только у анонимных обращений.")

    updated = actions_store.update_action_status(
        action_id=action_id_value,
        status=status_value,
        status_comment=payload.comment.strip(),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Обращение не найдено.")

    return {
        "status": "updated",
        "message": "Статус обращения обновлен.",
        "action": _serialize_action_for_ui(updated),
    }


@app.get("/api/dashboard/appeals")
def appeals_dashboard(days: int = Query(default=30, ge=7, le=365)) -> dict[str, object]:
    appeals = _anonymous_appeals(limit=5000)
    now_dt = datetime.now(timezone.utc)
    from_dt = now_dt - timedelta(days=days)

    recent: list[dict] = []
    for item in appeals:
        created_at = _parse_iso_datetime(str(item.get("created_at", "")))
        if not created_at:
            continue
        if created_at >= from_dt:
            recent.append(item)

    status_counter: Counter[str] = Counter()
    block_counter: Counter[str] = Counter()
    topic_counter: Counter[str] = Counter()
    close_hours: list[float] = []

    for item in recent:
        status_counter[str(item.get("status", "")).strip() or "Без статуса"] += 1
        block_counter[str(item.get("routing_block", "")).strip() or "Без блока"] += 1
        topic_counter[str(item.get("topic_name", "")).strip() or "Без темы"] += 1

        created_at = _parse_iso_datetime(str(item.get("created_at", "")))
        closed_at = _parse_iso_datetime(str(item.get("closed_at", "")))
        if not created_at or not closed_at:
            continue
        delta_hours = (closed_at - created_at).total_seconds() / 3600.0
        if delta_hours >= 0:
            close_hours.append(delta_hours)

    total = len(recent)
    closed = status_counter.get("Закрыто", 0)
    in_progress = status_counter.get("В работе", 0)
    clarification = status_counter.get("Нужны уточнения", 0)
    registered = status_counter.get("Зарегистрировано", 0)
    closure_rate = round((closed / total) * 100, 1) if total else 0.0
    avg_close_hours = round(sum(close_hours) / len(close_hours), 1) if close_hours else None

    by_block = [{"name": name, "count": count} for name, count in block_counter.most_common()]
    by_status = [{"name": name, "count": count} for name, count in status_counter.most_common()]
    by_topic = [{"name": name, "count": count} for name, count in topic_counter.most_common(10)]

    return {
        "period_days": days,
        "metrics": {
            "total": total,
            "closed": closed,
            "in_progress": in_progress,
            "clarification": clarification,
            "registered": registered,
            "closure_rate_percent": closure_rate,
            "avg_close_hours": avg_close_hours,
        },
        "by_block": by_block,
        "by_status": by_status,
        "by_topic": by_topic,
        "topic_catalog_size": len(APPEAL_TOPICS),
    }


@app.get("/api/admin/fallbacks")
def list_admin_fallbacks(
    status: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=3000),
) -> dict[str, object]:
    items = _kb_gap_actions(limit=5000)
    status_value = (status or "").strip()
    if status_value:
        items = [item for item in items if str(item.get("status", "")).strip() == status_value]

    from_dt = _parse_iso_datetime(date_from) if date_from else None
    to_dt = _parse_iso_datetime(date_to) if date_to else None
    if date_to and to_dt:
        to_dt = to_dt + timedelta(days=1)

    if from_dt or to_dt:
        filtered: list[dict] = []
        for item in items:
            created_at = _parse_iso_datetime(str(item.get("created_at", "")))
            if not created_at:
                continue
            if from_dt and created_at < from_dt:
                continue
            if to_dt and created_at >= to_dt:
                continue
            filtered.append(item)
        items = filtered

    return {
        "fallbacks": [_serialize_action_for_ui(item) for item in items[:limit]],
        "filters": {
            "statuses": list(ADMIN_FALLBACK_ALLOWED_STATUSES),
        },
    }


@app.post("/api/admin/fallbacks/{action_id}/status")
def update_admin_fallback_status(action_id: str, payload: AdminFallbackStatusUpdateRequest) -> dict[str, object]:
    status_value = payload.status.strip()
    if status_value not in ADMIN_FALLBACK_ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail="Указан некорректный статус fallback-обращения.")

    action_id_value = action_id.strip()
    existing = next(
        (
            item
            for item in actions_store.list_actions(limit=5000)
            if str(item.get("action_id", "")).strip() == action_id_value
        ),
        None,
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Fallback-обращение не найдено.")
    if existing.get("action_type") != "kb_gap_from_chat":
        raise HTTPException(status_code=400, detail="Этот endpoint доступен только для fallback-обращений.")

    updated = actions_store.update_action_status(
        action_id=action_id_value,
        status=status_value,
        status_comment=payload.comment.strip(),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Fallback-обращение не найдено.")

    return {
        "status": "updated",
        "message": "Статус fallback-обращения обновлен.",
        "action": _serialize_action_for_ui(updated),
    }


@app.get("/api/profile")
def get_profile() -> dict[str, object]:
    profile = profile_store.load_profile()
    return {
        "profile": _profile_to_dict(profile) if profile else None,
        "options": {
            "divisions": list(ALLOWED_DIVISIONS),
            "subdivisions_by_division": {
                division: list(subdivisions)
                for division, subdivisions in BRANCH_SUBDIVISIONS.items()
            },
        },
    }


@app.post("/api/profile")
def save_profile(payload: ProfileRequest) -> dict[str, object]:
    profile = _normalize_profile_payload(payload)
    saved = profile_store.save_profile(profile)
    return {"status": "saved", "profile": _profile_to_dict(saved)}


@app.get("/api/files/{file_path:path}")
def get_file(file_path: str, download: int = 0) -> FileResponse:
    kb_root_resolved = settings.kb_root.resolve()
    requested_path = (kb_root_resolved / file_path).resolve()

    if kb_root_resolved not in requested_path.parents:
        raise HTTPException(status_code=403, detail="Path is outside knowledge base")

    if not requested_path.exists() or not requested_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    if download:
        return FileResponse(
            path=requested_path,
            filename=requested_path.name,
            media_type="application/octet-stream",
        )

    return FileResponse(path=requested_path)
