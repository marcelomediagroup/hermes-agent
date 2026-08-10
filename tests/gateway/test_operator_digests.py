"""Behavior tests for the read-only MMG operator digest projections."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from gateway.operator_digests import (
    DigestSourceConfig,
    build_changes_card,
    build_decisions_card,
    build_ops_card,
    build_today_card,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _init_repo(path: Path, *, subject: str, proof: bool = False) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Digest Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "digest@example.test"],
        check=True,
    )
    target = path / ("docs/proofs/receipt.md" if proof else "change.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("evidence\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", str(target.relative_to(path))], check=True
    )
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", subject], check=True)


def _commit_change(path: Path, *, subject: str, sequence: int) -> None:
    target = path / f"change-{sequence}.txt"
    target.write_text(f"evidence {sequence}\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", str(target.relative_to(path))], check=True
    )
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", subject], check=True)


def _source_config(tmp_path: Path) -> DigestSourceConfig:
    m_os_root = tmp_path / "m-os"
    hermes_root = tmp_path / "hermes-agent"
    state_root = tmp_path / "state" / "workspace"
    _init_repo(m_os_root, subject="feat(ops): finish OE-500", proof=True)
    _init_repo(hermes_root, subject="fix(discord): improve digest cards")
    evaluator = m_os_root / "scripts/health/oe_lane_state.py"
    evaluator.parent.mkdir(parents=True)
    evaluator.write_text(
        """import json
import sys
state = json.loads(open(sys.argv[1], encoding="utf-8").read())
fail_streak = int(state.get("failStreak") or 0)
if fail_streak > 1 or state.get("status") != "ok" or state.get("exitCode") not in (0, "0"):
    raise SystemExit(1)
raise SystemExit(2 if fail_streak else 0)
""",
        encoding="utf-8",
    )

    jobs = [
        {
            "name": "open-engine-queue",
            "paused": False,
            "schedule": {"startInterval": 300},
        },
        {
            "name": "open-engine-queue-kimi",
            "paused": True,
            "schedule": {"startInterval": 300},
        },
        {
            "name": "open-engine-queue-opus",
            "paused": True,
            "schedule": {"startInterval": 300},
        },
        {
            "name": "open-engine-feeder",
            "paused": False,
            "schedule": {"startInterval": 600},
        },
        {
            "name": "open-engine-discord-approvals",
            "paused": False,
            "schedule": {"startInterval": 60},
        },
        {
            "name": "hermes-system-health-check",
            "paused": False,
            "schedule": {"startInterval": 1800},
        },
    ]
    _write_json(m_os_root / "infra/cron-runner/jobs.json", {"version": 1, "jobs": jobs})

    healthy = {
        "lastRun": "2026-08-10T18:30:00Z",
        "status": "ok",
        "exitCode": 0,
        "failStreak": 0,
        "durationSec": 2,
    }
    for name in (
        "open-engine-queue",
        "open-engine-queue-kimi",
        "open-engine-queue-opus",
        "open-engine-feeder",
        "open-engine-discord-approvals",
        "hermes-system-health-check",
    ):
        _write_json(state_root / "cron" / f"{name}.json", healthy)

    _write_json(
        state_root / "hermes/discord-approval-inbox.json",
        {
            "version": 1,
            "approvals": [
                {"id": "approval-pending", "status": "pending"},
                {"id": "approval-approved", "status": "approved"},
            ],
        },
    )
    _write_json(
        state_root / "hermes/open-engine-discord-approvals.json",
        {
            "version": 1,
            "actionAuditCursor": 0,
            "scopedActionEventIds": [],
            "events": [],
            "lastRunAt": "2026-08-10T18:31:00Z",
            "mappings": {
                "OE-500": {"approvalId": "approval-pending", "status": "pending"},
                "OE-499": {"approvalId": "approval-approved", "status": "approved"},
            },
        },
    )
    return DigestSourceConfig(
        m_os_root=m_os_root,
        state_root=state_root,
        hermes_root=hermes_root,
        since_hours=24,
    )


def _field(card, label: str) -> str:
    return next(field.value for field in card.fields if field.label == label)


def test_source_config_requires_explicit_enablement_and_external_paths(tmp_path):
    disabled = DigestSourceConfig.from_gateway_config(SimpleNamespace(platforms={}))
    assert disabled.enabled is False
    assert disabled.m_os_root is None
    assert disabled.state_root is None

    enabled = DigestSourceConfig.from_gateway_config(
        SimpleNamespace(
            platforms={
                "discord": SimpleNamespace(
                    extra={
                        "operator_digests": {
                            "enabled": True,
                            "m_os_root": str(tmp_path / "m-os"),
                            "state_root": str(tmp_path / "state"),
                        }
                    }
                )
            }
        )
    )
    assert enabled.enabled is True
    assert enabled.m_os_root == tmp_path / "m-os"
    assert enabled.state_root == tmp_path / "state"


def test_ops_card_uses_cron_state_and_treats_paused_lanes_as_real_state(tmp_path):
    config = _source_config(tmp_path)
    card = build_ops_card(
        config,
        now=datetime(2026, 8, 10, 18, 35, tzinfo=timezone.utc),
    )

    assert card.severity == "done"
    assert "codex: Healthy" in _field(card, "Open Engine lanes")
    assert "kimi: Paused" in _field(card, "Open Engine lanes")
    assert "opus: Paused" in _field(card, "Open Engine lanes")
    assert "oe_lane_state.py" in _field(card, "Source")


def test_ops_card_preserves_canonical_transient_lane_warning(tmp_path):
    config = _source_config(tmp_path)
    state_path = config.state_root / "cron/open-engine-queue.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["failStreak"] = 1
    _write_json(state_path, state)

    card = build_ops_card(
        config,
        now=datetime(2026, 8, 10, 18, 35, tzinfo=timezone.utc),
    )

    assert card.severity == "needs_review"
    assert "codex: Warning (transient failure streak)" in _field(
        card,
        "Open Engine lanes",
    )


def test_ops_card_marks_failed_source_unavailable_instead_of_zero(tmp_path):
    config = _source_config(tmp_path)
    (config.state_root / "cron/open-engine-queue.json").unlink()

    card = build_ops_card(
        config,
        now=datetime(2026, 8, 10, 18, 35, tzinfo=timezone.utc),
    )

    assert card.severity == "needs_review"
    assert "codex: Unavailable" in _field(card, "Open Engine lanes")
    assert "0 failures" not in card.summary


def test_ops_card_marks_malformed_failure_count_unavailable(tmp_path):
    config = _source_config(tmp_path)
    state_path = config.state_root / "cron/open-engine-queue.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["failStreak"] = "not-a-count"
    _write_json(state_path, state)

    card = build_ops_card(
        config,
        now=datetime(2026, 8, 10, 18, 35, tzinfo=timezone.utc),
    )

    assert card.severity == "needs_review"
    assert "codex: Unavailable (invalid failure count)" in _field(
        card,
        "Open Engine lanes",
    )


def test_decisions_card_counts_only_bounded_state_and_lists_issue_ids(tmp_path):
    config = _source_config(tmp_path)

    card = build_decisions_card(
        config,
        now=datetime(2026, 8, 10, 18, 35, tzinfo=timezone.utc),
    )

    assert card.severity == "needs_review"
    assert _field(card, "Pending decisions") == "1 · OE-500"
    assert "1 approved" in _field(card, "Resolved state")
    assert "approval inbox + bridge state" in _field(card, "Source")


def test_decisions_card_reports_unavailable_when_inbox_cannot_be_read(tmp_path):
    config = _source_config(tmp_path)
    (config.state_root / "hermes/discord-approval-inbox.json").unlink()

    card = build_decisions_card(config)

    assert card.severity == "blocked"
    assert "Unavailable" in _field(card, "Pending decisions")


def test_decisions_card_reports_malformed_bridge_as_unavailable(tmp_path):
    config = _source_config(tmp_path)
    _write_json(
        config.state_root / "hermes/open-engine-discord-approvals.json",
        {"lastRunAt": "not-a-timestamp", "mappings": []},
    )

    card = build_decisions_card(config)

    assert card.severity == "needs_review"
    assert "bridge state Unavailable" in _field(card, "Resolved state")
    assert _field(card, "Freshness") == "Unavailable"


def test_changes_card_reads_both_git_histories_and_completion_receipts(tmp_path):
    config = _source_config(tmp_path)

    card = build_changes_card(
        config,
        now=datetime(2026, 8, 10, 18, 35, tzinfo=timezone.utc),
    )

    assert "improve digest cards" in _field(card, "Hermes changes")
    assert "finish OE-500" in _field(card, "m-os changes")
    assert _field(card, "Agent completion changes").startswith("Unavailable")
    assert "finish OE-500" in _field(card, "Completion receipt changes")
    assert _field(card, "Cron failure changes").startswith("Unavailable")
    assert "current state: failures 0" in _field(card, "Cron failure changes")
    assert _field(card, "Data freshness changes").startswith("Unavailable")
    assert "current state: approval bridge" in _field(card, "Data freshness changes")
    assert "git history" in _field(card, "Source")


def test_today_card_distinguishes_real_counts_from_unavailable_deadlines(tmp_path):
    config = _source_config(tmp_path)

    card = build_today_card(
        config,
        active_agents=2,
        process_count=0,
        process_source_available=True,
        now=datetime(2026, 8, 10, 18, 35, tzinfo=timezone.utc),
    )

    assert _field(card, "Active work") == "2 agents · 0 background processes"
    assert _field(card, "Human decisions") == "1 pending"
    assert _field(card, "Urgent deadlines").startswith("Unavailable")
    assert "0 background processes" in _field(card, "Active work")


def test_today_card_marks_bounded_git_count_as_truncated(tmp_path):
    config = _source_config(tmp_path)
    for sequence in range(7):
        _commit_change(
            config.hermes_root,
            subject=f"fix(discord): follow-up {sequence}",
            sequence=sequence,
        )

    card = build_today_card(
        config,
        active_agents=0,
        process_count=0,
        process_source_available=True,
        now=datetime(2026, 8, 10, 18, 35, tzinfo=timezone.utc),
    )

    assert "Hermes 6+ recent entries" in _field(card, "Repository changes")
