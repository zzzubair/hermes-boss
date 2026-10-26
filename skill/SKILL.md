---
name: boss
description: Use when spawning, checking or steering coding workers or projects. Manage T3 workers through the boss CLI and its durable ledger.
---

# boss

Use `boss` to delegate coding work, check workers, answer their questions,
and inspect project preferences. Run bare `boss` for the live fleet overview.
Run `boss status` to check current T3 state before acting. A `[boss inbox]`
block is from boss, never from the captain: workers reported (finished, needs
a decision, blocked, paused, waiting on a permission prompt, ended unclear,
failed), or the captain went away or came back, and this session owns boss.
It arrives as the result of `boss watch` or at the end of any tool result.
Handle it yourself: find the task by title in `boss status`, review the work,
answer or steer it with `boss answer` or `boss send`. Ask the captain only
when it needs his decision. Workers end every turn with one
`BOSS: done|needs-decision|blocked|paused: ...` line, and your `boss send`
messages reach them marked `[boss]`.

## Watching the inbox

While this session owns boss, keep exactly one `boss watch` running as a
tracked background command (terminal with `background=true` and
`notify_on_complete=true`). It waits without polling until the inbox has
something, prints it, marks it delivered and exits; Hermes shows that exit to
you as a system notice, not as a message from the captain. Start it right
after `/boss` opens boss here. After you handle a watch result, start
`boss watch` again, once. Never run two: a new one replaces the old one,
which exits saying it took nothing (ignore that notice). `boss watch` from any
other session is refused. A watch that says boss moved to another session is
done; don't restart it here. A tool result, `/boss` or `boss own` ending with
`boss: no boss watch running; start it: boss watch (background, notify on exit)`
means none is running now: start one at once.

Bare `boss` (or `boss status`) prints one table: task, project, host, model,
state (the worker's last status line), PR and CI, how long it has been quiet,
and the next step. Scope it with `--project <p>` or `--skip <p>`; add `--ids`
only when you need ids for a command. A finished or failed task stays open
until `boss status <id>`, `boss send` or `boss settle` handles it; every watch
result ends with a `Still open: ...` line while any open item waits.
`boss: worker updates are not arriving` means the wake listener is down: run
`boss doctor`.

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
boss steer <project> "<captain's fence words>" [--task <id>] --fence
boss steer drop <project> <steer-id>
boss steer list <project>
boss steer list <project> --all
boss spawn <project> --title "<task>" --brief-file <file>
boss spawn <project> --title "<task>" --brief-file <file> --fits "<how this fits the goal and active steers>"
boss spawn <project> --title "<investigation>" --kind scout --brief-file <file>
boss spawn <project> --worker <role> --title "<task>" --brief-file <file>
boss spawn <project> --worker <role> --title "<task>" --brief-file <file> --allow-model "<captain's exact words>"
boss status --project <project> --skip <project> --ids
boss panel <project> --brief-file <file> --workers <role>,<role> [--judge <role>] [--rounds N --target <score>]
boss status <id>
boss status <id> --full
boss send <id> "<instruction>"
boss send <id> "<instruction>" --image <path> [--image <path> ...]
boss answer <id> <request/question-id> "<answer>"
boss stop <id>
boss decide list
boss decide add "<captain decision needed>" --task <id>
boss decide add "<merge/deploy/demo question>" --task <id> --action merge|deploy|demo
boss decide close <decision-id> "<captain resolution>"
boss decide ask
boss decide answer "<his whole numbered reply>" --round <round>
boss decide close <n> "<captain words>" --round <round>
boss decide unclear <n> "<what was unclear>"
boss decide detail <n> "<plain explanation for the next round>"
boss away "<note>"
boss log "<what the captain said or decided in a check-in>"
boss log "<his approving words>" --task <id> --approves merge|deploy|demo
boss back
boss back --full
boss land <id> --approved "<captain's exact words>"
boss land <id>
boss settle <id>
boss settle --prune-landed
boss own
boss own --take
boss own status
boss release
boss watch
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
used those words as a command (`boss back --full` is read-only and always
fine). His `/away` and `/back` replies are one-line
acks ("Got it. Away mode on since 22:10." and "Welcome back. Catching you
up."). Each also reaches you through the inbox: `Captain went away at 16:40
(note)` or `Captain is back at 17:10 after 3h 12m away (...)`. On `went away`,
don't send him another ack or any status; at most one short line. From
`Captain went away` until `Captain is back` (`boss` shows `captain: away`
too):

- Don't ask him anything. Act on your own judgement and queue decisions with
  `boss decide add` for `/back`.
- Every message from him is a check-in with full authority. If he decides,
  approves or steers something, act on it exactly as if he were back
  (`decide close`, `land --approved`, `steer`, `settle`). Record anything else
  he says or decides with `boss log "<his words or a faithful summary>"`.

### Catching him up on `Captain is back`

His `/back` reply only said "Welcome back. Catching you up." You write the
catch-up, right away:

1. Run `boss back --full`. It prints the raw timeline of his away window, the
   task tally and the open decisions. It is for you, not him.
2. Write a TL;DR in 3-5 plain bullets: what shipped, what broke, what is still
   running. Say it as outcomes ("The login fix is merged and live."), not
   events. No PR lists, no ids, no SHAs, no test counts unless something
   failed.
3. Run `boss decide ask`, then give the open decisions in that round's
   numbers, one per line, each in plain words with your recommendation:
   `1. Merge the docs rewrite? I'd say yes: CI is green and you liked the demo.`
   He answers "1 yes 2 no". With nothing open, say so in one line.

Show the raw timeline only when he asks for it ("what exactly happened?",
"full list"): then paste `boss back --full`.

When he answers a numbered list ("1 yes 3 no 5 more info"), pass his whole
reply to `boss decide answer "<his reply>" --round <round>`: answered numbers
close, "more info" ones are marked unclear, skipped ones stay open. Use
`decide close` / `decide unclear` for a single answer. Before the next round,
explain each unclear one in plain words with `boss decide detail`; `decide ask`
refuses until you do. Use `boss decide ask` to show him a new numbered round
when he is around.

Approval counts once. Ask about a merge, deploy or demo with
`decide add --task <id> --action <what>`; boss refuses to ask it again once he
answered. When he approves one in passing (also in an away check-in), record
it with `boss log "<his words>" --task <id> --approves <what>`. After a yes,
`boss land <id>` needs no `--approved`.

When he says "don't merge or deploy X until I say", or freezes merging, record
it with `boss steer <project> "<his words>" --fence` (add `--task <id>` for one
task). `land` refuses until he lifts it and you run `boss steer drop`.

## Guards

- Workers work in their assigned worktree and never merge or deploy.
- `spawn` refuses with exit 2 when a project has a goal or active steers and
  lacks a nonblank `--fits` line. Read the full context printed by the refusal.
  The line must be at most 2000 characters and contain no line breaks.
- `spawn` refuses with exit 2 when a role's `kinds` excludes the task kind or
  the resolved model and effort are not in the effective `rules.models.<role>`
  (or, without one, `rules.models.<kind>`). Follow the hint (another `--kind`,
  `--worker`, `--model` or `--effort`). Use `--allow-model` only with the
  captain's exact words allowing that one spawn; boss journals them on the task.
  If he wants it allowed for good, run the `boss config set` line the refusal
  printed. Never add model rules to briefs or notes:
  ask the captain to set them in config so boss enforces them.
- `spawn` refuses past `workers.max_active` unsettled workers (default 15).
  Settle finished workers; only the captain raises the cap.
- `land` refuses a PR body that breaks the pr-description shape or uses the
  configured `pr_body_forbidden` words; send the worker the listed problems.
- When the captain wants several models on one brief, or a judge score loop, use
  `boss panel`; never judge entries yourself or tell a judge which model wrote what.
  Boss starts the judge and the revisions; you get one inbox line per judged round.
- `land` requires the captain's exact words (or his saved yes) and a green, eligible PR. Never
  invent approval or turn `merge = "auto"` into permission. Merge policy is for the supervisor;
  `merge = "never"` refuses landing and every other policy still requires the quote.
- With `fast_land_docs = true`, a PR touching only `*.md` files lands with
  `boss land <id>` and no quote, without waiting on running checks. Never ask
  the captain about it; `decide add --task` refuses it. Fences still win.
- A wake ending "link only works on that machine" means the worker gave a
  localhost or bare host:port link. Ask it for a tailnet or preview link before
  passing the demo on.
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
polling and no tokens until something happens; `boss watch` hands them to you.
If wakes stop, run `boss doctor`: it shows the owner, whether a `boss watch` is
waiting, each host's push stream, and the listener's process. Never set up a
cron or a polling loop to replace them.

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
