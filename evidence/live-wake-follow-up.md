# Live finish wake investigation

Verified on ser5ver, 3 November 2026. No gateway, dashboard, or T3 service was
restarted. The live install still points at the primary checkout; all edits and
tests were confined to the assigned worktree or disposable directories.

## Stream and task lookup

Read-only `orchestration.subscribeShell` subscriptions to the configured hosts
accepted their events tokens. The ser5ver snapshot contained the missed test
thread `mcp:ab5484cb-5c44-48d8-ba17-8bd538c2c13c`, with status `completed`.
A subsequent live capture received four `thread.updated` items for a running
boss-owned thread. Their fields were `kind`, `sequence`, `location`, and
`thread`, matching `shell_threads`. The live stream does carry boss tasks.

The old task lookup cached all thread bindings using the parent `tasks/`
directory's mtime. But a launch creates the task directory and commits
`launch_intent` with no thread ID, then saves the returned thread ID inside that
directory. The second write does not change the parent directory's mtime.
Any lookup between those writes can cache a missing binding until another task
directory is added or the listener restarts. This is a reproduced defect that
explains the missing live finish; the old process did not expose its in-memory
map, so its exact historical cache contents cannot be recovered from the logs.

The reported test task's journal confirms the two-step launch:

```text
14:26:10.303051 launch_intent thread_id=None
14:26:10.567860 launched thread_id=mcp:ab5484cb-5c44-48d8-ba17-8bd538c2c13c
```

## Regression proof

`test_finish_after_launch_binds_thread_without_new_task_directory` warms the
listener with an unrelated completed shell while the task has no thread ID,
saves the binding without changing the parent mtime, and pushes the finish.
Before the fix, it failed because the inbox list was empty instead of containing
the task's `done` event. After the fix, one snapshot read holds one finish,
a duplicate push does not re-read or re-enqueue it, and `boss watch` receives
`Fix thing: finished: OK`. The subscription stays connected throughout.

Additional regressions cover two hosts subscribing independently, per-host frame
and match counts, one host dropping while the other stays listening, legacy
health, stale fingerprints, health from a previous PID, and a process whose
source files change after import but before it constructs its listener.

## Checks

```text
python3 -m unittest discover -v
Ran 440 tests in 77.177s
OK

python3 scripts/check_wake.py
Ran 91 tests in 27.747s
OK
PASS: worker events reach the owner once ... one listener per machine

python3 scripts/check_fresh_install.py
PASS: fresh install, idempotent init, doctor, role spawn, no poll command, ownership gate and claim

git diff --check
passed
```

The full suite preceded the final added stale-waiter test. The final wake suite
includes it and every changed wake/doctor regression. Tests use disposable homes
and fake MCP/WebSocket servers, with no model calls.

## Doctor against the existing live listener

Running this worktree's `./bin/boss doctor` against the live ser5ver installation
returned exit 1 as expected:

```text
ok: host ser5ver push stream token
ok: host oracle push stream token
ok: host wsl push stream token
ok: wake listener pid 2496099 (listening host=oracle); last connection change 14:25
fail: wake listener code: listener code unknown (legacy listener has no per-host health)
fail: host ser5ver listener stream: no listener stream health
fail: host oracle listener stream: no listener stream health
fail: host wsl listener stream: no listener stream health
```

The fix hints explicitly say to restart every Hermes host process for the
profile. A valid token is reported separately from the actual subscription.

## Deployment follow-up

After installing the new checkout on ser5ver, restart both persistent user units:

```sh
systemctl --user restart hermes-gateway.service hermes-dashboard.service
boss doctor
```

Also restart any TUI/app process using that profile. Otherwise an old process
may retain or later acquire `wake.lock`. Restarting interrupts those sessions.
The new fingerprint must match and each configured host must show its own
`listening` state after receiving a subscription chunk. Then spawn and finish a
new boss task and verify its inbox line and waiting `boss watch` without a
listener restart. This final live finish proof remains pending deployment.
