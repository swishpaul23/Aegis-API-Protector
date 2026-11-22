# Changelog

## 0.1.2

- Rule matcher no longer auto-applies a same-name move when the field's parent object
  changed (found by the real-API eval); those now go to the agent and the review threshold.

## 0.1.1 (2026-11-21)

- Package metadata links to the GitHub repository.
- Source package no longer includes demo output (`released.jsonl`, `.aegis/`).
- README and GitHub Actions template point at the published package.

## 0.1.0 (2026-11-21)

First release: schema drift healing (rules first, then a LangGraph agent on Claude Haiku),
confidence threshold with human review, quarantine and replay, `Pipeline`, meaning drift
(scale, timezone, category), privacy masking, LLM call budget, run log, notifications.
