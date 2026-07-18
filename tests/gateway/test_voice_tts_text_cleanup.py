"""Regression tests for spoken voice-reply cleanup."""

from gateway.platforms.base import BasePlatformAdapter


def _adapter():
    """Minimal concrete adapter instance; prepare_tts_text does not need init."""
    Concrete = type("ConcreteAdapter", (BasePlatformAdapter,), {"MAX_MESSAGE_LENGTH": 4096})
    setattr(Concrete, "__abstractmethods__", frozenset())
    return Concrete.__new__(Concrete)


def test_prepare_tts_text_drops_fenced_code_blocks():
    spoken = _adapter().prepare_tts_text(
        "Sure.\n\n```python\nprint('do not read this')\n```\n\nFinal answer."
    )

    assert spoken == "Sure. Final answer."
    assert "print" not in spoken
    assert "do not read" not in spoken


def test_prepare_tts_text_drops_reasoning_xml_blocks():
    spoken = _adapter().prepare_tts_text(
        "<think>\nI should narrate private reasoning.\n</think>\nActual response."
    )

    assert spoken == "Actual response."
    assert "private reasoning" not in spoken


def test_prepare_tts_text_drops_reasoning_code_fences():
    spoken = _adapter().prepare_tts_text(
        "```reasoning\nInternal tool plan and hidden scratchpad.\n```\nActual response."
    )

    assert spoken == "Actual response."
    assert "scratchpad" not in spoken
