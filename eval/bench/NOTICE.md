# Third-party material in the benchmark

`gold/*.json` and `../results/public/**` contain short excerpts (titles, decisions and reasons, condensed)
extracted from the Architecture Decision Records of three open-source repositories, at the pinned commits
below. They are used only as evaluation ground truth. All rights remain with their authors.

| file | source (pinned commit) | license | notice |
|---|---|---|---|
| `gold/educational-platform.json` | [anton-liauchuk/educational-platform](https://github.com/anton-liauchuk/educational-platform) `a6f575c3df95acd0e2891f23f8cab229cbb89c87`, `docs/architecture-decisions/` | MIT | Copyright (c) 2020 Anton Liauchuk — full text in [LICENSES/MIT-educational-platform.txt](LICENSES/MIT-educational-platform.txt) |
| `gold/adrs.json` | [joshrotenberg/adrs](https://github.com/joshrotenberg/adrs) `d3b08eaccc6c4d1eee1c7a216694e2ad6d6b7030`, `doc/adr/` | MIT OR Apache-2.0 (used under Apache-2.0) | [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt) |
| `gold/pypyr.json` | [pypyr/pypyr](https://github.com/pypyr/pypyr) `b3e8f8c6063c11e37c1c762b89b4cd8620460c79`, `docs/adr/` | Apache-2.0 | [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt) |

Modifications: the ADR text was extracted and condensed into the fields of `why-log-gold/1`; nothing else
from these repositories is included.
