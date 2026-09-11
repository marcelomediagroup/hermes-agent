"""Discord final-response mentions notify only the requesting user."""

import asyncio
from types import SimpleNamespace
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
    _build_allowed_mentions,
)


class _FakeChannel:
    def __init__(self):
        self.sent_kwargs = []

    async def send(self, **kwargs):
        self.sent_kwargs.append(kwargs)
        return SimpleNamespace(id=9000 + len(self.sent_kwargs))


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
async def test_final_stream_edit_starts_with_requesting_user_mention(adapter):
    instance, channel = adapter
    message = SimpleNamespace(edit=AsyncMock())
    channel.get_partial_message = MagicMock(return_value=message)

    result = await instance.edit_message(
        "555",
        "777",
        "Done.",
        finalize=True,
        metadata={"notify": True, "requester_user_id": "123456789"},
    )

    assert result.success is True
    assert message.edit.await_args.kwargs["content"] == "<@123456789>\nDone."


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


def test_yaml_config_stays_scoped_to_discord_platform(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCORD_MENTION_USER_ON_FINAL", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        """discord:
  enabled: true
  mention_user_on_final: true
  allow_mentions:
    users: true
    replied_user: false
""",
        encoding="utf-8",
    )

    config = load_gateway_config()

    discord_extra = config.platforms[Platform.DISCORD].extra
    assert discord_extra["mention_user_on_final"] is True
    assert discord_extra["allow_mentions"] == {
        "users": True,
        "replied_user": False,
    }


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


def test_final_mention_setting_is_per_adapter_instance(monkeypatch):
    monkeypatch.delenv("DISCORD_MENTION_USER_ON_FINAL", raising=False)

    enabled = DiscordAdapter(
        PlatformConfig(extra={"mention_user_on_final": True})
    )
    disabled = DiscordAdapter(
        PlatformConfig(extra={"mention_user_on_final": False})
    )

    assert enabled.mention_user_on_final_enabled is True
    assert disabled.mention_user_on_final_enabled is False


@pytest.mark.asyncio
async def test_identical_turn_final_frame_still_gets_notification_edit():
    class _CapturingAdapter:
        MAX_MESSAGE_LENGTH = 4096
        REQUIRES_EDIT_FINALIZE = False
        mention_user_on_final_enabled = True

        def __init__(self):
            self.edits = []

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
async def test_opt_out_skips_identical_final_edit_with_requester_metadata():
    class _CapturingAdapter:
        MAX_MESSAGE_LENGTH = 4096
        REQUIRES_EDIT_FINALIZE = False
        mention_user_on_final_enabled = False

        def __init__(self):
            self.edits = []

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
    assert adapter.edits == []


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
            SendResult(success=False, error="network down"),
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
async def test_empty_continuation_fallback_resends_for_final_mention():
    adapter = MagicMock()
    adapter.mention_user_on_final_enabled = True
    adapter.send = AsyncMock(
        return_value=SendResult(success=True, message_id="message-2")
    )
    adapter.MAX_MESSAGE_LENGTH = 4096
    consumer = GatewayStreamConsumer(
        adapter,
        "555",
        metadata={"requester_user_id": "123456789"},
    )
    consumer._message_id = "preview-1"
    consumer._last_sent_text = "Done."

    await consumer._send_fallback_final("Done.")

    assert adapter.send.await_count == 1
    sent = adapter.send.await_args.kwargs
    assert sent["content"] == "Done."
    assert sent["metadata"]["notify"] is True
    assert consumer.final_response_sent is True
