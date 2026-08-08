from types import SimpleNamespace

import pytest

from agent.model_router import route_request


def test_classifier_failure_fails_open_to_terra_medium():
    def failing_classifier(_message: str) -> str:
        raise TimeoutError("classifier timed out")

    decision = route_request("Please help me with this", classifier=failing_classifier)

    assert decision.to_metadata() == {
        "tier": "standard",
        "provider": "openai-codex",
        "model": "gpt-5.6-terra",
    }
    assert decision.reasoning_effort == "medium"


@pytest.mark.parametrize(
    ("label", "model", "effort"),
    [
        ("light", "gpt-5.6-luna", "low"),
        ("standard", "gpt-5.6-terra", "medium"),
        ("heavy", "gpt-5.6-sol", "high"),
    ],
)
def test_supported_tiers_map_to_the_fixed_codex_routes(label, model, effort):
    decision = route_request("request", classifier=lambda _message: label)

    assert decision.provider == "openai-codex"
    assert decision.model == model
    assert decision.reasoning_effort == effort


def test_default_classifier_is_locked_to_codex_luna(monkeypatch):
    calls = []

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="heavy"))]
        )

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    decision = route_request("Design a multi-service migration")

    assert decision.model == "gpt-5.6-sol"
    assert calls[0]["task"] == "routing_classifier"
    assert calls[0]["provider"] == "openai-codex"
    assert calls[0]["model"] == "gpt-5.6-luna"
    assert calls[0]["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert calls[0]["allow_fallback"] is False


def test_announcement_is_only_available_on_the_classification_turn():
    from agent.model_router import decision_from_metadata

    classified = route_request("simple rewrite", classifier=lambda _message: "light")
    resumed = decision_from_metadata(classified.to_metadata())

    assert classified.announcement({"announce": True}) == "Route: Luna / low"
    assert classified.announcement({"announce": False}) is None
    assert resumed is not None
    assert resumed.announcement({"announce": True}) is None


def test_default_config_exposes_the_fresh_session_router_contract():
    from hermes_cli.config import DEFAULT_CONFIG

    config = DEFAULT_CONFIG["smart_model_routing"]

    assert config["mode"] == "fresh_session_complexity"
    assert config["provider"] == "openai-codex"
    assert config["hold_for_session"] is True
    assert "tiers" not in config
    assert "default_tier" not in config
