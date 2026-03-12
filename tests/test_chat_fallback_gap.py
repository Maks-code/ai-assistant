from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.actions import ActionsStore
from app.profile import ProfileStore


class ChatFallbackGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp_dir.name)

        self._old_actions_store = main.actions_store
        self._old_profile_store = main.profile_store

        main.actions_store = ActionsStore(tmp_path / "actions.log")
        main.profile_store = ProfileStore(tmp_path / "profile.json")
        self.client = TestClient(main.app)

    def tearDown(self) -> None:
        main.actions_store = self._old_actions_store
        main.profile_store = self._old_profile_store
        self._tmp_dir.cleanup()

    def _fallback_question(self) -> str:
        return "zxqv991 неизвестный_регламент отсутствующий_процесс нет_информации_в_базе"

    def test_fallback_creates_kb_gap_action_for_admin(self) -> None:
        response = self.client.post(
            "/api/ask",
            json={"question": self._fallback_question(), "session_id": "gap-test-1"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["no_exact_match"])

        actions = main.actions_store.list_actions(limit=20)
        gaps = [item for item in actions if item.get("action_type") == "kb_gap_from_chat"]
        self.assertEqual(len(gaps), 1)

        gap = gaps[0]
        self.assertEqual(gap["routing_block"], "Администратор базы знаний")
        self.assertEqual(gap["recipient_email"], "admin_kb@mail.ru")
        self.assertEqual(gap["occurrences"], 1)
        self.assertTrue(str(gap.get("gap_signature", "")).strip())
        self.assertTrue(str(gap.get("last_seen_at", "")).strip())

    def test_fallback_deduplicates_same_gap_and_increments_occurrences(self) -> None:
        question = self._fallback_question()
        first = self.client.post("/api/ask", json={"question": question, "session_id": "gap-test-2"})
        second = self.client.post("/api/ask", json={"question": question, "session_id": "gap-test-3"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(first.json()["no_exact_match"])
        self.assertTrue(second.json()["no_exact_match"])

        actions = main.actions_store.list_actions(limit=50)
        gaps = [item for item in actions if item.get("action_type") == "kb_gap_from_chat"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["occurrences"], 2)


if __name__ == "__main__":
    unittest.main()
