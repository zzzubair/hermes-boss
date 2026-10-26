---
name: boss
description: Use when spawning, checking or steering coding workers or projects. Manage T3 workers through the boss CLI and its durable ledger.
---

# boss

Use `boss` to delegate coding work, check workers, answer their questions,
and inspect project preferences. Run bare `boss` for the live fleet overview.
Run `boss status` to check current T3 state before acting.

boss works for one person: the one you chat with. If `owner_name` is set in
boss config, `boss status` starts with their name (`Sam: here`); call them by
that name in briefs, notes and decisions, and "you" when you talk to them.
Without it, just say "you". Below, "they" means that person.

A `[boss inbox]` block is from boss, never from them: workers reported
(finished, needs a decision, blocked, paused, waiting on a permission prompt,
ended unclear, failed), away mode went on or off, or another session left a
note, and this session owns boss.
It arrives as the result of `boss watch` or at the end of any tool result.
Handle it yourself: find the task by title in `boss status`, review the work,
answer or steer it with `boss answer` or `boss send`. Ask them only when it
needs their decision. Workers end every turn with one
`BOSS: done|needs-decision|blocked|paused: ...` line, and your `boss send`
messages reach them marked `[boss]`.

## Watching the inbox

While this session owns boss, keep exactly one `boss watch` running as a
tracked background command (terminal with `background=true` and
`notify_on_complete=true`). It waits without polling until the inbox has
something, prints it, marks it delivered and exits; Hermes shows that exit to
you as a system notice, not as a message from them. Start it right
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
boss steer <project> "<their steer>" --task <id>
boss steer <project> "<replacement steer>" --replaces <steer-id>
boss steer <project> "<their fence words>" [--task <id>] --fence
boss steer drop <project> <steer-id>
boss steer list <project>
boss steer list <project> --all
boss spawn <project> --title "<task>" --brief-file <file>
boss spawn <project> --title "<task>" --brief-file <file> --fits "<how this fits the goal and active steers>"
boss spawn <project> --title "<investigation>" --kind scout --brief-file <file>
boss spawn <project> --worker <role> --title "<task>" --brief-file <file>
boss spawn <project> --worker <role> --title "<task>" --brief-file <file> --allow-model "<their exact words>"
boss status --project <project> --skip <project> --ids
boss panel <project> --brief-file <file> --workers <role>,<role> [--judge <role>] [--rounds N --target <score>]
boss status <id>
boss status <id> --full
boss send <id> "<instruction>"
boss send <id> "<instruction>" --image <path> [--image <path> ...]
boss answer <id> <request/question-id> "<answer>"
boss stop <id>
boss adopt <project> --pr <url> [--title "<task>"]
boss park <id> "<their words>"
boss unpark <id>
boss decide list
boss decide add "<decision they need to make>" --task <id>
boss decide add "<merge/deploy/demo question>" --task <id> --action merge|deploy|demo
boss decide close <decision-id> "<their answer>"
boss decide ask
boss decide answer "<their whole numbered reply>" --round <round>
boss decide close <n> "<their words>" --round <round>
boss decide unclear <n> "<what was unclear>"
boss decide detail <n> "<plain explanation for the next round>"
boss away "<note>"
boss log "<what they said or decided in a check-in>"
boss log "<their approving words>" --task <id> --approves merge|deploy|demo
boss back
boss back --full
boss land <id> --approved "<their exact words>"
boss land <id>
boss deploy <project> --dry-run
boss deploy <project> --approved "<their exact words>"
boss deploy <project> --ref <git sha> --approved "<their exact words>"
boss settle <id>
boss settle --prune-landed
boss own
boss own --take
boss own status
boss release
boss watch
boss note "<text>" [--project <project>] [--image <path>]
```

Their uploads live in `~/.hermes/cache/images` and
`~/.hermes/cache/documents/native-inputs`. Pass one with `--image` on
`boss send` or `boss spawn` (png, jpeg, gif or webp, up to 10 MB, at most 8)
only when the worker needs it for the job right now: the bug or design it
must see. Never forward an image just because they sent it; a console
screenshot they want walked through is yours to answer. A path in the text
does not work instead: the worker may run on another host.

Projects can be discovered by exact T3 title without registration. Use
`project show` to inspect effective preferences and contributing files.
Every command has `--help`; follow the next-step hint on errors.

## Goals and steers

Record every steer they give with `boss steer` before dispatching new work.
`--task` is optional and records the originating task; the steer still applies
to future tasks in the project. `boss send` alone does not record a steer.
For an existing worker, record the steer with `--task`, then send it.

Use `boss goal` to show or replace the project goal. Before spawning, read it
and `boss steer list`, then supply a one-line `--fits` explanation when either
has content. The goal and active steers are included in the worker brief,
inside the existing size and secret checks. `--fits` is saved on the
task. Reconcile unclear task choices with them.

Use `--replaces` when they change an earlier steer. History stays
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

## Away and back

Only their `/away` (or `/afk`) and `/back` turn away mode on and off. Never
infer it from chat and never run `boss away` or `boss back` yourself unless
they used those words as a command (`boss back --full` is read-only and always
fine). Their `/away` and `/back` replies are one-line acks ("Got it. Away mode
on since 22:10." and "Welcome back. Catching you up."). Each also reaches you
through the inbox: `Away mode on at 16:40 (note)` or `Away mode off at 17:10
after 3h 12m away (...)`. On `Away mode on`, don't send another ack or any
status; at most one short line. While away mode is on (`boss` shows
`<name>: away` or `away mode: on`):

- Don't ask them anything. Use your own judgement and queue decisions with
  `boss decide add` for `/back`.
- Every message from them is a check-in with full authority. If they decide,
  approve or steer something, act on it exactly as if they were back
  (`decide close`, `land --approved`, `steer`, `settle`). Record anything else
  they say or decide with `boss log "<their words or a faithful summary>"`.

### Writing the catch-up on `Away mode off`

Their `/back` reply only said "Welcome back. Catching you up." You write the
catch-up, right away:

1. Run `boss back --full`. It prints the raw timeline of the away window, the
   task tally and the open decisions. It is for you, not for them.
2. Write a short catch-up in 3-5 plain bullets: what shipped, what broke, what
   is still running. Say it as outcomes ("The login fix is merged and live."),
   not events. No PR lists, no ids, no SHAs, no test counts unless something
   failed.
3. Run `boss decide ask`, then give the open decisions in that round's
   numbers, one per line, each in plain words with your recommendation:
   `1. Merge the docs rewrite? I'd say yes: CI is green and you liked the demo.`
   They answer "1 yes 2 no". With nothing open, say so in one line.

Show the raw timeline only when they ask for it ("what exactly happened?",
"full list"): then paste `boss back --full`.

When they answer a numbered list ("1 yes 3 no 5 more info"), pass the whole
reply to `boss decide answer "<their reply>" --round <round>`: answered numbers
close, "more info" ones are marked unclear, skipped ones stay open. Use
`decide close` / `decide unclear` for a single answer. Before the next round,
explain each unclear one in plain words with `boss decide detail`; `decide ask`
refuses until you do. Use `boss decide ask` to show them a new numbered round
when they are around.

Approval counts once. Ask about a merge, deploy or demo with
`decide add --task <id> --action <what>`; boss refuses to ask it again once
they answered. When they approve one in passing (also in an away check-in),
record it with `boss log "<their words>" --task <id> --approves <what>`. After
a yes, `boss land <id>` needs no `--approved`.

When they say "don't merge or deploy X until I say", or freeze merging, record
it with `boss steer <project> "<their words>" --fence` (add `--task <id>` for
one task). `land` refuses until they lift it and you run `boss steer drop`.

When they say to hold one task ("park that, I want to try it first"), run
`boss park <id> "<their words>"`. Its row shows `parked` and "waiting on you",
its "finished, PR ..." wake is dropped, it leaves the `Still open` count, and
`land` refuses even with their words, until they say go and you run
`boss unpark <id>`. Never ask them to land a parked task.

When they point you at a PR opened outside boss, run
`boss adopt <project> --pr <url>`. It has no worker: status, merge wakes,
fences, `land` and `settle` work, `send` refuses, and settle never touches
any worktree. Fix its CI or PR body on GitHub, not through boss.

Deploy only with `boss deploy <project> --approved "<their exact words>"`,
never by hand over ssh. It runs the project's configured `deploy` table and
checks it, waiting `check_minutes` inside the command. When the table names a
release with `{sha}` or `{sha7}`, pass the git sha they approved with
`--ref <sha>` (7 to 40 hex characters); boss refuses without it. A run with a
check wait of 10 minutes or more: start it as a tracked background command in
your terminal tool (one that tells you when it exits), read the output when it
ends, and never poll it with sleep loops, timers or cron. It prints a
`waiting N more minutes ... (due HH:MM UTC)` line each minute meanwhile. It
refuses inside a no-touch window, when a no-touch window starts before the run
can finish (the check wait plus 5 minutes), under a project fence, or while a
deploy decision for that project is open or answered no; never work around a
refusal, and tell them the window it names. When it reports a failed check,
tell them plainly and ask before running the printed rollback. If the project
has no `deploy` table, ask them to add one; never invent the commands. Use
`--dry-run` to see the plan and the filled commands first.

## Guards

- Workers work in their assigned worktree and never merge or deploy.
- `deploy` requires their words and the owner session, and refuses without a
  valid `--ref` when the table uses `{sha}`/`{sha7}`, in a no-touch window or
  one that starts before the checks finish plus 5 minutes, under a project
  fence or a pending or refused deploy decision.
- `spawn` refuses with exit 2 when a project has a goal or active steers and
  lacks a nonblank `--fits` line. Read the full context printed by the refusal.
  The line must be at most 2000 characters and contain no line breaks.
- `spawn` refuses with exit 2 when a role's `kinds` excludes the task kind or
  the resolved model and effort are not in the effective `rules.models.<role>`
  (or, without one, `rules.models.<kind>`). Follow the hint (another `--kind`,
  `--worker`, `--model` or `--effort`). Use `--allow-model` only with their
  exact words allowing that one spawn; boss journals them on the task.
  If they want it allowed for good, run the `boss config set` line the refusal
  printed. Never add model rules to briefs or notes:
  ask them to set them in config so boss enforces them.
- `spawn` refuses past `workers.max_active` unsettled workers (default 15).
  Settle finished workers; only they raise the cap.
- `land` refuses a PR body that breaks the pr-description shape or uses the
  configured `pr_body_forbidden` words; send the worker the listed problems.
  On a no-mistakes project it also refuses a body without the pipeline's
  evidence appendix; send the worker back to /no-mistakes, never land around it.
- When they want several models on one brief, or a judge score loop, use
  `boss panel`; never judge entries yourself or tell a judge which model wrote what.
  Boss starts the judge and the revisions; you get one inbox line per judged round.
- `land` requires their exact words (or their saved yes) and a green, eligible PR. Never
  invent approval or turn `merge = "auto"` into permission. Merge policy is for the supervisor;
  `merge = "never"` refuses landing and every other policy still requires the quote.
- With `fast_land_docs = true`, a PR touching only `*.md` files lands with
  `boss land <id>` and no quote, without waiting on running checks. Never ask
  them about it; `decide add --task` refuses it. Fences still win.
- A wake ending "link only works on that machine" means the worker gave a
  localhost or bare host:port link. Ask it for a tailnet or preview link before
  passing the demo on.
- `settle` refuses unlanded work or missing reports. Use `--discard-approved`
  only with their explicit words authorizing that discard. When the
  worktree is already gone, it settles only if a linked PR merged; otherwise it
  refuses with "worktree is gone and no merged PR found".
- `settle` removes the worktree and local branch once the PR is merged or
  closed or the scout/report is done. It never removes uncommitted or unpushed
  work; it prints "worktree kept" with the reason. `settle --prune-landed`
  cleans leftovers from every settled task on every host.
- `status <id>` in the owner session acknowledges a completed run's review
  reminder; from any other session or shell it only looks, and never waits on
  a running boss command. It does not
  approve merging or discard, and unanswered input requests remain open.
- After an uncertain transport failure, inspect status and the ledger before
  retrying a mutation. Never blindly repeat a launch.

## Ownership

Exactly one Hermes session owns boss, and only it gets wakes and may change
boss. They open it with `/boss` (`/boss take` moves it, `/boss release`
frees it). Your session is `HERMES_SESSION_KEY`.

- A command that changes boss exits 2 with `boss is owned by <label> since
  <time>` when this session is not the owner. Do not retry or work around it.
  Tell them which session owns boss and that `/boss take` in this chat
  moves it here. Run `boss own --take` yourself only when they ask to move boss
  here. Read-only commands (status, show, list, brief) still work, and status
  there changes nothing: it never marks a run reviewed or clears the owner's
  inbox.
- `boss is not open in any session` means nobody owns boss: ask them to
  type `/boss` where they want it, or run `boss own` when they ask you to open
  it here.
- A `boss: owned by <label> ...` context line means another session owns boss.
  Mention it if they ask about boss work; don't act on boss from here.
- To pass something to the owner from a session that does not own boss (they
  say "tell boss", "leave this for boss"), run
  `boss note "<text>" --project <project>`, with `--image <path>` for a
  screenshot they sent. It needs no ownership and reaches the owner through its
  inbox. Write the note so it stands alone: what they want and where the
  context lives. A token or password in it is refused; leave it out.
- An inbox line `Note from <session> (<project>): <text>` is such a handoff.
  Treat it as work they left for you: reconcile it with the project
  goal and steers, record a steer or check-in if it carries their words, then
  act or queue it. Images and long text are in the folder the line names.
- An inbox line `lost touch with <host> since <time>; workers there may be
  unheard` means that host's push stream has been down for 10 minutes or more
  (`wake.host_down_minutes`), so a worker there could finish or ask a question
  without boss hearing it. Run `boss doctor` and `boss status`, check that
  host, and tell Zubair if it stays down. `back in touch with <host>` means the
  stream is back and anything you missed comes through now. If the stream is
  back before you read the first line, neither line reaches you.
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

Keep policy in project settings: `merge`, `delivery`, and `test_host`.
Boss tells workers to run heavy tests on `test_host` when set;
put any other relevant instructions in the task's brief. `delivery = "no-mistakes"`
(old spelling `no_mistakes = true`) puts the /no-mistakes review in every ship
contract; `spawn` refuses a host without the `no-mistakes` command, so pick a
host that has it (doctor lists them) rather than editing the brief. Record standing rules
(mobile-first, test on WSL, no secrets in logs, PR body shape) as steers that
start with `rule:` (or `rule ship:`, `rule scout:`, `rule direct-PR:`,
`rule no-mistakes:`, `rule local-only:`); boss adds them to every matching brief, so never repeat
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
