# Learned Quirks & Compounding Knowledge Base

This directory stores application-specific quirks, edge-case mitigations, and recovery heuristics discovered dynamically during Servo runs or manually curated by operators.

---

## Quirk Schema (Markdown + YAML Frontmatter)

Each quirk file must adhere to this structure so **Graphify** can index it into `graph.json` and link it to the appropriate Windows UI surface, OS primitive, and Servo bridge:

```markdown
---
id: quirk_unique_id
title: Short Human-Readable Title
app: Target Application Name or Process (e.g. msedge, code, slack)
surface: Win32 | WPF | Electron | Browser | Canvas
symptom: What failure or unexpected behavior occurs
root_cause: Why Windows or the application behaves this way
mitigation: The exact command, flag, or recovery action to take
tags: [tag1, tag2]
related_nodes: [node_id_1, node_id_2]
---

## Description
Detailed explanation of the phenomenon.

## Mitigation Playbook
Step-by-step instructions for System 2.
```
