"""Behavior contract for durable operator-card intent dispatch."""

import json

import pytest

from gateway.operator_actions import (
    OperatorActionDispatcher,
    OperatorActionPersistenceError,
)
from gateway.operator_cards import OperatorCard


def _card(**overrides) -> OperatorCard:
    payload = {
        "kind": "operator_card",
        "version": 1,
        "card_type": "approval",
        "title": "Choose the next step",
        "severity": "needs_review",
        "summary": "A human decision is required before later automation can continue.",
        "fields": [{"label": "Issue", "value": "OE-175"}],
        "actions": [
            {"id": "approve", "label": "Approve", "style": "success"},
            {"id": "hold", "label": "Hold", "style": "secondary"},
        ],
        "links": [],
        "state_ref": "oe-175:decision:1",
    }
    payload.update(overrides)
    return OperatorCard.from_mapping(payload)


def test_registered_card_survives_dispatcher_restart(tmp_path):
    first = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)
    registration = first.register_card(_card(), ttl_seconds=60)

    restarted = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 120.0)
    outcome = restarted.dispatch(
        registration_id=registration.id,
        action_id="approve",
        actor_id="123456789",
        actor_display="Martin",
    )

    assert outcome.result == "recorded"
    assert outcome.card.severity == "done"
    assert outcome.card.actions == ()
    assert "No external action was executed" in outcome.card.summary

    audit = restarted.read_audit_records()
    assert audit == [
        {
            "schema": "hermes-operator-action-audit-v1",
            "timestamp": "1970-01-01T00:02:00+00:00",
            "registration_id": registration.id,
            "actor_id": "123456789",
            "actor_display": "Martin",
            "action_id": "approve",
            "state_ref": "oe-175:decision:1",
            "result": "recorded",
        }
    ]


def test_success_is_audited_before_resolved_state_is_written(tmp_path, monkeypatch):
    dispatcher = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)
    registration = dispatcher.register_card(_card(), ttl_seconds=60)
    events = []
    original_append = dispatcher._append_audit
    original_write = dispatcher._write_state

    def recording_append(**kwargs):
        events.append("audit")
        return original_append(**kwargs)

    def recording_write(path, payload):
        events.append("state")
        return original_write(path, payload)

    monkeypatch.setattr(dispatcher, "_append_audit", recording_append)
    monkeypatch.setattr(dispatcher, "_write_state", recording_write)

    outcome = dispatcher.dispatch(
        registration_id=registration.id,
        action_id="approve",
        actor_id="1",
        actor_display="Operator",
    )

    assert outcome.result == "recorded"
    assert events == ["audit", "state"]


def test_audit_failure_does_not_consume_registration(tmp_path, monkeypatch):
    dispatcher = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)
    registration = dispatcher.register_card(_card(), ttl_seconds=60)
    state_path = dispatcher._state_path(registration.id)
    state_before = state_path.read_bytes()

    def fail_audit(**_kwargs):
        raise OperatorActionPersistenceError("audit unavailable")

    monkeypatch.setattr(dispatcher, "_append_audit", fail_audit)
    with pytest.raises(OperatorActionPersistenceError, match="audit unavailable"):
        dispatcher.dispatch(
            registration_id=registration.id,
            action_id="approve",
            actor_id="1",
            actor_display="Operator",
        )

    assert state_path.read_bytes() == state_before
    assert json.loads(state_path.read_text(encoding="utf-8"))["resolved"] is None


def test_short_audit_write_does_not_consume_registration(tmp_path, monkeypatch):
    dispatcher = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)
    registration = dispatcher.register_card(_card(), ttl_seconds=60)
    state_path = dispatcher._state_path(registration.id)
    state_before = state_path.read_bytes()

    def short_write(_fd, encoded):
        return len(encoded) - 1

    monkeypatch.setattr("gateway.operator_actions.os.write", short_write)
    with pytest.raises(
        OperatorActionPersistenceError, match="audit could not be persisted"
    ):
        dispatcher.dispatch(
            registration_id=registration.id,
            action_id="approve",
            actor_id="1",
            actor_display="Operator",
        )

    assert state_path.read_bytes() == state_before
    assert json.loads(state_path.read_text(encoding="utf-8"))["resolved"] is None


def test_audited_success_remains_authoritative_when_state_snapshot_write_fails(
    tmp_path, monkeypatch
):
    dispatcher = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)
    registration = dispatcher.register_card(_card(), ttl_seconds=60)
    state_path = dispatcher._state_path(registration.id)
    state_before = state_path.read_bytes()

    def fail_replace(_tmp_path, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("utils.atomic_replace", fail_replace)
    outcome = dispatcher.dispatch(
        registration_id=registration.id,
        action_id="approve",
        actor_id="1",
        actor_display="Operator",
    )

    assert outcome.result == "recorded"
    assert outcome.card.severity == "done"
    assert state_path.read_bytes() == state_before
    assert json.loads(state_path.read_text(encoding="utf-8"))["resolved"] is None
    assert dispatcher.read_audit_records()[0]["result"] == "recorded"

    monkeypatch.undo()
    restarted = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 101.0)
    replay = restarted.dispatch(
        registration_id=registration.id,
        action_id="approve",
        actor_id="1",
        actor_display="Operator",
    )
    assert replay.result == "blocked_already_resolved"


@pytest.mark.parametrize(
    "expires_at",
    [float("nan"), float("inf"), True, 100.0, 31_536_101.0],
)
def test_invalid_durable_timestamps_fail_closed(tmp_path, expires_at):
    dispatcher = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)
    registration = dispatcher.register_card(_card(), ttl_seconds=60)
    state_path = dispatcher._state_path(registration.id)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["expires_at"] = expires_at
    state_path.write_text(json.dumps(state), encoding="utf-8")

    outcome = dispatcher.dispatch(
        registration_id=registration.id,
        action_id="approve",
        actor_id="1",
        actor_display="Operator",
    )

    assert outcome.result == "blocked_state_unavailable"
    assert dispatcher.read_audit_records()[0]["result"] == "blocked_state_unavailable"


def test_unknown_registration_fails_closed_and_is_audited(tmp_path):
    dispatcher = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)

    outcome = dispatcher.dispatch(
        registration_id="abcdefghijklmnop",
        action_id="approve",
        actor_id="2",
        actor_display="Operator",
    )

    assert outcome.result == "blocked_unknown_state"
    assert outcome.card.severity == "blocked"
    assert outcome.card.actions == ()
    assert dispatcher.read_audit_records()[0]["result"] == "blocked_unknown_state"


def test_action_not_present_on_registered_card_fails_closed(tmp_path):
    dispatcher = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)
    registration = dispatcher.register_card(_card(), ttl_seconds=60)

    outcome = dispatcher.dispatch(
        registration_id=registration.id,
        action_id="publish",
        actor_id="3",
        actor_display="Operator",
    )

    assert outcome.result == "blocked_unsupported_action"
    assert dispatcher.read_audit_records()[0]["state_ref"] == "oe-175:decision:1"


def test_expired_registration_fails_closed(tmp_path):
    dispatcher = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)
    registration = dispatcher.register_card(_card(), ttl_seconds=10)
    expired = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 110.0)

    outcome = expired.dispatch(
        registration_id=registration.id,
        action_id="hold",
        actor_id="4",
        actor_display="Operator",
    )

    assert outcome.result == "blocked_expired"
    assert expired.read_audit_records()[0]["result"] == "blocked_expired"


def test_resolved_registration_cannot_be_replayed(tmp_path):
    dispatcher = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)
    registration = dispatcher.register_card(_card(), ttl_seconds=60)
    first = dispatcher.dispatch(
        registration_id=registration.id,
        action_id="hold",
        actor_id="5",
        actor_display="First operator",
    )
    replay = dispatcher.dispatch(
        registration_id=registration.id,
        action_id="approve",
        actor_id="6",
        actor_display="Second operator",
    )

    assert first.result == "recorded"
    assert replay.result == "blocked_already_resolved"
    assert [record["result"] for record in dispatcher.read_audit_records()] == [
        "recorded",
        "blocked_already_resolved",
    ]


def test_corrupt_durable_state_fails_closed_without_execution(tmp_path):
    dispatcher = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)
    registration = dispatcher.register_card(_card(), ttl_seconds=60)
    dispatcher._state_path(registration.id).write_text("{broken", encoding="utf-8")

    outcome = dispatcher.dispatch(
        registration_id=registration.id,
        action_id="approve",
        actor_id="7",
        actor_display="Operator",
    )

    assert outcome.result == "blocked_state_unavailable"
    assert dispatcher.read_audit_records()[0]["result"] == "blocked_state_unavailable"


def test_custom_ids_and_terminal_output_are_bounded_and_hide_private_state(tmp_path):
    private_ref = "private-customer-decision:account-123"
    dispatcher = OperatorActionDispatcher(state_root=tmp_path, clock=lambda: 100.0)
    registration = dispatcher.register_card(
        _card(state_ref=private_ref), ttl_seconds=60
    )

    custom_ids = [
        registration.custom_id(action_id) for action_id in registration.action_ids
    ]
    outcome = dispatcher.dispatch(
        registration_id=registration.id,
        action_id="approve",
        actor_id="8" * 100,
        actor_display="😀" * 1000,
    )

    assert all(len(custom_id.encode("utf-8")) <= 100 for custom_id in custom_ids)
    assert all(private_ref not in custom_id for custom_id in custom_ids)
    assert private_ref not in json.dumps(outcome.card.to_mapping())
    audit_line = (
        tmp_path / "gateway" / "operator_actions" / "audit.jsonl"
    ).read_bytes()
    assert len(audit_line) <= 2048
    assert len(dispatcher.read_audit_records()[0]["actor_id"]) == 64
    assert len(dispatcher.read_audit_records()[0]["actor_display"]) == 100
