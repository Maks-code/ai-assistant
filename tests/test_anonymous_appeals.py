from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.actions import ActionsStore
from app.profile import ProfileStore, UserProfile


class AnonymousAppealsApiTests(unittest.TestCase):
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

    def test_register_anonymous_appeal_routes_by_topic_and_keeps_requester_only_in_logs(self) -> None:
        response = self.client.post(
            "/api/appeals/anonymous",
            json={
                "topic_id": "aho_workplace_equipment",
                "details": "На рабочем месте требуется замена кресла.",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        action = payload["action"]

        self.assertEqual(action["action_type"], "anonymous_appeal")
        self.assertEqual(action["topic_id"], "aho_workplace_equipment")
        self.assertEqual(action["routing_block"], "Блок АХО")
        self.assertEqual(action["recipient_email"], "nachalnikblokaAHO@mail.ru")
        self.assertEqual(action["pu_name"], "ПУ Тюмень")
        self.assertNotIn("requester", action)

        raw_items = main.actions_store.list_actions(limit=1)
        self.assertEqual(len(raw_items), 1)
        self.assertEqual(raw_items[0]["requester"], "Тестовый Сотрудник")
        self.assertEqual(raw_items[0]["pu_name"], "ПУ Тюмень")

    def test_actions_list_hides_requester_for_anonymous_appeals(self) -> None:
        create_response = self.client.post(
            "/api/appeals/anonymous",
            json={
                "topic_id": "hr_documents_requests",
                "details": "Нужна консультация по кадровым документам.",
            },
        )
        self.assertEqual(create_response.status_code, 200)

        list_response = self.client.get("/api/actions?limit=5")
        self.assertEqual(list_response.status_code, 200)

        actions = list_response.json()["actions"]
        self.assertGreaterEqual(len(actions), 1)

        anonymous_item = actions[0]
        self.assertTrue(anonymous_item["is_anonymous"])
        self.assertNotIn("requester", anonymous_item)
        self.assertEqual(anonymous_item["pu_name"], "ПУ Тюмень")

    def test_anonymous_appeal_requires_manual_pu_when_profile_has_no_pu(self) -> None:
        main.profile_store.save_profile(
            UserProfile(
                full_name="Тестовый Сотрудник",
                division="ЦА",
                subdivision=None,
                job_title="Инженер",
                email="employee@example.com",
            )
        )

        missing_pu_response = self.client.post(
            "/api/appeals/anonymous",
            json={
                "topic_id": "hr_documents_requests",
                "details": "Нужна консультация по кадровым документам.",
            },
        )
        self.assertEqual(missing_pu_response.status_code, 400)

        manual_pu_response = self.client.post(
            "/api/appeals/anonymous",
            json={
                "topic_id": "hr_documents_requests",
                "details": "Нужна консультация по кадровым документам.",
                "pu_name": "ПУ Тюмень",
            },
        )
        self.assertEqual(manual_pu_response.status_code, 200)
        self.assertEqual(manual_pu_response.json()["action"]["pu_name"], "ПУ Тюмень")


if __name__ == "__main__":
    unittest.main()
