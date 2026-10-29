## 2024-09-26 - Add Tooltips to Icon-Only Buttons
**Learning:** Icon-only buttons often have `aria-label`s for screen reader support but lack native visual tooltips for sighted users. The lack of `title` attributes makes it harder for users to understand what icons mean, reducing the intuitiveness of the UX.
**Action:** Always ensure that icon-only interactive elements (like custom dialog close buttons, zoom controls, etc.) carry a `title` attribute reflecting the same information as the `aria-label`. Use the standard translation function where applicable (`title={t('key')}`).

## 2024-05-15 - Dialog accessibility
**Learning:** Found multiple instances of custom dialog components (e.g. `ConfirmDialog`, `InputDialog`, `AgentRunsDialog`, etc.) lacking proper ARIA attributes for accessibility. Specifically, many dialog wrappers are missing `role="dialog"`, `aria-modal="true"`, and `aria-labelledby` or `aria-label` attributes.
**Action:** Always verify that custom modal or dialog implementations include standard ARIA attributes (`role="dialog"`, `aria-modal="true"`, and an `aria-labelledby` attribute linked to an `id` on the title element) to ensure screen reader accessibility.
