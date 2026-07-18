from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _runner():
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    return runner


def _source(chat_id="1520445788679966931"):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_name="voice",
        chat_type="channel",
    )


def test_channel_model_override_switches_provider_and_model(monkeypatch):
    calls = []

    def fake_resolve_runtime_provider(**kwargs):
        calls.append(kwargs)
        return {
            "api_key": "test-key",
            "base_url": "https://api.anthropic.com",
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve_runtime_provider,
    )

    cfg = {
        "discord": {
            "channel_model_overrides": {
                "1520445788679966931": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4.5",
                    "max_tokens": 700,
                }
            }
        }
    }

    model, runtime = _runner()._apply_channel_model_override(
        cfg,
        _source(),
        "gpt-5.5",
        {"provider": "openai-codex", "api_key": "old-key"},
    )

    assert calls == [
        {
            "requested": "anthropic",
            "explicit_base_url": None,
            "explicit_api_key": None,
            "target_model": "claude-haiku-4.5",
        }
    ]
    assert model == "claude-haiku-4-5"
    assert runtime["provider"] == "anthropic"
    assert runtime["api_mode"] == "anthropic_messages"
    assert runtime["max_tokens"] == 700


def test_channel_model_override_accepts_string_same_provider():
    cfg = {"discord": {"channel_models": {"1520445788679966931": "gpt-5.3-codex-spark"}}}

    model, runtime = _runner()._apply_channel_model_override(
        cfg,
        _source(),
        "gpt-5.5",
        {"provider": "openai-codex"},
    )

    assert model == "gpt-5.3-codex-spark"
    assert runtime == {"provider": "openai-codex"}


def test_channel_reasoning_override_can_live_with_model_override():
    cfg = {
        "discord": {
            "channel_model_overrides": {
                "1520445788679966931": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5",
                    "reasoning_effort": "minimal",
                }
            }
        }
    }

    assert _runner()._resolve_channel_reasoning_config(cfg, _source()) == {
        "enabled": True,
        "effort": "minimal",
    }


def test_channel_model_override_ignores_other_channels():
    cfg = {
        "discord": {
            "channel_model_overrides": {
                "1520445788679966931": {"provider": "anthropic", "model": "claude-haiku-4-5"}
            }
        }
    }

    model, runtime = _runner()._apply_channel_model_override(
        cfg,
        _source("999"),
        "gpt-5.5",
        {"provider": "openai-codex"},
    )

    assert model == "gpt-5.5"
    assert runtime == {"provider": "openai-codex"}
