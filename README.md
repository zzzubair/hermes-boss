# boss

`boss` is a stdlib Python CLI for an agent to supervise T3 Code workers. It can
spawn, watch, steer and land workers while keeping a durable local ledger.
The CLI makes no LLM calls. An optional Hermes plugin thread polls for changes
and wakes the captain's existing chat. `poll` uses MCP and GitHub evidence;
only a worker or a Hermes agent that wakes consumes model tokens.

## Install and first run

Requires Python 3.12+, Git, authenticated `gh`, Hermes, and T3 Code with its
HTTP MCP endpoint enabled. Linux and macOS are supported.

```sh
git clone https://github.com/zzzubair/hermes-boss.git
cd hermes-boss
./install.sh
export PATH="$HOME/.local/bin:$PATH"
# Put the T3 MCP token in T3_MCP_TOKEN in your environment or $HERMES_HOME/.env.
boss init
hermes plugins enable boss
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
host configuration unchanged. A wake-only config from the installer still gets
its first host on `init`. `--with-wake` updates only wake settings when hosts already exist.
`--force` permits updating the named host and `default_host`, while
preserving other hosts, projects and roles. Failed probes leave config unchanged.
Existing configs can instead use `boss host add` followed by `boss config set default_host`.

The installer links this checkout into `~/.local/bin/boss`,
`$HERMES_HOME/plugins/boss`, `$HERMES_HOME/skills/boss` and
`~/.agents/skills/boss`. Keep the clone at its installed path.
The default install changes no Hermes configuration and creates no cron.
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
and the Hermes skill. When plugin wake is requested, target and gateway consent
are required too. Doctor reports an active legacy wake cron alongside plugin
wake as a failure. Wake is optional otherwise. SSH resolution does not prove remote
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
`no_mistakes = false`, with no default host or role.
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
boss decide close <decision-id> 'Captain resolution'
boss land <id> --approved 'The captain exact words'
boss settle <id>
boss poll
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
authority. Settlement preserves the worktree and ledger.

Launch intent is journaled before the network mutation. An uncertain launch is
reconciled by its unique title marker, never blindly retried. Commands serialize
ledger writes; metadata is recovered from the append-only journal. `status`
acknowledges completed-run reminders, without approving merges or discards.

Exit codes: 0 success, 1 usage/config, 2 guard refusal, 3 transport/external/data
failure. Every command has `--help` and actionable errors.

## Hermes wake-up

The plugin can wake the supervisor as a normal user turn inside the captain's
existing gateway chat. Hermes reuses the chat's history and normal reply path,
including its busy-session queue. There is no separate cron session or
`[Cron delivery: boss-wake]` wrapper.

Configure one target, either an existing gateway routing key or a chat origin:

```sh
boss init --with-wake --wake-session-key '<existing gateway session_key>'
# Or, including the captain's authorized platform user ID:
./install.sh --with-wake --wake-origin '{"platform":"telegram","chat_id":"123456","user_id":"123456"}'
# Optional on either command: --wake-interval 60
```

Both commands save `[wake]` in the selected boss config and print the consent
setting. They never edit Hermes `config.yaml`, enable plugins, or modify cron
jobs. `--with-wake` without a target saves the defaults but starts no thread.
Re-running it preserves a saved target and interval unless a flag replaces them.

Add this to the captain's **Hermes profile** config, merging into its existing
`plugins` block, then enable boss and restart the gateway:

```yaml
plugins:
  entries:
    boss:
      allow_gateway_injection: true
```

The exact setting is `plugins.entries.boss.allow_gateway_injection: true`.
Run `boss doctor` using the same `HERMES_HOME`, `BOSS_HOME` and XDG settings as
the gateway. Hermes still checks the target's platform connection, profile
route and current user allowlist. Choose an authorized captain `user_id`.
Plugins can inject only within their own Hermes profile. Acceptance means
queued for dispatch, not confirmed delivery; later routing failures appear
in the Hermes gateway log.

An equivalent boss config is:

```toml
[wake]
enabled = true
interval_seconds = 300
origin = { platform = "telegram", chat_id = "123456", user_id = "123456" }
# Alternatively, remove origin and set session_key = "<existing routing key>".
```

`interval_seconds` must be a positive integer. Set `wake.enabled = false` to
disable plugin wake while keeping its target. The target is global boss
configuration, separate from project and worker preferences. Restart the
gateway or reload the plugin after changing wake settings.

### Finding the target

Send a message in the captain's intended chat first, then inspect that
profile's local routing index with this read-only command. It prints routing
metadata, never conversation transcripts. Do not infer routing keys from the
agent's session ID or prompt, which may omit or redact chat identifiers:

```sh
python3 - <<'PY'
import json, os, pathlib, sqlite3
home = pathlib.Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
db = home / "state.db"
if db.exists():
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        entries = [(key, json.loads(value)) for key, value in
                   connection.execute("SELECT session_key, entry_json FROM gateway_routing")]
else:
    entries = json.loads((home / "sessions/sessions.json").read_text()).items()
for key, entry in entries:
    if not key.startswith("_") and entry.get("origin"):
        print(json.dumps({"session_key": key, "origin": entry["origin"]}))
PY
```

Pick the entry whose platform, chat and thread match the captain's chat. Use
its stable **routing key**, not its conversation/session ID. Or copy its origin
into `[wake]`, omitting null fields because TOML has no null value. Preserve
`thread_id`, `chat_type`, `scope_id` and `parent_chat_id` when present so a
threaded conversation stays in that thread. If you use a custom sessions
directory on an older Hermes, adjust the JSON fallback path above.

### Polling and failures

Loading the plugin in a gateway starts one daemon thread per profile and boss
home, guarded against plugin reloads. CLI and one-shot processes start none.
The loop waits for the gateway injector before running `boss poll` as a
subprocess with a 30-second timeout, then waits the configured interval.
The default interval is 300 seconds. No target means no thread.

A successful, nonempty poll injects a short `[boss wake]` summary and asks the
agent to run `boss status`. Quiet polls make no injections and use zero LLM
tokens. Poll's persisted `poll_observed` state suppresses repeats across CLI
processes and gateway restarts. A new completed run may have the same summary
text and still deserves a wake. The pre-LLM context hook continues to read only
the bounded local `attention.json` cache; it never polls or starts work.

Failed polls, timeouts and injector exceptions never become chat messages.
Errors go to `$BOSS_HOME/poll-errors.log`, default `~/.boss/poll-errors.log`,
with credential redaction. If injection returns `False`, the loop logs the
refusal once per load and keeps running. Poll changes are consumed before
injection and are **not retried** after refusal, timeout, or a crash between
poll and injection. Run `boss status` after fixing delivery to catch up.
There is no automatic failover that might double-wake the captain.

### Legacy cron fallback

Use this if your Hermes does not support gateway injection. Disable plugin
wake first with `boss config set wake.enabled false` and restart/reload the
gateway. `--with-cron` is available on both `install.sh` and `boss init`.

`boss poll --hermes-gate` is the bundled cron gate. When unchanged or on error,
it emits exactly `{"wakeAgent": false}` and exits 0. Errors are appended,
redacted, to `$BOSS_HOME/logs/poll.log`, or `~/.boss/logs/poll.log`. Otherwise it
emits changed actionable text. Plain `boss poll` retains its normal output and
error exits. A pass containing any error suppresses the wake for that pass.
Neither poll mode calls an LLM.

Opt in to the five-minute cron:

```sh
./install.sh --with-cron
```

This writes `$HERMES_HOME/scripts/boss-poll-gate.sh`, capturing the installed
Python path, checkout and explicit boss/XDG/Hermes homes. It creates this job
through Hermes, without `--no-agent`:

```sh
hermes cron create '*/5 * * * *' \
  'Run boss status, review changed workers and pending decisions, and report actionable items to the captain. Never invent merge approval.' \
  --name boss-poll --script boss-poll-gate.sh --deliver local
```

The same active gate job is not duplicated on reinstall, including an existing
`boss-wake` job. Keep the Hermes
gateway running for scheduled jobs. The gate skips the agent when quiet; a
change lets Hermes wake the agent with the actionable text. Agent work can
consume tokens in a separate cron session. The installer refuses `--with-cron`
while a plugin wake target is enabled. `boss doctor` detects both sources even
if a legacy job was installed separately. Do not run both: they compete for
changed-only poll state and can produce duplicate or missing notifications.

To migrate an existing cron, inspect `hermes cron list` and manually pause or
remove only the `boss-wake`/`boss-poll` job after configuring plugin wake and
consent. Run `boss doctor` before restarting the gateway. Installation never
changes or removes that existing job automatically.

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
needed. Follow [Hermes wake-up](#hermes-wake-up) to configure chat injection or
retain the legacy cron fallback. Inspect and change old cron jobs manually.

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
including init, doctor, a fake role spawn, and wake/quiet/error gate transitions.
It never touches a real Hermes profile or launches a paid worker. The checked-in
[evidence](evidence/portable-fresh-install.txt) records the portability run.
The [wake transcript](evidence/wake-verification.txt) exercises real poll
subprocesses with fake T3 and an injector, including restart dedupe and failures.
