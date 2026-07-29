# Repository Purpose

This repository contains RegEngine Inflow Lab, a non-production FSMA 204 simulator.

Inflow Lab plays the role of a RegEngine customer's own software — a fictional factory's production system (Meridian Fresh Foods) — so RegEngine can demonstrate, test, and validate the full customer experience without relying on live customer systems: onboarding, credential configuration, ingestion, per-event rejection and recovery, and evidence export.

## Purpose

- Act as the customer-side system in end-to-end RegEngine exercises
- Generate deterministic FSMA lifecycle events
- Produce RegEngine-compatible ingest payloads and deliver them the way a real integrator does (API key, tenant header, idempotency keys, optional HMAC)
- Stress-test ingestion and validation behavior, including the friction customers hit (KDE rejections, auth/billing/rate-limit failures, idempotent retries)
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

