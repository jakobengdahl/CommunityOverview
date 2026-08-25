## 2024-05-18 - Added ARIA label to FloatingHeader hamburger menu
**Learning:** Icon-only buttons using `react-bootstrap-icons` (like `<List size={20} />`) need explicit `aria-label`s for screen readers. The `title` attribute provides a tooltip but isn't a substitute for an accessible label.
**Action:** When adding or reviewing icon-only buttons, ensure an `aria-label` is present using the `t()` localization function if a localized tooltip `title` exists.
