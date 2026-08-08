import sys
import types

import pytest
import yaml

from agent.model_router import RoutingDecision
from gateway.config import GatewayConfig, Platform
import gateway.run as gateway_run
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore


CHANNEL_ID = "dynamic-channel"


def _source(*, chat_id=CHANNEL_ID, thread_id=None, parent_chat_id=None):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_name="dynamic",
        chat_type="channel",
        user_id="user-1",
        thread_id=thread_id,
        parent_chat_id=parent_chat_id,
    )


def _config():
    return {
        "smart_model_routing": {"enabled": True},
        "discord": {
            "channel_model_overrides": {
                CHANNEL_ID: {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-terra",
                    "smart_model_routing": True,
                }
            }
        },
    }


def _runner(tmp_path, source, *, durable_db=False):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    if not durable_db:
        store._db = None
    entry = store.get_or_create_session(source)
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    return runner, store, entry


def test_fresh_dynamic_session_routes_once_and_persists_only_safe_metadata(
    tmp_path, monkeypatch
):
    source = _source()
    runner, store, entry = _runner(tmp_path, source)
    classifier_calls = []

    def fake_route_request(message):
        classifier_calls.append(message)
        return RoutingDecision(
            tier="heavy",
            provider="openai-codex",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        )

    monkeypatch.setattr("agent.model_router.route_request", fake_route_request)

    model, runtime, decision = runner._resolve_dynamic_channel_route(
        message="Design a multi-service migration",
        source=source,
        session_key=entry.session_key,
        is_new_session=True,
        user_config=_config(),
        model="gpt-5.6-terra",
        runtime_kwargs={"provider": "openai-codex", "api_key": "secret"},
    )

    assert classifier_calls == ["Design a multi-service migration"]
    assert model == "gpt-5.6-sol"
    assert runtime == {"provider": "openai-codex", "api_key": "secret"}
    assert decision.reasoning_effort == "high"

    reloaded = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    reloaded._db = None
    assert reloaded.get_session_metadata(
        entry.session_key, "smart_model_route"
    ) == {
        "tier": "heavy",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
    }


def test_later_turn_reuses_persisted_route_without_reclassification(
    tmp_path, monkeypatch
):
    source = _source()
    runner, store, entry = _runner(tmp_path, source)
    store.set_session_metadata(
        entry.session_key,
        "smart_model_route",
        {
            "tier": "light",
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
        },
    )

    def unexpected_classifier(_message):
        raise AssertionError("an established session must not be reclassified")

    monkeypatch.setattr("agent.model_router.route_request", unexpected_classifier)

    model, _runtime, decision = runner._resolve_dynamic_channel_route(
        message="This follow-up looks much harder",
        source=source,
        session_key=entry.session_key,
        is_new_session=False,
        user_config=_config(),
        model="gpt-5.6-terra",
        runtime_kwargs={"provider": "openai-codex", "api_key": "secret"},
    )

    assert model == "gpt-5.6-luna"
    assert decision.reasoning_effort == "low"


def test_resume_restores_the_original_conversation_route(tmp_path, monkeypatch):
    source = _source()
    runner, store, entry = _runner(tmp_path, source, durable_db=True)
    original_session_id = entry.session_id
    monkeypatch.setattr(
        "agent.model_router.route_request",
        lambda _message: RoutingDecision(
            tier="heavy",
            provider="openai-codex",
            model="gpt-5.6-sol",
            reasoning_effort="high",
        ),
    )

    first_model, _runtime, _decision = runner._resolve_dynamic_channel_route(
        message="Design a multi-service migration",
        source=source,
        session_key=entry.session_key,
        is_new_session=True,
        user_config=_config(),
        model="gpt-5.6-terra",
        runtime_kwargs={"provider": "openai-codex"},
    )
    assert first_model == "gpt-5.6-sol"

    assert store.reset_session(entry.session_key) is not None
    resumed = store.switch_session(entry.session_key, original_session_id)
    assert resumed is not None
    assert resumed.metadata == {}

    def unexpected_classifier(_message):
        raise AssertionError("/resume must restore the conversation's route")

    monkeypatch.setattr("agent.model_router.route_request", unexpected_classifier)
    resumed_model, _runtime, decision = runner._resolve_dynamic_channel_route(
        message="continue",
        source=source,
        session_key=entry.session_key,
        is_new_session=True,
        user_config=_config(),
        model="gpt-5.6-terra",
        runtime_kwargs={"provider": "openai-codex"},
    )

    assert resumed_model == "gpt-5.6-sol"
    assert decision.reasoning_effort == "high"


def test_explicit_session_model_override_wins_without_classification(
    tmp_path, monkeypatch
):
    source = _source()
    runner, store, entry = _runner(tmp_path, source)
    runner._session_model_overrides[entry.session_key] = {
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
    }

    def unexpected_classifier(_message):
        raise AssertionError("an explicit /model must bypass smart routing")

    monkeypatch.setattr("agent.model_router.route_request", unexpected_classifier)

    model, runtime, decision = runner._resolve_dynamic_channel_route(
        message="simple request",
        source=source,
        session_key=entry.session_key,
        is_new_session=True,
        user_config=_config(),
        model="gpt-5.6-sol",
        runtime_kwargs={"provider": "openai-codex", "api_key": "secret"},
    )

    assert model == "gpt-5.6-sol"
    assert runtime["provider"] == "openai-codex"
    assert decision is None
    assert store.get_session_metadata(entry.session_key, "smart_model_route") is None


def test_discord_thread_inherits_parent_channel_marker_and_id_guard(
    tmp_path, monkeypatch
):
    source = _source(
        chat_id="thread-chat",
        thread_id="thread-1",
        parent_chat_id=CHANNEL_ID,
    )
    runner, _store, entry = _runner(tmp_path, source)
    config = _config()
    config["smart_model_routing"].update(
        {
            "mode": "fresh_session_complexity",
            "provider": "openai-codex",
            "marker_channel_ids": [CHANNEL_ID],
            "hold_for_session": True,
        }
    )
    monkeypatch.setattr(
        "agent.model_router.route_request",
        lambda _message: RoutingDecision(
            "light", "openai-codex", "gpt-5.6-luna", "low"
        ),
    )

    model, _runtime, decision = runner._resolve_dynamic_channel_route(
        message="rewrite this sentence",
        source=source,
        session_key=entry.session_key,
        is_new_session=True,
        user_config=config,
        model="gpt-5.6-terra",
        runtime_kwargs={"provider": "openai-codex", "api_key": "secret"},
    )

    assert model == "gpt-5.6-luna"
    assert decision.reasoning_effort == "low"


def test_dynamic_effort_is_a_fallback_below_explicit_reasoning_override(
    tmp_path, monkeypatch
):
    source = _source()
    runner, _store, entry = _runner(tmp_path, source)
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", _config)

    assert runner._resolve_session_reasoning_config(
        source=source,
        session_key=entry.session_key,
        model="gpt-5.6-sol",
        dynamic_reasoning_effort="high",
    ) == {"enabled": True, "effort": "high"}

    runner._session_reasoning_overrides[entry.session_key] = {
        "enabled": True,
        "effort": "low",
    }
    assert runner._resolve_session_reasoning_config(
        source=source,
        session_key=entry.session_key,
        model="gpt-5.6-sol",
        dynamic_reasoning_effort="high",
    ) == {"enabled": True, "effort": "low"}


def test_unmarked_channel_is_unchanged_even_when_router_is_globally_enabled(
    tmp_path, monkeypatch
):
    source = _source()
    runner, store, entry = _runner(tmp_path, source)
    config = _config()
    config["discord"]["channel_model_overrides"][CHANNEL_ID].pop(
        "smart_model_routing"
    )

    def unexpected_classifier(_message):
        raise AssertionError("only an explicitly marked channel may classify")

    monkeypatch.setattr("agent.model_router.route_request", unexpected_classifier)

    model, runtime, decision = runner._resolve_dynamic_channel_route(
        message="complex request",
        source=source,
        session_key=entry.session_key,
        is_new_session=True,
        user_config=config,
        model="gpt-5.6-terra",
        runtime_kwargs={"provider": "openai-codex", "api_key": "secret"},
    )

    assert model == "gpt-5.6-terra"
    assert runtime["provider"] == "openai-codex"
    assert decision is None
    assert store.get_session_metadata(entry.session_key, "smart_model_route") is None


def test_existing_session_without_route_fails_open_to_terra_without_classifying(
    tmp_path, monkeypatch
):
    source = _source()
    runner, store, entry = _runner(tmp_path, source)

    def unexpected_classifier(_message):
        raise AssertionError("an existing conversation must not be classified")

    monkeypatch.setattr("agent.model_router.route_request", unexpected_classifier)

    model, _runtime, decision = runner._resolve_dynamic_channel_route(
        message="hard-looking follow-up",
        source=source,
        session_key=entry.session_key,
        is_new_session=False,
        user_config=_config(),
        model="gpt-5.6-terra",
        runtime_kwargs={"provider": "openai-codex", "api_key": "secret"},
    )

    assert model == "gpt-5.6-terra"
    assert decision.reasoning_effort == "medium"
    assert store.get_session_metadata(
        entry.session_key, "smart_model_route"
    ) == {
        "tier": "standard",
        "provider": "openai-codex",
        "model": "gpt-5.6-terra",
    }


def test_route_persistence_failure_fails_open_to_terra(tmp_path, monkeypatch):
    source = _source()
    runner, store, entry = _runner(tmp_path, source)
    monkeypatch.setattr(
        "agent.model_router.route_request",
        lambda _message: RoutingDecision(
            "heavy", "openai-codex", "gpt-5.6-sol", "high"
        ),
    )
    monkeypatch.setattr(store, "set_conversation_metadata", lambda *_args: False)

    model, _runtime, decision = runner._resolve_dynamic_channel_route(
        message="complex request",
        source=source,
        session_key=entry.session_key,
        is_new_session=True,
        user_config=_config(),
        model="gpt-5.6-terra",
        runtime_kwargs={"provider": "openai-codex", "api_key": "secret"},
    )

    assert model == "gpt-5.6-terra"
    assert decision.reasoning_effort == "medium"


@pytest.mark.asyncio
async def test_real_gateway_turn_loads_config_announces_and_resumes_route(
    tmp_path, monkeypatch
):
    """Exercise config load -> TurnRunner -> SessionDB -> /resume continuity."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "openai-codex",
                    "default": "gpt-5.6-terra",
                },
                "agent": {
                    "reasoning_effort": "medium",
                    "reasoning_overrides": {
                        "gpt-5.6-luna": "low",
                        "gpt-5.6-terra": "medium",
                        "gpt-5.6-sol": "high",
                    },
                },
                "smart_model_routing": {
                    "enabled": True,
                    "mode": "fresh_session_complexity",
                    "provider": "openai-codex",
                    "marker_channel_ids": [CHANNEL_ID],
                    "hold_for_session": True,
                    "announce": True,
                },
                "discord": {
                    "channel_model_overrides": {
                        CHANNEL_ID: {
                            "provider": "openai-codex",
                            "model": "gpt-5.6-terra",
                            "smart_model_routing": True,
                        }
                    }
                },
                "gateway": {"sessions_dir": str(hermes_home / "sessions")},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openai-codex",
            "api_key": "test-credential",
            "api_mode": "codex_responses",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "openai-codex",
            "model": "gpt-5.6-terra",
            "api_key": "test-credential",
            "api_mode": "codex_responses",
        },
    )

    constructed_models = []

    class FakeAgent:
        def __init__(self, **kwargs):
            constructed_models.append(
                (kwargs.get("model"), kwargs.get("reasoning_config"))
            )
            self.tools = []

        def run_conversation(self, message, conversation_history=None, task_id=None):
            return {"final_response": "done", "messages": [], "api_calls": 1}

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    class CaptureAdapter:
        platform = Platform.DISCORD

        def __init__(self):
            self.sent = []
            self._pending_messages = {}

        async def send(self, chat_id, text, **kwargs):
            self.sent.append((chat_id, text, kwargs))
            return "route-message"

        def get_pending_message(self, _session_key):
            return None

    adapter = CaptureAdapter()
    runner = GatewayRunner()
    runner.adapters = {Platform.DISCORD: adapter}
    source = _source()
    entry = runner.session_store.get_or_create_session(source)
    original_session_id = entry.session_id
    monkeypatch.setattr(
        "agent.model_router.route_request",
        lambda _message: RoutingDecision(
            "heavy",
            "openai-codex",
            "gpt-5.6-sol",
            "high",
            classification_turn=True,
        ),
    )

    first = await runner._run_agent(
        message="Design a multi-service migration",
        context_prompt="",
        history=[],
        source=source,
        session_id=entry.session_id,
        session_key=entry.session_key,
        is_new_session=True,
    )
    assert first["final_response"] == "done"
    assert constructed_models[-1] == (
        "gpt-5.6-sol",
        {"enabled": True, "effort": "high"},
    )
    assert any(text == "Route: Sol / high" for _chat, text, _meta in adapter.sent)
    assert runner.session_store.get_conversation_metadata(
        entry.session_key, "smart_model_route"
    )["model"] == "gpt-5.6-sol"

    assert runner.session_store.reset_session(entry.session_key) is not None
    assert runner.session_store.switch_session(
        entry.session_key, original_session_id
    ) is not None
    monkeypatch.setattr(
        "agent.model_router.route_request",
        lambda _message: (_ for _ in ()).throw(
            AssertionError("resumed conversation must not be reclassified")
        ),
    )
    route_messages_before = len(
        [text for _chat, text, _meta in adapter.sent if text.startswith("Route:")]
    )

    resumed = await runner._run_agent(
        message="continue",
        context_prompt="",
        history=[],
        source=source,
        session_id=original_session_id,
        session_key=entry.session_key,
        is_new_session=True,
    )
    assert resumed["final_response"] == "done"
    assert constructed_models[-1][0] == "gpt-5.6-sol"
    assert len(
        [text for _chat, text, _meta in adapter.sent if text.startswith("Route:")]
    ) == route_messages_before
