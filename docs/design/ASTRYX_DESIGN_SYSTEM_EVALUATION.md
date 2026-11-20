# Astryx design system evaluation

## Recommendation

Do not migrate the web UI to Astryx now. Add a small design-token layer to the
current CSS first (option B below), and look at Astryx again when it has a 1.0
release and the app has moved to React 19 for its own reasons.

This is an analysis only. It changes no dependency, code, configuration or
build. Adopting Astryx, in whole or in part, needs an explicit owner decision.

The main reasons:

- **React 19 is a whole-app prerequisite.** Astryx needs `react` and
  `react-dom` 19 or later as peer dependencies. An app can load only one React,
  so "new screens first" cannot start before the whole app, the canvas package
  and the test setup have been upgraded. A pilot on one screen costs the full
  upgrade.
- **Fast-moving beta.** The project calls itself beta. `@astryxdesign/core` is
  at 0.7.x. There were 15 stable releases between 2026-10-21 and 2026-11-14,
  and about 27 sets of migration codemods since the first release in June
  2026. While the version is 0.x, breaking changes can come in any minor
  release. Open core would inherit that churn.
- **The gain is mostly visual consistency, and the same gain is cheaper to get
  another way.** Today the styling has almost no shared tokens. Adding tokens
  gives consistent colours, spacing and dark mode, and allows per-deployment
  theming. It needs no new dependency, and it makes any later move to a design
  system, Astryx or another, mostly mechanical.

## What was examined

Examined on 2026-11-20:

- The Astryx repository at `main`, a shallow clone (README, `packages/core`,
  release docs, licence). Nothing from it was built or run.
- The published npm tarballs of `@astryxdesign/core@0.7.16` (the `latest`
  tag), `@astryxdesign/theme-neutral@0.7.16` and `@stylexjs/stylex@0.19.2`.
  They were unpacked and measured, not installed into this repository.
- This repository's `frontend/web` and `packages/ui-graph-canvas` on `main`.

No bundle was built with Astryx. The JavaScript size figure below is therefore
open (see *What would decide it*).

## Today's UI building blocks

| Area | Today | Nearest Astryx component |
|---|---|---|
| Modal dialogs (18 `*Dialog.jsx`: create/edit node, settings, metamodel explorer, staged review, agents, skills, subscriptions…) | Hand-written, own CSS each; `ConfirmDialog`, `InputDialog` | `Dialog`, `AlertDialog` |
| Mobile sheets and drawers | `BottomSheet`, `SessionDrawer`, `ActivityDrawer`, sharing `useModalFocusTrap` | `BottomSheet`, `Drawer` |
| Context menus | `SessionContextMenu`, canvas `ContextMenus.jsx` | `ContextMenu`, `DropdownMenu`, `MoreMenu` |
| Search and command entry | `FloatingSearch` | `Typeahead`, `CommandPalette`, `PowerSearch` |
| Toolbars | `FloatingToolbar`, `AnnotationToolbox`, `WriteModeBar` | `Toolbar`, `ToggleButton`, `SegmentedControl` |
| Forms | Plain inputs, `SubtypeInput`, `CollectionForm` | `TextInput`, `TextArea`, `Selector`, `Tokenizer`, `Field`, `FormLayout` |
| Chat | `ChatPanel`, `MarkdownMessage` (react-markdown, KaTeX) | `Chat`, `Markdown` (math through a caller-supplied renderer such as KaTeX) |
| Lists and history | `HistoryList`, `SessionActivityList`, `NodeHistoryPanel` | `List`, `TreeList`, `Table` |
| Notices | `StagedEditNotice`, `SummonNotice`, `PendingActionCard` | `Banner`, `Toast`, `Card` |
| Icons | `react-bootstrap-icons` (17 files, plus 1 in the canvas package) | `Icon` with a theme icon set (neutral theme uses `lucide-react`) |
| Graph canvas | `packages/ui-graph-canvas` on React Flow 11: nodes, edges, annotations | **None.** The canvas stays custom; only its menus and toolbox chrome could change |

Size of today's UI: 52 non-test JSX files and 38 CSS files (about 7,000 lines,
130 KB) in `frontend/web/src`, and 19 JSX files in the canvas package. The CSS
defines one custom property and has one colour-scheme rule, so there is no
token or theme layer to map onto Astryx's themes.

Most of the app's screen area is the canvas, and Astryx does not replace it.
What Astryx would replace is the chrome around it: dialogs, sheets, menus,
forms and toolbars.

## Prerequisites and cost

1. **React 18.3 → 19 upgrade.** It affects:
   - `frontend/web` and its tests (`@testing-library/react` 16 already supports
     19);
   - `@types/react`;
   - `react-markdown` 10;
   - `zustand` 4;
   - the canvas package, whose peer range is `^18.0.0` and must be widened.

   React Flow 11 declares `react >=17` and installs, but React 19 is officially
   supported only by its successor, `@xyflow/react` 12. That move is a separate
   migration. The upgrade is worth doing on its own merits, and it is the
   gating step for Astryx.
2. **Styling.** Astryx ships prebuilt CSS, so Vite needs no StyleX plugin.
   `@stylexjs/stylex` is still a required runtime peer dependency.
   `astryx.css` is 292 KB (45 KB gzipped), on top of today's 130 KB of app
   CSS. Today's CSS would be kept during any transition.
3. **JavaScript size.** Not measured. Components are separate entry points
   (`sideEffects` limited to CSS and StyleX files), so tree-shaking should
   work, but this needs a measured build.
4. **i18n.** Astryx has its own locale files, including `sv-SE`, through
   `intl-messageformat`. Its built-in strings (close buttons, pagination and
   so on) would follow its locale, and app strings would stay on `useI18n()`.
   The app's language would have to be passed to Astryx's provider, so two
   string systems would need to stay in step.

## Fit with the canvas, kiosk, mobile and theming

- **Canvas:** no overlap. Menus and the annotation toolbox could use Astryx
  primitives, but they live in `packages/ui-graph-canvas`, which takes its text
  through props and has no i18n. Making that package depend on Astryx widens
  its dependencies for every embedder.
- **Kiosk view** (`CollectKioskView`): a good candidate, because it is mainly
  forms. It still needs the React 19 upgrade first.
- **Mobile layout:** Astryx's `BottomSheet`, `Drawer` and `MobileNav` cover
  the same ground as today's tested sheets and drawers. Replacing them would
  mean re-proving behaviour that the existing tests pin: long-press, visual
  viewport insets and focus return.
- **Theming per deployment:** Astryx themes are CSS custom property overrides
  over seven base themes. That model fits well. Today has nothing comparable,
  which is the argument for option B.

## Accessibility

Today's overlays share one tested modal contract (`useModalFocusTrap`: scroll
lock, focus entry, Tab wrap, focus restore), and 21 places set dialog or menu
roles. Astryx advertises accessible components and runs Playwright
accessibility contract suites in its own CI. It would probably be at least as
good for standard widgets.

A migration would still swap a contract the app owns and tests for one it
depends on. The app's own tests for focus and Escape behaviour would have to
move to the new components, and each component would have to pass them, before
the old ones are removed.

## Dependency risk for open core

- **Licence:** MIT, copyright Meta Platforms. Compatible with this repository.
- **Activity:** very active: 50 commits on 19 and 20 November 2026 alone,
  and releases every few days. Backed by Meta, and the README says it
  has been used internally for eight years. Being open source is new, though:
  the first npm release was June 2026.
- **Stability:** 0.x and beta. The chart packages are canary-only. Pinning an
  exact version and stepping up with the codemods would be a recurring cost.
- **Transitive additions:** `@stylexjs/stylex` and `intl-messageformat`, plus
  `lucide-react` from the theme.

## Options

| Option | What happens | Cost | Risk |
|---|---|---|---|
| **A. Adopt Astryx now** | Upgrade to React 19, then move dialogs, sheets and menus screen by screen | High. The upgrade, plus re-proving every overlay's tested behaviour | Beta churn in open core; two systems side by side for the transition |
| **B. Tokens first, revisit later** (recommended) | Introduce CSS custom properties for colour, spacing, radius and type in today's CSS, with dark mode and per-deployment overrides. Revisit Astryx at 1.0 | Low to medium. CSS only, no new dependency | Low |
| **C. React 19 upgrade, then a measured Astryx spike** | Do the React upgrade as its own task, then try Astryx on the kiosk view in a branch and measure | Medium. The upgrade is useful anyway, and the spike is thrown away if it fails | Medium |
| **D. Headless primitives instead** (React Aria or Radix) | Keep today's look and replace only the hand-written behaviour of dialogs, menus and popovers | Medium | Low. Both are stable, and both support React 18 and 19 |

B and C do not exclude each other. Tokens make a later C or A cheaper.

## What would decide it

A move to Astryx becomes reasonable when all of these hold:

- Astryx has a 1.0 release, or a stated stability policy for minor releases.
- The app runs on React 19, and the canvas works on it, either on React Flow 11
  verified or after a move to `@xyflow/react` 12.
- A spike branch shows:
  - the gzipped JS and CSS added for the kiosk view;
  - that the existing focus and keyboard tests pass against Astryx components;
  - that a theme override can express a deployment's colours.
