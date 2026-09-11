# Skills and Optional Skills

Applies with the repository root instructions. User documentation lives in
`website/docs/user-guide/features/skills.md`; deeper implementation guidance
lives in `website/docs/developer-guide/creating-skills.md`.

## Placement

- `skills/` contains small, broadly useful built-ins.
- `optional-skills/` contains niche, heavy, or dependency-specific packages.
- Custom user capabilities normally belong in a plugin or profile-local skill,
  not the core repository.

## Authoring Contract

Every new or materially revised skill must have valid YAML frontmatter with
`name`, `description`, `version`, `author`, `license`, `platforms`, and Hermes
tags.

- `name` is lowercase kebab-case and matches the package directory.
- `description` is one sentence, at most 60 characters, and starts with
  `Use when`, `Use for`, or `Use to`. Put the applicability signal first because
  the prompt index truncates after 60 characters.
- Describe the task boundary, not implementation details or marketing claims.
- Declare real platform restrictions and prerequisites. Prefer portable helpers
  before narrowing platform support.
- Credit the human contributor before “Hermes Agent”.

## Progressive Disclosure

`SKILL.md` is a router, not a complete manual. Keep only:

- when the skill applies and important non-triggers;
- the intended outcome and durable decision points;
- prerequisites that affect whether work can proceed;
- links to the exact reference, script, template, or asset needed next;
- concise verification and safety boundaries.

Move detailed recipes, command catalogs, examples, and historical troubleshooting
into `references/`. Put deterministic or repeated logic in `scripts/`; do not ask
the model to reproduce a parser or long transformation inline. Load supporting
files only when the current task needs them.

Do not require a fixed heading itinerary or target line count. A root over roughly
8 KB deserves review for routing opportunities, but clarity and task safety—not a
numeric limit—decide the final structure.

## Tools and Dependencies

Use native Hermes tool names in prose. Third-party CLIs are appropriate inside a
script or an explicit prerequisite. MCP dependencies must name the server and the
readiness check. Keep environment examples free of real credentials.

## Verification

- Add the smallest behavior test that exercises a new skill contract.
- Run `scripts/run_tests.sh tests/skills/test_<skill>_skill.py -q` for a targeted
  skill test, plus broader skill-authoring checks when shared rules change.
- Do not add source-shape or fixed-count tests.
- Skill-loading tools return the full root; do not add offset pagination to
  `skill_view`. References remain individually addressable by path.

## Curator Boundary

The background curator may maintain only skills marked `created_by: agent`.
Bundled and hub-installed skills are read-only to it. Archiving is recoverable;
pinned skills are exempt. Repository-owned skill cleanup belongs in reviewed
source changes and deterministic validation, not autonomous curation.
