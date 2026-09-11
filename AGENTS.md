# Hermes Agent Contribution Instructions

This file contains repository-wide invariants only. Read the `AGENTS.md` in the
area you are changing; long-form architecture and procedures live in
`website/docs/developer-guide/` and workflow skills.

## Before Editing

- Inspect the defining module and its real callers before changing behavior.
- Read only the applicable area instructions from the routing table below.
- Preserve unrelated working-tree changes. Do not commit, push, merge, or
  rewrite history unless the user requested it.
- Prefer a focused change to a broad compatibility layer or speculative
  abstraction. Update public documentation when user-visible behavior changes.

## Runtime Invariants

- **Prompt stability:** a conversation's system prompt and tool surface are
  byte-stable. Compression is the only normal prompt rebuild boundary. New
  mid-conversation context belongs in a user message or tool result.
- **Strict message order:** preserve legal provider role alternation and
  append-only persistence. Do not mutate historical rows to inject new context.
- **Profile-aware paths:** use `get_hermes_home()` for runtime paths and
  `display_hermes_home()` for user-facing text. Profile discovery itself remains
  anchored at `Path.home()/.hermes/profiles`.
- **Narrow public surfaces:** `run_agent.py`, `cli.py`, gateway entrypoints, and
  other facades keep stable imports and delegate implementation to focused
  siblings. Move behavior rather than duplicating it.
- **Capability footprint:** prefer, in order, an existing tool, a skill or
  script, a plugin, a deferred toolset, and only then a new core tool. A
  registered core tool must also belong to a toolset.
- **Provider contracts:** resolve model, endpoint, reasoning, tool, and cache
  behavior through provider profiles and the shared transport seams. Do not
  special-case one model in unrelated call sites.
- **Secrets:** never print, commit, snapshot, or place credentials in config.
  Secret values belong in environment-backed or approved secret-provider paths.
- **External compatibility:** preserve documented CLI, API, plugin, and storage
  contracts. Internal moves do not need one-off re-export shims; use the central
  compatibility layer when an external contract genuinely requires one.

## Code Shape

- Match neighboring style and keep comments focused on why a constraint exists.
- Split a file or function when a change would extend an already oversized
  facade. Prefer tables/registries to long name- or kind-based condition chains.
- Avoid silent exception handling, unused feature flags, and defensive wrappers
  around operations that cannot fail.
- Never infer process identity from loose command-line substrings. Use the
  canonical matchers documented in `hermes_cli/AGENTS.md`.
- Dependencies require an upper bound; Git dependencies use an immutable commit
  SHA. Run the relevant lockfile update when dependency metadata changes.

## Verification

Run the smallest checks that exercise the changed behavior, then broaden when
failures, shared seams, packaging, or risk justify it.

- Python tests run through `scripts/run_tests.sh`, not bare `pytest`; pass the
  affected file or directory whenever possible.
- Test public behavior and cross-module contracts. Do not test source text,
  fixed catalog counts, model lists, config versions, or other expected-to-change
  snapshots.
- Test host-specific behavior on that host with the repository markers. Do not
  make the interpreter pretend to be another operating system.
- Tests must use isolated temporary homes and must not write to the user's live
  `~/.hermes` state.
- For fixes, add the smallest invariant regression test that fails without the
  fix. Do not add unrelated coverage to satisfy a numeric target.

## Repository Hygiene

- Keep runtime state, caches, generated artifacts, credentials, and local
  databases out of Git.
- Stage only files belonging to the task; never use `git add .` or `git add -A`.
- Do not force-push or perform destructive Git cleanup without explicit user
  authorization.
- Commit messages use the repository's conventional `type(scope): subject`
  format when a commit is requested.

## Routing Table

| Area | Read | Owns |
|---|---|---|
| `run_agent.py`, `agent/` | `agent/AGENTS.md` | Turn phases, prompt assembly, caching, compression, model resolution |
| `cli.py`, `hermes_cli/`, `main.py` | `hermes_cli/AGENTS.md` | CLI commands, configuration, profiles, updates |
| `gateway/` | `gateway/AGENTS.md` | Adapters, guards, streaming, gateway lifecycle |
| `tools/`, `toolsets.py`, `model_tools.py` | `tools/AGENTS.md` | Tool registry, schemas, dispatch, toolsets, delegation |
| `plugins/` | `plugins/AGENTS.md` | Plugin manifests, compatibility, lifecycle |
| `tui_gateway/`, `ui-tui/` | `tui_gateway/AGENTS.md` | TUI process and protocol surfaces |
| `web/` | `web/AGENTS.md` | Web dashboard boundaries |
| `apps/desktop/` | `apps/desktop/AGENTS.md` | Desktop architecture and UX |
| `skills/`, `optional-skills/`, curator | `skills/AGENTS.md` | Skill authoring and lifecycle |
| `cron/`, kanban | `cron/AGENTS.md` | Scheduling and queued-work invariants |

Use `website/docs/developer-guide/` for deeper background only when the task
touches that subsystem. Historical issue narratives and step-by-step maintenance
recipes do not belong in this always-on file.
