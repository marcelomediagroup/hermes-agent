"""Behavior contracts for private Discord intake cards."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.intake_cards import (
    build_attachment_intake_card,
    build_voice_note_blocked_card,
    build_voice_note_intake_card,
)
from gateway.operator_cards import OperatorCard
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def test_voice_note_card_is_compact_advisory_and_opaque():
    transcript = (
        "We should review the launch contract tomorrow. "
        "Kelly owns the copy edits, and Martin will decide whether to publish. "
        + "Private detail. " * 80
    )

    card = build_voice_note_intake_card(
        transcript,
        source_ref="discord-message-123",
    )

    assert card.card_type == "capture"
    assert card.severity == "needs_review"
    assert card.title == "Voice note ready"
    assert len(card.summary) <= 500
    assert "We should review the launch contract tomorrow." in card.summary
    assert transcript not in card.summary
    assert [action.id for action in card.actions] == [
        "extract_tasks",
        "capture_decisions",
        "prepare_memory",
    ]
    assert "authenticated intent only" in next(
        field.value for field in card.fields if field.label == "Safety"
    )
    assert transcript not in card.state_ref
    assert "discord-message-123" not in card.state_ref
    assert card.state_ref == build_voice_note_intake_card(
        transcript,
        source_ref="discord-message-123",
    ).state_ref


def test_voice_note_failure_card_explains_recovery_without_actions():
    card = build_voice_note_blocked_card(
        source_ref="discord-message-124",
        ordinal=1,
    )

    fields = {field.label: field.value for field in card.fields}
    assert card.title == "Voice note blocked"
    assert card.severity == "blocked"
    assert card.actions == ()
    assert "transcription" in fields["Reason"].lower()
    assert "resend" in fields["Recovery"].lower()
    assert "no external action" in fields["Safety"].lower()
    assert "discord-message-124" not in card.state_ref


@pytest.mark.parametrize(
    ("filename", "mime_type", "expected_actions"),
    [
        (
            "agreement.pdf",
            "application/pdf",
            ["summarize", "extract_tasks", "review_contract", "create_oe_task"],
        ),
        (
            "campaign.png",
            "image/png",
            ["summarize", "extract_tasks", "turn_into_content_brief", "create_oe_task"],
        ),
        (
            "metrics.csv",
            "text/csv",
            ["summarize", "extract_tasks", "create_oe_task"],
        ),
        (
            "clip.mp4",
            "video/mp4",
            ["summarize", "extract_tasks", "turn_into_content_brief", "create_oe_task"],
        ),
        (
            "archive.bin",
            "application/octet-stream",
            ["summarize", "extract_tasks", "create_oe_task"],
        ),
    ],
)
def test_attachment_actions_are_deterministic_by_type(
    filename,
    mime_type,
    expected_actions,
):
    first = build_attachment_intake_card(
        filename=filename,
        mime_type=mime_type,
        status="ready",
        source_ref="message-42:attachment-7",
        size_bytes=1234,
        limit_bytes=32 * 1024 * 1024,
    )
    second = build_attachment_intake_card(
        filename=filename,
        mime_type=mime_type,
        status="ready",
        source_ref="message-42:attachment-7",
        size_bytes=1234,
        limit_bytes=32 * 1024 * 1024,
    )

    assert [action.id for action in first.actions] == expected_actions
    assert first.to_mapping() == second.to_mapping()
    assert first.state_ref == second.state_ref
    assert "cached privately" in first.summary.lower()
    assert "authenticated intent only" in next(
        field.value for field in first.fields if field.label == "Safety"
    )
    assert "deployment" in next(
        field.value for field in first.fields if field.label == "Safety"
    )


@pytest.mark.parametrize(
    ("status", "reason_fragment"),
    [
        ("oversized", "configured limit"),
        ("unreadable", "could not be read"),
        ("download_failed", "could not be downloaded"),
    ],
)
def test_blocked_attachment_cards_explain_recovery_without_actions(
    status,
    reason_fragment,
):
    card = build_attachment_intake_card(
        filename="private\ncontract.pdf",
        mime_type="application/pdf",
        status=status,
        source_ref="message-7:attachment-2",
        size_bytes=2048,
        limit_bytes=1024,
    )

    fields = {field.label: field.value for field in card.fields}
    assert card.severity == "blocked"
    assert card.actions == ()
    assert "\n" not in fields["File"]
    assert reason_fragment in fields["Reason"].lower()
    assert fields["Limit"] == "1 KiB"
    assert "resend" in fields["Recovery"].lower()
    assert "no external action" in fields["Safety"].lower()


def test_attachment_card_never_embeds_file_contents_or_local_path():
    private_contents = "account number 1234 and confidential payment instructions"
    local_path = "/private/cache/doc_deadbeef_contract.pdf"

    card = build_attachment_intake_card(
        filename="contract.pdf",
        mime_type="application/pdf",
        status="ready",
        source_ref="message-9:attachment-1",
        size_bytes=len(private_contents),
        limit_bytes=32 * 1024 * 1024,
    )
    rendered = str(card.to_mapping())

    assert private_contents not in rendered
    assert local_path not in rendered


def _discord_voice_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        stt_enabled=True,
        stt_echo_transcripts=True,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False
    return runner


def _discord_voice_event() -> tuple[MessageEvent, SessionSource]:
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="private-channel",
        chat_type="dm",
        user_id="authorized-user",
        message_id="message-voice-1",
    )
    return MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=source,
        message_id="message-voice-1",
        media_urls=["/private/cache/voice.ogg"],
        media_types=["audio/ogg"],
    ), source


@pytest.mark.asyncio
async def test_discord_voice_success_echoes_transcript_and_sends_action_card_once():
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="sent"))
    )
    runner = _discord_voice_runner(adapter)
    event, source = _discord_voice_event()
    transcript = "Decide the launch owner tomorrow. " + "private tail " * 60

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": transcript, "provider": "mock"},
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert transcript in result
    assert adapter.send.await_count == 2
    echo_call, card_call = adapter.send.await_args_list
    assert transcript in echo_call.args[1]
    card_mapping = card_call.kwargs["metadata"]["operator_card"]
    card = OperatorCard.from_mapping(card_mapping)
    assert card.title == "Voice note ready"
    assert "private tail " * 40 not in str(card_mapping)
    assert card_call.kwargs["metadata"]["requester_user_id"] == "authorized-user"
    assert card_call.kwargs["metadata"]["non_conversational"] is True


@pytest.mark.asyncio
async def test_discord_voice_failure_sends_blocked_card_without_actions():
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="sent"))
    )
    runner = _discord_voice_runner(adapter)
    event, source = _discord_voice_event()

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": False, "error": "provider unavailable"},
    ):
        await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert adapter.send.await_count == 1
    card = OperatorCard.from_mapping(
        adapter.send.await_args.kwargs["metadata"]["operator_card"]
    )
    assert card.title == "Voice note blocked"
    assert card.actions == ()


@pytest.mark.asyncio
async def test_discord_attachment_card_uses_authenticated_delivery_once():
    adapter = SimpleNamespace(
        send=AsyncMock(return_value=SendResult(success=True, message_id="sent"))
    )
    runner = _discord_voice_runner(adapter)
    event, source = _discord_voice_event()
    event.text = "review this"
    event.message_type = MessageType.DOCUMENT
    event.media_urls = []
    event.media_types = []
    event.metadata = {
        "intake_cards": [
            build_attachment_intake_card(
                filename="agreement.pdf",
                mime_type="application/pdf",
                status="ready",
                source_ref="message-voice-1:attachment-1",
                size_bytes=2048,
                limit_bytes=1024 * 1024,
            ).to_mapping()
        ]
    }

    for _ in range(2):
        await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    adapter.send.assert_awaited_once()
    metadata = adapter.send.await_args.kwargs["metadata"]
    card = OperatorCard.from_mapping(metadata["operator_card"])
    assert card.title == "Attachment ready"
    assert metadata["requester_user_id"] == "authorized-user"
    assert metadata["non_conversational"] is True
