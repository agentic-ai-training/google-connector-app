---
type: capability
title: Google service and tool catalog
owner: project-admin
version: 2
timestamp: 2026-07-29T00:00:00Z
visibility: public
publication_status: approved
approved_by: project-admin
approved_at: 2026-07-19T00:00:00Z
tags: [tools, services, oauth]
tools: [search_gmail, send_gmail, search_drive, create_google_sheet, create_calendar_event, resolve_chat_destination, send_chat_message, create_meet_space, create_task, search_contacts]
---
# Service boundaries

Gmail handles mail; Drive handles file discovery and sharing; Docs and Sheets handle
content; Calendar creates scheduled invitations and Meet links; Meet handles spaces,
conference records, and participants; Chat handles spaces/messages; Tasks and Contacts
use structured lookup. A capability answer never needs an LLM or live mutation.

Chat direct-message sends are an ordered capability: resolve or idempotently set up the
human DM, verify its `spaces/...` resource, then send and verify the message. Setup
requires the separately consented `chat.spaces.create` scope.

# Authority

The runtime tool registry and OAuth scope list are authoritative. Unknown tool names
must fail plan validation rather than being invented.
