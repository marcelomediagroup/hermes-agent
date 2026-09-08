# Native snapshot addressing

## Contract

`computer_use` keeps the most recent capture's native handles on its
session-owned `CuaDriverBackend`. An element-index action must carry the matching
opaque `element_token` when that action's live input schema accepts it. Older
per-tool `accessibility.element_tokens` advertisements remain supported.
If no usable token is available, an advertised `snapshot_id` property can carry
the capture's snapshot ID alongside an index actually returned by that capture.
Do not parse, reconstruct, normalize, or borrow handles from another target.

Both caches are replaced by capture and cleared on target selection, failed
capture, or transport reset. The driver still owns stale-snapshot and exact
pid/window validation. Refusals and unverifiable effects are surfaced unchanged;
this repair does not replay mutations or substitute coordinates/foreground input.
`app=` on an input remains a mismatch guard, not a retargeting operation.

The standalone `double_click` schema need not accept `button`. Omit an
unsupported default left-button field; reject an unsupported non-left request
rather than silently changing its meaning.

## Diagnosing the boundary

1. Check the authoritative [Hermes computer-use documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/computer-use)
   and the local access runbook. Separate installed binary, OS permission,
   runtime acceptance, and application effect. Do not add blanket grants.
2. Compare live `describe click` / MCP `tools/list` input properties with the
   **per-tool** capability list. An empty custom capability list does not imply
   that the input schema lacks `element_token`.
3. Capture only the authorized application, then compare returned element count
   with cached token count. Log counts/property names, not private trees.
   macOS app captures may also include global menus: filter those out of receipts.
4. Exercise the real registry handler in a disposable `HERMES_HOME`, using the
   task checkout's imports and the existing interpreter/driver. Disable lazy
   installs, explicitly select the existing driver, and use standard permission
   mode. Do not change a live profile or promote source to run a canary.
5. For a provider-free canary, use `mode=ax` and temporary
   `computer_use.capture_after_mode: ax`. The native capture still supplies
   pixels internally; save app-scoped images for direct inspection. A default
   SOM response in an unconfigured temporary home may attempt auxiliary vision.
6. Allow one harmless observed element action, and verify a fresh capture of the
   exact pid/window. `effect=unverifiable` is not permission to repeat input.
   Release only the canary's own session afterward.

## Repair verification

Base: `f17f18cd11e0dc203e683ab2b77b6a5aafe5afa7`.

Live CuaDriver 0.23.2 discovery returned no custom capability tags for the native
input tools, while `click`, `double_click`, `scroll`, and `set_value` all accepted
`element_token` and `snapshot_id`. Calculator capture parsed and cached all 155
returned element tokens. The parser was not dropping them: the old capability-only
attachment condition was excluding them. No session-discovery/parser repair was
needed. The strict double-click schema also exposed an unrelated-to-tokens but
blocking extra `button` field in the same element-input path.

Two parametrized invariant tests exercise registry dispatch, real discovery,
MCP result parsing, capture caching, native action routing, and verdict shaping;
only MCP I/O and the bridge execution boundary are substituted. They cover token,
snapshot-only, older capability-tag, and legacy advertisements; click variants,
scroll and set-value; refresh, focus, failed capture and transport invalidation;
and stale refusals without replay.

```sh
scripts/run_tests.sh tests/tools/test_computer_use_snapshot_addressing.py --file-retries 0 -q
scripts/run_tests.sh tests/tools/test_computer_use*.py tests/hermes_cli/test_computer_use_cli.py --file-retries 0 -j 4 -q
```

- RED before production changes: **26 failed, 10 passed**. Expected failures:
  missing snapshot binding, strict double-click extra property, and failure to
  refuse an unsupported non-left double-click before dispatch.
- GREEN: **36 passed**; broader native/CLI regression: **287 passed, 2 skipped**
  (Linux-only cases), no failures or retries.
- Ruff 0.15.10 and Python compilation passed. The live venv lacks Ruff, so the
  repository-pinned version was run via isolated `uvx`, not installed into it.
- `ty` 0.0.21 is **not clean** in this area: 49 existing diagnostic messages
  remain versus 52 on the base-file snapshot, with no added diagnostic messages.
  They concern existing mixin attributes and typing in surrounding code.
  This is a focused regression result, not a clean repository-wide typecheck.

## Real Calculator acceptance

Local evidence directory: `/tmp/mmg-native-targeting-20260908/`.

- `baseline-probe.json`: live schema, permission health, and token-count isolation.
- `red.log`, `green.log`, `regression.log`: canonical runner output.
- `calculator-canary.py`, `canary.json`: real worktree registry invocation and
  sanitized outbound argument/readback receipt.
- `canary-before.png`, `canary-after.png`: directly inspected screenshots.
- `typecheck-base.log`, `typecheck-patched.log`, `typecheck-comparison.json`:
  scoped static-analysis baseline comparison.

A fresh Calculator capture showed `3+4` and result `7`. Exactly one background
`click(element=2, app="Calculator", capture_after=True)` targeted the observed
**All Clear** button. Outbound arguments included the matching cached
`element_token`, no coordinates and no foreground mode. CuaDriver returned
`ok=true`, `AXPress`, route `accessibility`, and `effect=unverifiable`. The exact
pid/window follow-up screenshot showed **0** with the prior expression absent.
Visual readback, not the driver verdict or screenshot hash alone, verified the
change. No input was repeated.

An initial provider-free SOM attempt stopped before any input because auxiliary
vision was unconfigured; its receipt is `canary-preclick-aux-routing.json`.
The successful run obtained fresh native state and used AX response formatting.
No driver update, OS grant, profile change, live-source promotion, or service
restart occurred. Only the canary's own MCP session was released.

## Limits

- Hardware acceptance covers a single background left AX click on macOS
  Calculator, not every application/platform or click variant.
- Snapshot-ID fallback and compatibility advertisements are covered by contract
  tests, not live hardware: this driver returned tokens for every observed node.
- This driver's `drag` schema is coordinate-only and has no element/snapshot
  endpoints. Element-based drag remains unsupported; do not substitute a
  coordinate drag as evidence of native element acceptance.
- Modified double-click schemas and input escalation beyond the tested default
  are not repaired here. Legacy schemas without either handle advertisement keep
  their existing bare-index behavior; modern drivers still reject absent handles.
- No global capability inference, public tool-schema expansion, transport replay,
  or live session hot reload was added. Integration/restart decisions belong to
  the reviewing parent lane. The full repository suite was not run.
