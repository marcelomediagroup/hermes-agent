# Required-child completion barrier — verification receipt

Base: `f17f18cd11e0dc203e683ab2b77b6a5aafe5afa7`  
Branch: `mmg/completion-enforcement-20260908`

## Contract

Background `delegate_task` batch units with a durable parent session keep that
parent's turn `completed=false` until their completion is accepted by delivery or
an explicit stop abandons the requirement. Completed/error results awaiting
injection still block, including completion-before-parent-final races. The final
text retains questions/blockers and appends a waiting explanation; ordinary text
exits use `waiting_for_required_children`. This pauses the turn, not the task.

Consumption is scoped to the actual notification turn with a ContextVar (including
coalesced gateway results), not a delivery claim. Failed injection/claim release
retains the requirement. The canonical compression-chain resolver follows durable
session rotation, but not `/new` or a different conversation. Pending records
survive in-memory completion-tail pruning. Explicit stop clears finished-result
requirements without re-invoking finished children's interrupt callbacks or
falsely marking undelivered results as delivered.

This is **not a semantic all-outcomes guarantee**: consuming a failed child result
can permit a completed turn; it does not prove the requested work succeeded.
Workers/requirements remain process-local. Existing delivery acceptance and
at-least-once semantics remain; no restart durability, new optional/required tool
schema, retry scheduler, model-output censor, or new task manager is introduced.
Standalone background-runner jobs remain detached.

## Independent verification

All test runs used the existing `scripts/run_tests.sh`, its discovered shared
Python 3.11 venv, credential-stripped environment, temporary `HERMES_HOME`, and
per-file subprocess isolation. No provider calls or real paid child sessions.
Only model I/O is replaced in the canary agent loop; delivery tests use real
registry/SQLite claims, real injection functions, and a fake transport. Gateway
coverage crosses the production `_run_in_executor_with_context` worker boundary.

- New canaries: **27 passed** in the final scoped run. Covers running/completed/
  error children, completion-before-final, fresh notification turns, poller,
  post-turn drain, single/grouped gateway delivery, sibling isolation, failed
  delivery, pruned receipts, explicit stop, `/new` scope, compression, budget
  fallback, stream recovery, blockers, role alternation and stable prompt prefix.
- Base reproduction: the 12 parent-yield/consumption cases fail on the exact base
  because it incorrectly returns `completed=true` before result consumption.
- Additional stop regression: observed **26 passed / 1 failed** before the callback
  filter; all 27 passed after it. Existing active-child cancellation tests pass.
- Scoped regression: **40 files, 465 passed, 1 failed**. Same existing files on the
  base: **39 files, 438 passed, 1 failed**. The identical failure is
  `test_child_dedicated_db_follows_parents_db_path` (`/private/tmp` versus `/tmp`
  string comparison on macOS), not a new regression.
- Separate classification of failures from the interrupted broad run: both base
  and patch return **200 passed, 3 failed, 5 skipped** across the same three files.
  Existing failures: cron home permissions, missing CuaDriver.app in the mocked
  macOS daemon test, and Buzz media redaction/bounding. The previously observed
  auxiliary cancellation failure did not reproduce on either scoped run; the old
  required-child budget failure is now green. No full-suite pass is claimed.
- Ruff 0.15.10 (existing offline cache), compatibility-pointer check, and
  `git diff --check`: passed. No dependency or live-profile changes.

## Evidence and handoff

Task-generated `completion*.log` files and `.canary-baseline/` were moved intact
to `/tmp/mmg-completion-enforcement-20260908/`; unrelated files were not deleted.
That directory holds `regression-paths.json` (exact 40-file selection),
`independent-regression.log`, `independent-baseline.log`,
`independent-canary-base-red.log`, `independent-stop-red.log`, and matching
`independent-existing-{failures,baseline}.log` classification logs. The archived
base versions of all seven changed production files matched `git show <base>`.

Scope is seven production Python files, one canary test file, and this receipt.
Parent owns integration: no push, promotion, restart, profile edits, or retirement.
