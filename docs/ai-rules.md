# AI Coding Rules

Use this file as the stable context for Codex, Claude Code, Cursor, or any other AI coding agent.

## Before Editing

- Read `README.md`, `docs/product.md`, `docs/tech.md`, and this file.
- Check `git status` and do not overwrite unrelated user changes.
- Treat `config.yaml` as private local state.

## Implementation Preferences

- Prefer small, reversible changes.
- Reuse the current `tracker.py` / `region.py` / `viewer.py` boundaries.
- Do not add dependencies unless the task explicitly needs them.
- Keep OSC address and argument order backward-compatible unless the task is a schema migration.
- Keep camera credentials and installation-private details out of committed files.

## Verification Before Completion

- Run `python -m py_compile tracker.py region.py viewer.py`.
- If region math changes, run `python region.py` after dependencies are installed.
- If runtime behavior changes, document what still needs live-camera validation.

## Good Issue Shape

Each GitHub Issue should include:

- Goal
- Current behavior
- Desired behavior
- Acceptance criteria
- Any live-installation constraints
