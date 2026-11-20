# Theming the web UI

The web UI takes its colours, radii and shadows from design tokens: CSS custom
properties declared once in `frontend/web/src/styles/tokens.css`. A deployment
can change the accent colour without rebuilding the frontend.

## Tokens

Each token's default is the value the stylesheets used before tokens existed,
so the default appearance is unchanged. Names are semantic: a stylesheet asks
for the text colour on a raised surface, not for a particular grey.

| Group | Tokens | Used for |
|---|---|---|
| Surfaces | `--co-surface-app`, `-raised`, `-menu`, `-control`, `-button`, `-button-hover`, `-hover`; `--co-scrim`, `--co-scrim-sheet` | Page, dialogs and sheets, menus, inputs, secondary buttons, hover overlays, modal backdrops |
| Text | `--co-text-strong`, `--co-text`, `-soft`, `-subtle`, `-muted`, `-faint`, `-disabled` | From headings down to disabled labels |
| Lines | `--co-border`, `--co-border-strong`, `--co-handle` | Dividers and outlines, the sheet grab handle |
| Accent | `--co-accent`, `--co-accent-hover`, `--co-accent-contrast` | Primary buttons, focus rings, selection; text on an accent background |
| Status | `--co-danger`, `--co-danger-hover`, `--co-danger-soft`, `--co-warning`, `--co-success` | Destructive actions, warnings, success states |
| Shape | `--co-radius-sm` … `--co-radius-xl` | 4, 6, 8 and 12 px corners |
| Elevation | `--co-shadow-dialog`, `--co-shadow-sheet`, `--co-shadow-menu` | Shadows of dialogs, bottom sheets and menus |
| Spacing | `--co-space-1` … `--co-space-6` | A 4 px-based scale, from 4 to 24 px |
| Type | `--co-font-family`, `--co-font-size-sm`, `-md`, `-lg` | The UI font stack, 12, 14 and 16 px |

The accent is on tokens throughout the web UI stylesheets and inline styles,
including tints of it, which are written as
`color-mix(in srgb, var(--co-accent) N%, transparent)`, and every text on an
accent background uses `--co-accent-contrast`. Text on other coloured
backgrounds, such as danger buttons or the create button in a node type's own
colour, stays white. Surfaces, text, lines, status colours, radii and shadows
are on tokens in the shared chrome: the confirm, input, create-node and
edit-node dialogs, the bottom sheet and the session context menu, and the
new-session dialog apart from its option cards. The rest of the stylesheets
still use literal values. The spacing and type tokens are declared for new and
migrated styles; existing layout still uses literal sizes.

The graph canvas package (`packages/ui-graph-canvas`) and the embeddable
widget do not read these tokens.

When adding or changing styles in `frontend/web`, use a token where one fits
and add one to `tokens.css` when a value recurs.

## Per-deployment overrides

A profile's `schema_config.json` can set the accent under `presentation.theme`:

```json
"presentation": {
  "title": "…",
  "theme": {
    "accent": "#0a7f5c",
    "accent_hover": "#086446",
    "accent_contrast": "#ffffff"
  }
}
```

| Key | Token | If left out |
|---|---|---|
| `accent` | `--co-accent` | The default accent, `#646cff` |
| `accent_hover` | `--co-accent-hover` | With `accent` set, a darker shade of it (`color-mix(in srgb, <accent> 85%, black)`); otherwise the default |
| `accent_contrast` | `--co-accent-contrast` | `#fff` |

Values must be plain CSS colours: hex (`#rgb`, `#rgba`, `#rrggbb`,
`#rrggbbaa`) or `rgb()`, `rgba()`, `hsl()` and `hsla()` with numeric
arguments. The backend skips any other key or value with a warning in the log,
and the rest of the configuration loads as usual. `GET /api/presentation`
returns the kept entries as `theme`, and the web UI sets them on the root
element when the configuration loads.

Choose `accent_contrast` so that text on the accent stays readable. White on a
light accent does not. It colours only text on an accent background.

## What is not themable yet

The UI is designed as a dark appearance. Under a light system colour scheme,
`index.css` and the graph canvas package switch a few base colours (the page
background and base text, some canvas elements), but dialogs, panels and menus
stay dark. A full light appearance needs the rest of the stylesheets on tokens
first, or the parts still using literal values would stay dark next to light
ones.
