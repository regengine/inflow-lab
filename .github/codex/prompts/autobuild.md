# RegEngine Codex Autopilot

Read `AGENTS.md`, `README.md`, and `.agents/skills/regengine-api-contract/SKILL.md` before changing anything.

Your job is to make one safe, compounding improvement to this repository without needing a custom prompt from a human.

## Operating sequence

1. Run `uv run pytest` first.
2. If the baseline test suite fails, stop and explain the failure instead of changing code.
3. Read `AUTOPILOT_TASKS.md` and choose the highest-priority unchecked task you can complete safely in a single run.
4. Make the smallest coherent batch of changes needed for that task.
5. Run `uv run pytest` again after the edits.
6. Update `AUTOPILOT_TASKS.md` to reflect what you completed, what remains, or what is blocked.
7. Update `README.md` if setup steps, behavior, or endpoints changed.

## Hard constraints

- Keep the current RegEngine webhook contract intact.
- Keep delivery mode defaulted to `mock`.
- Do not commit secrets, tokens, or sample credentials.
- Do not commit generated runtime data, transcripts, or local environment files.
- Do not introduce React, Vue, or another frontend framework unless there is a compelling need.
- Prefer one theme of work per run. If a task is too large, ship the safest useful slice and document the remainder.

## Completion standard

- Leave the repo in a passing state.
- Summarize what changed.
- List the commands you ran.
- Call out any remaining risks or follow-up tasks.
