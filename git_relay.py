#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
# Two ways in, both first class. `uv tool install` puts a `git-relay` command on PATH
# via [project.scripts]; the inline block above lets a fresh clone run ./git_relay.py
# straight away. The block is not redundant with pyproject.toml despite declaring no
# dependencies: it is what pins requires-python for the script path, so a box whose
# `python3` is 3.9 gets the right interpreter downloaded instead of an import error
# about tomllib, which lands in 3.11.
"""git-relay: keep a network-isolated host's checkout in step with a git origin,
using your workstation as the relay.

    host (no route to origin)  <-->  workstation clone  <-->  origin

The host cannot reach origin, so commits travel through the workstation. Both hops
are plain git over SSH: nothing is rsynced, and `.git/` is never copied. That is a
deliberate choice -- copying `.git/` is what makes a sync tool able to destroy work
(a `--delete` that outruns a push, packed-refs drift, an inherited origin URL), and
none of those failure modes exist when git moves the refs itself.

Requires on the host, once:

    git config receive.denyCurrentBranch updateInstead

That makes a push to the checked-out branch update the host's working tree too, and
refuses the push outright if that tree is dirty. Two caveats it does NOT cover are
handled here by `preflight_remote`; see the comment there.

Usage:
    git-relay <target>              run the sync
    git-relay <target> --dry-run    show what would happen, change nothing
    git-relay --list                list configured targets
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------

# Colour only when stdout is a terminal. A relay is typically driven by a scheduler or
# a wrapper script that captures the output, and escape codes in a captured log are
# noise a reader has to filter out by hand.
_TTY = sys.stdout.isatty()
_C = {
    "red": "\033[0;31m",
    "green": "\033[0;32m",
    "yellow": "\033[0;33m",
    "blue": "\033[0;34m",
    "off": "\033[0m",
}


def _log(colour: str, level: str, msg: str, stream=sys.stdout) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [{level}] {msg}"
    print(f"{_C[colour]}{line}{_C['off']}" if _TTY else line, file=stream, flush=True)


def info(msg: str) -> None:
    _log("blue", "INFO", msg)


def warn(msg: str) -> None:
    _log("yellow", "WARN", msg, sys.stderr)


def ok(msg: str) -> None:
    _log("green", "SUCCESS", msg)


class Abort(Exception):
    """Every refusal goes through here, so main() has exactly one exit path."""


# --------------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------------


def config_dir() -> Path:
    """Config lives outside the checkout on purpose.

    Keeping targets inside the cloned repo meant `git clean -xdf` could delete them
    and every `git pull` of the tool touched user data. GIT_RELAY_CONFIG_DIR exists
    for tests and for pointing the directory at a backed-up location.
    """
    if env := os.environ.get("GIT_RELAY_CONFIG_DIR"):
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "git-relay"


@dataclass(frozen=True)
class Target:
    name: str
    remote_host: str
    remote_user: str
    remote_path: str
    remote_ssh_key: Path
    local_path: Path
    git_ssh_key: Path
    branch: str | None  # None -> detect from origin/HEAD

    @property
    def remote_url(self) -> str:
        # scp-style rather than ssh:// because it needs no escaping for absolute
        # paths and is what a user would type by hand.
        return f"{self.remote_user}@{self.remote_host}:{self.remote_path}"


def _require(table: dict, section: str, key: str, path: Path) -> str:
    try:
        value = table[section][key]
    except KeyError:
        raise Abort(f"{path}: missing required key [{section}] {key}")
    if not isinstance(value, str) or not value.strip():
        raise Abort(f"{path}: [{section}] {key} must be a non-empty string")
    return value


def load_target(name: str, explicit: Path | None = None) -> Target:
    path = explicit if explicit else config_dir() / f"{name}.toml"
    if not path.is_file():
        raise Abort(f"No config for target '{name}' (looked in {path})")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise Abort(f"{path}: invalid TOML -- {exc}")

    target = Target(
        name=name,
        remote_host=_require(data, "remote", "host", path),
        remote_user=_require(data, "remote", "user", path),
        remote_path=_require(data, "remote", "path", path),
        remote_ssh_key=Path(_require(data, "remote", "ssh_key", path)).expanduser(),
        local_path=Path(_require(data, "local", "path", path)).expanduser(),
        git_ssh_key=Path(_require(data, "local", "git_ssh_key", path)).expanduser(),
        branch=data.get("sync", {}).get("branch"),
    )

    for label, key in (
        ("remote.ssh_key", target.remote_ssh_key),
        ("local.git_ssh_key", target.git_ssh_key),
    ):
        if not key.is_file():
            raise Abort(f"{label} does not exist: {key}")
    if not target.local_path.is_dir():
        raise Abort(f"local.path does not exist: {target.local_path}")
    # A gitlink file means the clone is a submodule of something. The host would then
    # receive a path that does not resolve there; refuse rather than half-work.
    dot_git = target.local_path / ".git"
    if not dot_git.is_dir():
        raise Abort(
            f"local.path is not a standalone git repo (.git is not a directory): {target.local_path}"
        )
    return target


# --------------------------------------------------------------------------------
# Locking
# --------------------------------------------------------------------------------


class TargetLock:
    """Advisory lock so two runs cannot race the same target.

    Held on a file descriptor rather than on the file's existence: the kernel drops
    it when the process dies however it dies, so there is no stale-lock state for a
    user to clean up by hand, and no auto-delete that would mask a real double run.
    """

    def __init__(self, name: str) -> None:
        base = Path(
            os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TEMP") or "/tmp"
        )
        uid = os.getuid() if hasattr(os, "getuid") else os.getpid()  # nt has no uid
        self.path = base / f"git-relay-{name}.{uid}.lock"
        self._fd: int | None = None

    def __enter__(self) -> TargetLock:
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise Abort(
                f"Another run is in progress for this target (lock: {self.path})"
            )
        return self

    def __exit__(self, *_exc) -> None:
        if self._fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(self._fd)


# --------------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------------


def _ssh_command(key: Path) -> str:
    """GIT_SSH_COMMAND for one hop.

    IdentitiesOnly stops ssh-agent from offering an unrelated key first and burning
    the server's auth attempts. ConnectTimeout matters because a relayed host is by
    definition reachable only over some constrained path -- a VPN, a tunnel, a jump
    box -- and when that path drops, ssh's default is to hang rather than fail, which
    turns a scheduled run into one that never returns. The key is rendered with
    forward slashes so the same string works when Windows git invokes Windows ssh (a
    backslash there is an escape character, not a separator).
    """
    return (
        f'ssh -i "{key.as_posix()}" -o IdentitiesOnly=yes '
        f"-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
    )


def git(repo: Path, *args: str, key: Path | None = None, check: bool = True) -> str:
    env = os.environ.copy()
    if key is not None:
        env["GIT_SSH_COMMAND"] = _ssh_command(key)
    # Never let a pager or a credential prompt block a non-interactive run.
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        ["git", "-C", str(repo), "--no-pager", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise Abort(f"git {' '.join(args)} failed:\n{detail}")
    return proc.stdout.strip()


def ref_exists(repo: Path, ref: str) -> bool:
    # rev-parse --verify --quiet prints the sha on success and nothing on failure,
    # which makes the empty string the only reliable "absent" signal. show-ref
    # --quiet prints nothing either way and cannot be read this way at all.
    return git(repo, "rev-parse", "--verify", "--quiet", ref, check=False) != ""


def detect_branch(repo: Path) -> str:
    """Prefer origin/HEAD, because that is the branch origin itself calls default.

    The main-before-master fallback only matters for a clone that never had
    origin/HEAD set; the order is arbitrary there, which is why [sync] branch exists
    for anyone who cannot rely on the guess.
    """
    head = git(
        repo,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
        check=False,
    )
    if head:
        return head.removeprefix("origin/")
    for candidate in ("main", "master"):
        if ref_exists(repo, f"refs/heads/{candidate}"):
            return candidate
    raise Abort(
        f"Cannot determine the default branch in {repo}; set [sync] branch in the config"
    )


def commits_between(repo: Path, base: str, tip: str) -> list[str]:
    out = git(repo, "log", "--oneline", f"{base}..{tip}")
    return [line for line in out.splitlines() if line]


def untracked(repo: Path) -> list[str]:
    out = git(repo, "status", "--porcelain", "--untracked-files=all")
    return [line[3:] for line in out.splitlines() if line.startswith("??")]


# --------------------------------------------------------------------------------
# Pre-flight
# --------------------------------------------------------------------------------


def rebase_state_dir(repo: Path) -> Path | None:
    """The directory git keeps a paused rebase in, or None if none is paused.

    Both names have to be tried: `rebase-merge` is what the default merge backend
    leaves behind, `rebase-apply` what `--apply` and `git am` use. Asking git for the
    paths rather than hardcoding `.git/...` is what keeps this working in a worktree
    or a repo whose git dir lives elsewhere.
    """
    for name in ("rebase-merge", "rebase-apply"):
        path = repo / git(repo, "rev-parse", "--git-path", name)
        if path.is_dir():
            return path
    return None


def preflight_local(t: Target) -> str:
    """The workstation clone must be clean and must know where origin's branch is."""
    # Before the dirty check, not after: a conflict in step 1 or 2 of the previous run
    # leaves the clone mid-rebase, and both signals read below are misleading there. A
    # conflicted file reports as `UU`, so the dirty branch fires and tells the user to
    # "commit or discard" -- which mid-rebase means committing a file that still holds
    # conflict markers. HEAD is also detached onto the commit being replayed, so even
    # past that check the next message would name the wrong problem. Measured: both
    # were reproduced against real repos before this guard was written.
    if state := rebase_state_dir(t.local_path):
        raise Abort(
            f"A rebase is in progress in {t.local_path} (usually a previous run that "
            "stopped on a conflict, but any unfinished rebase lands here). Resolve the "
            "conflicted files, `git add` them and run `git rebase --continue`, or run "
            "`git rebase --abort` to undo the rebase entirely. Then run git-relay "
            f"again.\n    (state: {state})"
        )

    dirty = git(t.local_path, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise Abort(
            "The local clone has uncommitted tracked changes; commit or discard them first:\n"
            + dirty
        )

    branch = t.branch or detect_branch(t.local_path)
    if not ref_exists(t.local_path, f"refs/heads/{branch}"):
        raise Abort(f"Branch '{branch}' does not exist in {t.local_path}")
    current = git(t.local_path, "branch", "--show-current")
    if current != branch:
        raise Abort(
            f"The local clone is on '{current or 'a detached HEAD'}', expected '{branch}'"
        )
    # Without the remote-tracking ref every `origin/<branch>..HEAD` range below errors
    # out. That is survivable here, but the message would be a raw git error about an
    # unknown revision rather than the actual problem, which is a clone that has never
    # fetched. Name it instead.
    if not ref_exists(t.local_path, f"refs/remotes/origin/{branch}"):
        raise Abort(
            f"origin/{branch} does not exist in {t.local_path} -- run `git fetch origin` there first"
        )
    return branch


def _check_update_instead(t: Target) -> None:
    """Confirm the host will actually accept a push to its checked-out branch.

    Worth a separate round trip because `git push --dry-run` does not catch this:
    with receive.denyCurrentBranch left at its default, --dry-run reports the push as
    successful and the real push is then refused by the host. Measured, not assumed.
    A dry run that says "fine" and a real run that fails is the one thing a tool like
    this must never do, so the setting is read directly.
    """
    proc = subprocess.run(
        [
            "ssh",
            "-i",
            str(t.remote_ssh_key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=15",
            f"{t.remote_user}@{t.remote_host}",
            f"git -C {shlex.quote(t.remote_path)} config --get receive.denyCurrentBranch",
        ],
        capture_output=True,
        text=True,
    )
    # `config --get` exits 1 for an unset key, which is not an error worth reporting
    # as one; only the value matters.
    value = proc.stdout.strip()
    if value != "updateInstead":
        raise Abort(
            f"The host has receive.denyCurrentBranch={value or 'unset'}. A push would be "
            "refused and its working tree would never update. Run this once on the host:\n"
            f"    git -C {t.remote_path} config receive.denyCurrentBranch updateInstead"
        )


def preflight_remote(t: Target, branch: str) -> None:
    """Verify the host has `branch` actually checked out.

    This is the one thing `receive.denyCurrentBranch=updateInstead` does not give
    us. It only engages when the pushed branch is the checked-out one; if the host
    sits on a detached HEAD or on a different branch, the push still succeeds, the
    ref moves, and the working tree silently stays behind -- the host reports itself
    up to date while running the old code. Measured, not assumed: both cases were
    reproduced before this guard was written.

    `ls-remote --symref` answers it in one round trip and needs no shell on the host.
    """
    out = git(
        t.local_path,
        "ls-remote",
        "--symref",
        t.remote_url,
        "HEAD",
        key=t.remote_ssh_key,
    )
    # `ls-remote <url> HEAD` matches every ref whose tail is HEAD, so a clone also
    # answers with refs/remotes/origin/HEAD -- which carries a `ref:` line whatever
    # state the working tree is in. Taking the first `ref:` line therefore reports a
    # detached host as attached. Match on the described ref being exactly HEAD.
    symref = next(
        (
            line
            for line in out.splitlines()
            if line.startswith("ref: ") and line.split("\t")[-1].strip() == "HEAD"
        ),
        None,
    )
    if symref is None:
        raise Abort(
            "The host is on a detached HEAD. A push would move the ref without updating "
            "the working tree, leaving the host silently stale. Fix it on the host "
            f"(`git checkout {branch}`), then retry."
        )
    remote_branch = symref.split()[1].removeprefix("refs/heads/")
    _check_update_instead(t)
    if remote_branch != branch:
        raise Abort(
            f"The host has '{remote_branch}' checked out but this target syncs '{branch}'. "
            "Pushing would update the ref without touching the host's working tree. "
            "Check out the right branch on the host, then retry."
        )
    info(f"Pre-flight OK: host is on '{remote_branch}'")


def report_untracked(t: Target, local_untracked: list[str]) -> None:
    """Untracked files do not travel. Say so rather than let it be discovered later.

    Not fatal: a scratch file on either side is normal. But the previous rsync-based
    design moved these silently in one direction and deleted them in the other, so
    the one thing worth guaranteeing is that they are never a surprise.
    """
    if local_untracked:
        warn(
            f"{len(local_untracked)} untracked file(s) in the local clone will not propagate:"
        )
        for name in local_untracked[:10]:
            warn(f"    {name}")
        if len(local_untracked) > 10:
            warn(f"    ... and {len(local_untracked) - 10} more")


# --------------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------------


def sync(t: Target, dry_run: bool) -> None:
    info(f"Target: {t.name} (host={t.remote_host}, path={t.remote_path})")
    if dry_run:
        warn("DRY RUN -- fetches still happen, nothing is rebased or pushed")

    branch = preflight_local(t)
    info(f"Local clone on '{branch}'")
    preflight_remote(t, branch)
    report_untracked(t, untracked(t.local_path))

    # 1. Anything committed on the host comes in first, so the rebase onto origin
    #    below carries it along and a single push publishes both sides' work.
    info("Fetching from the host")
    git(t.local_path, "fetch", t.remote_url, branch, key=t.remote_ssh_key)
    from_host = commits_between(t.local_path, "HEAD", "FETCH_HEAD")
    if from_host:
        info(f"{len(from_host)} commit(s) on the host not here:")
        for line in from_host:
            info(f"    {line}")
        if dry_run:
            warn("would rebase onto the host's commits")
        else:
            git(t.local_path, "rebase", "FETCH_HEAD")
    else:
        info("Host has nothing new")

    # 2. Origin.
    info("Fetching from origin")
    git(t.local_path, "fetch", "origin", key=t.git_ssh_key)
    from_origin = commits_between(t.local_path, "HEAD", f"origin/{branch}")
    if from_origin:
        info(f"{len(from_origin)} commit(s) on origin not here")
        if dry_run:
            warn(f"would rebase onto origin/{branch}")
        else:
            git(t.local_path, "rebase", f"origin/{branch}", key=t.git_ssh_key)
    else:
        info(f"Already up to date with origin/{branch}")

    # 3. Publish. Origin first: it is the canonical copy, and if the push to the host
    #    fails the work is still safe somewhere other than this workstation.
    outgoing = commits_between(t.local_path, f"origin/{branch}", "HEAD")
    if outgoing:
        info(f"Pushing {len(outgoing)} commit(s) to origin")
        args = ["push", "origin", f"HEAD:{branch}"] + (["--dry-run"] if dry_run else [])
        git(t.local_path, *args, key=t.git_ssh_key)
    else:
        info("Nothing to push to origin")

    # 4. Host. Always attempted, even with nothing outgoing: the host may be behind
    #    because a previous run stopped here.
    info("Pushing to the host")
    args = ["push", t.remote_url, f"HEAD:{branch}"] + (["--dry-run"] if dry_run else [])
    git(t.local_path, *args, key=t.remote_ssh_key)

    ok(f"{'Dry run finished' if dry_run else 'Sync completed'} for {t.name}")


# --------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------


def cmd_list() -> int:
    directory = config_dir()
    targets = (
        sorted(p.stem for p in directory.glob("*.toml")) if directory.is_dir() else []
    )
    if not targets:
        print(f"No targets configured in {directory}")
        print("Create <name>.toml there; see examples/target.toml in this repo.")
        return 1
    print(f"Targets in {directory}:")
    for name in targets:
        print(f"  {name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-relay",
        description="Relay commits between a network-isolated host and a git origin.",
    )
    parser.add_argument("target", nargs="?", help="configured target name")
    parser.add_argument("--list", action="store_true", help="list configured targets")
    parser.add_argument(
        "--config",
        type=Path,
        help="use this TOML file instead of the configured target",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would happen, change nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        return cmd_list()
    if not args.target:
        build_parser().print_help()
        return 2

    try:
        target = load_target(args.target, args.config)
        with TargetLock(target.name):
            sync(target, args.dry_run)
    except Abort as exc:
        _log("red", "ERROR", str(exc), sys.stderr)
        return 1
    except KeyboardInterrupt:
        _log("red", "ERROR", "interrupted", sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
