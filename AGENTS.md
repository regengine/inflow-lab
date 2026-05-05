# AGENTS.md

## Mission

Build and maintain a realistic RegEngine-compatible FSMA 204 inflow simulator.

The simulator must prioritize:

1. payload correctness against the current RegEngine ingest contract
2. realistic lot lineage across CTEs
3. mock-first safety
4. clean operator UX for demos and design-partner testing

## Non-negotiables

- Keep the live ingest payload compatible with the documented RegEngine webhook shape:
  - top-level `source`
  - top-level `events[]`
  - per-event fields:
    - `cte_type`
    - `traceability_lot_code`
    - `product_description`
    - `quantity`
    - `unit_of_measure`
    - `location_name`
    - `timestamp`
    - `kdes`
- Do not remove or rename public API routes without updating tests and README.
- Preserve mock mode as the default so contributors do not accidentally send traffic to a live workspace.
- Prefer deterministic tests over brittle snapshot tests.
- When adding new CTE logic, maintain lot lineage so the trace feels legitimate rather than random.

## Working agreements

- Before changing code, read `README.md` and `.agents/skills/regengine-api-contract/SKILL.md`.
- In unattended GitHub Action runs, also read `AUTOPILOT_TASKS.md` and complete at most one priority theme per run.
- After changing Python code, run `uv run pytest`.
- After changing frontend behavior, manually verify the dashboard still starts, steps, resets, and loads lineage.
- Keep dependencies light. Avoid bringing in a frontend framework unless there is a clear win.
- Prefer small, composable Python modules over one giant file.

## UX expectations

- The dashboard should stay operator-friendly.
- Every important action should surface a visible success or error state.
- Avoid tables full of opaque IDs without enough context to understand the flow.

## Data-flow realism rules

- Harvesting should originate at farms.
- Cooling should move harvested lots through cooler facilities.
- Initial packing should create downstream packed lots.
- Shipping should create a believable destination and a reference document.
- Receiving should correspond to an actual prior shipment.
- Transformation should consume input lots and emit a new output lot.
- FDA export helpers should always be derivable from stored event records.

## Definition of done

A change is done when:

- tests pass
- README reflects the new behavior
- the mock flow still works end-to-end
- the change does not break the current RegEngine payload contract
