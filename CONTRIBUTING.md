# Contributing to Inflow Lab

## Checks

Run before opening a pull request:

    uv run --frozen --group dev pytest -q
    uvx ruff check .

`ruff check .` currently exits clean, and the rule set it enforces is pinned in
`pyproject.toml` under `[tool.ruff.lint]` (`select = ["E4", "E7", "E9", "F"]`)
with a comment explaining why that slice and not a broader one. Keep it clean:
the handful of legitimate exceptions carry an inline `# noqa` naming the reason,
so a new finding is a real one. `ruff` is not yet in the `dev` dependency group
and no workflow runs it — wiring that up is the remaining half of issue #137.

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
