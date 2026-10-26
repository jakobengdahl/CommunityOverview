## 2024-09-26 - Add Tooltips to Icon-Only Buttons
**Learning:** Icon-only buttons often have `aria-label`s for screen reader support but lack native visual tooltips for sighted users. The lack of `title` attributes makes it harder for users to understand what icons mean, reducing the intuitiveness of the UX.
**Action:** Always ensure that icon-only interactive elements (like custom dialog close buttons, zoom controls, etc.) carry a `title` attribute reflecting the same information as the `aria-label`. Use the standard translation function where applicable (`title={t('key')}`).
## 2024-10-26 - Added ARIA attributes to `.dialog-content.subscription-dialog` wrappers

**Learning:** There is a distinct set of components in this app that use `<div className="dialog-content subscription-dialog">` as a visual container for dialogs (e.g., `CreateAgentDialog`, `AgentRunsDialog`, `CreateSubscriptionDialog`, etc.). While they look like modal dialogs visually, they were missing the semantic ARIA attributes to announce themselves properly to screen readers.
**Action:** When creating or modifying custom dialogs using this specific CSS pattern, always remember to add `role="dialog"`, `aria-modal="true"`, and an `aria-labelledby` attribute pointing to an `id` on the `<h2>` title element.
