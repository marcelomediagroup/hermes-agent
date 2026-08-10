# OE-176 Media and Operations Discord Digests Receipt

Date: 2026-08-10  
Issue: OE-176  
Dependency: OE-408 was already in Agent Done with its authenticated operator-card contract accepted before this implementation began.

## Result

Hermes now exposes `/today`, `/changes`, `/decisions`, and `/ops` as opt-in gateway commands and enriches `/agents` with the same operations projection when the feature is enabled. The MMG Discord profile explicitly enables the commands and supplies the m-os, shared-state, and Hermes repository roots; generic Hermes installations retain the prior command surface and `/agents` output.

The commands return validated `OperatorCardReply` envelopes through the existing authenticated operator-card delivery path. They do not introduce a model tool, mutate their sources, or bypass Discord authorization, routing, busy-session dispatch, reply threading, or slash-command opt-out behavior.

## Implementation notes

- `/today` summarizes active agents/process availability, pending human decisions, operational exceptions, recent repository changes, and the absence of a governed deadline source.
- `/changes` reports bounded 24-hour Git activity for Hermes and m-os plus completion-receipt changes. Historical agent-completion, cron-failure, and freshness deltas are explicitly labeled `Unavailable` because no governed historical read model exists.
- `/decisions` reads the Discord approval inbox and bridge state. Malformed, missing, or stale sources fail closed as `Unavailable` or `needs_review`; they are never rendered as zero.
- `/ops` reads the cron manifest and bounded state files, validates source identity, and delegates lane verdicts to m-os's canonical `scripts/health/oe_lane_state.py` exit-code contract. Evaluator output is discarded so raw runtime payloads cannot leak into Discord.
- Git subjects are capped at six displayed entries and use a `6+` marker when truncated. JSON reads are capped at 1 MiB, card fields are bounded, and every card names its sources.
- All four digest cards are read-only and contain no actions or links. `/agents` preserves its legacy output unless the same explicit gate is enabled.
- The feature gate is `platforms.discord.extra.operator_digests.enabled`; command discovery supports both the top-level `platforms` form and the gateway-compatible `gateway.platforms` fallback. Runtime handlers independently re-check the gate and fail closed.
- Operator-card metadata is merged through the established normal, busy-dispatch, and background delivery paths without altering media collection, attachment routing, or fallback rendering.

## Integration

- Feature commit: `b408a933928ddb3c9b2f0832e065c93b79ac71e2`
- Integration merge: `769693fe539bdfc7b06f493658dcf8e9ac7b7c6a`
- Branch: `marcelomediagroup/oe176-drain`
- The integration merge preserves the concurrently accepted OE-179 commits and is present on both the MMG remote `main` and the canonical local Hermes `main`.
- Independent spec review found no remaining OE-176 gaps. Independent repository-standards review found no remaining hard correctness or standards findings.

## Verification

- Combined OE-176/OE-179 integration slice: 14 files, 267 tests passed, 0 failed.
- OE-176 focused slice before reconciliation: 11 files, 219 tests passed, 0 failed.
- Runtime config propagation: all four commands discovered, runtime gate enabled, both external roots configured, and Discord enabled.
- Real read-model projection: all four command handlers returned validated cards with only expected titles, severities, labels, and zero actions/links; no field values or private payloads were printed.
- Python byte compilation and `git diff --check` passed.
- Full-suite attempt on the final integration reached 26.6% with 7,772 passing tests and one unrelated failure after every OE-176 gateway test had passed. The failure was `tests/agent/test_credential_pool_routing.py::TestFailureAttribution::test_unmatched_key_does_not_retry_only_pool_entry`; an isolated run on the pre-OE-176 canonical main reproduced the same 16-pass/1-fail baseline result, so it is outside this change.

## Runtime acceptance

- The MMG profile configuration was written through `hermes config set`; no non-secret behavior setting was added to `.env`.
- The supervised `ai.hermes.gateway-mmg` service restarted onto the canonical integrated checkout and returned healthy with a new PID.
- Sanitized post-restart inspection found startup and Discord-ready markers with zero recent error/traceback lines. No message bodies, tokens, IDs, or private state values were printed.
- A live canary rendered the real `/today` card through the delivered Discord embed renderer in the approved `izel` target inside a non-discoverable guild. The API accepted the post, the observed title and field labels matched, actions and links were absent, and the canary was immediately deleted; deletion was verified by a subsequent 404.

## No-Go Confirmation

- No public Discord message was published. The only live write was the approved private canary, and it was deleted and verified absent.
- No approval, cron, lane, repository, or shared-state source was mutated; all digest sources were read-only.
- No media path, attachment path, media routing, or media retention behavior was changed.
- No payment, Plaid, billing, purchase, or other financial action was taken.
- No email was sent or drafted.
- No public release or deployment was performed. Runtime rollout was limited to the user-approved MMG profile configuration and supervised Hermes gateway restart.
- No Discord card action or external link was shipped for these commands, and no write-capable follow-up was triggered.
- No secret, credential, private payload, message body, channel ID, or raw runtime state is included in this receipt or the canary output.

## Remaining work

None for OE-176. The unrelated credential-pool baseline failure remains owned outside this issue.
