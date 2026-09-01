## 2026-08-21 - Added missing ARIA labels to close buttons
**Learning:** Found several icon-only close buttons lacking aria-labels, making them inaccessible to screen readers. Specifically in modal dialog components like CreateNodeDialog and EditEdgeDialog.
**Action:** Ensure all icon-only buttons include descriptive aria-labels. When dealing with generic close buttons across multiple dialogs, standardize on aria-label="Close" to improve screen reader experience.
## 2024-11-20 - Missing ARIA roles on custom dialog components
**Learning:** Custom dialog components in this app (like ConfirmDialog, InputDialog, SettingsDialog) were missing standard `role="dialog"`, `aria-modal="true"`, and `aria-labelledby` attributes, which are critical for screen readers to properly announce them.
**Action:** When creating or updating custom dialogs/modals in the future, always ensure they have these three ARIA attributes and a corresponding `id` on their title element.
