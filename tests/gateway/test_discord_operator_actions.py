"""Discord routing contract for durable operator-card actions."""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from gateway.config import PlatformConfig
from gateway.operator_actions import OperatorActionDispatcher
from gateway.operator_actions import OperatorActionPersistenceError
from gateway.operator_cards import OperatorCard
from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    OperatorActionDynamicItem,
    _build_operator_card_view,
)


@pytest.fixture(autouse=True)
def _isolate_discord_gate_environment(monkeypatch):
    """Keep auth routing tests independent of the operator's live profile."""
    for name in (
        "DISCORD_ALLOWED_USERS",
        "DISCORD_ALLOWED_ROLES",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS",
        "DISCORD_NO_THREAD_CHANNELS",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_MISSED_MESSAGE_BACKFILL_CHANNELS",
        "DISCORD_ALLOW_ALL_USERS",
        "DISCORD_ALLOW_BOTS",
        "GATEWAY_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
    ):
        monkeypatch.delenv(name, raising=False)


def _card_payload(**overrides):
    payload = {
        "kind": "operator_card",
        "version": 1,
        "card_type": "approval",
        "title": "Choose the next step",
        "severity": "needs_review",
        "summary": "A human decision is required before later automation can continue.",
        "fields": [{"label": "Issue", "value": "OE-175"}],
        "actions": [
            {"id": "primary", "label": "Primary", "style": "primary"},
            {"id": "secondary", "label": "Secondary", "style": "secondary"},
            {"id": "accept", "label": "Accept", "style": "success"},
            {"id": "reject", "label": "Reject", "style": "danger"},
            {"id": "review", "label": "Review", "style": "link"},
        ],
        "links": [],
        "state_ref": "private-customer-state:oe-175:1",
    }
    payload.update(overrides)
    return payload


def _button(dynamic_item):
    return getattr(dynamic_item, "item", dynamic_item)


def _interaction(user_id, *, client=None, channel_id=555, role_ids=()):
    guild = SimpleNamespace(id=77, get_member=lambda _user_id: None)
    user = SimpleNamespace(
        id=int(user_id),
        name=f"user-{user_id}",
        display_name=f"Operator {user_id}",
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        guild=guild,
    )
    return SimpleNamespace(
        client=client,
        user=user,
        channel=SimpleNamespace(id=channel_id, name="approvals"),
        channel_id=channel_id,
        guild=guild,
        guild_id=guild.id,
        response=SimpleNamespace(
            send_message=AsyncMock(),
            defer=AsyncMock(),
        ),
        edit_original_response=AsyncMock(),
    )


def test_validated_actions_build_bounded_opaque_buttons_with_safe_styles(tmp_path):
    card = OperatorCard.from_mapping(_card_payload())
    registration = OperatorActionDispatcher(
        state_root=tmp_path, clock=lambda: 100.0
    ).register_card(card, ttl_seconds=60)

    view = _build_operator_card_view(card, registration)
    buttons = [_button(child) for child in view.children]

    assert view.timeout is None
    assert [button.label for button in buttons] == [
        "Primary",
        "Secondary",
        "Accept",
        "Reject",
        "Review",
    ]
    assert [button.style for button in buttons] == [
        discord.ButtonStyle.primary,
        discord.ButtonStyle.secondary,
        discord.ButtonStyle.success,
        discord.ButtonStyle.danger,
        discord.ButtonStyle.secondary,
    ]
    assert all(
        re.fullmatch(r"hoa1:[A-Za-z0-9_-]{16}:[a-z][a-z0-9_-]{0,63}", button.custom_id)
        for button in buttons
    )
    assert all(len(button.custom_id.encode("utf-8")) <= 100 for button in buttons)
    assert all(card.state_ref not in button.custom_id for button in buttons)


def test_one_global_dynamic_router_is_registered_per_discord_client(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    client = SimpleNamespace(add_dynamic_items=MagicMock())

    adapter._install_operator_action_router(client)

    client.add_dynamic_items.assert_called_once_with(OperatorActionDynamicItem)
    assert (
        client._hermes_operator_action_handler
        == adapter._handle_operator_action_interaction
    )


@pytest.mark.asyncio
async def test_unauthorized_user_is_rejected_before_registry_mutation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._allowed_user_ids = {"111"}
    adapter._operator_actions.dispatch = MagicMock()
    interaction = _interaction("222")

    await adapter._handle_operator_action_interaction(
        interaction,
        "abcdefghijklmnop",
        "accept",
    )

    adapter._operator_actions.dispatch.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "You're not authorized to use this command.",
        ephemeral=True,
    )
    interaction.response.defer.assert_not_awaited()
    interaction.edit_original_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_user_in_disallowed_channel_is_rejected_before_mutation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "999")
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._allowed_user_ids = {"111"}
    adapter._operator_actions.dispatch = MagicMock()
    interaction = _interaction("111", channel_id=555)

    await adapter._handle_operator_action_interaction(
        interaction,
        "abcdefghijklmnop",
        "accept",
    )

    adapter._operator_actions.dispatch.assert_not_called()
    interaction.response.send_message.assert_awaited_once()
    interaction.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_role_records_intent_and_edits_same_message_terminally(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._allowed_role_ids = {42}

    class _NoExternalRunner:
        def __getattribute__(self, name):
            raise AssertionError(f"external gateway access is forbidden: {name}")

    adapter.gateway_runner = _NoExternalRunner()
    card = OperatorCard.from_mapping(
        _card_payload(actions=[{"id": "accept", "label": "Accept", "style": "success"}])
    )
    registration = adapter._operator_actions.register_card(card, ttl_seconds=60)
    interaction = _interaction("333", role_ids=(42,))

    await adapter._handle_operator_action_interaction(
        interaction,
        registration.id,
        "accept",
    )

    interaction.response.send_message.assert_not_awaited()
    interaction.response.defer.assert_awaited_once_with()
    interaction.edit_original_response.assert_awaited_once()
    edit_kwargs = interaction.edit_original_response.await_args.kwargs
    assert edit_kwargs["view"] is None
    assert edit_kwargs["content"].startswith("✅ **Done — Operator intent recorded**")
    assert edit_kwargs["embed"].title == "Operator intent recorded"
    assert adapter._operator_actions.read_audit_records()[0]["actor_id"] == "333"


@pytest.mark.asyncio
async def test_dynamic_item_routes_through_restarted_adapters_durable_registry(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    card = OperatorCard.from_mapping(
        _card_payload(actions=[{"id": "accept", "label": "Accept", "style": "success"}])
    )
    registration = first._operator_actions.register_card(card, ttl_seconds=60)
    custom_id = registration.custom_id("accept")

    restarted = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    restarted._allowed_user_ids = {"444"}
    client = SimpleNamespace(
        _hermes_operator_action_handler=restarted._handle_operator_action_interaction
    )
    interaction = _interaction("444", client=client)
    raw_button = discord.ui.Button(
        label="Accept",
        style=discord.ButtonStyle.success,
        custom_id=custom_id,
    )
    match = OperatorActionDynamicItem.__discord_ui_compiled_template__.fullmatch(
        custom_id
    )
    assert match is not None
    dynamic_item = await OperatorActionDynamicItem.from_custom_id(
        interaction,
        raw_button,
        match,
    )

    await dynamic_item.callback(interaction)

    interaction.edit_original_response.assert_awaited_once()
    assert restarted._operator_actions.read_audit_records()[0]["result"] == "recorded"


@pytest.mark.asyncio
async def test_unknown_dynamic_route_is_audited_and_edits_to_blocked_card(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._allowed_user_ids = {"555"}
    interaction = _interaction("555")

    await adapter._handle_operator_action_interaction(
        interaction,
        "abcdefghijklmnop",
        "accept",
    )

    interaction.response.defer.assert_awaited_once_with()
    edit_kwargs = interaction.edit_original_response.await_args.kwargs
    assert edit_kwargs["view"] is None
    assert edit_kwargs["content"].startswith("🔴 **Blocked — Operator action blocked**")
    assert adapter._operator_actions.read_audit_records()[0]["result"] == (
        "blocked_unknown_state"
    )


@pytest.mark.asyncio
async def test_normal_operator_card_send_includes_durable_view(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    channel = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(id=7001)),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.send(
        "555",
        "fallback",
        metadata={
            "operator_card": _card_payload(
                actions=[{"id": "accept", "label": "Accept", "style": "success"}]
            )
        },
    )

    assert result.success is True
    kwargs = channel.send.await_args.kwargs
    assert kwargs["embed"].title == "Choose the next step"
    assert len(kwargs["view"].children) == 1
    button = _button(kwargs["view"].children[0])
    assert button.custom_id.startswith("hoa1:")
    assert button.custom_id.endswith(":accept")


@pytest.mark.asyncio
async def test_forum_starter_operator_card_includes_durable_view(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    thread_channel = SimpleNamespace(id=888, send=AsyncMock())
    thread = SimpleNamespace(
        id=888,
        message=SimpleNamespace(id=7002),
        thread=thread_channel,
    )
    forum = discord.ForumChannel()
    forum.id = 999
    forum.create_thread = AsyncMock(return_value=thread)
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: forum,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.send(
        "999",
        "fallback",
        metadata={
            "operator_card": _card_payload(
                actions=[{"id": "accept", "label": "Accept", "style": "success"}]
            )
        },
    )

    assert result.success is True
    kwargs = forum.create_thread.await_args.kwargs
    assert kwargs["embed"].title == "Choose the next step"
    assert len(kwargs["view"].children) == 1
    assert thread_channel.send.await_count == 0


@pytest.mark.asyncio
async def test_reply_reference_retry_preserves_operator_action_view(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    send_calls = []

    async def send(**kwargs):
        send_calls.append(kwargs)
        if len(send_calls) == 1:
            raise RuntimeError("400 Bad Request (error code: 10008): Unknown Message")
        return SimpleNamespace(id=7003)

    channel = SimpleNamespace(id=555, guild=None, send=AsyncMock(side_effect=send))
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.send(
        "555",
        "fallback",
        reply_to="123",
        metadata={
            "operator_card": _card_payload(
                actions=[{"id": "accept", "label": "Accept", "style": "success"}]
            )
        },
    )

    assert result.success is True
    assert len(send_calls) == 2
    assert send_calls[0]["view"] is send_calls[1]["view"]
    assert send_calls[0]["reference"] is not None
    assert send_calls[1]["reference"] is None


@pytest.mark.asyncio
async def test_actionable_card_send_fails_closed_when_registry_is_unavailable(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._operator_actions.register_card = MagicMock(
        side_effect=OperatorActionPersistenceError("state unavailable")
    )
    channel = SimpleNamespace(send=AsyncMock())
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.send(
        "555",
        "fallback",
        metadata={
            "operator_card": _card_payload(
                actions=[{"id": "accept", "label": "Accept", "style": "success"}]
            )
        },
    )

    assert result.success is False
    assert "state unavailable" in (result.error or "")
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_actionless_card_keeps_existing_embed_send_without_registry_or_view(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._operator_actions.register_card = MagicMock()
    channel = SimpleNamespace(
        send=AsyncMock(return_value=SimpleNamespace(id=7004)),
    )
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(),
    )

    result = await adapter.send(
        "555",
        "fallback",
        metadata={"operator_card": _card_payload(actions=[])},
    )

    assert result.success is True
    adapter._operator_actions.register_card.assert_not_called()
    kwargs = channel.send.await_args.kwargs
    assert kwargs["embed"].title == "Choose the next step"
    assert "view" not in kwargs
