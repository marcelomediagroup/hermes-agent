"""Real turn loop + async registry/delivery contracts, with only model I/O replaced."""

import asyncio
import copy
from collections import OrderedDict
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from run_agent import AIAgent
from tools import async_delegation as ad
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            session_id="required-parent", api_key="test-key",
            base_url="https://example.invalid/v1", provider="openai-compat",
            model="test/model", max_iterations=4, quiet_mode=True,
            skip_context_files=True, skip_memory=True, skip_background_review=True,
        )
    agent._cached_system_prompt = "byte-stable completion canary"
    agent._session_json_enabled = False
    agent.save_trajectories = False
    agent.compression_enabled = False
    agent._cleanup_task_resources = lambda *_a, **_kw: None
    calls = []

    def model_call(kwargs):
        calls.append(copy.deepcopy(kwargs["messages"]))
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="Report ready.", tool_calls=None),
                finish_reason="stop")], model="test/model", usage=None,
        )

    agent._interruptible_api_call = model_call
    yield agent, calls
    if ad._executor is not None:
        ad._executor.shutdown(wait=True)
    ad._reset_for_tests()
    if agent._session_db is not None:
        agent._session_db.close()


def launch(agent, release, status="completed"):
    def runner():
        assert release.wait(5), "test must release its child"
        return {"results": [{"task_index": 0, "status": status,
                             "summary": "CHILD_RESULT" if status == "completed" else None,
                             "error": "CHILD_FAILURE" if status == "error" else None}]}

    return ad.dispatch_async_delegation_batch(
        goals=["required check"], context=None, toolsets=None, role="leaf", model=None,
        parent_session_id=agent.session_id, session_key="same-route",
        origin_ui_session_id="test-tab", runner=runner, interrupt_fn=release.set,
    )["delegation_id"]


@pytest.mark.parametrize("child_state", ["running", "completed", "error"])
@pytest.mark.parametrize("delivery_path", ["poller", "post_turn", "gateway", "gateway_group"])
def test_parent_yields_until_required_result_is_in_resuming_turn(runtime, monkeypatch, child_state, delivery_path):
    agent, calls = runtime
    release = threading.Event()
    delegation_id = launch(agent, release, "error" if child_state == "error" else "completed")
    event = None
    try:
        if child_state != "running":
            release.set()
            # Deterministic completed-before-parent-final race: publication precedes the model call.
            event = process_registry.completion_queue.get(timeout=5)
        result = agent.run_conversation("Finish the task including the required check")
        assert result["completed"] is False
        assert result["turn_exit_reason"] == "waiting_for_required_children"
        assert result["pending_required_delegations"] == [delegation_id]
        assert "Task not complete:" in result["final_response"]
        assert result["messages"][-1]["content"] == result["final_response"]
        assert len(calls) == 1  # yield, no repeated model calls or child polling
        prefix = copy.deepcopy(result["messages"])
        if event is None:
            release.set()
            event = process_registry.completion_queue.get(timeout=5)

        # Real delivery imports (facade wires sibling globals), real claim/ack + real resumed loop.
        from tui_gateway import server
        resumed = []

        def submit(_rid, _sid, _session, text, **_kwargs):
            resumed.append(agent.run_conversation(text, conversation_history=prefix))
            _session["running"] = False

        monkeypatch.setattr(server, "_run_prompt_submit", submit)
        monkeypatch.setattr(server, "_emit", lambda *_a, **_k: None)
        session = {"running": False, "history_lock": threading.RLock(), "session_key": "same-route"}
        if delivery_path.startswith("gateway"):
            from gateway.run import GatewayRunner
            runner = object.__new__(GatewayRunner)
            runner._completion_delivery_lock = threading.Lock()
            runner._completion_deliveries_inflight = set()
            runner._completion_deliveries_delivered = OrderedDict()
            runner._completion_delivery_retention = 20

            async def get_session(_sid):
                return {"id": agent.session_id, "ended_at": None}

            runner._session_db = SimpleNamespace(get_session=get_session)

            async def handle_message(synth_event):
                assert synth_event.internal is True
                assert synth_event.metadata["gateway_session_id"] == agent.session_id
                # Exercise the real gateway-owned executor, not asyncio.to_thread
                # standing in for the production ContextVar handoff.
                await runner._run_in_executor_with_context(
                    submit, "rid", "test-tab", session, synth_event.text)

            source = SimpleNamespace(platform="telegram", chat_id="chat", thread_id=None)
            runner._build_process_event_source = lambda _evt: source
            runner._resolve_injection_adapter = lambda _platform: SimpleNamespace(
                supports_async_delivery=True, handle_message=handle_message)
            try:
                if delivery_path == "gateway_group":
                    second = launch(agent, release)
                    sibling = process_registry.completion_queue.get(timeout=5)
                    assert sibling["delegation_id"] == second
                    assert asyncio.run(runner._deliver_async_delegation_group([event, sibling])) is True
                    assert ad.get_durable_delegation(second)["delivery_state"] == "delivered"
                else:
                    assert asyncio.run(runner._deliver_completion_notification(format_process_notification(event), event)) is True
            finally:
                assert runner._shutdown_executor(drain_timeout=5) == 0
        elif delivery_path == "poller":
            server._notif_dispatch_event("test-tab", session, event, format_process_notification(event))
        else:
            monkeypatch.setattr(server, "_drain_queued_prompt", lambda *_a: False)
            monkeypatch.setattr(server, "_session_owns_notification_event", lambda *_a: True)
            process_registry.completion_queue.put(event)
            server._run_post_turn_followups("rid", "test-tab", session, result, None)
        assert len(resumed) == 1
        assert resumed[0]["completed"] is True
        assert resumed[0].get("pending_required_delegations", []) == []
        assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "delivered"
        assert ad.claim_event_delivery(event, "duplicate") is None
        assert prefix == result["messages"]  # cached history never rewritten
        assert [m["role"] for m in resumed[0]["messages"]] == ["user", "assistant", "user", "assistant"]
        assert calls[0][0] == calls[1][0]  # system prefix byte-stable
        expected = "CHILD_FAILURE" if child_state == "error" else "CHILD_RESULT"
        assert expected in str(calls[1])
    finally:
        release.set()


@pytest.mark.parametrize("boundary", [
    "stop", "new", "failed_delivery", "optional", "pruned_receipt", "interrupt", "interim",
    "budget", "blocked", "failed_injection", "sibling",
])
def test_completion_obligation_respects_scope_and_delivery_failure(runtime, monkeypatch, boundary):
    agent, calls = runtime
    release = threading.Event()
    delegation_id = launch(agent, release)

    def stop():
        ad.interrupt_for_session(parent_session_id=agent.session_id, reason="user_stop")

    def optional():
        stop()
        ad.dispatch_async_delegation(
            goal="detached background job", context=None, toolsets=None, role="leaf", model=None,
            session_key="same-route", parent_session_id=agent.session_id,
            runner=lambda: {"status": "completed", "summary": "optional"},
        )

    def pruned_receipt():
        release.set()
        event = process_registry.completion_queue.get(timeout=5)
        monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 0)
        ad._prune_durable_records()
        with ad._records_lock:
            ad._prune_completed_locked()
        assert ad.get_durable_delegation(delegation_id) is None
        assert ad.pending_required_delegations(agent.session_id) == [delegation_id]
        claim = ad.claim_event_delivery(event, "legacy-consumer")
        assert claim is not None
        ad.complete_event_delivery(event, claim)

    def interrupt():
        original_call = agent._interruptible_api_call

        def model_interrupt(kwargs):
            original_call(kwargs)
            agent.interrupt("User stop")
            raise InterruptedError("User stop")

        agent._interruptible_api_call = model_interrupt

    def interim():
        event = {"type": "async_delegation", "delegation_id": delegation_id, "task_failure_notice": True}
        with ad.consuming_delegation_results([event]):
            assert ad.pending_required_delegations(agent.session_id) == [delegation_id]

    def failed_delivery():
        release.set()
        event = process_registry.completion_queue.get(timeout=5)
        claim = ad.claim_event_delivery(event, "failed-consumer")
        assert claim is not None
        ad.release_event_delivery(event, claim)

    def budget():
        agent.max_iterations = agent.iteration_budget.max_total = 1
        monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
        monkeypatch.setattr("agent.verification_stop.build_verify_on_stop_nudge", lambda **_kw: "Run tests")

    def blocked():
        original_call = agent._interruptible_api_call

        def question(kwargs):
            response = original_call(kwargs)
            response.choices[0].message.content = "Approval required. Which target should I use?"
            return response

        agent._interruptible_api_call = question

    def failed_injection():
        release.set()
        event = process_registry.completion_queue.get(timeout=5)
        from tui_gateway import server

        def fail(*_a, **_kw):
            raise RuntimeError("transport unavailable")

        monkeypatch.setattr(server, "_run_prompt_submit", fail)
        monkeypatch.setattr(server, "_emit", lambda *_a, **_kw: None)
        session = {"running": True, "history_lock": threading.RLock()}
        server._notif_dispatch_event("test-tab", session, event, format_process_notification(event))
        assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "pending"

    def sibling():
        sibling_release = threading.Event()
        sibling_release.set()
        sibling_id = launch(agent, sibling_release)
        event = process_registry.completion_queue.get(timeout=5)
        assert event["delegation_id"] == sibling_id
        with ad.consuming_delegation_results([event]):
            assert ad.pending_required_delegations(agent.session_id) == [delegation_id]
        claim = ad.claim_event_delivery(event, "sibling")
        assert claim is not None
        ad.complete_event_delivery(event, claim)

    actions = {"stop": stop, "new": lambda: setattr(agent, "session_id", "new-scope"),
               "optional": optional, "pruned_receipt": pruned_receipt, "interrupt": interrupt,
               "interim": interim, "failed_delivery": failed_delivery, "budget": budget,
               "blocked": blocked, "failed_injection": failed_injection, "sibling": sibling}
    try:
        actions[boundary]()
        result = agent.run_conversation("What is the status?")
        assert result["completed"] is (boundary in {"stop", "new", "optional", "pruned_receipt"})
        if boundary not in {"stop", "new", "optional", "pruned_receipt", "interrupt"}:
            assert result["pending_required_delegations"] == [delegation_id]
        if boundary == "budget":
            assert result["turn_exit_reason"].startswith("max_iterations_reached")
            assert result["messages"][-1]["content"] == result["final_response"]
        if boundary == "blocked":
            assert result["final_response"].startswith("Approval required. Which target should I use?")
        if boundary == "interrupt":
            assert result["interrupted"] is True
            assert "Task not complete:" not in (result["final_response"] or "")
        assert len(calls) == 1
    finally:
        release.set()


def test_stream_recovery_keeps_text_and_required_child_barrier(runtime):
    from agent.turn_finalizer import finalize_turn

    agent, _calls = runtime
    release = threading.Event()
    delegation_id = launch(agent, release)
    agent._current_streamed_assistant_text = "Recovered report text."
    messages = [{"role": "user", "content": "Finish"}, {"role": "assistant", "content": ""}]
    try:
        result = finalize_turn(
            agent, final_response=None, api_call_count=1, interrupted=False, failed=False,
            messages=messages, conversation_history=[], effective_task_id="canary", turn_id="turn",
            user_message="Finish", original_user_message="Finish", _should_review_memory=False,
            _turn_exit_reason="partial_stream_recovery",
        )
        assert result["completed"] is False
        assert result["pending_required_delegations"] == [delegation_id]
        assert result["final_response"].startswith(agent._current_streamed_assistant_text)
        assert "Task not complete:" in result["final_response"]
        assert messages[-1]["content"].startswith(agent._current_streamed_assistant_text)
        assert "Task not complete:" in messages[-1]["content"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
    finally:
        release.set()


def test_completed_result_cancel_and_consumption_context_are_scoped(runtime):
    agent, _calls = runtime
    release = threading.Event()
    delegation_id = launch(agent, release)
    release.set()
    event = process_registry.completion_queue.get(timeout=5)
    with ad.consuming_delegation_results([event]):
        assert ad.pending_required_delegations(agent.session_id) == []
        other_turn = []
        thread = threading.Thread(target=lambda: other_turn.extend(ad.pending_required_delegations(agent.session_id)))
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert other_turn == [delegation_id]
    assert ad.pending_required_delegations(agent.session_id) == [delegation_id]
    stop_calls = []
    with ad._records_lock:
        ad._records[delegation_id]["interrupt_fn"] = lambda: stop_calls.append(True)
    assert ad.interrupt_for_session(parent_session_id=agent.session_id, reason="user_stop") == 0
    assert stop_calls == []  # clear the obligation, never re-interrupt a finished child
    assert ad.pending_required_delegations(agent.session_id) == []
    receipt = ad.get_durable_delegation(delegation_id)
    assert receipt is not None and receipt["delivery_state"] == "pending"


@pytest.mark.parametrize("boundary", ["compression", "new"])
def test_required_child_follows_compression_not_new_session(runtime, tmp_path, boundary):
    from hermes_state import SessionDB

    agent, calls = runtime
    agent._session_db = SessionDB(db_path=tmp_path / "profile" / "state.db")
    db = agent._session_db
    db.create_session(agent.session_id, source="cli")
    release = threading.Event()
    delegation_id = launch(agent, release)
    try:
        db.end_session(agent.session_id, end_reason=boundary)
        db.create_session("continuation", source="cli", parent_session_id=agent.session_id)
        agent.session_id = "continuation"
        result = agent.run_conversation("Finish the current task")
        assert result["completed"] is (boundary == "new")
        assert result.get("pending_required_delegations", []) == ([delegation_id] if boundary == "compression" else [])
        assert len(calls) == 1
    finally:
        release.set()
