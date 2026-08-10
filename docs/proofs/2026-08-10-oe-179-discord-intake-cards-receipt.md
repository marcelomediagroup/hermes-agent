# OE-179 Discord intake action cards completion receipt

- Issue: OE-179 — Add Discord voice-note and attachment intake action cards
- Date: 2026-08-10
- Owning repository: `marcelomediagroup/hermes-agent`
- Isolated branch: `marcelomediagroup/oe179-drain`
- Integration base: `0a3c11b45` (`fork/main`, the Open Engine queue's canonical base)
- Prerequisite: OE-408 is Agent Done and its canonical runtime acceptance was verified before implementation
- Code commits: `279c58ea2`, `050cff5fe`, `958d2f513`

## Outcome

Discord native voice notes now retain the existing successful transcript echo and receive a separate compact operator card with a bounded extractive summary and advisory next actions. Failed or inaudible voice notes receive a blocked card, and input-aligned transcription outcomes keep each ready or blocked card attached to the correct opaque source reference even in mixed or partially failing batches.

Direct Discord attachments now receive type-aware ready cards, while oversized, unreadable, and download-failed attachments receive blocked cards with filename, reason, applicable limit, and recovery guidance. Referenced historical attachments remain available to the agent but do not create fresh cards, and attachment ordering does not suppress a later native voice note.

Cards use the existing authenticated, durable Discord operator-action dispatcher. A button press records intent in the existing audit store; it does not execute Linear creation or any other external action.

## Implementation notes

- Added `gateway/intake_cards.py` as a platform-neutral, deterministic card builder with bounded summaries and explicit safety fields.
- Extended Discord media intake to distinguish native voice notes, direct attachments, referenced attachments, real-size overflows, unreadable content, and download failures.
- Preserved exact STT media indexes, per-input outcomes, card source references, and pending-event cache state through queue merges.
- Kept card delivery platform-neutral in the gateway runner and kept the capability off the model tool schema.
- Redacted voice transcript text, provider payloads, signed URLs, raw filenames, and private cache paths from newly touched logging paths.
- Preserved cached media paths and raw originals in place; no original is moved, renamed, rewritten, or deleted.
- Added regression coverage for voice success/failure, partial multi-note failures, document-first mixed media, direct versus referenced attachments, size/download failures, privacy, queue merging, authenticated durable actions, and no automatic Linear action.

## Verification

- OE-179-focused matrix: 176 passed, 0 failed across 10 gateway files.
- Private local canary: 1 passed. The real Discord adapter rendered a type-aware action card in a temporary `HERMES_HOME`; an allowlisted user invoked the durable button; exactly one audit intent was recorded; an external-runner trap was untouched; and no Linear artifact was created.
- Gateway suite: 5,310 passed, 8 failed, 12 skipped across 619 files. All OE-179 files and behaviors passed.
- The eight broad-suite failures are outside this change:
  - one goal-status metadata expectation was reproduced unchanged on base `0a3c11b45`;
  - five queued TTS-media expectations omit the already-present `notify=True` metadata, and neither those tests nor their delivery function changed in this branch;
  - one Linux abstract-socket test cannot bind on this Darwin host, and its file and implementation are unchanged;
  - one shutdown-forensics subprocess diagnostic returned no PID on this host, and its file and implementation are unchanged.
- Static checks: Ruff passed on all changed Python files, `git diff --check` passed, bytecode compilation passed, and the focused type check for the new card module passed.
- Independent review: specification and engineering-standards reviewers both reported zero actionable findings after the final source-identity correction.

## No-Go Confirmation

- No email was sent.
- No social or public post was made.
- No payment was initiated or approved.
- No contract was approved or signed.
- No deployment or public/live Discord canary was performed.
- No Linear issue or other external action is automatically created by a card click.
- No raw media original was moved, renamed, altered, deleted, or exposed in this receipt.
- No credential, private runtime payload, transcript, signed URL, or private cache path is included in evidence.
- OE-179 remains out of Agent Done until the branch is integrated into canonical `fork/main` and receives runtime acceptance there.

## Next action

Integrate the branch through the serialized Open Engine queue, run the canonical private runtime acceptance, and only then move OE-179 to Agent Done.
