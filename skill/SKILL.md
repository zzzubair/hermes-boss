---
name: boss
description: Use when spawning, checking or steering coding workers or projects. Manage T3 workers through the boss CLI and its durable ledger.
---

# boss

Use `boss` to delegate coding work, check workers, answer their questions,
and inspect project preferences. Run bare `boss` for the live fleet overview.
An ambient `boss:` reminder comes from local records; run `boss status` to
check current T3 state before acting.

## Core commands

```sh
boss
boss project list
boss project show <name>
boss project add <name> --host <host>
boss spawn <project> --title "<task>" --brief-file <file>
boss spawn <project> --title "<investigation>" --kind scout --brief-file <file>
boss status <id>
boss status <id> --full
boss send <id> "<instruction>"
boss answer <id> <request> "<answer>"
boss stop <id>
boss decide list
boss decide add "<captain decision needed>" --task <id>
boss decide close <decision-id> "<captain resolution>"
boss land <id> --approved "<captain's exact words>"
boss settle <id>
```

Projects can be discovered by exact T3 title without registration. Use
`project show` to inspect effective preferences and contributing files.
Every command has `--help`; follow the next-step hint on errors.

## Guards

- Workers work in their assigned worktree and never merge or deploy.
- `land` requires the captain's exact words and a green, eligible PR. Never
  invent approval or turn `auto_merge` into permission. It is a supervisor flag.
- `settle` refuses unlanded work or missing reports. Use `--discard-approved`
  only with the captain's explicit words authorizing that discard.
- `status <id>` acknowledges a completed run's review reminder. It does not
  approve merging or discard, and unanswered input requests remain open.
- After an uncertain transport failure, inspect status and the ledger before
  retrying a mutation. Never blindly repeat a launch.

Only `boss poll` belongs in scheduled checks. It calls no LLM, emits changed
actionable state, and exits 0 silently when unchanged. The Hermes plugin reads
only local ledger context and never polls T3. Keep code conventions in the
repository's AGENTS.md; keep supervisor preferences in boss.
