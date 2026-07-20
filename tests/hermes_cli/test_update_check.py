"""Tests for the update check mechanism in hermes_cli.banner."""

import json
import threading
import time
from unittest.mock import MagicMock, patch


def test_check_for_updates_uses_cache(tmp_path, monkeypatch):
    """A fresh cache should avoid any git subprocess."""
    from hermes_cli import __version__
    from hermes_cli.banner import check_for_updates

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cache_file = tmp_path / ".update_check"
    cache_file.write_text(
        json.dumps({"ts": time.time(), "behind": 3, "ver": __version__})
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with patch("hermes_cli.banner.subprocess.run") as mock_run:
        result = check_for_updates()

    assert result == 3
    mock_run.assert_not_called()


def test_official_ssh_origin_uses_https_fetch_and_exact_count(tmp_path):
    """Official SSH installs avoid SSH prompts and still count exactly."""
    import hermes_cli.banner as banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return MagicMock(
                returncode=0,
                stdout="git@github.com:NousResearch/hermes-agent.git\n",
            )
        if cmd == ["git", "rev-parse", "--is-shallow-repository"]:
            return MagicMock(returncode=0, stdout="false\n")
        if cmd == [
            "git",
            "fetch",
            "https://github.com/NousResearch/hermes-agent.git",
            "refs/heads/main",
            "--quiet",
        ]:
            return MagicMock(returncode=0, stdout="")
        if cmd == ["git", "rev-list", "--count", "HEAD..FETCH_HEAD"]:
            return MagicMock(returncode=0, stdout="42\n")
        raise AssertionError(f"unexpected git command: {cmd!r}")

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
        result = banner._check_via_local_git(repo_dir)

    assert result == 42
    assert ["git", "fetch", "origin", "main", "--quiet"] not in calls


def test_official_ssh_shallow_clone_keeps_presence_only_count(tmp_path):
    """Official SSH shallow clones preserve their depth boundary via HTTPS."""
    import hermes_cli.banner as banner

    repo_dir = tmp_path / "hermes-agent"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    def fake_run(cmd, **kwargs):
        if cmd == ["git", "remote", "get-url", "origin"]:
            return MagicMock(
                returncode=0,
                stdout="git@github.com:NousResearch/hermes-agent.git\n",
            )
        if cmd == ["git", "rev-parse", "--is-shallow-repository"]:
            return MagicMock(returncode=0, stdout="true\n")
        if cmd == [
            "git",
            "fetch",
            "https://github.com/NousResearch/hermes-agent.git",
            "refs/heads/main",
            "--depth",
            "1",
            "--quiet",
        ]:
            return MagicMock(returncode=0, stdout="")
        if cmd == ["git", "rev-parse", "HEAD"]:
            return MagicMock(returncode=0, stdout="local-sha\n")
        if cmd == ["git", "rev-parse", "FETCH_HEAD"]:
            return MagicMock(returncode=0, stdout="upstream-sha\n")
        raise AssertionError(f"unexpected git command: {cmd!r}")

    with patch("hermes_cli.banner.subprocess.run", side_effect=fake_run):
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
