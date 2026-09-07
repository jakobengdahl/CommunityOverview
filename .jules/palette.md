## 2026-08-21 - Added missing ARIA labels to close buttons
**Learning:** Found several icon-only close buttons lacking aria-labels, making them inaccessible to screen readers. Specifically in modal dialog components like CreateNodeDialog and EditEdgeDialog.
**Action:** Ensure all icon-only buttons include descriptive aria-labels. When dealing with generic close buttons across multiple dialogs, standardize on aria-label="Close" to improve screen reader experience.

## 2024-09-02 - Custom Dialog Accessibility
**Learning:** Standard ARIA attributes (`role="dialog"`, `aria-modal="true"`, and `aria-labelledby` linked to an `id` on the title element) are required for custom dialog components in React to ensure they are properly identified and read by screen readers. Some legacy components like `ConfirmDialog`, `InputDialog`, and `SettingsDialog` were missing these properties.
**Action:** When creating or updating custom dialog components in the frontend, strictly include these standard ARIA attributes.

## 2024-09-02 - Accessible Chip Components
**Learning:** Custom interactive UI elements like chips, tags, or multi-select items with symbol-only action buttons (e.g., "×" for removal) must provide both an `aria-label` and a `title` to ensure accurate accessibility for screen readers and helpful tooltips for visual users.
**Action:** Explicitly provide both an `aria-label` and a `title` (e.g., `aria-label={"Remove ${item}"}`) when creating or updating chip components with symbol-only removal buttons.
