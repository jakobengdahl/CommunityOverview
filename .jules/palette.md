## 2024-09-26 - Add Tooltips to Icon-Only Buttons
**Learning:** Icon-only buttons often have `aria-label`s for screen reader support but lack native visual tooltips for sighted users. The lack of `title` attributes makes it harder for users to understand what icons mean, reducing the intuitiveness of the UX.
**Action:** Always ensure that icon-only interactive elements (like custom dialog close buttons, zoom controls, etc.) carry a `title` attribute reflecting the same information as the `aria-label`. Use the standard translation function where applicable (`title={t('key')}`).
## 2024-11-20 - Ensure `.dialog-content` wrappers have dialog roles
**Learning:** React components that use the `.dialog-content.subscription-dialog` custom wrapper pattern did not natively inherit standard dialog ARIA attributes, causing accessibility regressions for screen readers.
**Action:** When creating or updating custom dialog components utilizing a `.dialog-content` or similar wrapper pattern, strictly include standard ARIA attributes (`role="dialog"`, `aria-modal="true"`, and an `aria-labelledby` attribute linked to an `id` on the title element) to ensure proper accessibility.
