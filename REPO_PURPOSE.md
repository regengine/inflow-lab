# Repository Purpose

This repository contains RegEngine Inflow Lab, a non-production FSMA 204 simulator.

Inflow Lab generates deterministic supply-chain lifecycle data so RegEngine can demonstrate, test, and validate the FSMA ingestion flow without relying on live customer systems.

## Purpose

- Generate deterministic FSMA lifecycle events
- Produce RegEngine-compatible ingest payloads
- Stress-test ingestion and validation behavior
- Simulate errors and recovery scenarios
- Support demos, onboarding, and design-partner walkthroughs

## Non-Goals

- Customer-facing production application
- Source of record for compliance evidence
- Replacement for RegEngine canonical persistence
- General ERP system
- Non-FSMA simulation platform

## Relationship To RegEngine

Inflow Lab feeds RegEngine. It does not replace RegEngine.

```text
inflow-lab -> RegEngine ingestion -> validation -> evidence -> FDA export
```

