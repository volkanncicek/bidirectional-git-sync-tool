"""Tests for git_relay.

These run against real git repositories in a tmp_path rather than against mocks.
The behaviour under test is git's, not ours -- `receive.denyCurrentBranch=updateInstead`
engaging (or not) is the whole safety model, and a mock would only assert that we
believe what the docs say. Every guard here exists because the failure it prevents was
reproduced first.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import git_relay  # noqa: E402

# Captured before the autouse fixture below stubs it out, so the check can still be
# tested on its own.
_REAL_UPDATE_INSTEAD_CHECK = git_relay._check_update_instead


# --------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------


def run(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": __import__("os").environ["PATH"],
            "HOME": str(repo),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )
    assert proc.returncode == 0, f"git {' '.join(args)}\n{proc.stderr}"
    return proc.stdout.strip()


def commit(repo: Path, name: str, text: str = "x") -> None:
    (repo / name).write_text(text, encoding="utf-8")
    run(repo, "add", name)
    run(repo, "commit", "-m", f"add {name}")


@pytest.fixture
def world(tmp_path: Path):
    """origin (bare) <- workstation clone, plus a `host` clone standing in for the
    isolated machine. Local paths stand in for SSH URLs: git treats them identically
    for push/fetch/ls-remote, and the SSH layer is not what these tests are about."""
    origin, seed = tmp_path / "origin", tmp_path / "seed"
    run(tmp_path, "init", "--bare", "-b", "master", str(origin))
    seed.mkdir()
    run(seed, "init", "-b", "master")
    commit(seed, "a.txt")
    run(seed, "remote", "add", "origin", str(origin))
    run(seed, "push", "origin", "master")

    host, ws = tmp_path / "host", tmp_path / "ws"
    run(tmp_path, "clone", str(origin), str(host))
    run(tmp_path, "clone", str(origin), str(ws))
    run(host, "config", "receive.denyCurrentBranch", "updateInstead")

    # Identity has to live in each repo's own config, not just in this file's `run`
    # helper. The code under test shells out to git with the ambient environment, and
    # a developer whose real identity comes from an includeIf on a path pattern has
    # none at all under a tmp directory -- which surfaced as `git rebase` failing with
    # "Committer identity unknown" on Windows while passing on Linux.
    for repo in (host, ws):
        run(repo, "config", "user.name", "t")
        run(repo, "config", "user.email", "t@t")

    key = tmp_path / "fake_key"
    key.write_text("not a real key", encoding="utf-8")
    target = git_relay.Target(
        name="t",
        remote_host="ignored",
        remote_user="ignored",
        remote_path=str(host),
        remote_ssh_key=key,
        local_path=ws,
        git_ssh_key=key,
        branch="master",
    )
    # remote_url would normally be user@host:path; point it straight at the directory.
    object.__setattr__(target, "remote_path", str(host))
    return type(
        "World", (), {"origin": origin, "host": host, "ws": ws, "target": target}
    )


@pytest.fixture(autouse=True)
def _local_url(monkeypatch):
    """Point the tool at local directories instead of scp-style URLs.

    git treats a path and an SSH URL identically for push/fetch/ls-remote, so the
    guards can be exercised for real without a host. The one thing that genuinely
    needs SSH is the receive.denyCurrentBranch lookup; it is stubbed here and tested
    on its own below.
    """
    monkeypatch.setattr(
        git_relay.Target,
        "remote_url",
        property(lambda self: self.remote_path),
    )
    monkeypatch.setattr(git_relay, "_check_update_instead", lambda _t: None)


# --------------------------------------------------------------------------------
# preflight_remote -- the two silent-drift cases
# --------------------------------------------------------------------------------


def test_remote_on_matching_branch_passes(world):
    git_relay.preflight_remote(world.target, "master")


def test_remote_detached_head_is_refused(world):
    """A push to a detached-HEAD host moves the ref and leaves the working tree
    behind, silently. Reproduced before this guard existed."""
    run(world.host, "checkout", "--detach", "HEAD")
    with pytest.raises(git_relay.Abort, match="detached HEAD"):
        git_relay.preflight_remote(world.target, "master")


def test_remote_on_other_branch_is_refused(world):
    """Same silent drift when the host is on a different branch: updateInstead only
    engages for the checked-out branch."""
    run(world.host, "checkout", "-b", "main")
    with pytest.raises(git_relay.Abort, match="has 'main' checked out"):
        git_relay.preflight_remote(world.target, "master")


def test_guard_actually_prevents_the_drift(world):
    """The guard is only worth having if the unguarded push really does drift.
    Assert the failure mode itself, so a future git that fixed this makes this test
    fail loudly rather than leaving a guard nobody can justify."""
    run(world.host, "checkout", "--detach", "HEAD")
    commit(world.ws, "n.txt")
    before = run(world.host, "rev-parse", "master")
    run(world.ws, "push", str(world.host), "master")
    assert run(world.host, "rev-parse", "master") != before, "ref did not move"
    assert not (world.host / "n.txt").exists(), (
        "worktree updated; guard may be obsolete"
    )


@pytest.mark.parametrize(
    "value, should_raise",
    [("updateInstead", False), ("", True), ("refuse", True), ("warn", True)],
)
def test_update_instead_must_be_set_on_the_host(
    world, monkeypatch, value, should_raise
):
    """`git push --dry-run` reports success even when the host would refuse the real
    push, so the setting is read directly rather than inferred from a dry run."""
    monkeypatch.setattr(
        git_relay.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=value, stderr=""),
    )
    if should_raise:
        with pytest.raises(git_relay.Abort, match="denyCurrentBranch"):
            _REAL_UPDATE_INSTEAD_CHECK(world.target)
    else:
        _REAL_UPDATE_INSTEAD_CHECK(world.target)


def test_dry_run_would_have_missed_a_refusing_host(world):
    """The reason the check above exists, asserted against real git: a host left at
    the default accepts --dry-run and refuses the real push."""
    run(world.host, "config", "--unset", "receive.denyCurrentBranch")
    commit(world.ws, "n.txt")
    run(world.ws, "push", "--dry-run", str(world.host), "master")  # no raise
    proc = subprocess.run(
        ["git", "-C", str(world.ws), "push", str(world.host), "master"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "denyCurrentBranch" in proc.stderr


# --------------------------------------------------------------------------------
# preflight_local
# --------------------------------------------------------------------------------


def test_dirty_local_clone_is_refused(world):
    (world.ws / "a.txt").write_text("modified", encoding="utf-8")
    with pytest.raises(git_relay.Abort, match="uncommitted tracked changes"):
        git_relay.preflight_local(world.target)


def test_untracked_local_file_does_not_block(world):
    """Untracked files are noise, not a reason to stop -- and unlike the rsync design
    they are never deleted, so blocking would buy nothing."""
    (world.ws / "scratch.txt").write_text("wip", encoding="utf-8")
    assert git_relay.preflight_local(world.target) == "master"


def test_untracked_survives_a_full_sync(world):
    """The one guarantee that replaces rsync's silent delete."""
    (world.ws / "wip.txt").write_text("my notes", encoding="utf-8")
    commit(world.ws, "b.txt")
    git_relay.sync(world.target, dry_run=False)
    assert (world.ws / "wip.txt").read_text(encoding="utf-8") == "my notes"


def test_a_conflicting_sync_leaves_the_rebase_in_progress(world):
    """What actually happens when both sides edited the same lines.

    Measured before the guard below was written: `git rebase FETCH_HEAD` exits 1, the
    tool aborts, and the clone is left in `.git/rebase-merge` with a conflicted index.
    Nothing is lost -- but the run does not clean up after itself, because unpicking a
    conflict is the user's call and `--abort` would throw away the host's commits the
    fetch just brought in.
    """
    (world.host / "a.txt").write_text("host edit", encoding="utf-8")
    run(world.host, "commit", "-am", "host change")
    (world.ws / "a.txt").write_text("ws edit", encoding="utf-8")
    run(world.ws, "commit", "-am", "ws change")

    # Not matched on git's conflict wording: that text is translated, and the code
    # under test inherits the ambient locale. The state on disk says the same thing
    # in any language.
    with pytest.raises(git_relay.Abort):
        git_relay.sync(world.target, dry_run=False)

    assert (world.ws / ".git" / "rebase-merge").is_dir(), "expected a paused rebase"
    # The two signals preflight_local reads are both misleading in this state: the
    # conflicted file shows up as a tracked modification, and HEAD is detached onto
    # the commit being replayed. Assert them, so the guard below cannot be mistaken
    # for defensiveness against a state that does not occur.
    assert run(world.ws, "status", "--porcelain", "--untracked-files=no") == "UU a.txt"
    assert run(world.ws, "branch", "--show-current") == ""


def test_a_paused_rebase_is_named_rather_than_called_dirty(world):
    """The reason the guard exists.

    Without it the re-run says "commit or discard them first", which is the wrong
    instruction mid-rebase: committing a file that still holds conflict markers is
    exactly what a user following that message does. Name the state and the two
    commands that end it instead.
    """
    run(world.ws, "checkout", "-b", "side")
    (world.ws / "a.txt").write_text("side edit", encoding="utf-8")
    run(world.ws, "commit", "-am", "side change")
    run(world.ws, "checkout", "master")
    (world.ws / "a.txt").write_text("master edit", encoding="utf-8")
    run(world.ws, "commit", "-am", "master change")
    proc = subprocess.run(
        ["git", "-C", str(world.ws), "rebase", "side"], capture_output=True, text=True
    )
    assert proc.returncode != 0, "expected a conflict to set up the state under test"

    with pytest.raises(git_relay.Abort, match="rebase is in progress"):
        git_relay.preflight_local(world.target)


def test_detached_local_clone_is_refused(world):
    run(world.ws, "checkout", "--detach", "HEAD")
    with pytest.raises(git_relay.Abort, match="detached HEAD"):
        git_relay.preflight_local(world.target)


def test_missing_remote_tracking_ref_is_named(world, tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    run(fresh, "init", "-b", "master")
    commit(fresh, "z.txt")
    object.__setattr__(world.target, "local_path", fresh)
    with pytest.raises(git_relay.Abort, match="origin/master does not exist"):
        git_relay.preflight_local(world.target)


# --------------------------------------------------------------------------------
# sync
# --------------------------------------------------------------------------------


def test_host_commit_reaches_origin(world):
    """The relay's entire reason to exist: the host cannot push to origin itself."""
    commit(world.host, "from_host.txt")
    git_relay.sync(world.target, dry_run=False)
    assert "from_host.txt" in run(world.origin, "ls-tree", "--name-only", "master")


def test_workstation_commit_reaches_the_host_worktree(world):
    commit(world.ws, "from_ws.txt")
    git_relay.sync(world.target, dry_run=False)
    assert (world.host / "from_ws.txt").exists(), (
        "updateInstead did not update the worktree"
    )


def test_divergent_sides_both_survive(world):
    commit(world.host, "h.txt")
    commit(world.ws, "w.txt")
    git_relay.sync(world.target, dry_run=False)
    tree = run(world.origin, "ls-tree", "--name-only", "master")
    assert "h.txt" in tree and "w.txt" in tree


def test_dirty_host_is_refused_by_git_itself(world):
    """This replaces the old remote pre-flight: git refuses rather than us checking."""
    (world.host / "a.txt").write_text("host edit", encoding="utf-8")
    commit(world.ws, "w.txt")
    with pytest.raises(git_relay.Abort):
        git_relay.sync(world.target, dry_run=False)
    assert (world.host / "a.txt").read_text(encoding="utf-8") == "host edit"


def test_dry_run_changes_nothing(world):
    commit(world.ws, "w.txt")
    before_origin = run(world.origin, "rev-parse", "master")
    before_host = run(world.host, "rev-parse", "master")
    git_relay.sync(world.target, dry_run=True)
    assert run(world.origin, "rev-parse", "master") == before_origin
    assert run(world.host, "rev-parse", "master") == before_host


# --------------------------------------------------------------------------------
# Config + migration
# --------------------------------------------------------------------------------


def test_config_dir_prefers_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_RELAY_CONFIG_DIR", str(tmp_path / "cfg"))
    assert git_relay.config_dir() == tmp_path / "cfg"


def test_missing_required_key_names_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_RELAY_CONFIG_DIR", str(tmp_path))
    (tmp_path / "x.toml").write_text('[remote]\nhost = "h"\n', encoding="utf-8")
    with pytest.raises(git_relay.Abort, match=r"\[remote\] user"):
        git_relay.load_target("x")


# --------------------------------------------------------------------------------
# Lock
# --------------------------------------------------------------------------------


def test_second_run_is_refused_while_the_first_holds_the_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    with git_relay.TargetLock("t"):
        with pytest.raises(git_relay.Abort, match="Another run is in progress"):
            with git_relay.TargetLock("t"):
                pass


def test_lock_is_released_after_the_block(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    with git_relay.TargetLock("t"):
        pass
    with git_relay.TargetLock("t"):
        pass  # no raise
