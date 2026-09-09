## 2026-08-21 - Added missing ARIA labels to close buttons
**Learning:** Found several icon-only close buttons lacking aria-labels, making them inaccessible to screen readers. Specifically in modal dialog components like CreateNodeDialog and EditEdgeDialog.
**Action:** Ensure all icon-only buttons include descriptive aria-labels. When dealing with generic close buttons across multiple dialogs, standardize on aria-label="Close" to improve screen reader experience.

## 2024-09-02 - Custom Dialog Accessibility
**Learning:** Standard ARIA attributes (`role="dialog"`, `aria-modal="true"`, and `aria-labelledby` linked to an `id` on the title element) are required for custom dialog components in React to ensure they are properly identified and read by screen readers. Some legacy components like `ConfirmDialog`, `InputDialog`, and `SettingsDialog` were missing these properties.
**Action:** When creating or updating custom dialog components in the frontend, strictly include these standard ARIA attributes.
## 2024-05-18 - Missing ARIA structures in dialogs
**Learning:** Found a codebase-specific pattern where custom React modal dialogs frequently lack structural ARIA properties (`role="dialog"`, `aria-modal="true"`, and `aria-labelledby`). Without these, screen readers won't announce the element as a modal or read its title context properly, impairing the UX for visually impaired users.
**Action:** When implementing or editing modal dialog components in this codebase, explicitly include `role="dialog"`, `aria-modal="true"`, and link an `id` on the modal's primary heading (`<h2>`/`<h3>`) to the dialog container via `aria-labelledby`.
