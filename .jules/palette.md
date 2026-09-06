## 2026-09-06 - Accessible Search Results
**Learning:** Found an opportunity to improve accessibility of `FloatingSearch` by adding `aria-label` to the input field and search results. The input field only had a placeholder, which is insufficient for all screen readers. The search results were buttons without explicit ARIA labels.
**Action:** When creating custom search components, explicitly provide `aria-label` for both the input field and individual result buttons to ensure accurate accessibility for screen readers.
