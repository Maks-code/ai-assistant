from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


ALLOWED_DIVISIONS = (
    "Филиал Уфа",
    "Филиал Тюмень",
    "Филиал Красноярск",
    "ЦА",
)

BRANCH_SUBDIVISIONS: dict[str, tuple[str, ...]] = {
    "Филиал Красноярск": (
        "АУП",
        "ПУ Ангарск",
        "ПУ Ачинск",
        "ПУ Ванкор",
        "ПУ Восток",
        "ПУ Иркутск",
        "ПУ Комсомольск",
        "ПУ Славнефть",
        "ПУ Таас-Юрях",
    ),
    "Филиал Тюмень": (
        "АУП",
        "ПУ Губкинский",
        "ПУ Нефтеюганск",
        "ПУ Нижневартовск",
        "ПУ Новый Уренгой",
        "ПУ Тюмень",
        "ПУ ЮНГ",
    ),
    "Филиал Уфа": (
        "АУП",
        "ПУ БН-Добыча",
        "ПУ Бузулук",
        "ПУ Ижевск",
        "ПУ новокуйбышевск",
        "ПУ рязань",
        "ПУ самара",
        "ПУ сызрань",
        "ПУ туапсе",
        "ПУ Уфа",
    ),
}


@dataclass
class UserProfile:
    full_name: str
    division: str
    subdivision: str | None
    job_title: str
    email: str


class ProfileStore:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def load_profile(self) -> UserProfile | None:
        if not self.file_path.exists():
            return None

        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(raw, dict):
            return None

        return UserProfile(
            full_name=str(raw.get("full_name", "")).strip(),
            division=str(raw.get("division", "")).strip(),
            subdivision=(
                str(raw.get("subdivision", "")).strip()
                or str(raw.get("subdivision_type", "")).strip()
                or None
            ),
            job_title=str(raw.get("job_title", "")).strip(),
            email=str(raw.get("email", "")).strip(),
        )

    def save_profile(self, profile: UserProfile) -> UserProfile:
        payload = {
            "full_name": profile.full_name,
            "division": profile.division,
            "subdivision": profile.subdivision or "",
            "job_title": profile.job_title,
            "email": profile.email,
        }
        self.file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return profile
