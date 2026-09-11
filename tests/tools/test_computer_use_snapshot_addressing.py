"""Snapshot handles survive real discovery, capture parsing and registry dispatch.

Only MCP I/O is substituted. Schemas model the strict native 0.23.2 contract:
properties (not capability tags) advertise handles; double_click has no button.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tools.computer_use.cua_backend import CuaDriverBackend
from tools.computer_use import tool
import tools.computer_use_tool  # noqa: F401
from tools.registry import registry


class _InlineBridge:
    def run(self, coro, timeout=None):
        return asyncio.run(coro)


class _NativeTransport:
    def __init__(self, addressing):
        self.addressing = addressing
        self.snapshot = "s00000001"
        self.index = 2
        self.calls = []
        self.refuse_stale = False
        self.capture_error = False
        common = {"pid", "window_id", "session", "element_index"}
        self.properties = {
            "click": common | {"button", "modifier", "x", "y"},
            "double_click": common | {"x", "y"},
            "scroll": common | {"direction", "amount", "x", "y"},
            "set_value": common | {"value"},
        }
        for props in self.properties.values():
            if addressing != "legacy":
                props.add("snapshot_id")
            if addressing in {"token", "capability"}:
                props.add("element_token")

    async def list_tools(self):
        # Capability-only older advertisement remains supported; modern drivers
        # expose the property but no custom capability metadata at all.
        return SimpleNamespace(tools=[SimpleNamespace(
            name=name,
            inputSchema={"type": "object", "additionalProperties": False,
                         "properties": {p: {} for p in props
                                        if self.addressing != "capability" or p != "element_token"}},
            capabilities=["accessibility.element_tokens"] if self.addressing == "capability" else [],
        ) for name, props in self.properties.items()])

    @staticmethod
    def result(sc, error=False):
        return SimpleNamespace(content=[], structuredContent=sc, isError=error)

    async def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "list_windows":
            return self.result({"windows": [{"pid": 42, "window_id": 7, "app_name": "Calculator"}]})
        if name == "get_window_state":
            if self.capture_error:
                return self.result({"message": "capture failed"}, error=True)
            element = {"element_index": self.index, "role": "AXButton", "label": "All Clear"}
            if self.addressing in {"token", "capability"}:
                element["element_token"] = f"{self.snapshot}:{self.index}"
            return self.result({"snapshot_id": self.snapshot, "elements": [element]})
        if name in self.properties:
            if set(args) - self.properties[name]:
                return self.result({"code": "unsupported_property", "message": str(set(args) - self.properties[name])}, True)
            if self.addressing != "legacy":
                bound = (args.get("element_token") == f"{self.snapshot}:{self.index}"
                         or args.get("snapshot_id") == self.snapshot)
                if not bound:
                    return self.result({"code": "snapshot_id_required"}, True)
            if self.refuse_stale:
                return self.result({"code": "stale_snapshot", "effect": "suspected_noop"}, True)
            return self.result({"effect": "unverifiable", "verified": False})
        raise AssertionError(f"unexpected MCP call: {name}")


@pytest.fixture
def native(monkeypatch):
    def make(addressing):
        backend = CuaDriverBackend()
        transport = _NativeTransport(addressing)
        session = backend._session
        monkeypatch.setattr(session, "_bridge", _InlineBridge())
        monkeypatch.setattr(session, "_session", transport)
        session._started = True
        asyncio.run(session._populate_capabilities(transport))
        monkeypatch.setattr(tool, "_get_backend", lambda **kw: backend)
        monkeypatch.setattr(tool, "_approval_callback", None)
        return backend, transport
    return make


def _dispatch(action, **args):
    raw = registry._tools["computer_use"].handler({"action": action, **args}, session_id="snapshot-contract")
    return json.loads(raw)


@pytest.mark.parametrize("addressing", ["token", "snapshot", "capability", "legacy"])
@pytest.mark.parametrize("action,extra", [
    ("click", {}), ("right_click", {}), ("middle_click", {}),
    ("double_click", {}), ("double_click", {"button": "right"}),
    ("scroll", {"direction": "down"}),
    ("set_value", {"value": "example"}),
])
def test_capture_handle_reaches_each_native_action(native, addressing, action, extra):
    backend, transport = native(addressing)
    captured = _dispatch("capture", mode="ax", app="Calculator")
    index = captured["elements"][0]["index"]
    previous_calls = len(transport.calls)
    result = _dispatch(action, app="Calculator", element=index, **extra)
    if action == "double_click" and extra.get("button") == "right":
        assert not result["ok"] and result["code"] == "button_unsupported", result
        assert len(transport.calls) == previous_calls
        return
    if addressing == "legacy":
        assert not result["ok"] and result["code"] == "snapshot_binding_required", result
        assert len(transport.calls) == previous_calls
        return
    assert result["ok"], result
    name, payload = transport.calls[-1]
    assert payload["element_index"] == index
    assert payload["pid"] == 42 and payload["window_id"] == 7
    assert payload["session"] == backend._session_id
    assert not {"x", "y"} & payload.keys()  # never downgrade to a pixel click
    if addressing in {"token", "capability"}:
        assert payload["element_token"] == f"{transport.snapshot}:{index}"
    elif addressing == "snapshot":
        assert payload["snapshot_id"] == transport.snapshot
    assert result["verdict"]["decision"] == "verify_fresh_state"
    assert result["verified"] is False


@pytest.mark.parametrize("addressing", ["token", "snapshot"])
@pytest.mark.parametrize("invalidate", ["refresh", "focus", "failed_capture", "transport_reset"])
def test_snapshot_binding_never_survives_invalidation_or_replays_refusal(native, addressing, invalidate):
    backend, transport = native(addressing)
    _dispatch("capture", mode="ax", app="Calculator")
    first = _dispatch("click", element=transport.index)
    assert first["ok"], first
    old_snapshot = transport.snapshot
    transport.snapshot = "s00000002"
    if invalidate == "refresh":
        _dispatch("capture", mode="ax", app="Calculator")
    elif invalidate == "focus":
        _dispatch("focus_app", app="Calculator", raise_window=False)
    elif invalidate == "failed_capture":
        transport.capture_error = True
        _dispatch("capture", mode="ax", app="Calculator")
    else:
        backend._session._notify_transport_reset()
    transport.refuse_stale = True
    previous_calls = len(transport.calls)
    result = _dispatch("click", element=transport.index)
    assert not result["ok"], result
    emitted = transport.calls[previous_calls:]
    assert len(emitted) <= 1  # no replay, coordinate fallback, or foreground escalation
    for name, args in emitted:
        assert name == "click"
        assert args.get("snapshot_id") != old_snapshot
        assert args.get("element_token") != f"{old_snapshot}:{transport.index}"
    if invalidate == "refresh":
        assert result["code"] == "stale_snapshot"
        args = emitted[0][1]
        assert (args.get("snapshot_id") == transport.snapshot
                or args.get("element_token") == f"{transport.snapshot}:{transport.index}")
