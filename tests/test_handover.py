import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data, actor=None):
        return self.service.create(actor or self.admin, kind, data)

    def act(self, entity, action, data=None, actor=None, version=None):
        return self.service.transition(actor or self.admin, entity["id"], action, data or {}, version)

    def field(self, user_id, team):
        return Actor(user_id, "field", team)

    def active_task(self, team="A"):
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "gas leak"})
        task = self.create("task", {"incident_id": incident["id"], "task_type": "rescue", "target": "worker-1", "dedupe_key": "rescue-1"})
        task = self.act(task, "assign", {"team": team})
        task = self.act(task, "accept", {}, self.field("f-%s" % team.lower(), team))
        return incident, task

    def test_responsibility_transfers_on_confirm(self):
        _, task = self.active_task("A")
        handover = self.create("handover", {"task_id": task["id"], "to_team": "B", "pending_work": "finish the rescue"})
        self.assertEqual(handover["status"], "pending")

        # Before confirmation the receiving shift cannot start work.
        with self.assertRaises(PermissionDenied):
            self.act(task, "complete", {"result": "done"}, self.field("f-b", "B"))

        # Confirming moves responsibility to team B.
        handover = self.act(handover, "confirm", {}, self.field("f-b", "B"))
        self.assertEqual(handover["status"], "confirmed")
        self.assertEqual(handover["data"]["confirmed_by"], "f-b")

        # The original shift touching the task now oversteps its authority.
        with self.assertRaises(PermissionDenied):
            self.act(task, "complete", {"result": "done"}, self.field("f-a", "A"))

        # The receiving shift can finish the task.
        task = self.act(task, "complete", {"result": "worker recovered"}, self.field("f-b", "B"))
        self.assertEqual(task["status"], "completed")

    def test_field_can_only_hand_over_its_own_team(self):
        _, task = self.active_task("A")
        with self.assertRaises(PermissionDenied):
            self.create("handover", {"task_id": task["id"], "to_team": "B", "pending_work": "x"}, self.field("f-b", "B"))

    def test_only_one_pending_handover_per_task(self):
        _, task = self.active_task("A")
        self.create("handover", {"task_id": task["id"], "to_team": "B", "pending_work": "first"})
        with self.assertRaises(ConflictError):
            self.create("handover", {"task_id": task["id"], "to_team": "C", "pending_work": "second"})

    def test_status_change_voids_pending_handover(self):
        _, task = self.active_task("A")
        handover = self.create("handover", {"task_id": task["id"], "to_team": "B", "pending_work": "finish"})
        self.assertEqual(handover["status"], "pending")

        self.act(task, "cancel", {"reason": "no longer needed"})

        handover = self.service.get(handover["id"])
        self.assertEqual(handover["status"], "void")

    def test_offline_handover_merges_once(self):
        _, task = self.active_task("A")
        record = {
            "source_id": "field-a",
            "record_id": "h-1",
            "recorded_at": "2026-09-27T10:00:00Z",
            "payload": {
                "record_type": "handover",
                "task_id": task["id"],
                "to_team": "B",
                "pending_work": "continue rescue",
            },
        }
        first = self.service.merge_offline(self.admin, [record])
        self.assertEqual(len(first), 1)

        handovers = self.service.list("handover")
        self.assertEqual(len(handovers), 1)
        self.assertEqual(handovers[0]["status"], "pending")
        self.assertEqual(handovers[0]["data"]["to_team"], "B")

        # Re-uploading the same offline record must not create a second handover.
        second = self.service.merge_offline(self.admin, [record])
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(len(self.service.list("handover")), 1)

    def test_close_requires_responsible_team_for_unfinished_tasks(self):
        incident = self.create("incident", {"area_code": "M-01", "severity": "high", "summary": "gas leak"})
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.act(incident, action)
        self.create("ventilation", {"name": "fan-1", "area_code": "M-01", "capacity": 100})

        # An unfinished task with no responsible team blocks close.
        task = self.create("task", {"incident_id": incident["id"], "task_type": "rescue", "target": "worker-1", "dedupe_key": "rescue-1"})
        with self.assertRaises(ConflictError):
            self.act(incident, "close", {"summary": "all clear"})

        # Assigning a responsible team lets the event close.
        self.act(task, "assign", {"team": "A"})
        incident = self.act(incident, "close", {"summary": "all clear"})
        self.assertEqual(incident["status"], "closed")


if __name__ == "__main__":
    unittest.main()
