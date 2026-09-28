# why-log

[![CI](https://github.com/oh-namgyu/why-log/actions/workflows/ci.yml/badge.svg)](https://github.com/oh-namgyu/why-log/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.9%2B-blue)

> **한글 요약** — git 히스토리를 한 번에 읽어 **"왜 이렇게 만들었나"(설계 결정, ADR)를 역설계**하는 Claude Code 플러그인입니다.
> 이유는 히스토리에 **원문 그대로 존재하는 인용**이 그 이유를 직접 말할 때만 적고, 근거가 약하거나 없으면 **지어내지 않고 사람에게 묻습니다**("모름"도 정상 답).
> 모든 모델 호출은 도구 없이·사용자 설정과 격리되어 실행되며, 결과는 로컬 `.why-log/` 에만 남습니다.

Recover the design decisions buried in a repository's history — with evidence, and with questions instead of guesses.

## Why

Commit history records *what* changed. The *why* — the alternatives rejected, the incident that forced a
change — is usually scattered across commit bodies, code comments and people's memories. A language model can
read an entire history at once now, but it will also happily invent a plausible motive. why-log is built
around that failure mode:

- A reason is written as **"stated in history"** only if a quote exists **verbatim** in the pinned history
  (checked with `git`, not by the model) **and** a separate, blind judge agrees the quote states that reason.
- Everything weaker is labelled **inferred**, or becomes a **question** for the maintainer.
  **"Unknown"** is a legitimate final answer.
- The gates can only **lower** confidence, never raise it.

## Install

```sh
git clone https://github.com/oh-namgyu/why-log
claude --plugin-dir ./why-log      # load for one session
```

Requirements: the `claude` CLI (logged in), `git`, Python 3.9+. No API key and no Python packages.

Then ask Claude something like *"why-log this repo"* or *"recover our design decisions"*. The skill runs a
dry-run preview first, reconstructs decisions, asks you a few questions, and renders ADRs.

## What you get

```
.why-log/adr/
  README.md                       index with provenance labels
  0001-switch-storage-to-sqlite.md  Decision · Alternatives · Reason · Evidence (verbatim quotes)
  unknowns.md                     decisions whose reason is still unknown, with the question to ask
```

Each ADR carries one provenance label: *Stated in history*, *Inferred from history*, or *Maintainer
testimony*. When testimony disagrees with the history, both are kept and marked ⚠ — nothing is overwritten.

## CLI

The skill drives `scripts/whylog.py`; you can use it directly:

| command | does |
|---|---|
| `run <repo> [--dry-run] [--hide GLOB] [--abstain]` | build the history pack, reconstruct, gate, queue questions, render |
| `questions <repo> --next N` | open questions, highest priority first |
| `answer <repo> --id Q… --choice TEXT \| --free TEXT \| --unknown` | record an answer |
| `render <repo> [--out docs/adr] [--preview]` | write ADRs transactionally (refuses to overwrite files it did not generate) |
| `undo <repo> <run-id>` | restore what a render changed |
| `status <repo>` | counts of decisions and questions |

## How well does it work?

Measured on 2026-09-28 with `claude-opus-5-5[1m]`, one run per repository. Details and reproduction:
[`eval/README.md`](eval/README.md).

**Setup.** Gold = decisions whose reason the maintainers had written down (handoff notes; ADRs for public
repos). Those documents are **hidden** from the tool — paths, all past versions and renames — and the tool
must recover the decisions from the remaining history. Criteria were pre-registered before the runs.

| data | decisions in gold | recall (traceable gold) | reasons judged consistent among matches |
|---|---|---|---|
| 3 private repositories of the author (T2) | 75 (66 traceable) | 63.6% / 95.2% / 78.3% | 52 of 53 matched pairs consistent, 1 unsupported, 0 contradicted |
| same, commit bodies stripped (T3) | 75 | 63.6% / 76.2% / 82.6% | 47 of 50 consistent, 3 unsupported — reasons also live in diffs and code comments |
| [educational-platform](https://github.com/anton-liauchuk/educational-platform) (ADR hidden) | 13 | 84.6% | caveat: its README repeats the ADRs |
| [joshrotenberg/adrs](https://github.com/joshrotenberg/adrs) | 9 | 55.6% | 33% of gold reasons recovered |
| [pypyr](https://github.com/pypyr/pypyr) | 5 | 100% | 40% of gold reasons recovered |

What did **not** meet the pre-registered bar, stated plainly:

- **Precision** (20–29% on private repos, 4–45% public) — the gold lists are samples (25 or fewer decisions)
  while the tool reports 38–84 per repo, so most "unmatched" reconstructions are real decisions nobody
  recorded. A human spot check of the unmatched lists agreed; but as defined, the target (≥80%) was not met,
  and precision is not measurable with sampled gold.
- **Question efficiency** (target ≥0.5, measured 0.0) — a simulated maintainer answered 60 questions; none
  changed a matched decision, because questions go to decisions that are either already well-evidenced or
  outside the gold sample. The value of the question loop is **not demonstrated** by this benchmark.
- Hidden-document leakage: after excluding text that is also in visible commits, residual overlap was 0 in
  two repos and 11 8-grams (shell commands and paths, not decisions) in the third.

The judge was spot-checked by hand on 13 of the 21 sampled private pairs: all 13 matches and verdicts agreed.
The public-repo judgements have not been hand-checked.

## Limits

- **Only decisions that left a change in history can be recovered.** "We evaluated X and kept Y" leaves no
  commit; ask about those directly.
- Output quality depends on how much *why* the history contains. Terse histories give terse reasons, and more
  questions.
- Very long histories are split into time windows; decisions spanning windows are merged by title only.

## Security

See [SECURITY.md](SECURITY.md) — what is sent to the model, how repository text is treated as untrusted, and
what is written where.

## License

MIT — see [LICENSE](LICENSE). Security policy: [SECURITY.md](SECURITY.md).

The public benchmark includes condensed excerpts of third-party ADRs (MIT / Apache-2.0); see
[eval/bench/NOTICE.md](eval/bench/NOTICE.md).
