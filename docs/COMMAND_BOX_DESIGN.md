# Unified command box interaction design

**Status: Design (target state), for owner review before implementation.**
This note specifies future behaviour; it does not describe a shipped feature.
The five product choices below are settled. The remaining mechanics are design
choices made here under delegated authority, with a reason for each.

## Settled product choices

- Desktop: the box sits **below the chat history and replaces the chat input**;
  there is one place to type.
- Mobile: the box is **pinned to the bottom, above the on-screen keyboard, and
  always visible**, including when the keyboard is closed.
- Commands appear **only after a `/` prefix**. Otherwise suggestions mix nodes
  and node types; ordinary chat text never executes a command.
- Kiosk views get **neither the command box nor any of its features**. They do
  not register its commands, suggestion providers or keyboard handlers.
- **Hide classic surfaces** defaults to **off**, is **per-user and remembered**,
  and hides only the separate search bar and add-node / view buttons. Settings
  and account menus remain available.

## Suggestion lifecycle and ranking

| Choice | Rule | Reason |
|---|---|---|
| Trigger | With input focus, suggest nodes and node types after two non-whitespace characters. A slash at the start of the input, ignoring leading whitespace, enters command mode immediately; bare `/` lists available commands. Slashes elsewhere are ordinary text. | Avoid noisy single-letter searches while keeping command discovery immediate. |
| Debounce | Debounce node/type queries by 150 ms after the last edit; filter the local command registry immediately. Cancel superseded work and discard results for an old query, scope or closed popup. | Reduce query traffic without delaying local commands or showing stale results. |
| Show | Open when the trigger is satisfied and at least one eligible result exists. Initially no item is selected. Loading, no-results and error feedback are non-selectable status text. | Typing and sending chat must remain the default action. |
| Hide | Close on Esc, loss of focus outside the box and popup, cleared/short input, no results, submission or acceptance. After Esc, keep closed until the next edit or explicit ArrowDown request. | Dismissal stays effective and stale choices cannot be accepted. |
| Candidate sets | Plain text includes nodes and node types only; slash mode includes commands only, matching the text after `/`. Respect current graph scope and permissions in both modes. | Commands cannot leak into ordinary chat completion. |
| Ranking | Match case-insensitively against displayed labels and declared aliases: exact match, then prefix, then word prefix, then substring. Within a match tier put nodes before node types; then sort by locale-aware label and stable ID. Commands use the same match tiers, label and ID tie-breaks in their separate set. No recency boost or fuzzy matches. | Predictable primary match tiers prevent a weak node match from outranking an exact type match. |
| Size | Show at most eight results in a scrollable list, scrolling the active item into view. Bare `/` uses the command label/ID ordering. | Bound visual and assistive-technology navigation effort. |

For example, an exact node-type label beats a node's substring match. An exact
node label beats an exact type label. No command competes with either in plain
text mode. A language or graph-scope change clears selection and recomputes
results before acceptance is possible.

## Keyboard and acceptance

| Choice | Rule | Reason |
|---|---|---|
| Tab | While the list is open, accept its **first** suggestion, even if arrows have highlighted another. Prevent default only when a first item exists. With the list closed, use native focus traversal; Shift+Tab always traverses backwards. | Provide a predictable completion shortcut without trapping focus. |
| Arrows | ArrowDown selects the first item if none is selected; ArrowUp selects the last. Further presses move one item and stop at the ends. ArrowDown can reopen an eligible dismissed list. With no eligible list, preserve native caret movement. | Support explicit choice without accidental wraparound. |
| Enter | With a selected item, accept it. Otherwise send non-empty input as chat, including slash-prefixed text; never resolve or execute an unselected command by parsing the submitted text. Shift+Enter inserts a newline. | Preserve Enter-to-send and require a deliberate selection for commands. |
| Esc | Close the list and clear selection, leaving the draft intact; do not submit or move focus. | Let users dismiss assistance without losing their message. |
| Composition | During IME composition, do not accept, submit or intercept composition keys; query after composition ends. | Keep text entry safe across input methods. |
| Edit | Any edit clears the active item immediately, including while awaiting results. | A selection must correspond to the visible, current query. |

Accepting a node inserts a structured node reference at the caret; accepting a
node type inserts its displayed label as text. Both close the list and keep the
draft focused without sending chat or creating graph data. Accepting a command
closes the list and invokes its handler only if the input is still in slash mode
and the command is still available. The handler opens its existing action flow;
destructive operations retain their existing confirmation. Clear a command draft
only after successful handoff; preserve it and announce failure otherwise.

A plain message can therefore neither display nor select nor execute a command.
Tab completes nodes/types in plain text mode and invokes a command only in slash
mode, where its command label is visible before acceptance.

## Accessibility, touch and placement

| Choice | Rule | Reason |
|---|---|---|
| Combobox | Use an editable, labelled `role="combobox"` input with `aria-autocomplete="list"`, `aria-haspopup="listbox"`, `aria-expanded` and `aria-controls` pointing to the mounted popup. | Expose the relationship and open state to assistive technology. |
| Selection | Popup has `role="listbox"`; items have stable IDs, `role="option"` and `aria-selected`. Keep DOM focus in the input and set `aria-activedescendant` only for the explicitly active, mounted option; remove it on close or edit. | Screen readers can track arrow navigation without moving the typing focus. |
| Announcements | Use a separate polite status region for result counts, loading, empty results and failures. Include translated result-kind labels in option names; never announce the whole list on every edit. | Make mixed results and asynchronous feedback understandable without excessive speech. |
| Touch | Rows have at least 44 by 44 CSS-pixel targets; tapping accepts that row with the same semantics as keyboard acceptance. Handle the tap before blur dismisses the popup. Scrolling never accepts; tapping outside dismisses and preserves the draft. | Make deliberate selection possible without hover or a hardware keyboard. |
| Layout | Anchor the popup above the box, with bounded height and independent scrolling. On desktop it remains below the chat history; reserve space for the composer. On mobile use the visual viewport and safe-area inset to keep the composer above the keyboard and history clear of it. | Suggestions and the single typing surface remain reachable in small viewports. |

A visible send control follows Enter's rules, including the selected-item case,
so touch and keyboard have the same result. Closing the list does not hide the
mobile composer. Accessible labels and help explain the Tab and Enter behaviour.

## Localization and preference

**Choice:** Registry entries carry translation keys for labels and descriptions,
plus stable, language-independent IDs and optional aliases. Match localized
labels and aliases; display localized labels, never raw IDs. Add keys in both
English and Swedish and use the existing fallback rules in [I18N.md](I18N.md).
Language changes recompute the list without changing command identity. This
design does not change the current English-only language-switching lock.
**Reason:** Labels can change language without breaking dispatch or preferences.

**Choice:** Store `hideClassicSurfaces` with the signed-in user's remembered
preferences, never as a shared device-wide flag. Missing values mean false;
failed persistence leaves the previous saved value and reports an error. Changing
users reloads that user's value; anonymous sessions default off without inheriting
another user's value. Turning it off restores the classic surfaces immediately.
**Reason:** An opt-in layout must be reversible and must not affect another user.

The command box is the single chat composer regardless of this preference. The
preference controls only the settled classic surfaces listed above and has no
effect on kiosk views. It grants no permissions and changes no action semantics.

## Command registry sketch

Illustrative TypeScript contract, not implementation:

```ts
type CommandContext = {
  userId: string;
  graphId: string;
  view: 'interactive' | 'kiosk';
  locale: string;
};

type Command = {
  id: string; // Stable identity, independent of the translated label.
  labelKey: string;
  descriptionKey: string;
  aliasKeys?: readonly string[];
  isAvailable(context: CommandContext): boolean;
  execute(context: CommandContext): Promise<void>; // Opens existing action flow.
};

interface CommandRegistry {
  register(command: Command): () => void; // Reject duplicate IDs; return cleanup.
  list(context: CommandContext): readonly Command[];
  get(id: string): Command | undefined;
}
```

**Choice:** The interactive view registers commands; the suggestion controller
filters `list` by availability and translated query before ranking. Acceptance
rechecks slash mode, current context and availability before calling `execute`.
Kiosk never mounts this controller or registers these commands. Existing action
handlers and backend authorization remain authoritative.
**Reason:** Discovery and dispatch share one definition without making UI
visibility an authorization boundary.

## Review and consequences

Implementation waits for owner approval of this design. It will need keyboard,
screen-reader, touch and mobile-keyboard verification, including ordinary text
that resembles commands, IME entry, stale suggestions and per-user preference
isolation. This documentation change enables no feature, alters no saved data
and needs no rollout or migration. The main implementation risks are interception
of native editing keys and keeping the composer visible as mobile viewports
resize; the explicit acceptance rules and viewport checks above bound those risks.
