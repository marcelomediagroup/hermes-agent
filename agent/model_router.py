"""Fresh-session model routing for explicitly opted-in gateway channels."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Optional


_PROVIDER = "openai-codex"
_CLASSIFIER_PROMPT = """Classify the user's request by the capability it needs.
Return exactly one label and no other text:
- light: simple lookup, rewrite, summary, or routine instruction
- standard: normal analysis, planning, or coding work
- heavy: difficult multi-system design, debugging, or high-stakes synthesis
When uncertain, choose the higher tier."""


@dataclass(frozen=True)
class RoutingDecision:
    tier: str
    provider: str
    model: str
    reasoning_effort: str
    classification_turn: bool = False

    def to_metadata(self) -> dict[str, str]:
        """Return the non-secret fields safe for ``SessionEntry.metadata``."""
        return {
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
        }

    def announcement(self, config: Mapping[str, Any]) -> Optional[str]:
        """Return a rationale-free route marker for the classification turn."""
        announce = config.get("announce") if isinstance(config, Mapping) else False
        if not self.classification_turn or announce not in (
            True,
            1,
            "true",
            "yes",
            "on",
        ):
            return None
        display_name = {
            tier: spec.display_name for tier, spec in _TIERS.items()
        }[self.tier]
        return f"Route: {display_name} / {self.reasoning_effort}"


@dataclass(frozen=True)
class _TierSpec:
    model: str
    reasoning_effort: str
    display_name: str


_TIERS = {
    "light": _TierSpec("gpt-5.6-luna", "low", "Luna"),
    "standard": _TierSpec("gpt-5.6-terra", "medium", "Terra"),
    "heavy": _TierSpec("gpt-5.6-sol", "high", "Sol"),
}


def decision_for_tier(tier: str) -> RoutingDecision:
    """Resolve a classifier label to the fixed openai-codex route."""
    normalized = tier if tier in _TIERS else "standard"
    spec = _TIERS[normalized]
    return RoutingDecision(
        normalized,
        _PROVIDER,
        spec.model,
        spec.reasoning_effort,
    )


def decision_from_metadata(value: Any) -> Optional[RoutingDecision]:
    """Rehydrate only a valid, non-secret route metadata record."""
    if not isinstance(value, Mapping):
        return None
    tier = str(value.get("tier") or "").strip().lower()
    decision = decision_for_tier(tier)
    if tier not in _TIERS:
        return None
    if value.get("provider") != decision.provider:
        return None
    if value.get("model") != decision.model:
        return None
    return decision


def route_request(
    message: str,
    *,
    classifier: Optional[Callable[[str], str]] = None,
) -> RoutingDecision:
    """Classify one fresh-session request, failing open to Terra/medium."""
    try:
        if classifier is None:
            from agent.auxiliary_client import call_llm, extract_content_or_reasoning

            response = call_llm(
                task="routing_classifier",
                provider=_PROVIDER,
                model=_TIERS["light"].model,
                messages=[
                    {"role": "system", "content": _CLASSIFIER_PROMPT},
                    {"role": "user", "content": str(message or "")[:4000]},
                ],
                temperature=0,
                max_tokens=8,
                timeout=20,
                reasoning_config={"enabled": True, "effort": "low"},
                allow_fallback=False,
            )
            tier = extract_content_or_reasoning(response)
        else:
            tier = classifier(message)
        tier = str(tier or "").strip().lower()
    except Exception:
        tier = "standard"
    return replace(decision_for_tier(tier), classification_turn=True)
