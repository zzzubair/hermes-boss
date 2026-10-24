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

The installer symlinks `~/.local/bin/boss` to this checkout's `bin/boss`,
`~/.agents/skills/boss` to `skill/`, and `~/.hermes/plugins/boss` to `plugin/`.
Keep the checkout and put `~/.local/bin` on PATH. Existing real files or
directories at these destinations refuse installation; existing symlinks can
be updated. For development use `python3 -m boss` or `./bin/boss`.
Remove those three symlinks to uninstall; the ledger stays intact.

Installation does not enable the plugin or change any Hermes config. It
prints the opt-in enable command:

```sh
hermes plugins enable boss
```

The `boss` skill provides the command/guard reference on demand. Its ambient
plugin registers exactly one `pre_llm_call` callback and returns one line only
when local records need attention. It never calls the CLI, T3, GitHub, a
subprocess or an LLM. When nothing needs attention it returns `None`, adding
zero context tokens. No Hermes core files are patched.

## Configuration

Create `~/.boss/config.toml`, or set `BOSS_HOME` to another state directory.
Projects do not need a registry entry: `boss spawn <name>` looks for exactly
one matching T3 project title on `default_host`, which defaults to `oracle`.
`--host` overrides the selected host. No match or duplicate titles refuse with
candidate names and IDs. Discovery reads `git remote get-url origin` from the
project's `workspaceRoot` on that host, locally or over its configured SSH
connection. Only GitHub owner/repo origins are accepted, including HTTPS and
SSH Git remotes; T3's `repositoryIdentity` can be null.

Registry values override discovery and defaults. Registry IDs belong to a
particular T3 host; `--host` does not translate them between machines.

```toml
default_host = "oracle"

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
repository = "zzzubair/clashlens"   # Optional; otherwise read origin on the host
delivery = "direct-PR"            # Or "local-only"
auto_merge = false                # Supervisor preference only; workers never merge
test_host = "wsl"                 # Optional instruction for the worker
no_mistakes = true                # Optional instruction for the worker

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
Automatic secret discovery ignores values shorter than 12 characters,
boolean/number literals and config flag keys such as `HERMES_REDACT*`,
`*_SECRETS` and `*_ENABLED`. Explicit host `token_env` credentials are always
protected, even when short, including a file credential overridden by the env.
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

## Preference layers

All preferences are optional. `~/.boss/rules.md` supplies global worker rules.
`[projects.<name>]` supplies settings, and `~/.boss/projects/<name>.md` supplies
project notes. Settings-only entries can omit host, project ID and repository
and still use title discovery. `auto_merge` and `no_mistakes` default to false;
`test_host` has no default. These values are stored with each spawned task.
`auto_merge` appears in project details and task status for the supervisor;
it grants no merge authority to the worker or the CLI.

For example:

```text
~/.boss/rules.md: Work in small steps and include verification evidence.
~/.boss/projects/clashlens.md: Run browser checks on wsl.
```

With `test_host = "wsl"` and `no_mistakes = true` in the project's config,
the saved and sent brief contains, in order:

```markdown
<task brief>

## Rules
Work in small steps and include verification evidence.

## Project settings
test_host: wsl
no_mistakes: true

## Project notes
Run browser checks on wsl.

Project settings and project notes win over global rules when they conflict; the boss worker contract below always applies.

## boss worker contract
<existing kind/delivery instructions, including never merge and never deploy>
```

Empty sections are skipped. Only explicitly set `test_host` and `no_mistakes`
values are rendered in the settings section; `auto_merge` stays out of worker
instructions. The final composed brief, including the footer, is limited to
115000 characters. A size error names the layer to shorten. Rules and notes
are checked with the existing secret redactor before launch; a detected token
or secret refuses the spawn without echoing its value or saving a task.

Code conventions (package manager, folders not to touch) belong in the repo's AGENTS.md, not boss.

## Commands

```sh
boss                                      # Live active fleet, counts, decisions, next step
boss project list                         # T3 projects on default_host, with registry/notes markers
boss project list --host oracle
boss project add clashlens --host oracle  # Save discovery results; optional, not required to spawn
boss project add homepage --delivery local-only --auto-merge --test-host wsl --no-mistakes
boss project show clashlens               # Effective settings and contributing files
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

Every command, project subcommand and decision subcommand has `--help`. Omit `--brief-file` or
use `--brief-file -` to read piped stdin. Interactive missing input fails
instead of prompting. Multi-question input requests take one JSON object:
`boss answer <id> <request> '{"question_a":"yes","question_b":"existing"}'`.
Answers cannot approve permission requests. `--full` also works on the fleet
view and decision list. Default messages include a total character count when
truncated. Output uses terse key/value records with explicit totals such as
`0 active tasks` and `0 open decisions`.

`project add` appends a quoted `[projects.<name>]` block containing the host,
project ID and resolved repository, plus any supplied preference flags. It
preserves all existing config text and comments using an atomic write, and
refuses an existing entry. `project show` reports registry versus T3 lookup,
effective model/delivery/preferences, and whether `rules.md` and project notes
are present. It reads settings; it does not launch a worker or change config.

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

Schedule only `boss poll` for T3 checks. The command is LLM-free: one pass
reads active T3 tasks and GitHub CI, updates the ledger, and prints changed
actionable items. Unchanged successful polls exit 0 with exactly zero output,
including after a restart. A changed task can report finished, failed,
interrupted, waiting for input, open PRs and current CI aggregates. No actionable
items also means empty output. A failed host remains an error with a nonzero
exit and diagnostic output; other tasks are still checked.

Poll's last observation is separate from on-demand status reads, so reading
status does not consume an input or CI change before the next scheduled pass.
`boss status <id>` marks that completed run as reviewed; it does not approve
merging or discarding. Another completed run gets a new reminder. Bare `boss`
and `boss status` show the fleet without acknowledging individual tasks.
An input reminder stays until the request is answered or a live refresh shows
it resolved. Closing a decision and settling a task also clear their reminders.

After each CLI command, `attention.json` stores a bounded local summary: the
total and up to six validated IDs for open decisions, pending worker inputs,
and completed runs first recorded by poll that remain unreviewed. The plugin
reads only this file, at most 4096 bytes; it does not scan task journals or
follow network state on a turn. Missing or corrupt cache data adds no context.
Full questions, task text, tokens and decision bodies never enter this summary.
For example:

```text
boss: 2 need you (d3 decision, t7 input) - run boss status
```

The hook keeps reminding until the local records are resolved. Use the CLI
to check live state. Run `boss poll` once after upgrading to populate the view
from existing decisions and tasks. The timing check in `scripts/check_ambient.py`
measures the actual callback, including its bounded local read, against 50 ms.

For an opt-in, five-minute Hermes cron on ser5ver, first create this wrapper
under the active Hermes profile's `scripts/` directory:

```sh
mkdir -p ~/.hermes/scripts
cat > ~/.hermes/scripts/boss-poll.sh <<'SH'
#!/bin/sh
exec "$HOME/.local/bin/boss" poll
SH
hermes cron create '*/5 * * * *' --name boss-poll --script boss-poll.sh --no-agent --deliver local
```

This is documentation only; installation never creates the script or cron.
Hermes runs `.sh` scripts with bash. `--no-agent` skips the LLM completely,
silently accepts empty stdout and records nonempty output locally. Use this
script gate instead of `--monitor-script`, which expects a persistent snapshot
and would treat a transition back to empty output as a new change. The plugin
has no timer; interactive `boss`/`status` still query live state on demand.

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
  rules.md                  # Optional global worker rules
  projects/<name>.md         # Optional project notes
  tasks/<id>/meta.json
  tasks/<id>/brief.md
  tasks/<id>/events.log
  tasks/<id>/report.md       # Completed scout/local-only report
  decisions/<id>.json
  attention.json            # Bounded local-only view for the Hermes hook
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
python3 scripts/check_ambient.py
git diff --check
# Explicit opt-in, with a privately supplied existing MCP token:
python3 scripts/smoke.py --url http://127.0.0.1:3773/mcp --token-env T3_ORACLE_MCP_TOKEN
```

The unittest suite uses a fake MCP HTTP server and disposable Git repos.
It covers spawn/worktree contracts, model settings, lost-response recovery,
poll determinism, input requests, guard refusals, approved merge recording,
report capture, restart recovery, JSON/SSE sessions and token redaction.
The live script launches only a trivial scratch worker, checks repeat-poll silence,
captures its report, settles it and reads T3 back. It does not merge anything.
The original v1 launch/settle smoke output is in [evidence/oracle-smoke.txt](evidence/oracle-smoke.txt).
Project-discovery coverage also checks title matches, local/SSH origin lookup,
ordered preference layers, size and secret refusals, and config append safety.
The read-only oracle project-list smoke is recorded in
[evidence/oracle-project-list.txt](evidence/oracle-project-list.txt).

Design references only: [AXI's ten principles](https://axi.md/),
[Firstmate vision](https://github.com/kunchenguid/firstmate/blob/main/VISION.md),
and [Firstmate contract sections 1, 7, 8, 9](https://github.com/kunchenguid/firstmate/blob/main/AGENTS.md).
No Firstmate scripts or supervisor architecture are ported.
