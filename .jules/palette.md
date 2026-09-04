## 2026-08-21 - Added missing ARIA labels to close buttons
**Learning:** Found several icon-only close buttons lacking aria-labels, making them inaccessible to screen readers. Specifically in modal dialog components like CreateNodeDialog and EditEdgeDialog.
**Action:** Ensure all icon-only buttons include descriptive aria-labels. When dealing with generic close buttons across multiple dialogs, standardize on aria-label="Close" to improve screen reader experience.

## 2024-09-02 - Custom Dialog Accessibility
**Learning:** Standard ARIA attributes (`role="dialog"`, `aria-modal="true"`, and `aria-labelledby` linked to an `id` on the title element) are required for custom dialog components in React to ensure they are properly identified and read by screen readers. Some legacy components like `ConfirmDialog`, `InputDialog`, and `SettingsDialog` were missing these properties.
**Action:** When creating or updating custom dialog components in the frontend, strictly include these standard ARIA attributes.
## 2024-09-04 - Accessible names for custom interactive chips
**Learning:** Custom UI elements like chips with remove buttons (e.g., `SubtypeInput`) often lack accessible names because they are just an '×' character. It's critical to provide an `aria-label` that includes the dynamic item name (e.g., "Remove {subtype}") so screen reader users know exactly what they are removing, and a `title` provides identical clarity for mouse/hover users.
**Action:** Always check custom compound components (like tags, chips, or multi-select items) to ensure their embedded action buttons have descriptive `aria-label`s and `title`s tied to their specific content.
