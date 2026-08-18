## 2025-02-23 - ARIA Label on Toolbar Buttons
**Learning:** Found an icon-only button without an ARIA label in `FloatingToolbar.jsx`. Because it used dynamic icon generation based on node type, the `aria-label` had to be derived using the existing `getTooltipLabel(nodeType)` function to ensure consistency between the visual tooltip and screen reader output.
**Action:** Use existing tooltip label generation functions to populate `aria-label` for dynamically generated icon-only buttons.
