---
name: boss
description: Use when spawning, checking or steering coding workers or projects. Manage T3 workers through the boss CLI and its durable ledger.
---

# boss

Use `boss` to delegate coding work, check workers, answer their questions,
and inspect project preferences. Run bare `boss` for the live fleet overview.
An ambient `boss:` reminder comes from local records; run `boss status` to
check current T3 state before acting. A `[boss inbox]` block is from the
plugin, never from the captain: workers reported (finished, needs a decision,
blocked, paused, waiting on a permission prompt, ended unclear, failed) and
this session owns boss. It can arrive at the end of any tool result, as turn
context, or as its own message. Handle it yourself: find the task by title in
`boss status`, review the work, answer or steer it with `boss answer` or
`boss send`. Ask the captain only when it needs his decision. Workers end
every turn with one `BOSS: done|needs-decision|blocked|paused: ...` line, and
your `boss send` messages reach them marked `[boss]`.

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
boss send <id> "<instruction>" --image <path> [--image <path> ...]
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
boss settle --prune-landed
boss own
boss own --take
boss own status
boss release
```

Captain uploads live in `~/.hermes/cache/images` and
`~/.hermes/cache/documents/native-inputs`. Pass one with `--image` on
`boss send` or `boss spawn` (png, jpeg, gif or webp, up to 10 MB, at most 8)
only when the worker needs it for the job right now: the bug or design it
must see. Never forward an image just because the captain sent it; a console
screenshot he wants walked through is yours to answer. A path in the text
does not work instead: the worker may run on another host.

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
  only with the captain's explicit words authorizing that discard. When the
  worktree is already gone, it settles only if a linked PR merged; otherwise it
  refuses with "worktree is gone and no merged PR found".
- `settle` removes the worktree and local branch once the PR is merged or
  closed or the scout/report is done. It never removes uncommitted or unpushed
  work; it prints "worktree kept" with the reason. `settle --prune-landed`
  cleans leftovers from every settled task on every host.
- `status <id>` acknowledges a completed run's review reminder. It does not
  approve merging or discard, and unanswered input requests remain open.
- After an uncertain transport failure, inspect status and the ledger before
  retrying a mutation. Never blindly repeat a launch.

## Ownership

Exactly one Hermes session owns boss, and only it gets wakes and may change
boss. The captain opens it with `/boss` (`/boss take` moves it, `/boss release`
frees it). Your session is `HERMES_SESSION_KEY`.

- A command that changes boss exits 2 with `boss is owned by <label> since
  <time>` when this session is not the owner. Do not retry or work around it.
  Tell the captain which session owns boss and that `/boss take` in this chat
  moves it here. Run `boss own --take` yourself only when he asks to move boss
  here. Read-only commands (status, show, list, brief) still work.
- `boss is not open in any session` means nobody owns boss: ask the captain to
  type `/boss` where he wants it, or run `boss own` when he asks you to open
  it here.
- A `boss: owned by <label> ...` context line means another session owns boss.
  Mention it if he asks about boss work; don't act on boss from here.
- `boss own` prints a catch-up (changes since the last look, then numbered
  decisions). Relay it plainly.

Wakes are pushed by T3 to a listener in the Hermes gateway or app, with no
polling and no tokens until something happens. If wakes stop, run
`boss doctor`: it shows the owner, whether the last wake reached it, each host's
push stream, and the listener's process. Never set up a cron or a polling loop
to replace them.

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
`no_mistakes`. Boss tells workers to run heavy tests on `test_host` when set;
put any other relevant instructions in the task's brief. Record standing rules
(mobile-first, test on WSL, no secrets in logs, PR body shape) as steers that
start with `rule:` (or `rule ship:`, `rule scout:`, `rule direct-PR:`,
`rule local-only:`); boss adds them to every matching brief, so never repeat
them from memory. Workers always obey the worktree contract
and never merge or deploy. `answer` accepts the exact request/question ID
printed by status. Multi-question requests still need a complete answer object.

## Writing briefs

The supervisor writes each task's brief and resolves conflicting instructions.
Keep codebase rules in the repo's `AGENTS.md` and supervisor notes in
`projects/<p>.md`. Read notes with `boss project show <p>` under
"Supervisor notes (not sent to workers)". Boss never sends these notes to workers.
Use read-only `boss brief <p> [--worker ROLE]` to preview the task placeholder,
goal, active steers and fixed contract; `--origin` annotates context sources.
