## 2026-08-21 - Added missing ARIA labels to close buttons
**Learning:** Found several icon-only close buttons lacking aria-labels, making them inaccessible to screen readers. Specifically in modal dialog components like CreateNodeDialog and EditEdgeDialog.
**Action:** Ensure all icon-only buttons include descriptive aria-labels. When dealing with generic close buttons across multiple dialogs, standardize on aria-label="Close" to improve screen reader experience.
