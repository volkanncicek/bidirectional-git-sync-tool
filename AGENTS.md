# AGENTS.md

Coding-agent guide for **git-relay**, a tool that relays commits between a host with no route to your git server and that server, using the workstation clone in between as the only party that can see both.

Both hops are plain git over SSH. `.git/` is never copied and nothing is rsynced.

## Layout

```
git_relay.py            the whole tool, one module, no runtime dependencies
tests/test_git_relay.py real git repos in tmp_path, no mocks
examples/target.toml    the config template
pyproject.toml          packaging + the pytest dev dependency
```

Config lives in `~/.config/git-relay/<target>.toml`, **not** in this checkout. `GIT_RELAY_CONFIG_DIR` overrides it, which is what the tests use.

```bash
uv run pytest          # all of it, a few seconds on Linux
uv tool install .      # puts `git-relay` on PATH
```

## The invariants

**Never add an automatic commit.** The tool moves commits, it does not create them. Every consumer relies on nothing entering history that the user did not write.

**Never weaken `preflight_remote`.** `receive.denyCurrentBranch=updateInstead` protects only the branch the host has checked out. On a detached HEAD, or on a different branch, a push still moves the ref while the working tree stays behind, and the host then reports itself current while running old code. Two tests cover this, and one of them asserts the *unguarded* push really does drift, so the guard cannot outlive its justification.

**Never trust `git push --dry-run` for reachability.** It reports success against a host whose `receive.denyCurrentBranch` would refuse the real push. That is why `_check_update_instead` reads the setting over SSH instead of inferring it. A dry run that passes where a real run fails is the one behaviour this tool must not have.

**Never sync `.git/` with rsync.** It is the obvious way to build this and it is the wrong one. `rsync --delete` in the remote-to-local direction deletes uncommitted workstation files silently and copies the host's `.git` over the workstation's; both are reproducible in a minute with two throwaway repos. Git moving its own refs is what keeps that category out. If file-level mirroring is genuinely needed, that is a separate tool, not a flag here.

**Two SSH keys, never one.** `remote.ssh_key` authenticates to the host, `local.git_ssh_key` to origin. Collapsing them is how a host that must not reach origin ends up able to push to it.

## Things that will cost you an hour

- **`git ls-remote <url> HEAD` also matches `refs/remotes/origin/HEAD`.** A clone therefore always returns a `ref:` line, whatever its own HEAD is doing. Parse the line whose described ref is exactly `HEAD`, or a detached host reads as attached. This shipped as a bug once; `test_remote_detached_head_is_refused` is what caught it.
- **A paused rebase looks like two other problems.** Mid-conflict, `status --porcelain -uno` reports the file as `UU` and `branch --show-current` returns empty, so an unguarded `preflight_local` calls it a dirty tree and tells the user to commit or discard it -- the wrong instruction in both directions. Hence the rebase check runs *before* the dirty check. `test_a_paused_rebase_is_named_rather_than_called_dirty` pins the ordering.
- **`git config --get` exits 1 for an unset key.** Not an error worth surfacing; read the value, ignore the status.
- **Git identity may come from an `includeIf` on a path pattern.** Under a tmp directory no rule matches and `git rebase` fails with "Committer identity unknown". Test fixtures set identity in each repo's own config; do not rely on the ambient environment.
- **Windows paths in `GIT_SSH_COMMAND` need forward slashes.** A backslash is an escape character to ssh, not a separator. `_ssh_command` uses `Path.as_posix()` for exactly this.
- **The test suite is ~25x slower on Windows** (~2 min vs ~5 s) because of NTFS plus Defender. Both platforms are expected to pass; develop on whichever is convenient and check the other before releasing.

## Style

Comments explain *why*, especially where the code looks over-careful: nearly every guard exists because its failure was reproduced first. When adding one, reproduce the failure in a test before writing the fix, and say in the comment what was measured.
