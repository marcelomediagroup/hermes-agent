"""Private, deterministic operator cards for inbound media intake.

The builders in this module never inspect or persist local media bytes. They
accept only bounded display metadata (plus a voice transcript for an extractive
summary), produce validated :class:`OperatorCard` values, and keep every
suggested action advisory. Discord's authenticated operator-action dispatcher
records a click as intent; it does not execute an email, post, payment, contract
approval, deployment, or Linear mutation.
"""

from __future__ import annotations

import hashlib
import os
import re

from gateway.operator_cards import OperatorCard


_ATTACHMENT_STATUSES = frozenset(
    {"ready", "oversized", "unreadable", "download_failed"}
)
_CONTRACT_EXTENSIONS = frozenset({".doc", ".docx", ".odt", ".pdf", ".rtf"})
_SPREADSHEET_EXTENSIONS = frozenset({".csv", ".ods", ".tsv", ".xls", ".xlsx"})

_ACTION_LABELS = {
    "summarize": "Summarize",
    "extract_tasks": "Extract tasks",
    "review_contract": "Review contract",
    "turn_into_content_brief": "Content brief",
    "create_oe_task": "Prepare issue",
    "capture_decisions": "Capture decisions",
    "prepare_memory": "Prepare memory",
}


def _bounded_display_text(value: str, *, limit: int, fallback: str) -> str:
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return fallback
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 1)].rstrip() + "…"


def _compact_summary(transcript: str, *, limit: int = 320) -> str:
    normalized = _bounded_display_text(
        transcript,
        limit=max(limit * 4, limit),
        fallback="The voice note contained no readable words.",
    )
    if len(normalized) <= limit:
        return normalized

    prefix = normalized[:limit]
    sentence_end = max(prefix.rfind(". "), prefix.rfind("? "), prefix.rfind("! "))
    if sentence_end >= max(40, limit // 3):
        return prefix[: sentence_end + 1].rstrip()

    word_end = prefix.rfind(" ")
    clipped = prefix[:word_end] if word_end >= max(20, limit // 3) else prefix
    return clipped.rstrip(" ,;:-") + "…"


def _opaque_state_ref(*, kind: str, status: str, source_ref: str, content_key: str) -> str:
    digest = hashlib.sha256(
        f"{kind}\0{status}\0{source_ref}\0{content_key}".encode(
            "utf-8", "surrogatepass"
        )
    ).hexdigest()[:32]
    return f"intake:{kind}:{status}:{digest}"


def _actions(*action_ids: str) -> list[dict[str, str]]:
    return [
        {
            "id": action_id,
            "label": _ACTION_LABELS[action_id],
            "style": "secondary",
        }
        for action_id in action_ids
    ]


def _human_bytes(value: int | None) -> str:
    if value is None:
        return "Unknown"
    try:
        size = max(0, int(value))
    except (TypeError, ValueError):
        return "Unknown"
    for unit, divisor in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= divisor:
            amount = size / divisor
            rendered = f"{amount:.1f}".rstrip("0").rstrip(".")
            return f"{rendered} {unit}"
    return f"{size} bytes"


def build_voice_note_intake_card(
    transcript: str,
    *,
    source_ref: str,
    ordinal: int = 0,
) -> OperatorCard:
    """Build an advisory card for one successfully transcribed voice note.

    The full transcript continues to use the existing private chat echo. The
    card contains only a bounded extractive summary, so the durable action
    registration never duplicates the full transcript into local state.
    """
    summary = _compact_summary(transcript)
    state_ref = _opaque_state_ref(
        kind="voice",
        status="ready",
        source_ref=f"{source_ref}:{ordinal}",
        content_key=hashlib.sha256(
            str(transcript or "").encode("utf-8", "surrogatepass")
        ).hexdigest(),
    )
    return OperatorCard.from_mapping(
        {
            "kind": "operator_card",
            "version": 1,
            "card_type": "capture",
            "title": "Voice note ready",
            "severity": "needs_review",
            "summary": summary,
            "fields": [
                {
                    "label": "Suggested actions",
                    "value": "Extract tasks, capture decisions, or prepare a memory candidate.",
                },
                {
                    "label": "Safety",
                    "value": (
                        "Buttons record an authenticated intent only; no external "
                        "action is executed automatically."
                    ),
                },
            ],
            "actions": _actions(
                "extract_tasks",
                "capture_decisions",
                "prepare_memory",
            ),
            "links": [],
            "state_ref": state_ref,
        }
    )


def build_voice_note_blocked_card(
    *,
    source_ref: str,
    ordinal: int = 0,
) -> OperatorCard:
    """Build a content-free recovery card for a failed voice transcription."""
    return OperatorCard.from_mapping(
        {
            "kind": "operator_card",
            "version": 1,
            "card_type": "capture",
            "title": "Voice note blocked",
            "severity": "blocked",
            "summary": "Hermes could not prepare a readable private voice-note intake.",
            "fields": [
                {
                    "label": "Reason",
                    "value": "Speech transcription did not produce readable words.",
                },
                {
                    "label": "Recovery",
                    "value": "Resend the voice note, or type the request if the retry fails.",
                },
                {
                    "label": "Safety",
                    "value": (
                        "No external action ran and the raw original was not moved "
                        "or altered."
                    ),
                },
            ],
            "actions": [],
            "links": [],
            "state_ref": _opaque_state_ref(
                kind="voice",
                status="blocked",
                source_ref=f"{source_ref}:{ordinal}",
                content_key="transcription_failed",
            ),
        }
    )


def _attachment_action_ids(filename: str, mime_type: str) -> tuple[str, ...]:
    extension = os.path.splitext(filename)[1].lower()
    normalized_mime = (mime_type or "").split(";", 1)[0].strip().lower()
    base = ("summarize", "extract_tasks")
    if extension in _CONTRACT_EXTENSIONS:
        return (*base, "review_contract", "create_oe_task")
    if normalized_mime.startswith("image/") or normalized_mime.startswith("video/"):
        return (*base, "turn_into_content_brief", "create_oe_task")
    if extension in _SPREADSHEET_EXTENSIONS:
        return (*base, "create_oe_task")
    return (*base, "create_oe_task")


def build_attachment_intake_card(
    *,
    filename: str,
    mime_type: str,
    status: str,
    source_ref: str,
    size_bytes: int | None = None,
    limit_bytes: int | None = None,
) -> OperatorCard:
    """Build a ready or blocked card without reading attachment contents."""
    if status not in _ATTACHMENT_STATUSES:
        raise ValueError(f"unsupported attachment intake status: {status}")

    display_name = _bounded_display_text(
        filename,
        limit=120,
        fallback="attachment",
    )
    display_type = _bounded_display_text(
        mime_type,
        limit=120,
        fallback="application/octet-stream",
    )
    state_ref = _opaque_state_ref(
        kind="attachment",
        status=status,
        source_ref=source_ref,
        content_key=f"{display_name}\0{display_type}\0{size_bytes}",
    )
    safety = (
        "Buttons record an authenticated intent only; no email, post, payment, "
        "contract approval, deployment, or Linear issue is executed automatically."
    )

    if status == "ready":
        action_ids = _attachment_action_ids(display_name, display_type)
        return OperatorCard.from_mapping(
            {
                "kind": "operator_card",
                "version": 1,
                "card_type": "capture",
                "title": "Attachment ready",
                "severity": "info",
                "summary": (
                    "The attachment was cached privately without moving or altering "
                    "the original Discord media."
                ),
                "fields": [
                    {"label": "File", "value": display_name},
                    {"label": "Type", "value": display_type},
                    {"label": "Size", "value": _human_bytes(size_bytes)},
                    {
                        "label": "Suggested actions",
                        "value": ", ".join(
                            _ACTION_LABELS[action_id] for action_id in action_ids
                        ),
                    },
                    {"label": "Safety", "value": safety},
                ],
                "actions": _actions(*action_ids),
                "links": [],
                "state_ref": state_ref,
            }
        )

    reason = {
        "oversized": "The attachment exceeds the configured limit and was not downloaded.",
        "unreadable": "The attachment could not be read safely in its declared format.",
        "download_failed": "The attachment could not be downloaded through the authenticated media path.",
    }[status]
    recovery = {
        "oversized": "Resend a smaller file or share a safe link with the requested task.",
        "unreadable": "Re-export the file in a readable format and resend it.",
        "download_failed": "Retry the upload, then resend the message if Discord still reports a failure.",
    }[status]
    limit_value = "No configured size cap"
    if limit_bytes is not None and int(limit_bytes) > 0:
        limit_value = _human_bytes(limit_bytes)

    return OperatorCard.from_mapping(
        {
            "kind": "operator_card",
            "version": 1,
            "card_type": "capture",
            "title": "Attachment blocked",
            "severity": "blocked",
            "summary": "Hermes did not expose the media to the agent or any external system.",
            "fields": [
                {"label": "File", "value": display_name},
                {"label": "Type", "value": display_type},
                {"label": "Reason", "value": reason},
                {"label": "Limit", "value": limit_value},
                {"label": "Recovery", "value": recovery},
                {
                    "label": "Safety",
                    "value": "No external action ran and the raw original was not moved or altered.",
                },
            ],
            "actions": [],
            "links": [],
            "state_ref": state_ref,
        }
    )


__all__ = [
    "build_attachment_intake_card",
    "build_voice_note_blocked_card",
    "build_voice_note_intake_card",
]
