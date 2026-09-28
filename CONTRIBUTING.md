# Contributing

- Python 3.9+ and the standard library only — no runtime dependencies.
- Run the suite with `python3 -m unittest discover -s tests -v`. Tests never call a model; the one
  live test is skipped unless `WHYLOG_LIVE=1`.
- Every model call goes through `scripts/llm.py`. Do not add another path, and do not loosen the
  isolation flags there without a canary test showing the new behaviour.
- The gates may only **lower** confidence. A change that can promote a claim needs a counterexample
  test (see `tests/test_gates.py`).
- Keep files under 300 lines and functions under 50 lines.
