---
id: wpf_virtualized_tree
title: WPF VirtualizingStackPanel Element Absence
app: wpf_generic
surface: WPF
symptom: UIA search fails for elements that are known to exist in a table, list, or tree view.
root_cause: WPF VirtualizingStackPanel cleans up and destroys AutomationPeers for elements scrolled outside the current physical viewport.
mitigation: System 2 must emit scroll or pagedown commands to scroll items into the viewport before searching the UIA tree.
tags: [wpf, uia, virtualization, scroll, table]
related_nodes: [surface_wpf, bridge_uia_sensor, playbook_failure_recovery]
---

## Description
WPF applications that host large collections (`ListBox`, `DataGrid`, `TreeView`) enable virtualization by default (`VirtualizingStackPanel.IsVirtualizing="True"`). To conserve memory, WPF only instantiates visual elements and `AutomationPeer` objects for items that are currently positioned inside the visible bounding box of the container. Off-screen items do not exist in the Win32 accessibility hierarchy.

## Mitigation Playbook
1. When a target item is not found via `uia_sensor`:
   - Inspect the container's scroll patterns (`IScrollPattern`).
   - If scrollable, emit `scroll down` or `press pagedown`.
   - Wait for `settling` watchdog (100ms).
   - Re-scan `uia_sensor`.
2. If UIA tree still fails to populate the item after 2 scroll attempts:
   - Fall back to `mirage_sensor` to optically inspect if text is rendered on screen without accessible peers.
