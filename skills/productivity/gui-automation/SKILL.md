---
name: gui-automation
description: Safe, element-anchored cross-platform GUI automation and on-screen element understanding
tags: [gui, automation, accessibility, desktop, ui]
---

# GUI Automation & On-Screen Element Understanding

This skill enables Hermes to interact with native and virtual desktop applications using structured on-screen element trees and safe execution primitives.

## Workflow

1. **Scan Screen & Detect Elements**:
   - Invoke `gui_elements()` to retrieve the list of accessible UI elements (buttons, inputs, text fields, tabs) with stable element IDs.
2. **Plan & Execute Targeted Actions**:
   - Use `gui_act(element_id=..., action="click" | "type" | "focus" | "scroll", text=...)` to interact directly with discrete element IDs rather than fragile raw pixel coordinates.
3. **Verify State**:
   - Call `gui_screenshot()` to capture the result of the interaction and visually verify completion.
