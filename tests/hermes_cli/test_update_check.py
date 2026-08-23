"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import threading
import time
from unittest.mock import MagicMock, patch


def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """A fresh cache for the current source revision avoids network work."""
    from hermes_cli import __version__
    import hermes_cli.banner as banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(
        json.dumps({
            "ts": time.time(),
            "behind": 3,
            "ver": __version__,
            "local_rev": "a" * 40,
        })
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with (
        patch.object(banner, "__file__", str(repo_dir / "hermes_cli" / "banner.py")),
        patch.object(banner, "_git_stdout", return_value="a" * 40) as mock_git,
        patch.object(banner, "_check_via_local_git") as mock_check,
    ):
        result = banner.check_for_updates()

    assert result == 3
    mock_git.assert_called_once_with(["rev-parse", "HEAD"], cwd=repo_dir)
    mock_check.assert_not_called()


def test_check_for_updates_invalidates_cache_when_source_revision_changes(tmp_path, monkeypatch):
    """A rebase at the same package version must not reuse stale update status."""
    from hermes_cli import __version__
    import hermes_cli.banner as banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    cache_file = tmp_path / ".update_check"
    cache_file.write_text(json.dumps({
        "ts": time.time(),
        "behind": -1,
        "ver": __version__,
        "local_rev": "a" * 40,
    }))

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with (
        patch.object(banner, "__file__", str(repo_dir / "hermes_cli" / "banner.py")),
        patch.object(banner, "_git_stdout", return_value="b" * 40),
        patch.object(banner, "_check_via_local_git", return_value=0) as mock_check,
    ):
        result = banner.check_for_updates()

    assert result == 0
    mock_check.assert_called_once_with(repo_dir)
    assert json.loads(cache_file.read_text())["local_rev"] == "b" * 40


def test_official_ssh_origin_uses_https_fetch_and_exact_count(tmp_path):
    """Official SSH installs avoid SSH prompts and still count exactly."""
    import hermes_cli.banner as banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    def fake_git_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "git@github.com:NousResearch/hermes-agent.git"
        if args == ["rev-parse", "HEAD"]:
            return "b" * 40
        raise AssertionError(f"unexpected git command: {args!r}")

    with (
        patch.object(banner, "_git_stdout", side_effect=fake_git_stdout),
        patch.object(banner, "_upstream_main_sha", return_value="a" * 40),
        patch.object(banner.subprocess, "run", return_value=MagicMock(returncode=1)),
        patch.object(banner, "_github_compare_behind", return_value=42),
    ):
        result = banner._check_via_local_git(repo_dir)

    assert result == 42


def test_official_ssh_shallow_clone_keeps_presence_only_count(tmp_path):
    """Official SSH checks keep an honest sentinel when counting is unavailable."""
    import hermes_cli.banner as banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    def fake_git_stdout(args, *, cwd, timeout=5):
        if args == ["remote", "get-url", "origin"]:
            return "git@github.com:NousResearch/hermes-agent.git"
        if args == ["rev-parse", "HEAD"]:
            return "b" * 40
        raise AssertionError(f"unexpected git command: {args!r}")

    with (
        patch.object(banner, "_git_stdout", side_effect=fake_git_stdout),
        patch.object(banner, "_upstream_main_sha", return_value="a" * 40),
        patch.object(banner.subprocess, "run", return_value=MagicMock(returncode=1)),
        patch.object(banner, "_github_compare_behind", return_value=None),
    ):
        result = banner._check_via_local_git(repo_dir)

    assert result == banner.UPDATE_AVAILABLE_NO_COUNT


def test_print_version_info_handles_unknown_update_count(monkeypatch, capsys):
    """The version command should surface presence-only updates honestly."""
    import hermes_cli.banner as banner
    import hermes_cli.config as config
    import hermes_cli.main as main_mod

    monkeypatch.setattr(
        banner,
        "check_for_updates",
        lambda: banner.UPDATE_AVAILABLE_NO_COUNT,
    )
    monkeypatch.setattr(
        config,
        "recommended_update_command",
        lambda: "hermes update",
    )

    main_mod._print_version_info(check_updates=True)
    output = capsys.readouterr().out
    assert "Update available — run 'hermes update'" in output


def test_prefetch_non_blocking():
    """prefetch_update_check() should return immediately without blocking."""
    import hermes_cli.banner as banner

    banner._update_result = None
    banner._update_check_done = threading.Event()

    with patch.object(banner, "check_for_updates", return_value=5):
        start = time.monotonic()
        banner.prefetch_update_check()
        elapsed = time.monotonic() - start

        assert elapsed < 1.0
        banner._update_check_done.wait(timeout=5)
        assert banner._update_result == 5
