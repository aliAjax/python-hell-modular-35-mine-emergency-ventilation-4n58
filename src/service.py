import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, DomainError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "handover" and action == "confirm":
            return self._confirm_handover(actor, entity, data, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if kind == "task":
            self._void_pending_handovers(actor, updated)
        return updated

    def _confirm_handover(self, actor, handover, data, expected_version):
        """Confirm a handover and move task responsibility in one transaction."""
        task = self.repository.get_entity(handover["data"].get("task_id"))
        if not task:
            raise NotFoundError("task not found for handover: " + handover["id"])
        next_status, patch = self.rules.validate_transition(
            actor, handover, "confirm", dict(data or {}), self._lookup
        )
        merged_handover = dict(handover["data"])
        merged_handover.update(patch)
        merged_task = dict(task["data"])
        merged_task["responsible_team"] = handover["data"]["to_team"]
        merged_task["open_items"] = handover["data"]["pending_items"]
        merged_task["last_handover_id"] = handover["id"]
        expected = int(expected_version) if expected_version is not None else handover["version"]
        _, updated_task = self.repository.update_many([
            (handover["id"], expected, next_status, merged_handover),
            (task["id"], task["version"], task["status"], merged_task),
        ])
        self.audit.record(
            handover["id"], actor, "confirm", handover["status"], next_status, {"patch": patch}
        )
        self.audit.record(
            task["id"],
            actor,
            "handover_confirm",
            task["status"],
            updated_task["status"],
            {"handover_id": handover["id"], "responsible_team": handover["data"]["to_team"]},
        )
        return self.repository.get_entity(handover["id"])

    def _void_pending_handovers(self, actor, task):
        """Void unconfirmed handovers once the underlying task has changed."""
        reason = "task changed to " + task["status"]
        for handover in self.repository.find_entities("handover", "task_id", task["id"]):
            if handover["status"] != "pending":
                continue
            merged = dict(handover["data"])
            merged["void_reason"] = reason
            merged["voided_by"] = actor.user_id
            try:
                self.repository.update_entity(handover["id"], handover["version"], "void", merged)
            except ConflictError:
                continue  # concurrently confirmed or voided; the single winner stands
            self.audit.record(
                handover["id"],
                actor,
                "void",
                "pending",
                "void",
                {"reason": reason, "task_id": task["id"]},
            )

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            outcome = self._apply_offline_payload(actor, digest, payload.get("payload"))
            if outcome:
                payload.update(outcome)
            try:
                entity = self.repository.create_entity(
                    entity_id,
                    "offline_record",
                    self.rules.initial_status("offline_record", payload),
                    payload,
                    actor.user_id,
                )
            except ConflictError:
                # A concurrent upload of the same record won the race; that copy stands.
                entity = self.repository.get_entity(entity_id)
                if entity:
                    created.append(entity)
                    continue
                raise
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
        return created

    def _apply_offline_payload(self, actor, digest, payload):
        """Apply a domain payload carried by an offline record, at most once."""
        if not isinstance(payload, dict):
            return None
        ptype = payload.get("type")
        if ptype == "handover":
            return self._apply_offline_handover(actor, digest, payload)
        if ptype == "handover_confirm":
            return self._apply_offline_handover_confirm(actor, payload)
        return None

    def _apply_offline_handover(self, actor, digest, payload):
        handover_id = "handover-offline-" + digest
        if self.repository.get_entity(handover_id):
            return {"applied": True, "handover_id": handover_id, "duplicate": True}
        data = {
            key: payload[key]
            for key in ("task_id", "from_team", "to_team", "pending_items", "note")
            if payload.get(key) is not None
        }
        data["id"] = handover_id
        data["origin"] = "offline"
        try:
            self.create(actor, "handover", data)
        except ConflictError:
            if self.repository.get_entity(handover_id):
                return {"applied": True, "handover_id": handover_id, "duplicate": True}
            return {"applied": False, "apply_error": "pending handover already exists for task"}
        except DomainError as exc:
            return {"applied": False, "apply_error": str(exc)}
        return {"applied": True, "handover_id": handover_id}

    def _apply_offline_handover_confirm(self, actor, payload):
        handover = None
        handover_id = payload.get("handover_id")
        if handover_id:
            handover = self.repository.get_entity(str(handover_id))
        if not handover and payload.get("task_id"):
            for candidate in self.repository.find_entities("handover", "task_id", payload.get("task_id")):
                if candidate["status"] == "pending":
                    handover = candidate
                    break
        if not handover:
            return {"applied": False, "apply_error": "no matching handover"}
        if handover["status"] == "confirmed":
            return {"applied": True, "handover_id": handover["id"], "duplicate": True}
        try:
            self.transition(actor, handover["id"], "confirm", {"team": payload.get("team")})
        except DomainError as exc:
            return {"applied": False, "apply_error": str(exc), "handover_id": handover["id"]}
        return {"applied": True, "handover_id": handover["id"]}

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
