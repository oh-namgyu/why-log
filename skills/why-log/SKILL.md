---
name: why-log
description: Reconstruct WHY a codebase is the way it is — design decisions (ADRs) recovered from git history with verbatim evidence, and questions for the maintainer where history is silent. Use when the user asks "why was this built this way", "recover/document our design decisions", "write ADRs from history", "decision log", "architecture archaeology", or wants to onboard onto a repo's past choices.
---

# why-log

Recover the design decisions of a git repository. A reason is written down only when a quote
copied verbatim from history states it; anything weaker becomes a question for the user. "Unknown"
is an acceptable final answer — never fill a gap with a plausible guess.

The engine is `scripts/whylog.py` in this plugin (two directories above this file). Every model call
it makes is tool-less and isolated from the user's configuration; repository content is treated as
untrusted data. Results stay in `<repo>/.why-log/` (git-excluded) unless the user asks for `--out`.

## Steps

1. **Preview.** Run `python3 <plugin>/scripts/whylog.py run <repo> --dry-run`. Show the user the pack
   size, commit count and which docs will be sent to the model. Ask before sending anything if the repo
   looks private or large (> 300k tokens). Offer `--hide <glob>` for paths they want excluded.
2. **Reconstruct.** Run `python3 <plugin>/scripts/whylog.py run <repo> --json` (can take several minutes;
   run it in the background and wait). It prints a summary: decisions by confidence, questions queued,
   run id. Exit code 1 means some decisions are unresolved — that is normal, not a failure.
3. **Ask.** Unless the user chose abstain mode, fetch questions with
   `python3 <plugin>/scripts/whylog.py questions <repo> --next 8 --json` and ask them with
   AskUserQuestion — at most 4 questions per call, so 8 questions take two calls.
   - Show the evidence line in the question text so the user can recall the context.
   - Use the queued options as choices (at most 3), always including one labelled "Unknown / don't remember".
     The tool adds "Other" for a free-text reason.
   - Never pre-select, rephrase leadingly, or suggest which option is likely.
4. **Record.** For each answer run `python3 <plugin>/scripts/whylog.py answer <repo> --id <Qid>` with
   exactly one of `--choice "<option text>"`, `--free "<their words>"`, or `--unknown`. Record "Other"
   answers with `--free`. If the command reports a conflict with code evidence, tell the user — both
   versions are kept, nothing is overwritten.
5. **Render.** Run `python3 <plugin>/scripts/whylog.py render <repo>` and show the index path. Report
   counts: explicit / inferred / testimony / unknown, and how many questions remain for a later session.
6. **Resume later.** `questions` only returns open items; the queue file
   (`<repo>/.why-log/questions.jsonl`) can also be edited by hand and re-rendered.

## Modes

- **Abstain** (`run --abstain`, or when the original authors are not available): skip step 3–4;
  every unsupported decision is listed in `unknowns.md` instead of being asked.
- **Write into the repo** (`render --out docs/adr`): only when the user asks. The command refuses to
  overwrite files it did not generate and shows a preview first.
- **Undo** (`undo <repo> <run-id>`): restores the files a render changed.

## What it cannot do

Decisions that left no change in history (for example "we evaluated X and kept Y") cannot be
recovered from git. Ask the user about them directly if they matter.
