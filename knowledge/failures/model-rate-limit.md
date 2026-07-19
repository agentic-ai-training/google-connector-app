---
type: runbook
title: Model rate-limit recovery
description: Handles exhausted model quota without unsafe quality degradation.
owner: project-admin
version: 1
visibility: public
tags: [model, quota, recovery]
timestamp: 2026-07-19T00:00:00Z
---
# Response

Record the provider/model/limit and retry timing. For a complex external mutation, do not silently replace the approved quality model with an unreliable fallback. Pause or use a constrained validated plan. For safe reads, an approved fallback may be used and must be recorded.

# Prevention

Estimate run budget, reserve quality-model quota for complex workflows, prevent unbounded loops, and answer simple capability questions deterministically.
