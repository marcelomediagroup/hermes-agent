"""Discord final-response mentions notify only the requesting user."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import plugins.platforms.discord.adapter as discord_adapter
from gateway.config import Platform, PlatformConfig, load_gateway_config
from gateway.platforms.base import SendResult, _thread_metadata_for_source
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    _apply_yaml_config,
    _build_allowed_mentions,
)


class _FakeMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.edit = AsyncMock()
        self.delete = AsyncMock()


class _FakeChannel:
    def __init__(self):
        self.sent_kwargs = []
        self.messages = {}

    async def send(self, **kwargs):
        self.sent_kwargs.append(kwargs)
        message = _FakeMessage(9000 + len(self.sent_kwargs))
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        return self.messages[message_id]


class _FakeClient:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, _channel_id):
        return self.channel


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.delenv("DISCORD_MENTION_USER_ON_FINAL", raising=False)
    channel = _FakeChannel()
    instance = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"mention_user_on_final": True},
        )
    )
    instance._client = _FakeClient(channel)
    return instance, channel


@pytest.mark.asyncio
async def test_final_response_starts_with_requesting_user_mention(adapter):
    instance, channel = adapter

    result = await instance.send(
        "555",
        "Done — the report is ready.",
        metadata={"notify": True, "requester_user_id": "123456789"},
    )

    assert result.success is True
    assert channel.sent_kwargs[0]["content"] == (
        "<@123456789>\nDone — the report is ready."
    )


@pytest.mark.asyncio
async def test_progress_response_does_not_mention_requesting_user(adapter):
    instance, channel = adapter

    result = await instance.send(
        "555",
        "Running tests…",
        metadata={"notify": False, "requester_user_id": "123456789"},
    )

    assert result.success is True
    assert channel.sent_kwargs[0]["content"] == "Running tests…"


@pytest.mark.asyncio
async def test_final_response_does_not_duplicate_existing_leading_mention(adapter):
    instance, channel = adapter

    await instance.send(
        "555",
        "<@123456789>\nDone.",
        metadata={"notify": True, "requester_user_id": "123456789"},
    )

    assert channel.sent_kwargs[0]["content"] == "<@123456789>\nDone."


@pytest.mark.asyncio
async def test_notify_worthy_stream_edit_is_replaced_by_fresh_message(adapter):
    instance, channel = adapter
    message = _FakeMessage(777)
    channel.fetch_message = AsyncMock(return_value=message)

    result = await instance.edit_message(
        "555",
        "777",
        "Done.",
        finalize=True,
        metadata={"notify": True, "requester_user_id": "123456789"},
    )

    assert result.success is True
    assert result.message_id == "9001"
    assert channel.sent_kwargs[0]["content"] == "<@123456789>\nDone."
    message.edit.assert_not_awaited()
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_consumer_adopts_fresh_id_when_final_edit_becomes_replacement(adapter):
    instance, channel = adapter
    preview = _FakeMessage(777)
    channel.messages[777] = preview
    consumer = GatewayStreamConsumer(
        instance,
        "555",
        metadata={"requester_user_id": "123456789"},
    )
    consumer._message_id = "777"
    consumer._last_sent_text = "Almost done."
    consumer._try_fresh_final = AsyncMock(return_value=False)

    assert await consumer._send_or_edit(
        "Done.",
        finalize=True,
        is_turn_final=True,
    )

    assert consumer.message_id == "9001"
    assert channel.sent_kwargs[0]["content"] == "<@123456789>\nDone."
    preview.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_streaming_final_is_fresh_message_so_the_mention_can_notify(adapter):
    instance, channel = adapter
    consumer = GatewayStreamConsumer(
        instance,
        "555",
        StreamConsumerConfig(
            edit_interval=0.01,
            buffer_threshold=1,
            cursor="",
        ),
        metadata={"requester_user_id": "123456789"},
    )

    consumer.on_delta("Done.")
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.05)
    consumer.finish()
    await task

    assert [sent["content"] for sent in channel.sent_kwargs] == [
        "Done.",
        "<@123456789>\nDone.",
    ]
    channel.messages[9001].delete.assert_awaited_once()
    assert consumer.final_response_sent is True


@pytest.mark.asyncio
async def test_fresh_interim_segment_stays_unmentioned(adapter):
    instance, channel = adapter
    preview = await instance.send(
        "555",
        "I’ll check that first.",
        metadata={"requester_user_id": "123456789", "expect_edits": True},
    )
    consumer = GatewayStreamConsumer(
        instance,
        "555",
        metadata={"requester_user_id": "123456789"},
    )
    consumer._message_id = preview.message_id
    consumer._track_preview_id(preview.message_id)

    assert await consumer._try_fresh_final(
        "I’ll check that first.",
        is_turn_final=False,
    )

    assert [sent["content"] for sent in channel.sent_kwargs] == [
        "I’ll check that first.",
        "I’ll check that first.",
    ]
    channel.messages[9001].delete.assert_awaited_once()
    assert consumer.final_response_sent is False


@pytest.mark.asyncio
async def test_stream_consumer_marks_only_turn_final_edits_notify_worthy():
    class _CapturingAdapter:
        def __init__(self):
            self.metadata = []

        async def send(self, chat_id, content, *, reply_to=None, metadata=None):
            self.metadata.append(metadata)
            return SendResult(success=True, message_id="sent-1")

        async def edit_message(
            self, chat_id, message_id, content, *, finalize=False, metadata=None
        ):
            self.metadata.append(metadata)
            return SendResult(success=True, message_id=message_id)

    fake_adapter = _CapturingAdapter()
    consumer = GatewayStreamConsumer(
        adapter=fake_adapter,
        chat_id="555",
        metadata={"requester_user_id": "123456789"},
    )

    await consumer._edit_message(
        message_id="777",
        content="interim",
        finalize=True,
        notify=False,
    )
    await consumer._edit_message(
        message_id="777",
        content="final",
        finalize=True,
        notify=True,
    )

    assert fake_adapter.metadata == [
        {"requester_user_id": "123456789"},
        {"requester_user_id": "123456789", "notify": True},
    ]

    preamble_adapter = _CapturingAdapter()
    preamble_consumer = GatewayStreamConsumer(
        adapter=preamble_adapter,
        chat_id="555",
        metadata={"requester_user_id": "123456789"},
    )
    await preamble_consumer._send_or_edit(
        "I’ll check that first.",
        finalize=True,
        is_turn_final=False,
    )

    assert preamble_adapter.metadata == [
        {"requester_user_id": "123456789", "expect_edits": True}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("requester_user_id", [None, "", "alice", "123 bad"])
async def test_final_response_ignores_missing_or_non_snowflake_requester(
    adapter, requester_user_id
):
    instance, channel = adapter

    await instance.send(
        "555",
        "Done.",
        metadata={"notify": True, "requester_user_id": requester_user_id},
    )

    assert channel.sent_kwargs[0]["content"] == "Done."


def test_yaml_config_keeps_final_mention_toggle_out_of_global_env(monkeypatch):
    monkeypatch.delenv("DISCORD_MENTION_USER_ON_FINAL", raising=False)

    _apply_yaml_config(
        {"discord": {"mention_user_on_final": True}},
        {"mention_user_on_final": True},
    )

    assert "DISCORD_MENTION_USER_ON_FINAL" not in os.environ


def test_nested_platform_config_preserves_final_mention_settings_per_adapter(
    monkeypatch, tmp_path
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "platforms:\n"
        "  discord:\n"
        "    mention_user_on_final: true\n"
        "    allow_mentions:\n"
        "      users: true\n"
        "      replied_user: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("DISCORD_MENTION_USER_ON_FINAL", raising=False)
    monkeypatch.delenv("DISCORD_ALLOW_MENTION_USERS", raising=False)
    monkeypatch.delenv("DISCORD_ALLOW_MENTION_REPLIED_USER", raising=False)

    config = load_gateway_config()

    extra = config.platforms[Platform.DISCORD].extra
    assert extra["mention_user_on_final"] is True
    assert extra["allow_mentions"] == {
        "users": True,
        "replied_user": False,
    }
    assert "DISCORD_MENTION_USER_ON_FINAL" not in os.environ
    assert "DISCORD_ALLOW_MENTION_USERS" not in os.environ
    assert "DISCORD_ALLOW_MENTION_REPLIED_USER" not in os.environ


def test_top_level_discord_config_preserves_live_mention_settings(
    monkeypatch,
    tmp_path,
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "discord:\n"
        "  mention_user_on_final: true\n"
        "  allow_mentions:\n"
        "    everyone: false\n"
        "    roles: false\n"
        "    users: true\n"
        "    replied_user: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("DISCORD_MENTION_USER_ON_FINAL", raising=False)

    extra = load_gateway_config().platforms[Platform.DISCORD].extra

    assert extra["mention_user_on_final"] is True
    assert extra["allow_mentions"] == {
        "everyone": False,
        "roles": False,
        "users": True,
        "replied_user": False,
    }


@pytest.mark.asyncio
async def test_final_mention_setting_is_isolated_per_adapter(monkeypatch):
    monkeypatch.delenv("DISCORD_MENTION_USER_ON_FINAL", raising=False)
    enabled_channel = _FakeChannel()
    disabled_channel = _FakeChannel()
    enabled = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"mention_user_on_final": True},
        )
    )
    disabled = DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"mention_user_on_final": False},
        )
    )
    enabled._client = _FakeClient(enabled_channel)
    disabled._client = _FakeClient(disabled_channel)

    metadata = {"notify": True, "requester_user_id": "123456789"}
    await enabled.send("555", "Done.", metadata=metadata)
    await disabled.send("555", "Done.", metadata=metadata)

    assert enabled.mention_user_on_final_enabled is True
    assert disabled.mention_user_on_final_enabled is False
    assert enabled_channel.sent_kwargs[0]["content"] == "<@123456789>\nDone."
    assert disabled_channel.sent_kwargs[0]["content"] == "Done."


def test_gateway_thread_metadata_carries_requester_without_thread():
    runner = object.__new__(GatewayRunner)
    source = SessionSource(
        platform=Platform.DISCORD,
        user_id="123456789",
        chat_id="555",
        message_id="777",
    )

    metadata = GatewayRunner._thread_metadata_for_source(runner, source)

    assert metadata == {"requester_user_id": "123456789"}


def test_base_adapter_thread_metadata_carries_requester_without_thread():
    source = SessionSource(
        platform=Platform.DISCORD,
        user_id="123456789",
        chat_id="555",
        message_id="777",
    )

    metadata = _thread_metadata_for_source(source)

    assert metadata == {"requester_user_id": "123456789"}


def test_requester_metadata_is_not_added_to_other_platforms():
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="123456789",
        chat_id="555",
        message_id="777",
    )

    assert _thread_metadata_for_source(source) is None


def test_final_mention_mode_disables_reply_pings_by_default(monkeypatch):
    monkeypatch.delenv("DISCORD_MENTION_USER_ON_FINAL", raising=False)
    monkeypatch.delenv("DISCORD_ALLOW_MENTION_REPLIED_USER", raising=False)

    allowed_mentions_cls = MagicMock()
    monkeypatch.setattr(
        discord_adapter.discord,
        "AllowedMentions",
        allowed_mentions_cls,
    )
    _build_allowed_mentions(mention_user_on_final=True)

    kwargs = allowed_mentions_cls.call_args.kwargs
    assert kwargs["users"] is True
    assert kwargs["replied_user"] is False


@pytest.mark.asyncio
async def test_identical_turn_final_frame_still_gets_notification_edit():
    class _CapturingAdapter:
        MAX_MESSAGE_LENGTH = 4096
        REQUIRES_EDIT_FINALIZE = False

        def __init__(self):
            self.edits = []
            self.mention_user_on_final_enabled = True

        async def edit_message(
            self,
            chat_id,
            message_id,
            content,
            *,
            finalize=False,
            metadata=None,
        ):
            self.edits.append(metadata)
            return SendResult(success=True, message_id=message_id)

    adapter = _CapturingAdapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "555",
        metadata={"requester_user_id": "123456789"},
    )
    consumer._message_id = "message-1"
    consumer._last_sent_text = "Done."

    assert await consumer._send_or_edit(
        "Done.",
        finalize=True,
        is_turn_final=True,
    )
    assert adapter.edits == [
        {"requester_user_id": "123456789", "notify": True}
    ]


@pytest.mark.asyncio
async def test_chunked_turn_final_notifies_only_on_first_chunk():
    adapter = MagicMock()
    adapter.send = AsyncMock(
        side_effect=[
            SendResult(success=True, message_id="message-1"),
            SendResult(success=True, message_id="message-2"),
        ]
    )
    adapter.edit_message = AsyncMock()
    adapter.MAX_MESSAGE_LENGTH = 700
    adapter.truncate_message = MagicMock(
        side_effect=lambda text, limit, **kwargs: [text[:600], text[600:]]
    )
    consumer = GatewayStreamConsumer(
        adapter,
        "555",
        StreamConsumerConfig(buffer_only=True),
        metadata={"requester_user_id": "123456789"},
    )

    consumer.on_delta("x" * 1200)
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.02)
    consumer.finish()
    await task

    metadata = [call.kwargs["metadata"] for call in adapter.send.await_args_list]
    assert metadata[0]["notify"] is True
    assert "notify" not in metadata[1]


@pytest.mark.asyncio
async def test_chunked_turn_final_notifies_first_successful_chunk():
    adapter = MagicMock()
    adapter.send = AsyncMock(
        side_effect=[
            SendResult(success=False, error="temporary failure"),
            SendResult(success=True, message_id="message-2"),
        ]
    )
    adapter.edit_message = AsyncMock()
    adapter.MAX_MESSAGE_LENGTH = 700
    adapter.truncate_message = MagicMock(
        side_effect=lambda text, limit, **kwargs: [text[:600], text[600:]]
    )
    consumer = GatewayStreamConsumer(
        adapter,
        "555",
        StreamConsumerConfig(buffer_only=True),
        metadata={"requester_user_id": "123456789"},
    )

    consumer.on_delta("x" * 1200)
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.02)
    consumer.finish()
    await task

    metadata = [call.kwargs["metadata"] for call in adapter.send.await_args_list]
    assert metadata[0]["notify"] is True
    assert metadata[1]["notify"] is True
    assert consumer.final_response_sent is True


@pytest.mark.asyncio
async def test_fallback_turn_final_notifies_only_on_first_chunk():
    adapter = MagicMock()
    adapter.send = AsyncMock(
        side_effect=[
            SendResult(success=True, message_id="message-1"),
            SendResult(success=True, message_id="message-2"),
        ]
    )
    adapter.MAX_MESSAGE_LENGTH = 700
    consumer = GatewayStreamConsumer(
        adapter,
        "555",
        metadata={"requester_user_id": "123456789"},
    )

    await consumer._send_fallback_final("x" * 1200)

    metadata = [call.kwargs["metadata"] for call in adapter.send.await_args_list]
    assert metadata[0]["notify"] is True
    assert "notify" not in metadata[1]


@pytest.mark.asyncio
async def test_empty_fallback_does_not_suppress_fresh_final_when_mention_edit_fails():
    class _FailingEditAdapter:
        MAX_MESSAGE_LENGTH = 4096

        def __init__(self):
            self.edits = []
            self.mention_user_on_final_enabled = True

        async def edit_message(
            self,
            chat_id,
            message_id,
            content,
            *,
            finalize=False,
            metadata=None,
        ):
            self.edits.append(metadata)
            return SendResult(success=False, error="network down")

    adapter = _FailingEditAdapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "555",
        StreamConsumerConfig(cursor=""),
        metadata={"requester_user_id": "123456789"},
    )
    consumer._message_id = "message-1"
    consumer._last_sent_text = "Done."

    await consumer._send_fallback_final("Done.")

    assert adapter.edits == [
        {"requester_user_id": "123456789", "notify": True}
    ]
    assert consumer.final_response_sent is False
    assert consumer.final_content_delivered is False
    assert consumer.has_delivered_text("Done.") is False


@pytest.mark.asyncio
async def test_empty_fallback_confirms_delivery_after_successful_mention_edit():
    class _SuccessfulEditAdapter:
        MAX_MESSAGE_LENGTH = 4096

        def __init__(self):
            self.edits = []
            self.mention_user_on_final_enabled = True

        async def edit_message(
            self,
            chat_id,
            message_id,
            content,
            *,
            finalize=False,
            metadata=None,
        ):
            self.edits.append(metadata)
            return SendResult(success=True, message_id=message_id)

    adapter = _SuccessfulEditAdapter()
    consumer = GatewayStreamConsumer(
        adapter,
        "555",
        StreamConsumerConfig(cursor=""),
        metadata={"requester_user_id": "123456789"},
    )
    consumer._message_id = "message-1"
    consumer._last_sent_text = "Done."

    await consumer._send_fallback_final("Done.")

    assert adapter.edits == [
        {"requester_user_id": "123456789", "notify": True}
    ]
    assert consumer.final_response_sent is True
    assert consumer.final_content_delivered is True
