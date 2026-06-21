# Manual Test Plan — dev branch (pre-preview)

Features merged to `dev` but not yet in `preview`. Tested against the default profile unless otherwise noted.

> **Status legend:** ⬜ Not tested · ✅ Pass · ❌ Fail · ⚠️ Partial

---

## 1. Node Marking (AI assistant) — #137

AI can annotate nodes with colors and optional labels; markers are session-only.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 1.1 | Open app with nodes in the visualization | Graph loads normally | ⬜ |
| 1.2 | Ask AI: "Mark all Actor nodes in red" | Nodes get a colored badge dot (red); legend panel appears in graph | ⬜ |
| 1.3 | Ask AI: "Mark Initiative nodes in green with label 'Active'" | Green badge + tooltip label "Active" on hovered nodes | ⬜ |
| 1.4 | Ask AI: "Remove all marks" or clear via UI | All badge dots disappear; legend hidden | ⬜ |
| 1.5 | Refresh the page | Marks are gone (session-only, not persisted) | ⬜ |
| 1.6 | Light mode: toggle if available | Legend and badge dots render correctly in light theme | ⬜ |

---

## 2. Markdown Rendering in Chat — #137

AI assistant and expert chat messages render Markdown (bold, lists, code, tables).

| # | Step | Expected | Status |
|---|------|----------|--------|
| 2.1 | Ask AI: "List three node types as a markdown bullet list" | Response renders with `•` bullets, not raw `- text` | ⬜ |
| 2.2 | Ask AI for a response with **bold** and `inline code` | Bold and monospace code rendered correctly | ⬜ |
| 2.3 | Ask AI for a table | GFM table renders with borders | ⬜ |
| 2.4 | Expert agent response (if configured) | Expert messages also render Markdown | ⬜ |

---

## 3. Schema-driven Extra Fields in CreateNodeDialog — #137

Node creation dialog reads extra fields from `schema.node_types[type].fields` rather than a hardcoded map.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 3.1 | Click toolbar icon to create a node type that has custom fields in schema (e.g. Legislation) | Dialog shows extra fields beyond name/description/summary | ⬜ |
| 3.2 | Add a new field to a node type in `schema_config.json`, restart app, open create dialog | New field appears dynamically in dialog without code changes | ⬜ |
| 3.3 | Node type with only base fields (name/description/summary) | Dialog shows only base fields — no empty extra-field rows | ⬜ |

---

## 4. Schema-driven Context Menu (open_url) — #137

Right-click context menu items come from `schema.node_types[type].context_menu` config.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 4.1 | Right-click a node type that has a `context_menu` entry in schema | Extra menu item(s) appear below standard actions | ⬜ |
| 4.2 | Click an `open_url` menu item with a `{field}` template | URL opens in new tab with the node's field value substituted | ⬜ |
| 4.3 | Node type without `context_menu` in schema | No extra items; standard menu unchanged | ⬜ |

---

## 5. Skill Dialog / Skill Node Type — #137

Nodes configured with `ui_form: "skill"` in schema open CreateSkillDialog instead of generic dialog.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 5.1 | Drag or click to create a Skill node (if Skill type is in active profile schema) | CreateSkillDialog opens (not generic CreateNodeDialog) | ⬜ |
| 5.2 | Fill in skill form fields and save | Skill node created and appears in graph | ⬜ |
| 5.3 | Double-click existing Skill node to edit | Skill dialog opens pre-filled with existing data | ⬜ |

---

## 6. Callback Context Menu Action — #137

Schema context menu supports `type: "callback"` items that fire `onContextMenuAction`.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 6.1 | Right-click node type with a `callback` context menu entry in schema | Callback item appears in menu | ⬜ |
| 6.2 | Click callback item | Action fires without JS error; console.warn logged for unhandled actions | ⬜ |

---

## 7. Expert Agents with SKILL.md Skills — #134

Expert agents can load skills from SKILL.md files (URL or local directory).

| # | Step | Expected | Status |
|---|------|----------|--------|
| 7.1 | Configure expert agent with `skills_urls` pointing to a SKILL.md | App starts; skill loads without error in server logs | ⬜ |
| 7.2 | Activate expert agent in chat, ask a question within its skill domain | Agent uses skill context in response (more specific/accurate) | ⬜ |
| 7.3 | Configure `skills_config.skills_dir` in schema for default profile | Server logs confirm skills loaded from local directory at startup | ⬜ |
| 7.4 | SKILL.md with prompt-injection attempt in name/description | Sanitized — injection text does not appear in agent responses | ⬜ |

---

## 8. App Without LLM Keys — #131

Application starts and functions normally without any LLM API key configured.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 8.1 | Start backend with no `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` set | Server starts; logs show "LLM not available" (no crash) | ⬜ |
| 8.2 | Open the web UI | Graph canvas, search, and toolbar visible; chat panel hidden entirely | ⬜ |
| 8.3 | Use search to find and add nodes | Search works; nodes appear in visualization | ⬜ |
| 8.4 | Create/edit/delete nodes via toolbar and context menu | All non-chat operations work normally | ⬜ |
| 8.5 | Check `GET /ui/capabilities` response | `{"llm_available": false, "llm_provider": null}` | ⬜ |
| 8.6 | MCP server (if active) | MCP tools respond correctly without LLM | ⬜ |

---

## 9. MCP Bulk Edge Deletion + UI Edge Delete Fix

Bulk edge deletion via MCP tool; fixes to UI edge delete.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 9.1 | Via MCP client: call `delete_edges` with an array of edge IDs | All specified edges deleted; success response returned | ⬜ |
| 9.2 | Select an edge in the UI and delete via context menu or keyboard | Edge removed from graph without error; no ghost edge remains | ⬜ |
| 9.3 | Delete an edge in UI, then undo (if supported) or refresh | Graph reflects correct state after delete | ⬜ |

---

## 10. Reverse Proxy Path Auto-detection — #131 / revert commit

Frontend auto-detects path root from `window.location` for subpath deployments.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 10.1 | Deploy app at root path (`/`) | All API calls use base URL without prefix; app works normally | ⬜ |
| 10.2 | Deploy behind reverse proxy at `/myapp/` | Frontend detects `/myapp` prefix; all API calls correctly prefixed | ⬜ |
| 10.3 | No double-prefix: proxy strips path before forwarding | API calls arrive at backend without double path segment | ⬜ |

---

## 11. stat-metadata Profile — #137

New profile for ESS statistical metadata with domain-specific node types.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 11.1 | Start app with `SCHEMA_FILE=config/stat-metadata/schema_config.json` | App loads; toolbar shows stat-metadata node types (DataSet, DataStructure, etc.) | ⬜ |
| 11.2 | Create a DataSet node | DataSet created with correct fields | ⬜ |
| 11.3 | Create an InstanceVariable linked to a DataSet | Edge created; both nodes visible in graph | ⬜ |
| 11.4 | Right-click a ProductionSolution node | SSPCloud launcher URL in context menu | ⬜ |
| 11.5 | GSIM lineage skill visible in expert agent selector (if configured) | Skill appears and provides GSIM-specific guidance | ⬜ |

---

## 12. Startup Diagnostics — #126

Startup diagnostics log key system state at launch.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 12.1 | Start backend normally | Startup log shows: profile loaded, LLM status, MCP status, graph backend | ⬜ |
| 12.2 | Check `GET /info` endpoint | Response includes `llm_available` field | ⬜ |

---

## 13. Capability Manifest via MCP Discovery — #118

MCP server exposes a capability manifest for client discovery.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 13.1 | Connect MCP client; list available tools | `get_capabilities` tool (or similar) present | ⬜ |
| 13.2 | Call capability tool | Returns structured manifest with enabled capabilities | ⬜ |

---

## 14. Tenant Config Layering — #121

Config can be layered: base profile + tenant-specific overrides.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 14.1 | Configure a tenant override file; start app | Tenant-specific settings (e.g. title, colors) override base profile | ⬜ |
| 14.2 | Fields not in tenant config inherit from base | Base profile values still apply for non-overridden fields | ⬜ |

---

## 15. Actor Attribution for Mutations — #125

Write operations record the actor (user/service) that made the change.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 15.1 | Create a node via UI or API | Node record includes actor/attribution metadata | ⬜ |
| 15.2 | Update a node | Update attributed to correct actor | ⬜ |

---

## 16. Admin-safe Export Boundaries — #130

Export APIs enforce authorization boundaries.

| # | Step | Expected | Status |
|---|------|----------|--------|
| 16.1 | Export graph data as admin | Full export returned | ⬜ |
| 16.2 | Export with restricted scope | Only authorized data included in export | ⬜ |

---

## 17. Interactive Guide System — #138

Step-by-step guides triggered via URL parameter or AI assistant. Tooltips position near named UI elements; action steps run automatically before showing the tooltip.

### 17a. URL trigger

| # | Step | Expected | Status |
|---|------|----------|--------|
| 17a.1 | Open app with `?guide=first_intro` in URL | Guide starts automatically after config loads; tooltip appears near header | ⬜ |
| 17a.2 | Open app with `?guide=actions_demo` | Actions demo guide starts | ⬜ |
| 17a.3 | Open app with `?guide=nonexistent` | No guide starts; app loads normally | ⬜ |
| 17a.4 | Switch language (sv/en) while URL has `?guide=first_intro` | Guide does NOT restart on language change (one-shot guard) | ⬜ |

### 17b. Navigation and keyboard

| # | Step | Expected | Status |
|---|------|----------|--------|
| 17b.1 | Click "Next" through all steps of `first_intro` | Progress dots advance; tooltip repositions to correct UI target each step | ⬜ |
| 17b.2 | Press **Enter** on a non-input step | Same as clicking Next | ⬜ |
| 17b.3 | Press **Escape** at any step | Guide cancels immediately; backdrop disappears | ⬜ |
| 17b.4 | Click "Cancel" button | Guide stops; UI returns to normal | ⬜ |
| 17b.5 | Reach last step | "Next" button changes to "Close"; clicking closes guide | ⬜ |

### 17c. Tooltip positioning

| # | Step | Expected | Status |
|---|------|----------|--------|
| 17c.1 | Step targeting `header` | Tooltip appears below/beside the header element with correct arrow | ⬜ |
| 17c.2 | Step targeting `search` | Tooltip appears near search bar | ⬜ |
| 17c.3 | Step targeting `toolbar` | Tooltip appears to the left of the toolbar | ⬜ |
| 17c.4 | Step targeting `chat` | Tooltip appears near chat panel | ⬜ |
| 17c.5 | Step targeting `canvas` | Tooltip appears near the graph canvas | ⬜ |
| 17c.6 | Step targeting `center` (e.g. input step) | Tooltip centered on screen | ⬜ |
| 17c.7 | Resize browser window during guide | Tooltip stays within viewport bounds | ⬜ |

### 17d. Input step

| # | Step | Expected | Status |
|---|------|----------|--------|
| 17d.1 | Reach the input step in `first_intro` | Input field is visible and auto-focused | ⬜ |
| 17d.2 | Type text and press Enter | Input captured; guide advances | ⬜ |
| 17d.3 | Leave input empty and click Next | Guide still advances (no required-field block) | ⬜ |

### 17e. Action steps — search & visualization

| # | Step | Expected | Status |
|---|------|----------|--------|
| 17e.1 | `search_nodes` step (Actor in `first_intro` step 4) | Actor nodes load into canvas; spinner shown during load | ⬜ |
| 17e.2 | `clear_visualization` step | Canvas clears; spinner visible briefly | ⬜ |
| 17e.3 | `focus_node` step (if configured) | Graph pans/zooms to the specified node | ⬜ |

### 17f. Action steps — chat and search fill (actions_demo guide)

| # | Step | Expected | Status |
|---|------|----------|--------|
| 17f.1 | `fill_search_input` step | Text animates character-by-character into search bar; debounce search triggers | ⬜ |
| 17f.2 | `fill_chat_input` step | Text animates into chat textarea | ⬜ |
| 17f.3 | `fill_chat_input` with `auto_send: true` | After animation completes, message is sent automatically with the typed text | ⬜ |
| 17f.4 | `minimize_chat` step | Chat panel collapses | ⬜ |
| 17f.5 | `maximize_chat` step | Chat panel re-opens | ⬜ |
| 17f.6 | `toggle_chat` step | Chat panel toggles state | ⬜ |

### 17g. Action steps — node and edge CRUD

| # | Step | Expected | Status |
|---|------|----------|--------|
| 17g.1 | `create_node` step | Node created in backend; appears in visualization | ⬜ |
| 17g.2 | `update_node` step | Node updated in backend; visualization reflects change | ⬜ |
| 17g.3 | `delete_node` step | Node deleted from backend; removed from visualization | ⬜ |
| 17g.4 | `show_node_detail` step | Node detail panel opens with correct node | ⬜ |
| 17g.5 | `create_edge` step | Edge created in backend; appears in graph | ⬜ |
| 17g.6 | `delete_edge` step | Edge deleted from backend; removed from graph | ⬜ |

### 17h. Cancellation safety

| # | Step | Expected | Status |
|---|------|----------|--------|
| 17h.1 | Cancel guide during a running `search_nodes` action (click Cancel while spinner is visible) | Spinner disappears; no nodes added after cancel; no JS error | ⬜ |
| 17h.2 | Rapidly click Next through two consecutive action steps | No duplicate API calls; store not left in inconsistent state | ⬜ |
| 17h.3 | Cancel guide during `fill_chat_input` animation | Animation stops; `guideChatInput` cleared; chat textarea not further modified | ⬜ |

### 17i. AI assistant trigger

| # | Step | Expected | Status |
|---|------|----------|--------|
| 17i.1 | Ask AI: "Start the first_intro guide" (requires backend tool returning `start_guide` action) | Guide starts from step 1 | ⬜ |
| 17i.2 | Ask AI to start a non-existent guide ID | No guide starts; AI shows error or fallback message | ⬜ |

### 17j. Backdrop and accessibility

| # | Step | Expected | Status |
|---|------|----------|--------|
| 17j.1 | While guide is active, click on the graph canvas behind the backdrop | Click passes through; graph responds (backdrop is non-blocking) | ⬜ |
| 17j.2 | While guide is active, use search bar | Search still works while guide tooltip is visible | ⬜ |
| 17j.3 | Screen reader / tab navigation | Guide tooltip is focusable; ARIA role="dialog" present | ⬜ |

### 17k. i18n

| # | Step | Expected | Status |
|---|------|----------|--------|
| 17k.1 | Start guide with language set to Swedish | Tooltip text, button labels, and guide name appear in Swedish | ⬜ |
| 17k.2 | Switch language mid-guide | Tooltip text updates to new language for current step | ⬜ |

---

## Notes

- Tests marked ⬜ are not yet executed.
- Add new feature sections here as more features land on `dev` before the next preview release.
- For backend seam features (#123–#129): these are hook/extension points that may require integration tests rather than manual UI testing; verify via API calls or unit test suite (`pytest`).
- Guide system example guides: `?guide=first_intro` and `?guide=actions_demo` (both defined in `config/default/schema_config.json`).
