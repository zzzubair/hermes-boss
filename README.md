# hermes-firstmate

Hermes plugin that turns JigglyPuff (Zubair's Hermes default profile on ser5ver) into his dev lead, replacing the separate Firstmate agent.

Goal: one assistant for everything. JigglyPuff already has the memory (OptMem), skills and Discord control room; this adds Firstmate's dev loop on top.

## Scope (to be shaped, nothing built yet)

- Port Firstmate's working loop: fm-inbox handoffs, worker spawning (Orca/T3 worktrees), no-mistakes review pipeline, PR flow (go-green / all-in).
- Keep Firstmate's state (`~/code/p/firstmate/state`, `data/`) readable during migration.
- Clash Lens first, since that's Firstmate's only job today.

## Open questions

- Plugin vs skills: how much of this is a Hermes plugin (tools, hooks) vs skills JigglyPuff loads?
- Cutover: run side by side on Clash Lens, then retire Firstmate?
