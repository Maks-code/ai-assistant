from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.actions import ActionsStore
from app.profile import ProfileStore, UserProfile


class AdminFallbackPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp_dir.name)

        self._old_actions_store = main.actions_store
        self._old_profile_store = main.profile_store

        main.actions_store = ActionsStore(tmp_path / "actions.log")
        main.profile_store = ProfileStore(tmp_path / "profile.json")
        main.profile_store.save_profile(
            UserProfile(
                full_name="Тестовый Сотрудник",
                division="Филиал Тюмень",
                subdivision="ПУ Тюмень",
                job_title="Инженер",
                email="employee@example.com",
            )
        )
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        main.actions_store = self._old_actions_store
        main.profile_store = self._old_profile_store
        self._tmp_dir.cleanup()

    def _create_fallback_gap(self, question: str, signature: str) -> dict:
        item, _ = main.actions_store.upsert_kb_gap(
            question=question,
            signature=signature,
            routing_block="Администратор базы знаний",
            recipient_email="admin_kb@mail.ru",
            window_hours=24,
        )
        return item

    def test_admin_fallbacks_list_returns_only_chat_fallback_items(self) -> None:
        self._create_fallback_gap(
            question="Нет информации по неизвестному процессу 1",
            signature="unknown process one",
        )
        self._create_fallback_gap(
            question="Нет информации по неизвестному процессу 2",
            signature="unknown process two",
        )
        # Non-fallback action should not appear in admin fallback list.
        self.client.post(
            "/api/appeals/anonymous",
            json={
                "topic_id": "aho_workplace_equipment",
                "details": "Нужно обновить оснащение рабочего места.",
            },
        )

        response = self.client.get("/api/admin/fallbacks?limit=50")
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        items = payload["fallbacks"]
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["action_type"] == "kb_gap_from_chat" for item in items))
        self.assertIn("filters", payload)
        self.assertIn("statuses", payload["filters"])

    def test_admin_can_update_fallback_status(self) -> None:
        created = self._create_fallback_gap(
            question="Нет информации по регламенту АБВ",
            signature="reglament abv",
        )

        response = self.client.post(
            f"/api/admin/fallbacks/{created['action_id']}/status",
            json={"status": "В работе", "comment": "Передано на уточнение контента"},
        )
        self.assertEqual(response.status_code, 200)

        updated = response.json()["action"]
        self.assertEqual(updated["status"], "В работе")
        self.assertEqual(updated["status_comment"], "Передано на уточнение контента")

        list_response = self.client.get("/api/admin/fallbacks?status=В работе&limit=50")
        self.assertEqual(list_response.status_code, 200)
        filtered = list_response.json()["fallbacks"]
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["action_id"], created["action_id"])


if __name__ == "__main__":
    unittest.main()
