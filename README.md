# git-relay

Two-way (bidirectional) git sync for a **server that cannot reach your git remote**, relayed through your workstation.

```
host (no route to origin)  <-->  workstation clone  <-->  origin
```

Plenty of machines can be reached from your desk but cannot reach your git server themselves: a box inside a VPC with no egress to an internal Bitbucket or GitLab, an air-gapped or firewalled lab machine, an ETL host on a segmented network, a customer's server behind a jump box. Its `git fetch` times out, so commits can only arrive through something that can see both ends. That something is your workstation, and this is the tool that drives it.

Both hops are **plain git over SSH**. Nothing is rsynced and `.git/` is never copied, so no run can overwrite one side's history with the other's.

---

## Install

```bash
uv tool install git+https://github.com/volkanncicek/git-relay
```

That puts a `git-relay` command on your PATH. Or clone and run the file directly, which works because the tool has no runtime dependencies:

```bash
git clone https://github.com/volkanncicek/git-relay
./git-relay/git_relay.py --help
```

Needs `git` and `ssh` on your machine and `git` on the host. Nothing else. Windows, macOS and Linux all work, and so does WSL.

## Set up a target

One file per target. Copy [`examples/target.toml`](examples/target.toml) to `~/.config/git-relay/<name>.toml` and fill in the host, the paths and the two SSH keys.

Then, **once, on the host**:

```bash
git config receive.denyCurrentBranch updateInstead
```

Without it git refuses any push to the branch the host has checked out. With it, a push updates the host's working tree too, and is refused outright if that tree has uncommitted changes.

## Run

```bash
git-relay <name>              # sync
git-relay <name> --dry-run    # report what would happen, change nothing
git-relay --list              # show configured targets
```

Each run does:

1. **Local pre-flight.** Refuse if the workstation clone is dirty, detached, or has no `origin/<branch>`.
2. **Remote pre-flight.** Refuse unless the host has the right branch checked out. ([Why this matters.](#why-the-remote-pre-flight-exists))
3. **Fetch from the host**, and rebase onto anything committed there.
4. **Fetch from origin**, and rebase onto it.
5. **Push to origin**, then **push to the host**.

Commits are yours to make. The tool never creates one, so nothing enters history that you did not write.

---

## Limitations

**Untracked files do not propagate.** Only committed work moves. A file that is neither committed nor gitignored stays where it is, on whichever side it was created. The tool lists such files at the start of every run so this is never a surprise. If you need file-level mirroring including untracked content, you want [osync](https://github.com/deajan/osync) or [Unison](https://github.com/bcpierce00/unison), not this.

**Edits made on the host are not collected.** Commit them on the host and they arrive on the next run. Leave them uncommitted and the run stops with git's own error, which is the safe outcome: the tool never discards work it did not create.

**One branch per target.** Add a second config file to relay a second branch.

---

## When a run stops on a conflict

Both hops rebase, so if the same lines were changed on the host and on origin, git stops with a conflict and the run aborts. The tool does not resolve it and does not clean up after itself: `git rebase --abort` would throw away the host commits the fetch had just brought in, and choosing between two edits is not a decision a sync tool should make on your behalf.

The paused rebase is on your **workstation clone**, at `local.path`. Finish it there:

```bash
git status                    # the conflicted files, marked UU
$EDITOR <file>                # resolve, removing the <<<<<<< markers
git add <file>                # do not commit
git rebase --continue         # repeat if more commits conflict
git-relay <name>              # re-run; it starts over from a clean tree
```

`git rebase --abort` gets you back to where the run started, at the cost of re-fetching the host's commits next time.

Running `git-relay` before the rebase is finished stops and says so, rather than reporting the conflicted file as an ordinary uncommitted change.

---

## Why not just rsync the folder?

It is the obvious approach, and it works until it does not. It has one property that eventually costs you something: syncing `.git/` with `--delete` means every run is a moment where one side's history can overwrite the other's. Guards narrow that window; they do not close it, because the window is the design.

Concretely, and measured rather than argued: under that approach a file you had created on the workstation but not yet committed was **deleted without a word** on the next run, because the host did not have it and `--delete` says so. The transfer summary reported a count, never a name.

Moving both hops to git removes the category rather than guarding it. Git updates refs atomically, refuses non-fast-forwards, and leaves untracked files alone. It also means no rsync dependency, which is what would otherwise stop the tool running on Windows without WSL.


## Why the remote pre-flight exists

`receive.denyCurrentBranch=updateInstead` only engages when the branch you push **is** the one the host has checked out. If the host sits on a detached HEAD, or on a different branch, the push still succeeds, the ref still moves, and the working tree silently stays behind. The host then reports itself up to date while running the old code.

That is the worst kind of failure for a deployment target, so the tool reads the host's checked-out branch with `git ls-remote --symref` before pushing anything, and stops if it does not match. Both cases are reproduced in the test suite, including an assertion that the unguarded push really does drift, so the guard cannot quietly outlive its reason.

## How it compares

| | scope |
|---|---|
| **git-relay** | host **cannot** reach origin; workstation relays both hops |
| [kubernetes/git-sync](https://github.com/kubernetes/git-sync) | one-way, upstream to a pod; sidecar, not a CLI |
| [simonthum/git-sync](https://github.com/simonthum/git-sync) | one repo on one machine, against its own origin |
| [osync](https://github.com/deajan/osync) | two-way file sync; knows nothing about git |

---

## Development

```bash
uv run pytest
```

Tests run against real git repositories rather than mocks, because the behaviour under test is git's. Every guard has a test for the failure it prevents.

## Licence

MIT. See [LICENSE](LICENSE).
