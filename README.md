# boss

`boss` lets JigglyPuff, the Hermes agent on ser5ver, supervise coding workers as
T3 Code threads. One CLI owns task briefs, live status, pending questions,
PR checks and captain decisions. Workers never merge or deploy.

Python 3.12+, standard library only. Runtime commands also use authenticated
`gh`, `git`, and `ssh` when inspecting remote worktrees. There is no daemon,
Python package download, Firstmate dependency, or background watcher.

## Install

```sh
git clone https://github.com/zzzubair/hermes-boss.git
cd hermes-boss
./install.sh
boss --help
```

The installer symlinks `~/.local/bin/boss` to this checkout's `bin/boss`.
Keep the checkout and put `~/.local/bin` on PATH. For development use
`python3 -m boss` or `./bin/boss`. Remove the symlink to uninstall; the ledger
stays intact. No agent hooks or Hermes configuration are changed by installation.

## Configuration

Create `~/.boss/config.toml`, or set `BOSS_HOME` to another state directory.
Get project IDs from T3's `t3_project_list` MCP tool. IDs belong to a particular
T3 host; `--host` does not translate them between machines.

```toml
# These three hosts are built in, using loopback tunnels as seen from ser5ver.
[hosts.ser5ver]
url = "http://127.0.0.1:3773/mcp"
token_env = "T3_SER5VER_MCP_TOKEN"
ssh = ""                          # Git runs locally on ser5ver

[hosts.oracle]
url = "http://127.0.0.1:13773/mcp"
token_env = "T3_ORACLE_MCP_TOKEN"
ssh = "oracle"                    # Inspect Git on oracle, not ser5ver

[hosts.wsl]
url = "http://127.0.0.1:23773/mcp"
token_env = "T3_WSL_MCP_TOKEN"
ssh = "wsl"

[projects.clashlens]
host = "ser5ver"
project_id = "<T3 project ID on ser5ver>"
repository = "zzzubair/clashlens"   # Optional if T3 reports GitHub identity
delivery = "direct-PR"            # Or "local-only"

[projects.clashlens.model_selection]
instanceId = "codex"
model = "gpt-6.1-sol"

[projects.clashlens.model_selection.options]
reasoningEffort = "high"
```

For a CLI running on oracle itself, override `hosts.oracle.url` to
`http://127.0.0.1:3773/mcp` and set its `ssh = ""`. Git inspection uses the
configured worker host and the worktree path returned by T3. It requires
noninteractive SSH authorization and Python 3 on that host.

Tokens come from the named environment variable, then `~/.hermes/.env`.
The file accepts literal `KEY=value` lines, optionally with matching outer
quotes. It is never sourced or evaluated. Environment values take precedence.
Never put tokens in TOML, command arguments, URLs or Git. Use T3's pairing and
OAuth flow to grant MCP read/operate access. `boss` discovers tool schemas on
every new MCP session, captures `mcp-session-id` from `initialize`, sends the
initialized notification, and uses Bearer authentication on subsequent requests.
It supports JSON and SSE responses. HTTP is allowed only on loopback;
use HTTPS or an SSH tunnel for other addresses. Redirects are refused.

`gh auth status` must succeed on the machine running `boss`. Launch resolves
the repository's current GitHub default branch, then asks T3 to fetch origin
and create a worktree with branch `boss/<id>` from that default branch.
Model defaults come from the registry, then the T3 project. `--model` and
`--effort` are checked against T3's live catalog; unsupported settings fail
before launch. Use `providerInstanceId:model` if a model ID is ambiguous.

## Commands

```sh
boss                                      # Live active fleet, counts, decisions, next step
boss spawn clashlens --title "Fix login" --brief-file brief.md
cat brief.md | boss spawn clashlens --title "Inspect login" --kind scout
boss spawn clashlens --title "Fix login" --brief-file brief.md \
  --host ser5ver --model codex:gpt-6.1-sol --effort high
boss status                               # Same fleet overview
boss status <id>                          # Run, input questions, PRs/CI, last assistant text
boss status <id> --full                   # Complete assistant text and questions
boss send <id> "Check the regression too"
boss answer <id> <request> "Use the existing endpoint"
boss stop <id>                            # Request interruption; keep all work
boss land <id> --approved "I reviewed this PR, merge it."
boss settle <id>
boss settle <id> --discard-approved "Discard this experiment."
boss decide add "Which API should we use?" --task <id>
boss decide list
boss decide close <decision-id> "Use the existing API."
boss poll
```

Every command and decision subcommand has `--help`. Omit `--brief-file` or
use `--brief-file -` to read piped stdin. Interactive missing input fails
instead of prompting. Multi-question input requests take one JSON object:
`boss answer <id> <request> '{"question_a":"yes","question_b":"existing"}'`.
Answers cannot approve permission requests. `--full` also works on the fleet
view and decision list. Default messages include a total character count when
truncated. Output uses terse key/value records with explicit totals such as
`0 active tasks` and `0 open decisions`.

Delivery modes:

- `ship` + `direct-PR`: verify, commit, push, open exactly one linked PR, stop.
- `ship` + `local-only`: verify, commit, keep a clean local branch, write a final
  report, stop. No push or PR. The captain arranges landing separately;
  `boss land` only merges PRs. Settlement proves default-branch ancestry.
- `scout`: investigate, leave a clean worktree, write a final report, no PR.
  The final assistant message starts with `BOSS_REPORT:`. `boss` copies the
  complete report into the task's durable `report.md`; local-only ships use
  the same report convention. A report from an older run does not count.

All briefs append the worktree, configured Git identity, no AI co-author,
no merge and no deploy rules. These are worker instructions; the merge and
settlement checks below are enforced in the CLI.

## Polling from Hermes

Use `boss poll` as the Hermes cron monitor script. One pass checks all active
tasks and prints only unresolved actionable state: finished, failed,
interrupted, waiting for input, open PRs and their current CI aggregate.
No actionable state means exactly zero output and exit 0. A failed host is
reported while the rest of the fleet is still checked.

Poll is a current alert snapshot, not a consume-on-read event queue. Alerts
remain while they need attention. `PR opened` identifies an open PR;
`CI changed` supplies its current aggregate for the monitor's comparison.
Identical live state always produces identical bytes, even after a restart or
a `status` call. Task IDs, request IDs and PR URLs are sorted; timestamps,
durations, last-message text and GitHub update times are excluded. A CI count
or state change changes the snapshot. Settled tasks leave the poll entirely.
The ledger records observation changes without duplicating unchanged events.

To provide ambient context, an operator can call bare `boss` from a Hermes
session-start hook. Hook installation is opt-in and outside this CLI's scope.

## Guard rules

- `land` requires a nonblank captain quote, a terminal worker with no pending
  questions, and exactly one linked PR for this task's repository and branches.
  The PR must be open, nondraft, mergeable with GitHub `CLEAN` state, and have
  at least one successful check, no failing or pending checks, and no change
  request review. No checks, only skipped checks and unknown checks refuse.
  `gh pr merge --squash --match-head-commit <approved SHA>` binds the merge to
  the inspected head. `boss` then verifies the merged state and records the
  quote, head and merge commit. It never enables auto-merge or deletes branches.
- `settle` requires a terminal worker with no pending questions. Open PRs,
  missing scout/local-only reports, dirty files, a different branch/origin,
  unpushed commits and unlanded branches refuse. Git evidence is refreshed
  from origin before checking ancestry. A squash-merged PR counts only when
  its head matches the worktree head. Previously seen PR links remain in the
  ledger, so unlinking one cannot hide it from the guard.
- A nonblank `--discard-approved` quote overrides the unlanded-work checks,
  including missing evidence. It cannot override a running worker or pending
  input. The quote is recorded. Settlement unwatches linked PRs and settles
  the thread, then reads T3 back to confirm. It never deletes the worktree,
  closes PRs, resets files or discards Git commits itself. T3's own configured
  settle lifecycle and project scripts still apply.
- Approval quotes are an audit record supplied by the trusted supervisor.
  `boss` cannot authenticate who originally spoke the words.

Exit codes: `0` success; `1` usage/configuration; `2` guard refusal;
`3` transport, T3, GitHub or local I/O failure. Structured errors go to stdout
as `error`, `message`, `next`; no traceback or token is printed.

## Durability and recovery

```text
~/.boss/
  config.toml
  tasks/<id>/meta.json
  tasks/<id>/brief.md
  tasks/<id>/events.log
  tasks/<id>/report.md       # Completed scout/local-only report
  decisions/<id>.json
```

Files are written with replacement + fsync and owner-only permissions for
new state. A nonblocking file lock permits one ledger writer at a time.
`events.log` is an atomically replaced JSON-lines journal; each record includes
the committed metadata. If a crash interrupts its `meta.json` replacement,
the journal restores the state on the next read. Live T3 and GitHub evidence
is refreshed for status, poll and guards; a cached completion is never merge
or settlement authority. Keep the ledger directory private.

Launch intent and the complete brief are durable before calling T3.
Launch has no retry key: an ambiguous failure leaves the task as
`launch_unknown`. Run `boss status <id>` to find its unique `[boss:<id>]`
thread before doing anything else. Never blindly respawn. An absent or
ambiguous match refuses and requires inspecting T3. Send uses a T3
`clientRequestId` recorded in its intent event; `boss` never automatically
retries mutations after transport failures. Inspect the worker and journal
before manually resending. A merge interrupted after GitHub accepted it is
reconciled against the recorded approved head on the next `land` call.
Settlement and decision closure can safely be repeated.

## Verification

```sh
python3 -m unittest -v
python3 -m compileall -q boss scripts tests
git diff --check
# Explicit opt-in, with a privately supplied existing MCP token:
python3 scripts/smoke.py --url http://127.0.0.1:3773/mcp --token-env T3_ORACLE_MCP_TOKEN
```

The unittest suite uses a fake MCP HTTP server and disposable Git repos.
It covers spawn/worktree contracts, model settings, lost-response recovery,
poll determinism, input requests, guard refusals, approved merge recording,
report capture, restart recovery, JSON/SSE sessions and token redaction.
The live script launches only a trivial scratch worker, compares two polls,
captures its report, settles it and reads T3 back. It does not merge anything.
Oracle's verified smoke output is in [evidence/oracle-smoke.txt](evidence/oracle-smoke.txt).

Design references only: [AXI's ten principles](https://axi.md/),
[Firstmate vision](https://github.com/kunchenguid/firstmate/blob/main/VISION.md),
and [Firstmate contract sections 1, 7, 8, 9](https://github.com/kunchenguid/firstmate/blob/main/AGENTS.md).
No Firstmate scripts or supervisor architecture are ported.
