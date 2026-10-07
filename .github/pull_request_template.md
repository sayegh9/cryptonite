## Summary

<!-- Plan item (e.g. B3, D4, D1, B4) and what this PR changes. Link the plan section. -->

## Checklist

- [ ] Targets `review-plan` (item PRs); any hotspot overlap with another open PR is noted below
- [ ] CI green on the **current head**: `ruff`, `web`, `pytest`, `e2e-smoke`, `verify`
- [ ] `docs/CHANGELOG.md`: one `Unreleased — review-plan` heading, every existing line kept
- [ ] Migration: single new head; `down_revision` points at the current head — second of the `039` pair renumbers to `040`, chain stays linear `038→039→040`
- [ ] No secrets committed: no credential-bearing DSNs/keys/passwords/tokens, even in docstrings or tests (passwordless DSNs are fine; TruffleHog scans every commit); tests never touch the dev DB `cryptonite`, Redis DB 0/1, or paid models
- [ ] Hotspot files (`services/debate_worker/main.py`, `services/scanner/cryptonite_scanner_service/worker.py`, `services/orchestrator/cryptonite_orchestrator/server.py`): serialized with other open PRs or conflicts called out
- [ ] Docs updated where behavior changed (HANDOVER, HANDOFF, AUDIT_STATUS, README, CHANGELOG as applicable)
- [ ] No migration run, deploy, or dev-DB change included unless the Phase 0 run rules are satisfied: handover §7 hold-release gates passed, fresh backup taken, independent Reality Checker pass (hold D5 blocks timing, not authority)

## Validation

<!-- Commands run and their results; test evidence; any UNVERIFIED items stated explicitly. -->
