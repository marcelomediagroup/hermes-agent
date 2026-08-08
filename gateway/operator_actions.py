"""Durable, profile-scoped operator-card intent dispatch.

The dispatcher is deliberately local: resolving an action records a human
decision for a later workflow, but never calls an executor or mutates an
external system. Discord and other adapters carry only an opaque registration
ID; the validated card and its ``state_ref`` remain in profile-scoped state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeGuard

from gateway.operator_cards import OperatorCard
from hermes_constants import get_hermes_home
from utils import atomic_json_write


logger = logging.getLogger(__name__)

_STATE_SCHEMA = "hermes-operator-action-state-v1"
_AUDIT_SCHEMA = "hermes-operator-action-audit-v1"
_CUSTOM_ID_PREFIX = "hoa1"
_REGISTRATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16}$")
_ACTION_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_TTL_SECONDS = 31_536_000
_MAX_AUDIT_RECORD_BYTES = 2048


class OperatorActionPersistenceError(RuntimeError):
    """Raised when durable state or audit storage cannot be trusted."""


@dataclass(frozen=True, slots=True)
class OperatorActionRegistration:
    """Opaque durable route for one validated operator card."""

    id: str
    action_ids: tuple[str, ...]

    def custom_id(self, action_id: str) -> str:
        """Build a bounded Discord component ID without embedding card state."""
        if action_id not in self.action_ids:
            raise ValueError("action is not present on this registration")
        custom_id = f"{_CUSTOM_ID_PREFIX}:{self.id}:{action_id}"
        if len(custom_id.encode("utf-8")) > 100:
            raise ValueError("operator action custom_id exceeds Discord's limit")
        return custom_id


@dataclass(frozen=True, slots=True)
class OperatorActionOutcome:
    """Bounded terminal result of one authenticated human action."""

    result: str
    card: OperatorCard


class OperatorActionDispatcher:
    """Register exact card capabilities and consume each registration once."""

    def __init__(
        self,
        *,
        state_root: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        base = Path(state_root) if state_root is not None else get_hermes_home()
        self._root = base / "gateway" / "operator_actions"
        self._states = self._root / "states"
        self._audit_path = self._root / "audit.jsonl"
        self._clock = clock
        self._lock = threading.Lock()

    def register_card(
        self,
        card: OperatorCard,
        *,
        ttl_seconds: int,
    ) -> OperatorActionRegistration:
        """Persist one validated card and return its opaque component route."""
        if not isinstance(card, OperatorCard):
            raise TypeError("card must be a validated OperatorCard")
        if not card.actions:
            raise ValueError("operator card has no actions to register")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= _MAX_TTL_SECONDS
        ):
            raise ValueError(
                f"ttl_seconds must be an integer from 1 to {_MAX_TTL_SECONDS}"
            )

        now = float(self._clock())
        with self._lock:
            for _attempt in range(8):
                registration_id = secrets.token_urlsafe(12)
                if not _REGISTRATION_ID_RE.fullmatch(registration_id):
                    continue
                state_path = self._state_path(registration_id)
                if state_path.exists():
                    continue
                record = {
                    "schema": _STATE_SCHEMA,
                    "registration_id": registration_id,
                    "card": card.to_mapping(),
                    "created_at": now,
                    "expires_at": now + ttl_seconds,
                    "resolved": None,
                }
                self._write_state(state_path, record)
                return OperatorActionRegistration(
                    id=registration_id,
                    action_ids=tuple(action.id for action in card.actions),
                )
        raise OperatorActionPersistenceError(
            "could not allocate an operator action registration"
        )

    def dispatch(
        self,
        *,
        registration_id: str,
        action_id: str,
        actor_id: str,
        actor_display: str,
    ) -> OperatorActionOutcome:
        """Audit one local human intent and consume its registration once."""
        now = float(self._clock())
        safe_registration_id = self._safe_registration_id(registration_id)
        safe_action_id = self._safe_action_id(action_id)
        actor_id = self._bounded_text(actor_id, 64, fallback="unknown")
        actor_display = self._bounded_text(
            actor_display, 100, fallback="Unknown Discord user"
        )

        with self._lock:
            state_path = self._state_path(safe_registration_id)
            if safe_registration_id == "invalid" or not state_path.exists():
                return self._audit_blocked(
                    timestamp=now,
                    registration_id=safe_registration_id,
                    state_ref="unavailable",
                    action_id=safe_action_id,
                    actor_id=actor_id,
                    actor_display=actor_display,
                    result="blocked_unknown_state",
                )

            try:
                state, card = self._read_state(state_path, safe_registration_id)
            except OperatorActionPersistenceError:
                return self._audit_blocked(
                    timestamp=now,
                    registration_id=safe_registration_id,
                    state_ref="unavailable",
                    action_id=safe_action_id,
                    actor_id=actor_id,
                    actor_display=actor_display,
                    result="blocked_state_unavailable",
                )

            action_ids = {action.id for action in card.actions}
            if safe_action_id not in action_ids:
                return self._audit_blocked(
                    timestamp=now,
                    registration_id=safe_registration_id,
                    state_ref=card.state_ref,
                    action_id=safe_action_id,
                    actor_id=actor_id,
                    actor_display=actor_display,
                    result="blocked_unsupported_action",
                )

            if state["resolved"] is not None or self._success_was_audited(
                safe_registration_id
            ):
                return self._audit_blocked(
                    timestamp=now,
                    registration_id=safe_registration_id,
                    state_ref=card.state_ref,
                    action_id=safe_action_id,
                    actor_id=actor_id,
                    actor_display=actor_display,
                    result="blocked_already_resolved",
                )

            if state["expires_at"] <= now:
                return self._audit_blocked(
                    timestamp=now,
                    registration_id=safe_registration_id,
                    state_ref=card.state_ref,
                    action_id=safe_action_id,
                    actor_id=actor_id,
                    actor_display=actor_display,
                    result="blocked_expired",
                )

            # The JSONL record is the write-ahead receipt. If it cannot be
            # durably appended, state remains unresolved and success is never
            # reported. If the atomic state write subsequently fails, the next
            # attempt sees this receipt and treats the registration as consumed.
            self._append_audit(
                timestamp=now,
                registration_id=safe_registration_id,
                state_ref=card.state_ref,
                action_id=safe_action_id,
                actor_id=actor_id,
                actor_display=actor_display,
                result="recorded",
            )
            state["resolved"] = {
                "at": now,
                "action_id": safe_action_id,
                "actor_id": actor_id,
                "actor_display": actor_display,
            }
            try:
                self._write_state(state_path, state)
            except OperatorActionPersistenceError as exc:
                # The fsynced audit receipt is authoritative once appended.
                # Keep the operator-facing result and future audit consumer in
                # agreement; replay detection consults the receipt when this
                # secondary state snapshot could not be reconciled.
                logger.warning(
                    "Operator action %s was recorded, but its state snapshot "
                    "could not be updated: %s",
                    safe_registration_id,
                    exc,
                )
            return self._terminal_outcome(
                action_id=safe_action_id,
                result="recorded",
                registration_id=safe_registration_id,
            )

    def read_audit_records(self) -> list[dict]:
        """Read the bounded audit stream for diagnostics and future consumers."""
        if not self._audit_path.exists():
            return []
        records: list[dict] = []
        try:
            with self._audit_path.open("rb") as handle:
                for raw_line in handle:
                    if len(raw_line) > _MAX_AUDIT_RECORD_BYTES:
                        raise OperatorActionPersistenceError(
                            "operator action audit record exceeds its bound"
                        )
                    if not raw_line.strip():
                        continue
                    record = json.loads(raw_line.decode("utf-8"))
                    if (
                        not isinstance(record, dict)
                        or record.get("schema") != _AUDIT_SCHEMA
                    ):
                        raise OperatorActionPersistenceError(
                            "operator action audit record is invalid"
                        )
                    records.append(record)
        except OperatorActionPersistenceError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OperatorActionPersistenceError(
                "operator action audit could not be loaded"
            ) from exc
        return records

    def blocked_outcome(
        self,
        *,
        action_id: str,
        result: str = "blocked_state_unavailable",
    ) -> OperatorActionOutcome:
        """Build a terminal fail-closed card when durable dispatch is unavailable."""
        return self._terminal_outcome(
            action_id=self._safe_action_id(action_id),
            result=result,
            registration_id="unavailable",
        )

    def _audit_blocked(
        self,
        *,
        timestamp: float,
        registration_id: str,
        state_ref: str,
        action_id: str,
        actor_id: str,
        actor_display: str,
        result: str,
    ) -> OperatorActionOutcome:
        self._append_audit(
            timestamp=timestamp,
            registration_id=registration_id,
            state_ref=state_ref,
            action_id=action_id,
            actor_id=actor_id,
            actor_display=actor_display,
            result=result,
        )
        return self._terminal_outcome(
            action_id=action_id,
            result=result,
            registration_id=registration_id,
        )

    def _terminal_outcome(
        self,
        *,
        action_id: str,
        result: str,
        registration_id: str,
    ) -> OperatorActionOutcome:
        recorded = result == "recorded"
        digest = hashlib.sha256(
            f"{registration_id}:{action_id}:{result}".encode("utf-8")
        ).hexdigest()[:24]
        card = OperatorCard.from_mapping({
            "kind": "operator_card",
            "version": 1,
            "card_type": "approval",
            "title": (
                "Operator intent recorded" if recorded else "Operator action blocked"
            ),
            "severity": "done" if recorded else "blocked",
            "summary": (
                "Hermes recorded the decision. No external action was executed."
                if recorded
                else "The decision could not be accepted. No external action was executed."
            ),
            "fields": [
                {
                    "label": "Action",
                    "value": action_id if action_id != "invalid" else "Unrecognized",
                },
                {
                    "label": "Result",
                    "value": result.replace("_", " ").title(),
                },
            ],
            "actions": [],
            "links": [],
            "state_ref": f"operator-result:{digest}",
        })
        return OperatorActionOutcome(result=result, card=card)

    def _success_was_audited(self, registration_id: str) -> bool:
        return any(
            record.get("registration_id") == registration_id
            and record.get("result") == "recorded"
            for record in self.read_audit_records()
        )

    def _read_state(
        self,
        path: Path,
        registration_id: str,
    ) -> tuple[dict, OperatorCard]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created_at = (
                payload.get("created_at") if isinstance(payload, dict) else None
            )
            expires_at = (
                payload.get("expires_at") if isinstance(payload, dict) else None
            )
            valid_timestamps = (
                self._is_finite_timestamp(created_at)
                and self._is_finite_timestamp(expires_at)
                and 0 < float(expires_at) - float(created_at) <= _MAX_TTL_SECONDS
            )
            if (
                not isinstance(payload, dict)
                or payload.get("schema") != _STATE_SCHEMA
                or payload.get("registration_id") != registration_id
                or not valid_timestamps
                or payload.get("resolved") is not None
                and not isinstance(payload.get("resolved"), dict)
            ):
                raise ValueError("invalid state envelope")
            card = OperatorCard.from_mapping(payload["card"])
        except Exception as exc:
            raise OperatorActionPersistenceError(
                "operator action state could not be loaded"
            ) from exc
        return payload, card

    def _write_state(self, path: Path, payload: dict) -> None:
        try:
            atomic_json_write(
                path,
                payload,
                indent=0,
                separators=(",", ":"),
                mode=0o600,
            )
        except OSError as exc:
            raise OperatorActionPersistenceError(
                "operator action state could not be persisted"
            ) from exc

    def _append_audit(
        self,
        *,
        timestamp: float,
        registration_id: str,
        state_ref: str,
        action_id: str,
        actor_id: str,
        actor_display: str,
        result: str,
    ) -> None:
        record = {
            "schema": _AUDIT_SCHEMA,
            "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "registration_id": self._bounded_text(
                registration_id, 32, fallback="invalid"
            ),
            "actor_id": self._bounded_text(actor_id, 64, fallback="unknown"),
            "actor_display": self._bounded_text(
                actor_display, 100, fallback="Unknown Discord user"
            ),
            "action_id": self._safe_action_id(action_id),
            "state_ref": self._bounded_text(state_ref, 200, fallback="unavailable"),
            "result": self._bounded_text(result, 64, fallback="blocked_unknown"),
        }
        encoded = (
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > _MAX_AUDIT_RECORD_BYTES:
            raise OperatorActionPersistenceError(
                "operator action audit record exceeds its bound"
            )
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                self._audit_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                written = os.write(fd, encoded)
                if written != len(encoded):
                    raise OSError("short operator action audit write")
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise OperatorActionPersistenceError(
                "operator action audit could not be persisted"
            ) from exc

    def _state_path(self, registration_id: str) -> Path:
        digest = hashlib.sha256(str(registration_id).encode("utf-8")).hexdigest()
        return self._states / f"{digest}.json"

    @staticmethod
    def _safe_registration_id(value: str) -> str:
        value = str(value or "")
        return value if _REGISTRATION_ID_RE.fullmatch(value) else "invalid"

    @staticmethod
    def _safe_action_id(value: str) -> str:
        value = str(value or "")
        return value if _ACTION_ID_RE.fullmatch(value) else "invalid"

    @staticmethod
    def _bounded_text(value: str, limit: int, *, fallback: str) -> str:
        normalized = str(value or "").strip()
        return normalized[:limit] or fallback

    @staticmethod
    def _is_finite_timestamp(value: object) -> TypeGuard[int | float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(float(value))
        except (OverflowError, ValueError):
            return False


__all__ = [
    "OperatorActionDispatcher",
    "OperatorActionOutcome",
    "OperatorActionPersistenceError",
    "OperatorActionRegistration",
]
