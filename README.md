# boss

`boss` is a stdlib Python CLI for an agent to supervise T3 Code workers. It can
spawn, watch, steer and land workers while keeping a durable local ledger.
The CLI makes no LLM calls. One Hermes session owns boss at a time; the Hermes
plugin listens to T3's push stream and wakes only that session when a worker
needs input or approval, finishes, or fails. Nothing polls and nothing runs on a
timer; only a worker or a Hermes agent that wakes consumes model tokens.

## Install and first run

Requires Python 3.12+, Git, authenticated `gh`, Hermes, and T3 Code with its
HTTP MCP endpoint enabled. Linux and macOS are supported.

```sh
git clone https://github.com/zzzubair/hermes-boss.git
cd hermes-boss
./install.sh
export PATH="$HOME/.local/bin:$PATH"
# Put the T3 MCP token in T3_MCP_TOKEN in your environment or $HERMES_HOME/.env.
# For pushed wakes, put a read-only T3 session token in T3_EVENTS_TOKEN the same way:
#   t3 auth session issue --scope orchestration:read --label boss-events --ttl 365d --token-only
boss init
hermes plugins enable boss
# Then type /boss in the Hermes chat that should own boss and get its wakes.
boss doctor
boss project list
printf '%s\n' 'Inspect the project and report what you find.' |
  boss spawn my-project --title 'First scout' --kind scout
```

`init` probes `http://127.0.0.1:3773/mcp`, performs an MCP handshake and lists
projects before saving a minimal config with a `local` host and `default_host`.
There are **no built-in hosts**. To use another endpoint or token variable:

```sh
boss init --url https://coding.example.com/mcp --name workstation --token-env MY_T3_TOKEN
```

All setup flags are non-interactive. Repeating plain `init` leaves an existing
host configuration unchanged. `--force` permits updating the named host and `default_host`, while
preserving other hosts, projects and roles. Failed probes leave config unchanged.
Existing configs can instead use `boss host add` followed by `boss config set default_host`.

The installer links this checkout into `~/.local/bin/boss`,
`$HERMES_HOME/plugins/boss`, `$HERMES_HOME/skills/boss` and
`~/.agents/skills/boss`. Keep the clone at its installed path.
The install changes no Hermes configuration and creates no cron job.
The plugin remains opt-in through `hermes plugins enable boss`.

`HERMES_HOME` defaults to `~/.hermes`. To install into a profile or custom paths,
export the corresponding variables when installing **and when running boss**:

| Environment variable | Installer flag | Default |
| --- | --- | --- |
| `HERMES_HOME` | `--hermes-home` | `~/.hermes` |
| `BOSS_BIN_DIR` | `--bin-dir` | `~/.local/bin` |
| `BOSS_PLUGINS_DIR` | `--plugins-dir` | `$HERMES_HOME/plugins` |
| `BOSS_SKILLS_DIR` | `--skills-dir` | `$HERMES_HOME/skills` |
| `BOSS_AGENT_SKILLS_DIR` | `--agent-skills-dir` | `~/.agents/skills` |
| `BOSS_PYTHON` | none | `python3`, for installation |

`boss doctor` prints one line per check with a fix hint on failures. Required
checks cover Python, GitHub authentication, configured hosts and credentials,
MCP project access, SSH destination resolution, plugin installation/enabling,
and the Hermes skill. It also reports who owns boss and whether the last wake
reached them. Once a session owns boss, gateway consent, each host's push stream
token and a live listener are required too. SSH resolution does not prove remote
Git access. Keep custom paths discoverable by Hermes as well as by boss.

## Hosts and credentials

```sh
boss host add workstation --url http://127.0.0.1:3773/mcp --token-env MY_T3_TOKEN
boss host add remote --url https://coding.example.com/mcp --ssh build-host
boss host list
boss host test workstation
boss config set default_host workstation
boss host remove remote
```

`host test` performs the MCP handshake and lists projects. `--token-env` defaults
to `T3_MCP_TOKEN`; `--ssh` is an optional SSH destination for Git inspection on
the worker machine. Without SSH, Git commands run locally. `host remove` clears
a matching `default_host`; references in projects or roles must be removed first.
Host additions refuse duplicates. Use `config set hosts.<name>.url` to edit one.

Tokens come from the named environment variable, then `$HERMES_HOME/.env`.
The dotenv reader accepts literal `KEY=value` lines and quoted values. It never
sources a shell or evaluates commands. Tokens are never stored in config.
Explicit host credentials, including overridden dotenv values, are registered
with the secret redactor. URL credentials, queries and fragments are forbidden;
plain HTTP is limited to loopback. Use HTTPS or an SSH tunnel for remote MCP.

## Layered configuration

The config file is selected in this order:

1. If `BOSS_HOME` is set, `$BOSS_HOME/config.toml`.
2. Otherwise `$XDG_CONFIG_HOME/boss/config.toml`, with XDG defaulting to `~/.config`.
3. If the XDG file does not exist, an existing `~/.boss/config.toml`.

A fresh install writes the XDG file. Config files are selected, not combined.
The ledger stays at `$BOSS_HOME` or `~/.boss`, independently of XDG config.
Tasks, decisions, project notes, goals and append-only steers keep their
existing locations and formats.

Effective settings come from built-in defaults, the user file, a project's
settings, the selected role, and explicit CLI flags. Defaults are
`delivery = "direct-PR"`, `merge = "approved"`, `kind = "ship"`,
`no_mistakes = false`, with no default host, role or worker rules.
Unknown keys and invalid types fail with the full key path, including settings
for projects that are not currently being spawned.

```toml
default_host = "local"
default_worker = "builder"
merge = "approved"

[hosts.local]
url = "http://127.0.0.1:3773/mcp"
token_env = "T3_MCP_TOKEN"

[workers.builder]
model = "codex:gpt-6.1-sol"
effort = "high"
kind = "ship"

[workers.ui]
model = "claude:opus-5.5"
effort = "high"

[workers.scout]
model = "codex:gpt-6.1-sol"
effort = "low"
kind = "scout"

[projects.my-project]
worker = "builder"
# Optional project_id bypasses title matching. Repository bypasses Git origin lookup.
# project_id = "<T3 project ID>"
# repository = "owner/repo"
merge = "never"
delivery = "direct-PR"
test_host = "workstation"
no_mistakes = true

[projects.my-project.workers.ui]
effort = "xhigh"
```

Provider instance IDs and model options must exist on the selected host. The
examples above are illustrative; unsupported effort values fail before launch.
The legacy `[projects.<name>.model_selection]` table with `model`, `instanceId`
and an `options` table is still accepted. Live T3 project defaults apply when
no model is configured.

```sh
boss config show --origin
boss config show --origin --project my-project
boss config show --origin --project my-project --worker ui --effort high
boss config get projects.my-project.merge
boss config set projects.my-project.merge never
boss config unset projects.my-project.merge
```

`show` prints effective keys and the stored host, role and project tables.
`--origin` identifies built-in defaults, the config file, the project or role
block, or CLI flags for each key. `--project` selects the project layer;
without it, only global settings and a global default role are effective.
`get` reads effective global defaults or a dotted stored key. `set` accepts
TOML literals or bare strings. Quote a component containing dots, for example
`boss config get 'projects."my.project".merge'`. Editing with `set`, `unset`,
or host commands rewrites TOML formatting and comments. `project add` preserves
the existing file and appends its new block.

## Worker roles

Role choice is `--worker`, then the project `worker`, then global
`default_worker`, then T3's current default behavior. `--kind scout` without
`--worker` selects the `scout` role when defined. The explicit `--kind` still
wins over a role's kind.

For a selected role, each field resolves in this order:

```text
[workers.<role>]
[projects.<project>.workers.<role>]
explicit --model / --effort / --host / --kind
```

Project roles merge field by field and may exist without a global definition.
An unknown role fails with the available roles for that project. Both
`config show --origin --project <name>` and `project show <name>` show the
selected role's effective model, effort, host and kind with their origins.
The task metadata records the resolved `worker` name.

```sh
boss spawn my-project --worker ui --title 'Polish navigation' --brief-file brief.md
boss spawn my-project --worker ui --model claude:opus-5.5 --effort high --title 'Review UI' --brief-file brief.md
```

Bare model IDs remain accepted on `spawn --model` when unambiguous. Stored
`model` values use `instance:model`. Boss checks the merged selection against
the host's real model catalog, sends exact options, and verifies the returned
selection. A mismatch requests an interruption and reports the task for review.

## Worker rules

Rules the captain sets once are enforced by `boss spawn`, not left to notes.
Nothing is restricted until you add them.

```toml
[workers.astra]
kind = "scout"
kinds = ["scout"]          # astra may only run report-only scout tasks

[rules]
ship_models = ["claudeAgent:claude-opus-5-5"]
scout_models = ["claudeAgent:claude-opus-5-5", "codex:gpt-6.1-sol"]

[projects.my-project.rules]
scout_models = ["codex:gpt-6.1-sol"]   # replaces only the global scout list here
```

`kinds` is a role field and merges like the other role fields. `ship_models`
and `scout_models` list `providerInstanceId:model` entries; a project list
replaces only the same key from `[rules]`. Spawn resolves the model through the
live catalog first, so bare `--model` IDs, role models and T3 project defaults
are all checked. With a list set, an unknown model (T3 picks its default) is
refused too. An empty list refuses every spawn of that kind.

A spawn that breaks a rule exits 2 before anything is written or launched,
names each broken rule with its origin, and suggests a fix. When the captain
explicitly allows it, add `--override "<captain's exact words>"`; the words and
the broken rules are saved on the task under `override` and shown by
`boss status <id>`. `--override` on a spawn that breaks no rule is a usage error.

`config show --origin`, `project show` and `boss brief` (on stderr, so the
preview still matches the sent brief) print the effective rules. Rules are
never sent to workers. `boss doctor` checks rule models against each host's
live catalog, flags roles whose own model or default kind breaks the rules,
and warns about empty lists.

## Project policy and worker briefs

`merge` is supervisor policy: `never` blocks `boss land`, `approved` requires
captain approval, and `auto` records the supervisor's intended workflow.
**Every `boss land` still requires `--approved` and all existing guards.**
Workers never merge or deploy, regardless of policy. The deprecated
`auto_merge = true` maps to `merge = "auto"`, and false maps to `"approved"`,
with a warning. Supplying both keys in the same block is an error.

`delivery` is `direct-PR` or `local-only`. Direct-PR ship workers commit, push
and open exactly one PR against the origin default branch. Local-only workers
commit locally and return a `BOSS_REPORT:`. Scouts investigate and return a
report with a clean worktree. `test_host` and `no_mistakes` are supervisor
preferences; include any relevant instructions in the task's brief.

Every worker asks questions only through its harness's question tool, which
T3 turns into an input request (`AskUserQuestion` in Claude Code,
`request_user_input` in Codex), then stops until answered. `boss.events.classify`
reads a task snapshot and says what it needs: `input`, `approval`,
`finished_pr` (the final reply names a linked PR), `finished_report`,
`error`, `working`, or `ended_unclear`. A turn that ends with none of these
is `ended_unclear` and quotes the first 300 characters of the last reply,
because it is probably a question asked in plain text. Each event has a
`dedupe_key` (thread, kind, and turn or request IDs), so one event wakes the
captain once. `approval` needs a real pending permission request: T3's
`pendingRequestCount` minus the question requests it lists. A bare `waiting`
status with nothing pending is `working`; T3 shows it for a moment as a turn
finishes.

Each worker gets its own worktree on a branch named after its title:
`boss/fix-the-ledger-lock` (lowercase letters, digits and hyphens, at most 40
characters, cut at a whole word). A `-2`, `-3` suffix is added only when origin
or the ledger already has that branch. The T3 thread title and the ledger keep
the task ID; `land` and `settle` match the branch saved on the task.

## Writing briefs

The supervisor writes each task's brief and resolves conflicting instructions.
Keep codebase rules in the repo's `AGENTS.md`. Keep supervisor notes in
`$BOSS_HOME/projects/<p>.md`; `boss project show <p>` prints them under
"Supervisor notes (not sent to workers)". Boss never sends these notes to workers.

`boss brief <project> [--worker ROLE]` previews spawn's message with `<task>` in
place of the task text, followed by the goal, active steers and fixed contract.
`--origin` annotates context sources. The preview writes nothing and needs no T3
connection. The composed brief has a 115,000-character limit; secret-bearing
goals and steers are refused, and task text is redacted.

## Commands and guards

```sh
boss                            # live fleet and open decisions
boss project list
boss brief my-project --origin
boss project show my-project
boss project add my-project --host local --merge approved --worker builder
boss goal my-project 'The project goal'
boss steer my-project 'Keep the change portable' --task <id>
boss steer list my-project
boss steer my-project 'New direction' --replaces <steer-id>
boss steer drop my-project <steer-id>
boss steer list my-project --all
boss spawn my-project --title 'Task' --brief-file brief.md --fits 'How this fits the goal and active steers'
boss status <id> --full
boss send <id> 'Additional instruction'
boss answer <id> <request-id/0> 'Answer text'
boss stop <id>
boss decide add 'Captain decision needed' --task <id>
boss decide list
boss decide ask                 # number open decisions 1..N for the captain
boss decide close 1 'yes'       # by number from the latest round, or by id
boss decide unclear 2 'Which name did he mean?'
boss away 'going to bed'        # also /away or /afk in the Hermes chat
boss log 'What he said or decided in a check-in'
boss back                       # also /back: catch-up timeline and decisions
boss land <id> --approved 'The captain exact words'
boss settle <id>
boss settle --prune-landed      # remove leftover worktrees of settled, landed tasks on all hosts
boss own                        # also /boss: open boss in this Hermes session
boss own --take                 # also /boss take: move it here from another session
boss own status
boss release                    # also /boss release: nobody owns boss; wakes are held
```

Projects can be discovered by exact T3 title without registration. Zero or
multiple matches fail with candidates. Registering saves the host, T3 ID and
GitHub repository, plus optional policy. Git origin is resolved on the actual
worker host. Explicit registry values take precedence over discovery.

Record every captain steer before dispatching work. `send` alone does not
record a project steer. `--replaces` supersedes an earlier steer without
rewriting its history. `spawn` requires a nonblank, single-line `--fits` when
a project has a goal or active steers. It saves the explanation with the task.
Boss does not ask an LLM to judge it; the captain resolves unclear choices.
Spawn also refuses tasks that break the [worker rules](#worker-rules) unless
`--override` carries the captain's exact words.

### Away, back and numbered decisions

Only `/away` (alias `/afk`, optional note) and `/back` change whether the
captain is around. The Hermes plugin registers them and runs the same
`boss away` and `boss back` commands. Any other message in between is a
check-in and never changes that state. While he is away, the context hook adds
one line telling the supervisor not to ask him anything, to act on its own
judgement and to queue decisions for `/back`. `boss decide ask` is refused
while he is away unless he asked for the list (`--requested`). Away never
weakens what he says: a decision close, steer, land or settle from a check-in
has full authority. Record the rest of what he says with `boss log`.

`/back` prints, without any model call, how long he was away and every ledger
event since then in time order: workers started, finished, failed, stopped,
landed and settled, PRs opened, steers, decisions opened and closed, and
check-in log entries. Long lists are grouped by hour. It then shows the open
decisions as a new numbered round. That text carries no task IDs or SHAs.
`/away` twice keeps the first time; `/back` when he is not away says so and
still lists decisions.

`boss decide ask` records each round, so the captain can reply "1 yes 3 no".
`decide close` and `decide unclear` take a number from the latest round or a
decision ID. `--round <n>` names the round he answered; older rounds are
refused. Unanswered decisions stay open and come back marked "asked again".
`decide unclear` adds what was unclear to the next round. Answered decisions
never come back. Bare `boss` shows presence and the latest round's numbers.

### Retracting a steer

`boss steer drop <project> <steer_id>` retracts an active steer without new
text. It appends a tombstone; earlier log lines stay unchanged. Dropped steers
leave future worker briefs and the dispatch gate. `steer list <project> --all`
marks them `dropped`, distinct from `superseded`, and shows the tombstones.
Unknown, other-project, inactive and tombstone IDs are refused with a `next:` hint.
`list` and `drop` are reserved verbs in the first position. For a T3 project
with either title, set `boss config set projects.alias.project_id '<T3 project ID>'`
and use `alias` for all boss commands, including goals and steers.

`answer` accepts the request ID or the exact `request-id/question-id` printed
by `status`, including `/0`. A suffix must identify a real question in that
pending request. Requests with multiple questions still require a JSON object
keyed by all question IDs. Pending permission requests remain in T3's approval
UI; `answer` cannot approve them.

Landing checks a terminal worker, no pending inputs, exactly one linked PR,
the recorded repository and branches, green CI, a clean merge state, no draft
or changes-requested review, and the captain's explicit quote. It matches the
approved head SHA and confirms the merged result. `settle` checks live thread,
PR and worktree evidence before unwatching and settling. It refuses unlanded
work and missing reports. Use `--discard-approved` only with explicit captain
authority. Settlement keeps the ledger. Once the thread is settled, boss
removes the worker's worktree and local branch on its host (over its `ssh`
when remote) if the work is done: a merged or closed PR, or a finished scout
or report. The host re-checks right before removing and refuses, keeping the
worktree, when it has uncommitted work or commits origin does not have (not in
the default branch, not on the pushed branch, not a PR's head). Each refusal is
printed with its reason. `boss settle --prune-landed` does the same for every
already-settled task on every host, reading old tasks' worktree paths from T3.

Launch intent is journaled before the network mutation. An uncertain launch is
reconciled by its unique title marker, never blindly retried. Commands serialize
ledger writes; metadata is recovered from the append-only journal. `status`
acknowledges completed-run reminders, without approving merges or discards.

Exit codes: 0 success, 1 usage/config, 2 guard refusal, 3 transport/external/data
failure. Every command has `--help` and actionable errors.

## Ownership and wakes

boss is inactive until a Hermes session opens it. Type `/boss` in any Hermes
chat (Discord, Telegram, the Hermes app or TUI, the CLI, T3) to make it the
owner. Exactly one session owns boss at a time:

- `/boss` with nobody owning it claims it here and prints a catch-up: what
  changed since anyone last looked, then the open numbered decisions (as
  `/back` does). Run it again in the owner session for a fresh catch-up.
- `/boss` in another session refuses: `boss is owned by Discord DM since 14:02.
  Run /boss take (or boss own --take) to move it here.`
- `/boss take` moves it here and tells the previous session in one line.
  `/boss release` leaves nobody owning it. `/boss status` shows the owner.

Claims, transfers and releases are logged and appear in the `/back` timeline.
The owner record is `$BOSS_HOME/owner.json` (0600). The supervisor agent uses
the CLI: `boss own [--take]`, `boss own status`, `boss release`. Hermes gives
tool subprocesses their session in `HERMES_SESSION_KEY`. Any command that
changes boss (spawn, send, answer, stop, land, settle, steer, goal, decide,
away, back, log, release, config/host/project edits) exits 2 from a session
that does not own boss, naming the owner and how to take over. Read-only
commands (status, show, list, brief, doctor) always work. A human shell
without `HERMES_SESSION_KEY` may change boss but gets a one-line warning when
a session owns it.

### How a wake reaches the owner

```text
T3 server --push--> listener in the Hermes gateway/app --one line--> owner session
          (orchestration.subscribeShell over T3's WebSocket, one per host)
```

The listener is a thread inside an already-running Hermes host process (the
messaging gateway, the Ink TUI backend, or the desktop/web app). It blocks on
T3's push stream and costs no CPU or tokens while nothing happens. Pushes
that say a worker is still running are dropped unread. When a task's thread
changes status, run or pending request, that push triggers one status read and
`boss.events.classify` decides what it means. Anything but `working`
(input, approval, finished with a PR or report, an unclear end, an error) is
held once in `$BOSS_HOME/wakes.jsonl` under its `dedupe_key`. It is then injected
as one user line, `[boss wake] <task> (<title>): finished with a PR: <url>`,
into the owner session with `ctx.inject_message(session_key=owner)`. Hermes
routes the key to the TUI/desktop host first, then the messaging gateway. PR
check results arrive the same way: T3's `watch_pull_request` wakes the worker,
and its new turn is pushed like any other.

One event wakes the owner once, including across restarts and T3 re-pushes.
With no owner, or an owner that refuses the injection (offline, app closed),
the wake stays held on disk and shows in the owner's `boss:` context line. It
is delivered with the next pushed event, the moment a session claims or takes
boss, or on the owner's next turn. Exactly one listener runs per machine: it
holds `$BOSS_HOME/wake.lock`, and any other Hermes process waits on that lock
without polling, taking over if the holder exits. A dropped stream reconnects
with backoff, and T3's fresh snapshot covers the gap. Errors go to
`$BOSS_HOME/wake-errors.log`, redacted.

Setup, once per profile:

1. Mint a read-only T3 session token for the push stream. T3 keeps MCP
   client tokens off its WebSocket, so this is a second token:
   `t3 auth session issue --scope orchestration:read --label boss-events --ttl 365d --token-only`.
   Put it in `T3_EVENTS_TOKEN` (env or `$HERMES_HOME/.env`), or name another
   variable with `hosts.<name>.events_token_env`.
2. Allow plugin injection: add `allow_gateway_injection: true` under
   `plugins.entries.boss` in that profile's `config.yaml`
   (`plugins.entries.boss.allow_gateway_injection: true`). Setup never edits
   Hermes config.
3. Enable the plugin, restart the gateway (or app), and type `/boss` where you
   want wakes. `boss doctor` shows the owner, whether the last wake reached it,
   each host's push stream, and which process holds the listener.

Set `wake.enabled = false` to turn wakes off. Older configs may still contain
`wake.interval_seconds` (ignored; doctor suggests removing it) and a fixed
`wake.origin`/`wake.session_key` target, which is used only while no session
owns boss, as a migration fallback. Run `/boss` once and unset it.

## MIGRATION

Built-in hosts have been removed. Before upgrading an existing setup, add an
explicit host block **for every host name referenced by your tasks and projects**:

```toml
default_host = "my-existing-host"

[hosts.my-existing-host]
url = "http://127.0.0.1:3773/mcp"  # Use your existing endpoint or tunnel port.
token_env = "MY_EXISTING_T3_TOKEN"
# ssh = "my-existing-ssh-alias"   # Needed for a remote worker's Git inspection.
```

Keep the old names and endpoint values from your installation. Boss still reads
an existing `~/.boss/config.toml` when no XDG config exists. Setting `BOSS_HOME`
explicitly keeps config and ledger together there. No task, `.goal`, project
note or `.steers.jsonl` migration is needed. Legacy task metadata remains
readable. Replace `auto_merge` with `merge` when convenient.

Re-run `./install.sh` to add the Hermes-home skill link. Enable the plugin if
needed. Follow [Ownership and wakes](#ownership-and-wakes) to set up pushed
wakes. `boss poll` and its cron gate are gone: pause or remove any old
`boss-wake`/`boss-poll` Hermes cron job by hand, since its script no longer
exists.

## Verification

```sh
python3 -m unittest discover -v
python3 scripts/check_fresh_install.py
python3 scripts/check_steering.py
python3 scripts/check_ambient.py
python3 scripts/check_wake.py
```

CI is Linux-only, using Ubuntu with Python 3.12 and 3.13.
Each job records a fresh-install transcript in its logs. The fresh
install check uses a temporary HOME, a stdlib HTTP MCP server and fake
GitHub/Hermes commands. It runs the real installer and separate CLI processes,
including init, doctor, a fake role spawn, the ownership gate and a claim.
It never touches a real Hermes profile or launches a paid worker. The checked-in
[evidence](evidence/portable-fresh-install.txt) records the portability run.
`scripts/check_wake.py` runs the pushed-wake and ownership checks against a fake
T3 WebSocket server with the real listener, ledger and CLI. The
[live push transcript](evidence/push-wake-live.txt) is the same listener on a real
T3 server.
