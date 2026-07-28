# CLAUDE.md

**git-relay** relays commits between a host that cannot reach your git server and that server, through the workstation clone in between. One module, `git_relay.py`, no runtime dependencies.

```bash
uv run pytest                    # 25 tests, seconds on Linux, ~2 min on Windows
uv tool install .                # installs the `git-relay` command
git-relay <target> --dry-run     # safe: fetches are real, nothing is pushed or rebased
```

Config is **not** in this checkout. Targets live in `~/.config/git-relay/<name>.toml`; `GIT_RELAY_CONFIG_DIR` overrides the directory and is what the tests use.

---

## Critical Rules

**IMPORTANT: Never add an automatic commit.** The tool moves commits, it never creates one. Users rely on nothing entering history that they did not write themselves.

**IMPORTANT: Never sync `.git/` with rsync.** It is the obvious way to build this and it is the wrong one. `rsync --delete` from the host silently deletes uncommitted workstation files and replaces the workstation's `.git` with the host's; both are reproducible in a minute with two throwaway repos. Letting git move its own refs is what keeps that category out. If someone asks for file-level mirroring of untracked content, that is a different tool, not a flag here.

**IMPORTANT: Never weaken `preflight_remote`.** `receive.denyCurrentBranch=updateInstead` protects only the branch the host has checked out. On a detached HEAD or a different branch, a push moves the ref while the working tree stays behind, so the host reports itself current while running old code. Two tests cover it, and one asserts the *unguarded* push really does drift.

**IMPORTANT: Never treat `git push --dry-run` as proof a push will work.** It succeeds against a host whose `receive.denyCurrentBranch` would refuse the real thing. `_check_update_instead` reads that setting over SSH for this reason. A dry run that passes where a real run fails is the one behaviour this tool must not have.

**IMPORTANT: Keep the two SSH keys separate.** `remote.ssh_key` authenticates to the host, `local.git_ssh_key` to origin. Merging them is how a host that must not reach origin gains the ability to push to it.

---

## Working method here

Every guard in this codebase exists because its failure was reproduced first. Keep that order: write a test that demonstrates the broken behaviour against real git repositories, then write the fix, then record in a comment what was measured. Tests use real repos in `tmp_path` rather than mocks, because the behaviour under test is git's own and a mock would only assert that we believe the documentation.

Do not shorten a comment that explains why something looks over-careful. Those comments are the reason the guards survive refactors.

Before changing the config schema, target naming, or output format, remember that something downstream drives this tool non-interactively and parses or logs its output. Check that caller first: a silent downstream break costs hours.

## Non-obvious traps

- **`git ls-remote <url> HEAD` also matches `refs/remotes/origin/HEAD`.** A clone therefore always returns a `ref:` line whatever its own HEAD is doing, so "take the first `ref:` line" reports a detached host as attached. This shipped as a bug once and a test caught it.
- **A paused rebase looks like two other problems.** Mid-conflict, `status --porcelain -uno` reports the file as `UU` and `branch --show-current` returns empty, so an unguarded `preflight_local` calls it a dirty tree and tells the user to commit or discard, which is the wrong instruction in both directions. That is why the rebase check runs *before* the dirty check, not after.
- **`git config --get` exits 1 for an unset key.** Read the value, ignore the status.
- **Git identity can come from an `includeIf` path rule.** Under a tmp directory nothing matches and `git rebase` dies with "Committer identity unknown". Fixtures set identity per repo; never rely on the ambient environment.
- **Windows paths inside `GIT_SSH_COMMAND` need forward slashes.** A backslash is an escape to ssh, not a separator; `_ssh_command` uses `Path.as_posix()`.
- **`.gitattributes` pins `*.py` to LF on purpose.** `git_relay.py` is executable and carries a shebang; a CRLF checkout makes it fail with "bad interpreter" on Linux and WSL.
