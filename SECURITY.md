# Security

## What leaves your machine

why-log sends **one thing** to a model: the history pack — commit messages, per-file change stats,
selected diffs and a few docs at HEAD of the repository you point it at. `run --dry-run` shows its
size and the docs it contains before anything is sent, and `--hide <glob>` removes paths (and every
past version of them, following renames) from the pack. Nothing else is uploaded, and there is no
telemetry.

Model calls go through the `claude` CLI you are already logged into (`claude -p`). No API key is read
or stored by why-log.

## Repository content is untrusted

Commit messages and diffs can contain text written to manipulate a model ("ignore previous
instructions…"). why-log treats them as data:

- Every model call runs with **no tools** (`--tools "" --strict-mcp-config`) and in **safe mode**
  (`--safe-mode`), so the model cannot read files, run commands or reach MCP servers, and your own
  `CLAUDE.md`, memory and plugins are not mixed into the analysis. We verified both with canary
  prompts; `--disallowedTools` alone does **not** block MCP tools.
- Repository text is wrapped as data in the prompt, and model output is only adopted after a
  deterministic schema check.
- A reason is written as "stated in history" only when a quote exists **verbatim** in the pinned
  history (checked by `git`, not by the model) **and** a separate, blind judge agrees the quote states
  that reason. The test suite includes a commit that tries prompt injection.

## What it writes

- By default everything stays in `<repo>/.why-log/`, which is added to `.git/info/exclude` so it is
  never committed by accident. Answers you give are stored there in plain text.
- `render --out <dir>` writes into your repository only when you ask. It previews changes, refuses to
  overwrite files it did not generate, writes through a staging directory, and `undo <run-id>`
  restores the previous state from a backup.

## Reporting a vulnerability

Please open a [private security advisory](https://github.com/oh-namgyu/why-log/security/advisories/new)
rather than a public issue.
