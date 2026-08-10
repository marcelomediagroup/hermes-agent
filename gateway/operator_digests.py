"""Read-only operator digest projections for messaging command cards.

The projections intentionally consume bounded local read models: m-os cron
state, the Open Engine approval inbox/bridge state, and git history.  A source
failure is represented as ``Unavailable``; it is never coerced to a zero.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from gateway.operator_cards import OperatorCard


_MAX_JSON_BYTES = 1024 * 1024
_MAX_GIT_LINES = 6
_DEFAULT_HERMES_ROOT = Path(__file__).resolve().parent.parent
_LANE_EVALUATOR_RELATIVE_PATH = Path("scripts/health/oe_lane_state.py")
_LANE_MAX_AGE_SECONDS = 3600
_LANE_TRANSIENT_FAILSTREAK_MAX = 1


@dataclass(frozen=True, slots=True)
class DigestSourceConfig:
    """Filesystem sources for one operator digest projection."""

    m_os_root: Path | None = None
    state_root: Path | None = None
    hermes_root: Path = _DEFAULT_HERMES_ROOT
    since_hours: int = 24
    enabled: bool = False

    @classmethod
    def from_gateway_config(cls, gateway_config: Any) -> "DigestSourceConfig":
        """Resolve optional Discord ``extra.operator_digests`` overrides."""
        settings: Mapping[str, Any] = {}
        platforms = getattr(gateway_config, "platforms", {}) or {}
        for platform, platform_config in platforms.items():
            if getattr(platform, "value", platform) != "discord":
                continue
            extra = getattr(platform_config, "extra", {}) or {}
            candidate = extra.get("operator_digests", {})
            if isinstance(candidate, Mapping):
                settings = candidate
            break

        def _path(name: str, default: Path | None) -> Path | None:
            raw = settings.get(name)
            if not isinstance(raw, str) or not raw.strip():
                return default
            return Path(raw).expanduser()

        raw_hours = settings.get("since_hours", 24)
        try:
            since_hours = int(raw_hours)
        except (TypeError, ValueError):
            since_hours = 24
        since_hours = min(168, max(1, since_hours))
        return cls(
            m_os_root=_path("m_os_root", None),
            state_root=_path("state_root", None),
            hermes_root=_path("hermes_root", _DEFAULT_HERMES_ROOT)
            or _DEFAULT_HERMES_ROOT,
            since_hours=since_hours,
            enabled=settings.get("enabled") is True,
        )


@dataclass(frozen=True, slots=True)
class OpsJob:
    state_name: str
    label: str
    group: str
    default_interval_seconds: int


_OPS_JOBS = (
    OpsJob("open-engine-queue", "codex", "lane", 300),
    OpsJob("open-engine-queue-kimi", "kimi", "lane", 300),
    OpsJob("open-engine-queue-opus", "opus", "lane", 300),
    OpsJob("open-engine-feeder", "feeder", "service", 600),
    OpsJob("open-engine-discord-approvals", "approvals", "service", 60),
    OpsJob("hermes-system-health-check", "system health", "service", 1800),
)


@dataclass(frozen=True, slots=True)
class OpsSnapshot:
    lanes: str
    services: str
    summary: str
    severity: str
    problems: int
    warnings: int
    unavailable: int
    freshness: str


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    pending: int | None
    pending_identifiers: tuple[str, ...]
    resolved: str
    freshness: str
    severity: str
    unavailable: int


@dataclass(frozen=True, slots=True)
class GitProbe:
    available: bool
    count: int
    text: str
    truncated: bool


def _read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return None, "source exceeds its read bound"
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "source file is missing"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "source file is unreadable"


def _parse_timestamp(raw: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_text(timestamp: datetime, now: datetime) -> str:
    seconds = max(0, int((now - timestamp).total_seconds()))
    if seconds < 120:
        return f"{seconds}s old"
    if seconds < 7200:
        return f"{seconds // 60}m old"
    return f"{seconds // 3600}h old"


def _bounded(value: Any, limit: int = 1024) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _canonical_lane_verdict(
    config: DigestSourceConfig,
    *,
    state_path: Path,
    state_name: str,
    label: str,
) -> str | None:
    """Run m-os's canonical pure lane evaluator and return its verdict.

    The evaluator's stdout is intentionally discarded: the digest needs only
    the documented exit-code contract, and must not relay raw runtime payloads.
    ``None`` means the evaluator itself was unavailable or violated that
    contract, which callers render as ``Unavailable`` rather than guessing.
    """
    if config.m_os_root is None:
        return None
    evaluator = config.m_os_root / _LANE_EVALUATOR_RELATIVE_PATH
    if not evaluator.is_file():
        return None
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(evaluator),
                str(state_path),
                str(_LANE_MAX_AGE_SECONDS),
                str(_LANE_TRANSIENT_FAILSTREAK_MAX),
                label,
                state_name,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: "healthy", 2: "warning", 1: "failing"}.get(result.returncode)


def _card(
    *,
    title: str,
    severity: str,
    summary: str,
    fields: list[tuple[str, str]],
    now: datetime,
) -> OperatorCard:
    slug = "-".join(title.lower().split())[:48]
    state_ref = f"digest:{slug}:{now.astimezone(timezone.utc).strftime('%Y%m%d%H%M')}"
    return OperatorCard.from_mapping({
        "kind": "operator_card",
        "version": 1,
        "card_type": "digest" if severity != "critical" else "ops_alert",
        "title": _bounded(title, 120),
        "severity": severity,
        "summary": _bounded(summary, 500),
        "fields": [
            {"label": _bounded(label, 80), "value": _bounded(value)}
            for label, value in fields[:12]
        ],
        "actions": [],
        "links": [],
        "state_ref": state_ref,
    })


def collect_ops_snapshot(
    config: DigestSourceConfig,
    *,
    now: datetime | None = None,
) -> OpsSnapshot:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if config.m_os_root is None:
        manifest, manifest_error = None, "m-os root is not configured"
    else:
        manifest, manifest_error = _read_json(
            config.m_os_root / "infra/cron-runner/jobs.json"
        )
    manifest_jobs: dict[str, Mapping[str, Any]] = {}
    if isinstance(manifest, Mapping) and isinstance(manifest.get("jobs"), list):
        manifest_jobs = {
            str(job.get("name")): job
            for job in manifest["jobs"]
            if isinstance(job, Mapping) and job.get("name")
        }
    elif manifest_error is None:
        manifest_error = "jobs manifest is invalid"

    rendered: dict[str, list[str]] = {"lane": [], "service": []}
    problems = 0
    warnings = 0
    unavailable = 1 if manifest_error else 0
    observed_last_runs: list[datetime] = []
    for job in _OPS_JOBS:
        state_name = job.state_name
        label = job.label
        group = job.group
        manifest_job = manifest_jobs.get(state_name, {})
        if manifest_job.get("paused") is True:
            rendered[group].append(f"{label}: Paused")
            continue

        if config.state_root is None:
            state_path = Path(f"unconfigured/{state_name}.json")
            state, state_error = None, "state root is not configured"
        else:
            state_path = config.state_root / "cron" / f"{state_name}.json"
            state, state_error = _read_json(state_path)
        if state_error or not isinstance(state, Mapping):
            rendered[group].append(f"{label}: Unavailable")
            unavailable += 1
            continue

        last_run = _parse_timestamp(state.get("lastRun"))
        if last_run is None:
            rendered[group].append(f"{label}: Unavailable (invalid freshness)")
            unavailable += 1
            continue
        observed_last_runs.append(last_run)

        try:
            fail_streak = int(state.get("failStreak") or 0)
        except (TypeError, ValueError):
            rendered[group].append(f"{label}: Unavailable (invalid failure count)")
            unavailable += 1
            continue

        if group == "lane":
            verdict = _canonical_lane_verdict(
                config,
                state_path=state_path,
                state_name=state_name,
                label=label,
            )
            if verdict == "healthy":
                rendered[group].append(f"{label}: Healthy ({_age_text(last_run, now)})")
            elif verdict == "warning":
                warnings += 1
                rendered[group].append(f"{label}: Warning (transient failure streak)")
            elif verdict == "failing":
                problems += 1
                rendered[group].append(f"{label}: Failing (canonical lane evaluator)")
            else:
                unavailable += 1
                rendered[group].append(f"{label}: Unavailable (lane evaluator)")
            continue

        schedule = manifest_job.get("schedule", {})
        interval = (
            schedule.get("startInterval") if isinstance(schedule, Mapping) else None
        )
        try:
            interval_seconds = int(interval)
        except (TypeError, ValueError):
            interval_seconds = job.default_interval_seconds
        stale_after = max(900, interval_seconds * 3)
        age_seconds = max(0, int((now - last_run).total_seconds()))
        healthy = (
            state.get("status") == "ok"
            and state.get("exitCode") in (0, "0")
            and fail_streak == 0
            and age_seconds <= stale_after
        )
        if healthy:
            rendered[group].append(f"{label}: Healthy ({_age_text(last_run, now)})")
        else:
            problems += 1
            if age_seconds > stale_after:
                reason = "stale"
            elif fail_streak:
                reason = f"failure streak={fail_streak}"
            elif state.get("exitCode") not in (0, "0"):
                reason = f"exit={state.get('exitCode')}"
            else:
                reason = f"status={state.get('status') or 'unknown'}"
            rendered[group].append(f"{label}: Failing ({reason})")

    if problems:
        severity = "critical"
    elif warnings or unavailable:
        severity = "needs_review"
    else:
        severity = "done"
    summary_parts = [f"{problems} operational exception{'s' if problems != 1 else ''}"]
    if warnings:
        summary_parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
    if unavailable:
        summary_parts.append(
            f"{unavailable} unavailable source{'s' if unavailable != 1 else ''}"
        )
    else:
        summary_parts.append("all configured sources readable")
    freshness = (
        f"oldest active cron state {_age_text(min(observed_last_runs), now)}"
        if observed_last_runs
        else "Unavailable"
    )
    return OpsSnapshot(
        lanes=" · ".join(rendered["lane"]),
        services=" · ".join(rendered["service"]),
        summary="; ".join(summary_parts),
        severity=severity,
        problems=problems,
        warnings=warnings,
        unavailable=unavailable,
        freshness=freshness,
    )


def build_ops_card(
    config: DigestSourceConfig,
    *,
    now: datetime | None = None,
) -> OperatorCard:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    snapshot = collect_ops_snapshot(config, now=now)
    return _card(
        title="Operations digest",
        severity=snapshot.severity,
        summary=snapshot.summary,
        fields=[
            ("Open Engine lanes", snapshot.lanes),
            ("Runtime services", snapshot.services),
            ("Data freshness", snapshot.freshness),
            (
                "Source",
                "m-os oe_lane_state.py evaluator + cron-runner state + infra/cron-runner/jobs.json",
            ),
        ],
        now=now,
    )


def collect_decision_snapshot(
    config: DigestSourceConfig,
    *,
    now: datetime | None = None,
) -> DecisionSnapshot:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if config.state_root is None:
        return DecisionSnapshot(
            pending=None,
            pending_identifiers=(),
            resolved="Unavailable (state root is not configured)",
            freshness="Unavailable",
            severity="blocked",
            unavailable=1,
        )
    inbox, inbox_error = _read_json(
        config.state_root / "hermes/discord-approval-inbox.json"
    )
    if (
        inbox_error
        or not isinstance(inbox, Mapping)
        or not isinstance(inbox.get("approvals"), list)
    ):
        return DecisionSnapshot(
            pending=None,
            pending_identifiers=(),
            resolved="Unavailable (approval inbox read failed)",
            freshness="Unavailable",
            severity="blocked",
            unavailable=1,
        )

    approvals = [item for item in inbox["approvals"] if isinstance(item, Mapping)]
    pending_ids = {
        str(item.get("id"))
        for item in approvals
        if item.get("status") == "pending" and item.get("id")
    }
    status_counts: dict[str, int] = {}
    for item in approvals:
        status = str(item.get("status") or "unknown")
        if status == "pending":
            continue
        status_counts[status] = status_counts.get(status, 0) + 1

    bridge, bridge_error = _read_json(
        config.state_root / "hermes/open-engine-discord-approvals.json"
    )
    identifiers: list[str] = []
    freshness = "Unavailable"
    bridge_mappings = bridge.get("mappings") if isinstance(bridge, Mapping) else None
    bridge_last_run = (
        _parse_timestamp(bridge.get("lastRunAt"))
        if isinstance(bridge, Mapping)
        else None
    )
    bridge_available = (
        bridge_error is None
        and isinstance(bridge_mappings, Mapping)
        and bridge_last_run is not None
    )
    if bridge_available:
        identifiers = sorted(
            str(identifier)
            for identifier, mapping in bridge_mappings.items()
            if isinstance(mapping, Mapping)
            and str(mapping.get("approvalId") or "") in pending_ids
            and str(identifier).startswith("OE-")
        )[:8]
        freshness = _age_text(bridge_last_run, now)

    resolved = (
        " · ".join(
            f"{count} {status.replace('_', ' ')}"
            for status, count in sorted(status_counts.items())
        )
        or "0 resolved"
    )
    if not bridge_available:
        resolved += " · bridge state Unavailable"
    return DecisionSnapshot(
        pending=len(pending_ids),
        pending_identifiers=tuple(identifiers),
        resolved=resolved,
        freshness=freshness,
        severity="needs_review" if pending_ids or not bridge_available else "done",
        unavailable=0 if bridge_available else 1,
    )


def build_decisions_card(
    config: DigestSourceConfig,
    *,
    now: datetime | None = None,
) -> OperatorCard:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    snapshot = collect_decision_snapshot(config, now=now)
    if snapshot.pending is None:
        pending_text = "Unavailable (approval inbox read failed)"
        summary = "The human-decision source could not be read."
    else:
        identifiers = " · ".join(snapshot.pending_identifiers)
        pending_text = str(snapshot.pending)
        if identifiers:
            pending_text += f" · {identifiers}"
        summary = f"{snapshot.pending} pending human decision{'s' if snapshot.pending != 1 else ''}."
    return _card(
        title="Decisions digest",
        severity=snapshot.severity,
        summary=summary,
        fields=[
            ("Pending decisions", pending_text),
            ("Resolved state", snapshot.resolved),
            ("Freshness", snapshot.freshness),
            ("Source", "Open Engine approval inbox + bridge state (read-only)"),
        ],
        now=now,
    )


def _git_recent(
    root: Path | None,
    *,
    since_hours: int,
    pathspec: str | None = None,
) -> GitProbe:
    git = shutil.which("git")
    if not git or root is None or not root.is_dir():
        return GitProbe(False, 0, "Unavailable (git source missing)", False)
    command = [
        git,
        "-C",
        str(root),
        "log",
        f"--since={since_hours} hours ago",
        f"--max-count={_MAX_GIT_LINES + 1}",
        "--format=%h %s",
    ]
    if pathspec:
        command.extend(["--", pathspec])
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return GitProbe(False, 0, "Unavailable (git read failed)", False)
    if result.returncode != 0:
        return GitProbe(False, 0, "Unavailable (git read failed)", False)
    lines = [_bounded(line, 180) for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return GitProbe(True, 0, "0 changes", False)
    truncated = len(lines) > _MAX_GIT_LINES
    shown = lines[:_MAX_GIT_LINES]
    text = " · ".join(shown)
    if truncated:
        text += " · … additional changes not shown"
    return GitProbe(True, len(shown), text, truncated)


def _git_probe_count_text(probe: GitProbe) -> str:
    if not probe.available:
        return "Unavailable"
    suffix = "+" if probe.truncated else ""
    return f"{probe.count}{suffix} recent entries"


def collect_change_probes(
    config: DigestSourceConfig,
) -> tuple[GitProbe, GitProbe, GitProbe]:
    return (
        _git_recent(config.hermes_root, since_hours=config.since_hours),
        _git_recent(config.m_os_root, since_hours=config.since_hours),
        _git_recent(
            config.m_os_root,
            since_hours=config.since_hours,
            pathspec="docs/proofs",
        ),
    )


def build_changes_card(
    config: DigestSourceConfig,
    *,
    now: datetime | None = None,
) -> OperatorCard:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    hermes, m_os, receipts = collect_change_probes(config)
    ops = collect_ops_snapshot(config, now=now)
    decisions = collect_decision_snapshot(config, now=now)
    available = sum(probe.available for probe in (hermes, m_os, receipts))
    if not available:
        severity = "blocked"
    elif ops.problems or ops.warnings or ops.unavailable or decisions.unavailable:
        severity = "needs_review"
    else:
        severity = "info"
    return _card(
        title="Changes digest",
        severity=severity,
        summary=(
            f"Read-only repository and proof changes from the last "
            f"{config.since_hours} hours, with current cron/freshness context; "
            f"{available}/3 git sources available."
        ),
        fields=[
            ("Hermes changes", hermes.text),
            ("m-os changes", m_os.text),
            (
                "Agent completion changes",
                "Unavailable (no bounded agent-completion history read model configured)",
            ),
            ("Completion receipt changes", receipts.text),
            (
                "Cron failure changes",
                "Unavailable (no bounded cron-failure history read model configured); "
                f"current state: failures {ops.problems} · warnings {ops.warnings} · "
                f"Unavailable sources {ops.unavailable}",
            ),
            (
                "Data freshness changes",
                "Unavailable (no prior digest freshness checkpoint configured); "
                f"current state: approval bridge {decisions.freshness} · {ops.freshness}",
            ),
            (
                "Source",
                "Hermes/m-os git history + m-os docs/proofs history + current canonical cron/approval state",
            ),
        ],
        now=now,
    )


def build_today_card(
    config: DigestSourceConfig,
    *,
    active_agents: int,
    process_count: int | None,
    process_source_available: bool,
    now: datetime | None = None,
) -> OperatorCard:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    decisions = collect_decision_snapshot(config, now=now)
    ops = collect_ops_snapshot(config, now=now)
    hermes, m_os, _receipts = collect_change_probes(config)

    if process_source_available and process_count is not None:
        active_work = f"{active_agents} agents · {process_count} background processes"
    else:
        active_work = f"{active_agents} agents · background processes Unavailable"
    decision_text = (
        f"{decisions.pending} pending"
        if decisions.pending is not None
        else "Unavailable (approval inbox read failed)"
    )
    changes_text = (
        f"Hermes {_git_probe_count_text(hermes)} · m-os {_git_probe_count_text(m_os)}"
    )
    if ops.problems or ops.warnings or (decisions.pending or 0):
        severity = "needs_review"
    elif ops.unavailable or decisions.unavailable or decisions.pending is None:
        severity = "needs_review"
    else:
        severity = "done"
    return _card(
        title="Today command panel",
        severity=severity,
        summary="Daily read-only command panel from current runtime and bounded MMG read models.",
        fields=[
            ("Active work", active_work),
            ("Human decisions", decision_text),
            ("Operational exceptions", ops.summary),
            ("Repository changes", changes_text),
            (
                "Urgent deadlines",
                "Unavailable (no bounded deadline read model configured)",
            ),
            (
                "Source",
                "Hermes runtime/process registry + Open Engine approval state + m-os cron state + git history",
            ),
        ],
        now=now,
    )


def ops_rollup_text(config: DigestSourceConfig) -> str:
    """Compact source-labelled Open Engine health for the /agents card."""
    snapshot = collect_ops_snapshot(config)
    return f"{snapshot.summary} (source: m-os oe_lane_state.py + cron-runner state)"
