# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/), versioning: [SemVer](https://semver.org/).

## [0.1.0] - 2026-09-28

Initial public release.

### Added
- `why-log` Claude Code plugin: reconstruct design decisions from git history into MADR-style ADRs.
- Two-stage evidence gate: verbatim quote existence (git) + a blind support judge; confidence can only go down.
- Question queue (`qa-queue/1`) with priorities, resumable answers, maintainer testimony and conflict tracking.
- Transactional rendering with preview, refusal to overwrite user files, and `undo`.
- Budgeted history packs with diff trimming and time-window splitting; rename-aware hiding for evaluation.
- `scripts/whylog.py` CLI with `run`, `questions`, `answer`, `render`, `undo` and `status`.
- Evaluation harness and a public ADR benchmark under `eval/`; results, including unmet targets, are in the README.
