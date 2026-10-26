---
name: boss
description: Use when spawning, checking or steering coding workers or projects. Manage T3 workers through the boss CLI and its durable ledger.
---

# boss

Use `boss` to delegate coding work, check workers, answer their questions,
and inspect project preferences. Run bare `boss` for the live fleet overview.
An ambient `boss:` reminder comes from local records; run `boss status` to
check current T3 state before acting. A `[boss wake]` message is a plugin
injection in this chat. Run `boss status`, review the changed work using the
live conversation context, and reply normally to the captain.

## Core commands

```sh
boss
boss init
boss doctor
boss host list
boss host test <host>
boss config show --origin --project <name>
boss project list
boss project show <name>
boss brief <project> --origin
boss brief <project> --worker <role>
boss project add <name> --host <host>
boss goal <project> "<project goal>"
boss goal <project>
boss steer <project> "<captain steer>" --task <id>
boss steer <project> "<replacement steer>" --replaces <steer-id>
boss steer drop <project> <steer-id>
boss steer list <project>
boss steer list <project> --all
boss spawn <project> --title "<task>" --brief-file <file>
boss spawn <project> --title "<task>" --brief-file <file> --fits "<how this fits the goal and active steers>"
boss spawn <project> --title "<investigation>" --kind scout --brief-file <file>
boss spawn <project> --worker <role> --title "<task>" --brief-file <file>
boss spawn <project> --worker <role> --title "<task>" --brief-file <file> --override "<captain's exact words>"
boss status <id>
boss status <id> --full
boss send <id> "<instruction>"
boss answer <id> <request/question-id> "<answer>"
boss stop <id>
boss decide list
boss decide add "<captain decision needed>" --task <id>
boss decide close <decision-id> "<captain resolution>"
boss decide ask
boss decide close <n> "<captain words>" --round <round>
boss decide unclear <n> "<what was unclear>"
boss away "<note>"
boss log "<what the captain said or decided in a check-in>"
boss back
boss land <id> --approved "<captain's exact words>"
boss settle <id>
```

Projects can be discovered by exact T3 title without registration. Use
`project show` to inspect effective preferences and contributing files.
Every command has `--help`; follow the next-step hint on errors.

## Goals and steers

Record every captain steer with `boss steer` before dispatching new work.
`--task` is optional and records the originating task; the steer still applies
to future tasks in the project. `boss send` alone does not record a steer.
For an existing worker, record the steer with `--task`, then send it.

Use `boss goal` to show or replace the project goal. Before spawning, read it
and `boss steer list`, then supply a one-line `--fits` explanation when either
has content. The goal and active steers are included in the worker brief,
inside the existing size and secret checks. `--fits` is saved on the
task. Reconcile unclear task choices with the captain.

Use `--replaces` when the captain changes an earlier steer. History stays
append-only; `--all` shows superseded records. Goals and steers are local and
can be recorded without registering a project or contacting T3.

### Retracting a steer

Use `boss steer drop <project> <steer-id>` to retract an active steer without
replacement text. It appends a tombstone and removes the steer from future
worker briefs and the dispatch gate. `--all` shows dropped and superseded
steers distinctly, plus tombstones. Inactive, unknown, other-project and
tombstone IDs are refused. `list` and `drop` are reserved verbs; for a T3
project with either title, set `boss config set projects.alias.project_id '<T3 project ID>'`
and use `alias` for all boss commands, including goals and steers.

## Captain away and back

Only the captain's `/away` (or `/afk`) and `/back` change his presence. Never
infer it from chat and never run `boss away` or `boss back` yourself unless he
used those words as a command. While a `boss: captain away` line is present:

- Don't ask him anything. Act on your own judgement and queue decisions with
  `boss decide add` for `/back`.
- Every message from him is a check-in with full authority. If he decides,
  approves or steers something, act on it exactly as if he were back
  (`decide close`, `land --approved`, `steer`, `settle`). Record anything else
  he says or decides with `boss log "<his words or a faithful summary>"`.

When he answers a numbered list ("1 yes 3 no"), close each with
`boss decide close <n> "<his words>" --round <round>`. Mark an answer you
don't understand with `boss decide unclear <n> "<what was unclear>"`; skipped
ones are asked again automatically. Use `boss decide ask` to show him a new
numbered round when he is around.

## Guards

- Workers work in their assigned worktree and never merge or deploy.
- `spawn` refuses with exit 2 when a project has a goal or active steers and
  lacks a nonblank `--fits` line. Read the full context printed by the refusal.
  The line must be at most 2000 characters and contain no line breaks.
- `spawn` refuses with exit 2 when a role's `kinds` excludes the task kind or
  the resolved model is not in the effective `rules.ship_models` /
  `rules.scout_models`. Follow the hint (another `--kind`, `--worker` or
  `--model`). Use `--override` only with the captain's exact words allowing that
  spawn; boss saves them on the task. Never add model rules to briefs or notes:
  ask the captain to set them in config so boss enforces them.
- `land` requires the captain's exact words and a green, eligible PR. Never
  invent approval or turn `merge = "auto"` into permission. Merge policy is for the supervisor;
  `merge = "never"` refuses landing and every other policy still requires the quote.
- `settle` refuses unlanded work or missing reports. Use `--discard-approved`
  only with the captain's explicit words authorizing that discard.
- `status <id>` acknowledges a completed run's review reminder. It does not
  approve merging or discard, and unanswered input requests remain open.
- After an uncertain transport failure, inspect status and the ledger before
  retrying a mutation. Never blindly repeat a launch.

## Waking in the captain's chat

Use `boss init --with-wake --wake-session-key '<gateway routing key>'` or
`./install.sh --with-wake --wake-origin '<SessionSource JSON>'`. Add
`--wake-interval <seconds>` to change the 300-second default. Without a target,
`--with-wake` saves defaults but no thread starts. Existing host/project settings
are preserved. Both commands print `plugins.entries.boss.allow_gateway_injection: true`;
the captain must add `allow_gateway_injection: true` under `plugins.entries.boss`
in that Hermes profile's config. Setup never changes Hermes config or old cron
jobs. Enable the boss plugin and restart/reload the gateway after configuration.

Read the target from the profile's `state.db` gateway routing index with the
read-only command in the repository README. Do not infer identifiers from
prompt metadata, which may be redacted. Use the stable `session_key`, not the session ID. An origin
needs platform/chat_id and an authorized captain user_id. Copy any thread,
chat_type and scope/parent identifiers; omit nulls in TOML. Hermes controls
profile routing and authorization. Never invent or silently select a chat.

One gateway daemon thread runs bounded `boss poll` subprocesses. The CLI and
one-shot processes start no thread. Boss makes no LLM calls. Quiet polls inject
nothing and cost zero model tokens. Poll's durable observed state prevents repeat
wakes, including after restart. The context hook still only reads local records.
Errors go to `$BOSS_HOME/poll-errors.log` redacted and never enter chat. Refusal
is logged once per load; consumed changes are not retried. After fixing delivery,
run `boss status` to catch up. A queued injection does not guarantee delivery;
check the Hermes gateway log for later routing failures.

Run `boss doctor` with the gateway's profile and boss environment. It checks
target, consent and competing wake cron jobs. For the legacy fallback, set
`wake.enabled false`, restart/reload, then use `./install.sh --with-cron` or
`boss init --with-cron`. Never enable both sources. Manually inspect and
pause/remove the old `boss-wake` or `boss-poll` job when migrating; do not
change a live job without the captain's authority. The fallback gate calls no
LLM, prints `{"wakeAgent": false}` when quiet/on error, and logs redacted errors
under `logs/poll.log`. Never use `--no-agent` for a waking cron.

## Portable settings and roles

`HERMES_HOME` selects the Hermes profile, including credentials, plugins and
skills. Config is `$BOSS_HOME/config.toml` if set, otherwise XDG
`~/.config/boss/config.toml`, falling back to an existing `~/.boss/config.toml`.
The ledger stays at `$BOSS_HOME` or `~/.boss`. No hosts are built in.

Role choice is `--worker`, project `worker`, global `default_worker`, then
current T3 behavior. `--kind scout` with no `--worker` selects the scout role
when present. Each project role merges over the global role field by field;
explicit model, effort, host and kind flags win. Read `project show` or
`config show --origin --project <name>` before dispatching; both show
the effective worker rules, and `boss brief` prints them on stderr. Unknown roles and
unsupported live model options fail loudly. The resolved role is saved on the task.

Keep policy in project settings: `merge`, `delivery`, `test_host`, and
`no_mistakes`. These are supervisor preferences; put any relevant instructions
in the task's brief. Workers always obey the worktree contract
and never merge or deploy. `answer` accepts the exact request/question ID
printed by status. Multi-question requests still need a complete answer object.

## Writing briefs

The supervisor writes each task's brief and resolves conflicting instructions.
Keep codebase rules in the repo's `AGENTS.md` and supervisor notes in
`projects/<p>.md`. Read notes with `boss project show <p>` under
"Supervisor notes (not sent to workers)". Boss never sends these notes to workers.
Use read-only `boss brief <p> [--worker ROLE]` to preview the task placeholder,
goal, active steers and fixed contract; `--origin` annotates context sources.
