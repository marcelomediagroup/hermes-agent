"""Indexed input must stay bound to the capture the caller observed."""
import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from tools.computer_use.cua_backend import CuaDriverBackend


ACTIONS = [
    ("click", {"element": 1}, "click"),
    ("click", {"element": 1, "button": "right"}, "click"),
    ("click", {"element": 1, "button": "middle"}, "click"),
    ("click", {"element": 1, "click_count": 2}, "double_click"),
    ("scroll", {"element": 1, "direction": "down"}, "scroll"),
    ("set_value", {"element": 1, "value": "new"}, "set_value"),
]


def backend_with_capture(binding, properties):
    backend = CuaDriverBackend()
    session: Any = backend._session
    tools = [SimpleNamespace(name=name, inputSchema={"properties": {p: {} for p in properties}},
                             capabilities=[], model_extra={})
             for name in {row[2] for row in ACTIONS} | {"drag", "bring_to_front"}]

    class Listing:
        async def list_tools(self):
            return SimpleNamespace(tools=tools, model_extra={})

    asyncio.run(session._populate_capabilities(Listing()))
    calls = []
    payload = {"elements": [{"element_index": 1, "role": "AXButton", "label": "one"}], **binding}

    class McpPeer:
        async def call_tool(self, name, args):
            calls.append((name, dict(args)))
            return SimpleNamespace(isError=False,
                                   content=[SimpleNamespace(type="text", text="Window\n[1] AXButton one")],
                                   structuredContent=payload if name == "get_window_state" else {"effect": "confirmed"})

    # Replace only external I/O; keep the native session and MCP envelope parser in the path.
    session._session = McpPeer()
    session._bridge = SimpleNamespace(run=lambda coroutine, timeout: asyncio.run(coroutine))
    session._started = True
    backend.capture(mode="ax", pid=42, window_id=7)
    calls.clear()
    return backend, calls, payload


@pytest.mark.parametrize("method,kwargs,tool", ACTIONS)
@pytest.mark.parametrize("binding_kind", ["token", "snapshot", "markdown", "snapshot_only_schema", "unsupported", "missing"])
def test_indexed_actions_preserve_observed_binding_or_refuse(method, kwargs, tool, binding_kind, monkeypatch):
    properties = {"element_index", "element_token", "snapshot_id"} if binding_kind != "unsupported" else {"element_index"}
    binding: dict[str, Any] = {"snapshot_id": "s00000001"} if binding_kind != "missing" else {}
    if binding_kind in {"token", "snapshot_only_schema"}:
        binding["elements"] = [{"element_index": 1, "element_token": "opaque-handle", "role": "AXButton"}]
    if binding_kind == "snapshot_only_schema":
        properties.remove("element_token")
    if binding_kind == "markdown":
        binding["elements"] = []
    backend, calls, _ = backend_with_capture(binding, properties)
    import tools.computer_use_tool  # noqa: F401 — register the native tool
    from tools.computer_use import tool as computer_use
    from tools.registry import registry

    monkeypatch.setattr(computer_use, "_get_backend", lambda **kw: backend)
    request = {"action": "double_click" if tool == "double_click" else method, **kwargs}
    request.pop("click_count", None)
    response = registry.dispatch("computer_use", request, session_id="binding-test")
    assert isinstance(response, str)
    result = SimpleNamespace(**json.loads(response))
    if binding_kind in {"missing", "unsupported"}:
        assert not result.ok
        assert result.code == "snapshot_binding_required"
        assert calls == []
    else:
        assert result.ok
        assert calls[0][0] == tool
        args = calls[0][1]
        assert args["element_index"] == kwargs["element"]
        assert args["pid"] == 42 and args["window_id"] == 7
        key, value = ("element_token", "opaque-handle") if binding_kind == "token" else ("snapshot_id", binding["snapshot_id"])
        assert args[key] == value


@pytest.mark.parametrize("transition", ["fresh_without_binding", "vision", "failed_capture", "reset", "retarget", "unknown_index", "drag", "missing_window", "foreground"])
def test_binding_cannot_leak_to_unobserved_input(transition):
    backend, calls, payload = backend_with_capture({"snapshot_id": "s00000001"},
                                                   {"element_index", "snapshot_id", "delivery_mode"})
    kwargs: dict[str, Any] = {"element": 1}
    method = "click"
    if transition == "fresh_without_binding":
        payload.pop("snapshot_id")
        backend.capture(mode="ax", pid=42, window_id=7)
    elif transition == "vision":
        # Avoid screenshot fallback: a successful capture-only response cannot authorize AX input.
        backend._capture_vision = lambda: (None, None, [], "")
        backend.capture(mode="vision", pid=42, window_id=7)
    elif transition == "failed_capture":
        def failed(*args, **kwargs):
            raise RuntimeError("capture failed")
        backend._capture_window_state = failed
        with pytest.raises(RuntimeError, match="capture failed"):
            backend.capture(mode="ax", pid=42, window_id=7)
    elif transition == "reset":
        backend._handle_transport_reset()
        backend._active_pid, backend._active_window_id = 42, 7
    elif transition == "retarget":
        backend._set_active_target({"pid": 43, "window_id": 8})
    elif transition == "unknown_index":
        kwargs["element"] = 99
    elif transition == "drag":
        method, kwargs = "drag", {"from_element": 1, "to_element": 1}
    elif transition == "missing_window":
        backend._active_window_id = None
        method, kwargs = "scroll", {"element": 1, "direction": "down"}
    else:
        payload.pop("snapshot_id")
        backend.capture(mode="ax", pid=42, window_id=7)
        kwargs.update(delivery_mode="foreground", bring_to_front=True)
    calls.clear()
    result = getattr(backend, method)(**kwargs)
    assert not result.ok
    assert calls == []
    if transition != "missing_window":
        # Coordinate input stays available; don't silently convert an indexed request to it.
        assert backend.click(x=10, y=20).ok
        assert "snapshot_id" not in calls[-1][1]
        assert "element_token" not in calls[-1][1]
