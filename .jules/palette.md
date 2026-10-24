## 2024-09-26 - Add Tooltips to Icon-Only Buttons
**Learning:** Icon-only buttons often have `aria-label`s for screen reader support but lack native visual tooltips for sighted users. The lack of `title` attributes makes it harder for users to understand what icons mean, reducing the intuitiveness of the UX.
**Action:** Always ensure that icon-only interactive elements (like custom dialog close buttons, zoom controls, etc.) carry a `title` attribute reflecting the same information as the `aria-label`. Use the standard translation function where applicable (`title={t('key')}`).

## 2024-10-24 - Add ARIA roles to custom dialog components
**Learning:** Custom dialog wrapper components (like those utilizing the `.dialog-content.subscription-dialog` pattern) need explicit ARIA attributes since they don't inherently communicate their modal nature to assistive technologies. Simply styling a `<div>` as a modal overlay is insufficient for screen reader accessibility.
**Action:** When creating or updating custom dialog components, always include `role="dialog"`, `aria-modal="true"`, and an `aria-labelledby` attribute linked to an `id` on the dialog's title element (e.g., `<h2>`).
