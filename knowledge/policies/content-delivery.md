---
type: policy
title: Content delivery safety boundary
description: Classifies disallowed sexual-content delivery before planning or provider access.
owner: project-admin
version: 1
visibility: public
publication_status: approved
approved_by: project-admin
approved_at: 2026-08-01T00:00:00Z
tags: [policy, safety, delivery, chat, gmail]
timestamp: 2026-08-01T00:00:00Z
---
# Rule

Classify a request to transmit sexual imagery or video before planning. When adult
status, consent, and ownership cannot be established, do not initiate delivery. Record
the result as a policy decision, not as planning, model, tool-selection, or Google API
failure. Do not call an LLM, tenant RAG, or a Workspace API for the rejected delivery.

# Boundary

This rule does not block ordinary lawful writing, relationship guidance, medical or
educational discussion, or non-sexual Workspace delivery. Hard enforcement remains in
versioned application code; this document supplies human-readable provenance and cannot
weaken the enforced rule.

