# Decisions

## 2026-05-01: Keep Runtime Config Local

`config.yaml` contains real RTSP URLs and credentials, so it is ignored by git. The repository tracks `config.example.yaml` instead.

Consequence: new machines should copy `config.example.yaml` to `config.yaml` and fill in local camera details.

## 2026-05-01: Use GitHub As Project Memory

This repository is intended to hold both code and AI-useful context. Product intent, technical constraints, AI rules, issues, PRs, and decisions should live in the repo rather than only in chat history.
