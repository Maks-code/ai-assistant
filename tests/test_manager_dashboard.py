from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.actions import ActionsStore
from app.profile import ProfileStore, UserProfile


class ManagerCabinetAndDashboardTests(unittest.TestCase):
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

    def _create_anonymous_appeal(self, topic_id: str, details: str) -> dict:
        response = self.client.post(
            "/api/appeals/anonymous",
            json={
                "topic_id": topic_id,
                "details": details,
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["action"]

    def test_manager_cabinet_filter_and_status_update(self) -> None:
        aho_appeal = self._create_anonymous_appeal(
            topic_id="aho_facility_issue",
            details="Требуется ремонт освещения в кабинете.",
        )
        self._create_anonymous_appeal(
            topic_id="hr_documents_requests",
            details="Нужна справка с места работы.",
        )

        manager_list_response = self.client.get("/api/appeals/manager?block_id=aho&limit=50")
        self.assertEqual(manager_list_response.status_code, 200)
        payload = manager_list_response.json()
        appeals = payload["appeals"]
        self.assertEqual(len(appeals), 1)
        self.assertEqual(appeals[0]["action_id"], aho_appeal["action_id"])
        self.assertEqual(appeals[0]["status"], "Зарегистрировано")

        update_response = self.client.post(
            f"/api/appeals/{aho_appeal['action_id']}/status",
            json={"status": "В работе", "comment": "Принято в обработку"},
        )
        self.assertEqual(update_response.status_code, 200)
        updated = update_response.json()["action"]
        self.assertEqual(updated["status"], "В работе")
        self.assertEqual(updated["status_comment"], "Принято в обработку")
        self.assertNotIn("requester", updated)

    def test_dashboard_aggregates_anonymous_appeals(self) -> None:
        first = self._create_anonymous_appeal(
            topic_id="dev_system_failures",
            details="Падение внутренней системы.",
        )
        self._create_anonymous_appeal(
            topic_id="dev_process_changes",
            details="Предложение по упрощению согласования.",
        )
        self._create_anonymous_appeal(
            topic_id="pbotos_hazardous_conditions",
            details="Опасный участок в производственной зоне.",
        )

        close_response = self.client.post(
            f"/api/appeals/{first['action_id']}/status",
            json={"status": "Закрыто", "comment": "Выполнено"},
        )
        self.assertEqual(close_response.status_code, 200)

        dashboard_response = self.client.get("/api/dashboard/appeals?days=365")
        self.assertEqual(dashboard_response.status_code, 200)
        dashboard = dashboard_response.json()

        self.assertEqual(dashboard["metrics"]["total"], 3)
        self.assertEqual(dashboard["metrics"]["closed"], 1)
        self.assertEqual(dashboard["metrics"]["in_progress"], 0)
        self.assertGreaterEqual(dashboard["metrics"]["closure_rate_percent"], 30.0)
        self.assertIn("by_block", dashboard)
        self.assertIn("by_topic", dashboard)
        self.assertGreaterEqual(len(dashboard["by_block"]), 2)


if __name__ == "__main__":
    unittest.main()
