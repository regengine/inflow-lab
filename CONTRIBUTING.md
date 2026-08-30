# Contributing to Inflow Lab

## Checks

CI runs these, and they are the same commands to run locally. All four must
pass before a change lands.

| Check | Command | Covers |
|---|---|---|
| Lint | `uv run --frozen --group dev ruff check app scripts tests` | `app/`, `scripts/`, `tests/` |
| Type-check | `uv run --frozen --group dev mypy` | `app/`, `scripts/` |
| Tests | `uv run --frozen --group dev pytest` | `tests/` |
| Console lint | `npm ci && npm run lint` | `app/static/*.js` |

`ruff check --fix` applies the mechanical fixes. Configuration lives in
`pyproject.toml` (`[tool.ruff]`, `[tool.mypy]`) and `eslint.config.mjs`; each
disabled rule carries a comment saying why, so if one is in your way, read that
first and change it deliberately rather than adding a local suppression.

The rule sets are a starting posture, not a finished one: they were chosen so
the gate went in green rather than alongside a backlog of pre-existing
violations, since a gate that starts red gets switched off. Tighten them by
removing entries from the `ignore` lists, not by adding per-file suppressions.

There is also a browser smoke test, which CI runs and which needs a real
Chromium:

    uv sync --group browser
    uv run --frozen --group browser playwright install --with-deps chromium
    uv run --frozen --group browser python scripts/browser_smoke.py


## Developer Certificate of Origin (DCO)

By contributing to this repository, you certify the following for each commit
(Developer Certificate of Origin 1.1, https://developercertificate.org/):

- The contribution was created in whole or in part by you, and you have the
  right to submit it under the Apache License, Version 2.0; or
- The contribution is based upon previous work that is, to the best of your
  knowledge, covered under an appropriate open-source license, and you have
  the right to submit that work with modifications under the Apache License,
  Version 2.0; and
- You understand and agree that this project and your contribution are public,
  and that a record of the contribution (including your sign-off) is
  maintained indefinitely.

Sign off each commit with:

    git commit -s

which appends:

    Signed-off-by: Your Name <you@example.com>

Contributions are accepted under the project's Apache License, Version 2.0
("inbound = outbound"). No contributor license agreement is required. Do not
submit code you do not have the right to contribute — including output of
third-party tools whose terms prohibit it.
