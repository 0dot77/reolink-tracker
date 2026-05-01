# AGENTS.md

This repository is a small Python tool for Reolink RTSP camera tracking in an interactive installation pipeline.

## Working Rules

- Keep `config.yaml`, model weights, virtualenvs, caches, and OMX runtime state out of git.
- Use `config.example.yaml` for shareable configuration shape.
- Do not commit real RTSP URLs, passwords, device IPs that imply private network setup, or project-private credentials.
- Preserve the primary OSC schema unless the TouchDesigner/receiver side is updated at the same time.
- Keep changes small and verify with at least `python -m py_compile tracker.py region.py viewer.py`.

## Project Context

- Product context lives in `docs/product.md`.
- Technical context lives in `docs/tech.md`.
- AI coding rules live in `docs/ai-rules.md`.
- Decisions and reversals live in `docs/decisions.md`.

Read those files before making non-trivial changes.
