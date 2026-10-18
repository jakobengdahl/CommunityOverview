## 2024-09-26 - Add Tooltips to Icon-Only Buttons
**Learning:** Icon-only buttons often have `aria-label`s for screen reader support but lack native visual tooltips for sighted users. The lack of `title` attributes makes it harder for users to understand what icons mean, reducing the intuitiveness of the UX.
**Action:** Always ensure that icon-only interactive elements (like custom dialog close buttons, zoom controls, etc.) carry a `title` attribute reflecting the same information as the `aria-label`. Use the standard translation function where applicable (`title={t('key')}`).

## 2025-02-15 - Add htmlFor and id to Custom Input Components
**Learning:** Custom input components (like autocomplete wrappers or multi-select chips) often use standard `<label>` tags but forget to associate them with the underlying `<input>` via the `htmlFor` and `id` attributes. This breaks accessibility for screen readers, as the label is not programmatically linked to the input.
**Action:** When building or reviewing custom input components, always ensure that a unique ID (preferably using React's `useId()` hook) is generated and applied to the `<input>`, and that the corresponding `<label>` uses `htmlFor` with this same ID to establish the association.
