# Contributing to Inflow Lab

<<<<<<< HEAD
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

=======
## Running the lint and type gates locally

CI's `lint` job (`.github/workflows/ci.yml`) runs exactly three commands. Run
the same three before pushing and the job cannot tell you anything new:

```bash
uvx ruff@0.15.8 check .
uvx mypy@1.19.1
for file in app/static/*.js; do node --check "$file"; done
```

Notes on why each is shaped the way it is:

- **The tool versions are pinned, and pinned in two places.** `ruff` and `mypy`
  run through `uvx <tool>@<version>` rather than as dependency-group entries:
  neither is imported by the app, and keeping them out of `uv.lock` leaves
  `uv pip check` and `pip-audit` reasoning about runtime dependencies only.
  Pinning also means an upstream release cannot turn CI red on its own. The
  pins live in `.github/workflows/ci.yml` and are repeated in the comment
  above `[tool.ruff]` in `pyproject.toml`; if you bump one, bump both, or local
  runs and CI stop agreeing.
- **Rule selection and scope live in `pyproject.toml`, not on the command
  line.** `[tool.ruff.lint]` selects `E4`, `E7`, `E9`, `F`, `W` — close to
  ruff's defaults, deliberately without `E501`, since the existing code has
  long lines throughout and reflowing them would be churn rather than a fix.
  `[tool.mypy]` sets `files = ["scripts"]`: `app/` still has real annotation
  gaps, and a gate that lands red is a gate everyone learns to ignore. Widen
  `files` once those are cleared. Because the config carries the scope, `uvx
  mypy@1.19.1` takes no path argument.
- **There is no `package.json` and no ESLint.** The operator console is
  vanilla ES modules served straight out of `app/static`, so `node --check` is
  the gate that needs no toolchain and no config, and it catches the failure
  that actually breaks the dashboard — a syntax error that only surfaces when a
  browser parses the file. Check every file in `app/static/*.js`, not just
  `app.js`: the console was split into modules, so a single-file check misses
  most of it.

CI also runs the test suite separately (`uv run --frozen --group dev pytest`,
on both 3.11 and 3.12). Run `uv run pytest` after changing Python code.
>>>>>>> origin/main

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
