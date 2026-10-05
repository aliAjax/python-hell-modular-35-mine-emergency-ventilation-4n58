import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, DomainError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("dispatch-1", "dispatcher")

    def tearDown(self):
        self.tmp.cleanup()

    def add_worker(self, name, team):
        return self.service.create(
            self.admin, "worker", {"name": name, "location_code": "M-01", "team": team}
        )

    def make_task(self, team="A"):
        incident = self.service.create(
            self.admin, "incident", {"area_code": "M-01", "severity": "high", "summary": "gas"}
        )
        task = self.service.create(
            self.admin,
            "task",
            {
                "incident_id": incident["id"],
                "task_type": "rescue",
                "target": "worker-1",
                "dedupe_key": "rescue-" + incident["id"][:8],
            },
        )
        task = self.service.transition(self.admin, task["id"], "assign", {"team": team})
        return incident, task

    def make_handover(self, task, to_team="B", actor=None):
        return self.service.create(
            actor or self.dispatcher,
            "handover",
            {
                "task_id": task["id"],
                "from_team": task["data"]["responsible_team"],
                "to_team": to_team,
                "pending_items": ["支护未加固", "瓦斯待复测"],
            },
        )

    def test_handover_confirm_transfers_responsibility(self):
        _, task = self.make_task(team="A")
        handover = self.make_handover(task)
        self.assertEqual(handover["status"], "pending")
        self.assertEqual(handover["data"]["pending_items"], ["支护未加固", "瓦斯待复测"])

        self.add_worker("bo-B", "B")
        confirmed = self.service.transition(Actor("bo-B", "field"), handover["id"], "confirm", {})
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["data"]["confirmed_by"], "bo-B")

        task = self.service.get(task["id"])
        self.assertEqual(task["data"]["responsible_team"], "B")
        self.assertEqual(task["data"]["open_items"], ["支护未加固", "瓦斯待复测"])

    def test_receiving_team_cannot_start_before_confirm(self):
        _, task = self.make_task(team="A")
        self.make_handover(task)
        self.add_worker("an-A", "A")
        self.add_worker("bo-B", "B")
        with self.assertRaises(PermissionDenied):
            self.service.transition(Actor("bo-B", "field"), task["id"], "accept", {})
        # The original team still holds responsibility until confirmation.
        task = self.service.transition(Actor("an-A", "field"), task["id"], "accept", {})
        self.assertEqual(task["status"], "in_progress")

    def test_original_team_loses_rights_after_confirm(self):
        _, task = self.make_task(team="A")
        self.add_worker("an-A", "A")
        self.add_worker("bo-B", "B")
        task = self.service.transition(Actor("an-A", "field"), task["id"], "accept", {})
        handover = self.make_handover(task)
        self.service.transition(Actor("bo-B", "field"), handover["id"], "confirm", {})

        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("an-A", "field"), task["id"], "complete", {"result": "done"}
            )
        task = self.service.transition(
            Actor("bo-B", "field"), task["id"], "complete", {"result": "done"}
        )
        self.assertEqual(task["status"], "completed")

    def test_second_pending_handover_is_rejected_not_overwritten(self):
        _, task = self.make_task(team="A")
        first = self.make_handover(task, to_team="B")
        with self.assertRaises(ConflictError):
            self.make_handover(task, to_team="C")
        handover = self.service.get(first["id"])
        self.assertEqual(handover["status"], "pending")
        self.assertEqual(handover["data"]["to_team"], "B")

    def test_task_change_voids_pending_handover(self):
        _, task = self.make_task(team="A")
        handover = self.make_handover(task)
        self.service.transition(self.admin, task["id"], "accept", {})

        handover = self.service.get(handover["id"])
        self.assertEqual(handover["status"], "void")
        self.assertIn("task changed", handover["data"]["void_reason"])
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.dispatcher, handover["id"], "confirm", {})

    def test_dispatcher_reassigns_after_void(self):
        _, task = self.make_task(team="A")
        handover = self.make_handover(task)
        task = self.service.transition(self.dispatcher, task["id"], "reassign", {"team": "C"})

        self.assertEqual(self.service.get(handover["id"])["status"], "void")
        self.assertEqual(task["data"]["responsible_team"], "C")
        self.assertEqual(task["status"], "assigned")
        # The freed slot allows the dispatcher to hand over again from the new team.
        follow_up = self.make_handover(task, to_team="D")
        self.assertEqual(follow_up["status"], "pending")

    def test_handover_requires_pending_items_and_distinct_teams(self):
        _, task = self.make_task(team="A")
        base = {"task_id": task["id"], "from_team": "A", "to_team": "B"}
        with self.assertRaises(ValidationError):
            self.service.create(self.dispatcher, "handover", dict(base, pending_items=[]))
        with self.assertRaises(ValidationError):
            self.service.create(self.dispatcher, "handover", dict(base, pending_items=["  "]))
        with self.assertRaises(ValidationError):
            self.service.create(
                self.dispatcher, "handover", dict(base, to_team="A", pending_items=["x"])
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.dispatcher, "handover", dict(base, from_team="C", pending_items=["x"])
            )

    def test_field_actor_must_belong_to_from_team(self):
        _, task = self.make_task(team="A")
        self.add_worker("an-A", "A")
        self.add_worker("cy-C", "C")
        with self.assertRaises(PermissionDenied):
            self.make_handover(task, actor=Actor("cy-C", "field"))
        handover = self.make_handover(task, actor=Actor("an-A", "field"))
        self.assertEqual(handover["status"], "pending")

    def test_confirm_requires_receiving_team(self):
        _, task = self.make_task(team="A")
        handover = self.make_handover(task, to_team="B")
        self.add_worker("cy-C", "C")
        with self.assertRaises(PermissionDenied):
            self.service.transition(Actor("cy-C", "field"), handover["id"], "confirm", {})

    def test_offline_handover_merge_is_idempotent(self):
        _, task = self.make_task(team="A")
        record = {
            "source_id": "device-1",
            "record_id": "h-1",
            "recorded_at": "2026-10-05T07:55:00Z",
            "payload": {
                "type": "handover",
                "task_id": task["id"],
                "from_team": "A",
                "to_team": "B",
                "pending_items": ["支护未加固"],
            },
        }
        first = self.service.merge_offline(self.dispatcher, [record])
        self.assertTrue(first[0]["data"]["applied"])
        handovers = self.service.list("handover")
        self.assertEqual(len(handovers), 1)
        self.assertEqual(handovers[0]["status"], "pending")
        self.assertEqual(handovers[0]["data"]["origin"], "offline")

        second = self.service.merge_offline(self.dispatcher, [record])
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(len(self.service.list("handover")), 1)
        self.assertEqual(len(self.service.list("offline_record")), 1)

    def test_offline_handover_and_confirm_merge(self):
        _, task = self.make_task(team="A")
        handover_record = {
            "source_id": "device-1",
            "record_id": "h-9",
            "recorded_at": "2026-10-05T07:55:00Z",
            "payload": {
                "type": "handover",
                "task_id": task["id"],
                "from_team": "A",
                "to_team": "B",
                "pending_items": ["支护未加固"],
            },
        }
        merged = self.service.merge_offline(self.dispatcher, [handover_record])
        handover_id = merged[0]["data"]["handover_id"]
        confirm_record = {
            "source_id": "device-1",
            "record_id": "c-9",
            "recorded_at": "2026-10-05T08:05:00Z",
            "payload": {"type": "handover_confirm", "handover_id": handover_id, "team": "B"},
        }
        confirmed = self.service.merge_offline(self.dispatcher, [confirm_record])
        self.assertTrue(confirmed[0]["data"]["applied"])
        self.assertEqual(self.service.get(handover_id)["status"], "confirmed")
        self.assertEqual(self.service.get(task["id"])["data"]["responsible_team"], "B")

        again = self.service.merge_offline(self.dispatcher, [handover_record, confirm_record])
        self.assertEqual([item["id"] for item in again], [merged[0]["id"], confirmed[0]["id"]])
        self.assertEqual(len(self.service.list("handover")), 1)
        self.assertEqual(len(self.service.list("offline_record")), 2)

    def test_offline_handover_conflict_is_recorded(self):
        _, task = self.make_task(team="A")
        self.make_handover(task, to_team="B")
        record = {
            "source_id": "device-2",
            "record_id": "h-7",
            "recorded_at": "2026-10-05T07:50:00Z",
            "payload": {
                "type": "handover",
                "task_id": task["id"],
                "from_team": "A",
                "to_team": "C",
                "pending_items": ["备用电源未接"],
            },
        }
        merged = self.service.merge_offline(self.dispatcher, [record])
        self.assertFalse(merged[0]["data"]["applied"])
        self.assertIn("pending handover", merged[0]["data"]["apply_error"])
        self.assertEqual(len(self.service.list("handover")), 1)

    def test_close_requires_responsible_team_on_incomplete_tasks(self):
        incident, task = self.make_task(team="A")
        # A never-assigned task has no responsible team and blocks closure.
        orphan = self.service.create(
            self.admin,
            "task",
            {
                "incident_id": incident["id"],
                "task_type": "repair",
                "target": "pump-1",
                "dedupe_key": "repair-pump-1",
            },
        )
        for action in ("begin_evacuation", "search", "stabilize", "recover"):
            incident = self.service.transition(self.admin, incident["id"], action)
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.admin, incident["id"], "close", {"summary": "done"})
        self.assertIn("responsible team", str(ctx.exception))

        self.service.transition(self.dispatcher, orphan["id"], "cancel", {"reason": "not needed"})
        self.service.transition(self.dispatcher, task["id"], "cancel", {"reason": "stood down"})
        incident = self.service.transition(self.admin, incident["id"], "close", {"summary": "done"})
        self.assertEqual(incident["status"], "closed")

    def test_concurrent_confirm_has_single_winner(self):
        _, task = self.make_task(team="A")
        handover = self.make_handover(task)
        results = []

        def confirm(tag):
            try:
                self.service.transition(Actor("disp-" + tag, "dispatcher"), handover["id"], "confirm", {})
                results.append("ok")
            except DomainError:
                results.append("rejected")

        threads = [threading.Thread(target=confirm, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("rejected"), 1)
        self.assertEqual(self.service.get(handover["id"])["status"], "confirmed")
        self.assertEqual(self.service.get(task["id"])["data"]["responsible_team"], "B")

    def test_concurrent_handover_create_has_single_winner(self):
        _, task = self.make_task(team="A")
        results = []

        def create(tag):
            try:
                self.service.create(
                    Actor("disp-" + tag, "dispatcher"),
                    "handover",
                    {
                        "task_id": task["id"],
                        "from_team": "A",
                        "to_team": "B",
                        "pending_items": ["支护未加固"],
                    },
                )
                results.append("ok")
            except ConflictError:
                results.append("rejected")

        threads = [threading.Thread(target=create, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("rejected"), 1)
        self.assertEqual(len(self.service.list("handover")), 1)


if __name__ == "__main__":
    unittest.main()
