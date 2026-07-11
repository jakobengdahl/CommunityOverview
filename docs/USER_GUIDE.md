# User Guide

Community Knowledge Sharing is a visual, AI-powered platform for building and exploring
shared knowledge graphs. This guide covers all user-facing features.

---

## Contents

1. [The main interface](#1-the-main-interface)
2. [Working with the graph](#2-working-with-the-graph)
   - [Creating nodes](#21-creating-nodes)
   - [Creating edges](#22-creating-edges)
   - [Editing and deleting](#23-editing-and-deleting)
   - [Right-click context menu](#24-right-click-context-menu)
   - [Groups and annotations](#25-groups-and-annotations)
   - [Saved views](#26-saved-views)
   - [Agents](#27-agents)
3. [Search](#3-search)
4. [AI Chat](#4-ai-chat)
   - [Asking questions](#41-asking-questions)
   - [Adding nodes through chat](#42-adding-nodes-through-chat)
   - [Node proposals and duplicate detection](#43-node-proposals-and-duplicate-detection)
   - [Skills](#44-skills)
   - [Node marking](#45-node-marking)
   - [Document upload](#46-document-upload)
5. [Session menu and settings](#5-session-menu-and-settings)
   - [Collaborating in a session](#51-collaborating-in-a-session)
   - [Recent activity (audit log)](#52-recent-activity-audit-log)
6. [Federation — searching across multiple graphs](#6-federation--searching-across-multiple-graphs)
7. [Interactive guides](#7-interactive-guides)
8. [Connecting external AI tools via MCP](#8-connecting-external-ai-tools-via-mcp)
   - [Available MCP tools](#81-available-mcp-tools)
   - [Connecting](#82-connecting)
   - [Live visualization control via session ID](#83-live-visualization-control-via-session-id)

---

## 1. The main interface

![Main interface overview](images/ui-overview.png)
*The main interface: left toolbar (node types and tools), graph canvas (centre), search bar (top), chat panel (right). Skill nodes and Agent nodes are visible in the graph alongside domain content.*

The application is divided into four main areas:

| Area | Purpose |
|------|---------|
| **Left toolbar** | Create new nodes by clicking a node type icon; system tools (save view, minimap, etc.) at the bottom |
| **Graph canvas** | Visual graph — drag nodes, zoom, pan, right-click for the context menu |
| **Top bar** | Search, session menu (☰), and session ID for connecting external AI clients |
| **Chat panel** | Conversational AI assistant (right side, collapsible via the → arrow) |

---

## 2. Working with the graph

### 2.1 Creating nodes

Click a node type icon in the left toolbar to open the creation dialog.

![Toolbar with node type icons](images/toolbar.png)
*The left toolbar. Each icon represents a node type defined by the active profile. Domain types are listed first; system types (Agent, EventSubscription, Skill, SavedView, etc.) appear below a divider.*

Fill in the node fields and click **Save**. The node appears in the canvas immediately.

**Subtypes** — most node types support an optional subtype tag (e.g. "Government agency"
for an Actor, or "Research project" for an Initiative). The field autocompletes based on
subtypes already in the graph, helping maintain consistent terminology across the team.

![Create node dialog](images/create-node.png)
*The node creation dialog. Fields shown depend on the node type and active profile.*

**Skill nodes** use a specialised form with fields for the SKILL.md instruction set
(name, description, when-to-use, content, allowed tools, version).

You can also drag a node type icon from the toolbar and drop it anywhere on the canvas
to create a node at that position.

### 2.2 Creating edges

Hover over a node until the connection handle appears on its border, then drag to a target
node. A dialog lets you select the relationship type.

![Edge creation](images/create-edge.png)
*Hover over a node to reveal the connection handle (small circle), then drag to the target node to create an edge.*

### 2.3 Editing and deleting

**Edit:** Double-click a node to open its detail dialog, then click **Edit**. All
fields are editable. The detail dialog also has a **History** tab showing the
read-only change log for that node (see [5.2](#52-recent-activity-audit-log)).

**Delete node:** Right-click → **Delete**, or select a node and press **Delete**.
Deletions require confirmation. A single operation can remove up to 10 nodes at once.

**Delete edge:** Select an edge and press **Delete**, or right-click the edge.

### 2.4 Right-click context menu

Right-clicking a node opens the context menu:

- **Edit** — open the edit dialog
- **Hide** — remove the node from the current view (does not delete it from the graph)
- **Expand** — load all nodes directly connected to this node into the canvas
- **Select all nodes of the same type** — select every node of this node's type across the
  whole visualization, including ones scrolled outside the current viewport
- **Custom actions** — schema-defined items per node type, e.g. "Open in SSPCloud" that
  substitutes a field value into a URL and opens it in a new tab
- **Delete** — remove the node permanently (shown in red, requires confirmation)

![Context menu](images/context-menu.png)
*Context menu on a ProductionSolution node. "Open in SSPCloud" is a custom action defined in the schema's `context_menu` configuration.*

Right-clicking on the **canvas background** offers quick-create options:
- Create EventSubscription (webhook)
- Create Agent

Right-clicking a **selection of multiple nodes** shows bulk actions: Show only these,
Select all nodes of the same type, Hide all, Delete all. "Select all nodes of the same
type" extends the selection to every node whose type matches any type already in the
selection.

### 2.5 Groups and annotations

Nodes can be visually grouped by dragging them into a Group container (create one from
the toolbar or via the group icon). Groups help organise large graphs without affecting
the underlying data model.

Alongside groups, you can add free-floating **annotations** to a session by right-clicking
an empty area of the canvas and choosing one of:

- **Note** — a resizable sticky note for longer comments. Double-click to edit its text;
  right-click for a colour, to change the text size, or to delete it.
- **Label** — a short free-floating text label. Double-click to edit, right-click to
  recolour, change its text size, or delete.
- **Arrow / line** — a connector you can position anywhere on the canvas. Right-click to
  recolour, toggle an arrowhead on either end (so it can be a plain line, a single arrow,
  or a double arrow), or delete. When selected, drag either endpoint; bring an endpoint
  close to a node or another annotation and it snaps onto it (magnetic), staying attached
  as that target moves. Arrows do not attach to other arrows.

Annotations are part of the session, not the knowledge graph: they are stored with the
session (so everyone sharing the session sees them) and never change the underlying node
and edge data. Select an annotation and press **Delete** to remove it.

### 2.6 Saved views

Click **Save view** in the toolbar or ask the AI assistant to save the current layout.

Saved views are stored as **SavedView nodes** in the graph, making them visible,
shareable, and editable like any other node.

To restore a view: double-click its SavedView node in the graph, or search for it and
click "Restore view" in its detail panel.

### 2.7 Agents

Agents are autonomous AI processes that listen to graph change events and react using
LLM-powered reasoning and MCP tools.

Create an agent by right-clicking the canvas background → **Create Agent**, or by
dragging the Agent icon from the toolbar.

![Create Agent dialog](images/create-agent.png)
*The Create Agent dialog. Agent configuration, MCP integrations, and event trigger settings are defined in a single form.*

**Agent configuration:**

| Field | Purpose |
|-------|---------|
| **Enabled** | When checked, the agent begins listening to events immediately |
| **Name / Description** | Identification — the agent appears as a node in the graph |
| **Task Prompt** | Instructions added to the agent's base system prompt to define its behaviour |

**MCP Integrations** — which tool sets the agent may use:

| Integration | Capability |
|-------------|-----------|
| **GRAPH** | Read and write to the knowledge graph |
| **WEB** | Fetch and convert web content |
| **FS** | Read and write files |

**Event trigger** — filter which node types cause the agent to react (leave empty to
react to all node type changes).

Agents appear as Agent nodes in the graph and can be edited by double-clicking or
via the right-click menu.

---

## 3. Search

The search bar at the top of the page searches across all node names, aliases,
descriptions, summaries, and tags in the graph (including federated graphs if
federation is enabled).

![Search bar and results](images/search.png)
*Search results shown as a dropdown. Click a result to centre the canvas on that node and highlight it.*

**Tips:**
- Searches are fuzzy — partial words and approximate spellings are matched.
- Results show the node type alongside the name.
- Any node can carry **aliases / synonyms** (set them in the node editor). A node
  is found by its aliases too, so an abbreviation or alternate spelling still
  surfaces it — an alias match ranks just below a match on the node's real name.
- When multiple graphs are federated, results show a `<GraphName>: <NodeName>` prefix
  so you can tell which graph the result comes from.

---

## 4. AI Chat

Click the **→** arrow or the chat icon in the top bar to open the chat panel.

### 4.1 Asking questions

![Chat panel with conversation](images/chat-panel.png)
*The chat panel during a multi-turn conversation. The assistant can answer follow-up questions and load results directly into the canvas on request.*

Type any question in natural language. The assistant has full access to the graph:

> *"Which datasets are connected to the Labour Force Survey?"*
> *"Show me the production solutions for economic statistics."*
> *"What is the data structure for the employment dataset?"*

The assistant responds in whatever language you write in — switch mid-conversation freely.

Responses support **Markdown**: bold text, bullet lists, tables, and inline code are
rendered properly.

### 4.2 Adding nodes through chat

Ask the assistant to add content:

> *"Add a new initiative called 'Digital Identity Pilot' run by Skatteverket."*
> *"Create a connection between the LFS dataset and the NSI node for Sweden."*

Write operations go through the same validation as the UI — the node type must exist
in the current profile's schema.

### 4.3 Node proposals and duplicate detection

When the assistant extracts entities (from text or a document), it checks the graph
for similar existing nodes before proposing new ones.

You choose which proposals to **Approve** (adds to graph), **Link** (connects to an
existing match instead), or **Reject** (discard).

No nodes are added until you explicitly confirm them.

### 4.4 Skills

Skills are **Skill nodes** stored in the graph that contain domain-specific instructions.
Activating a skill in the chat adds those instructions to the AI assistant's context,
giving it specialised knowledge for a particular domain or task.

![Skills active in the chat panel](images/skills.png)
*Bottom of the chat panel showing an active skill ("Economic domain expert") and the currently selected nodes. Both are sent as context with every message.*

**Activating skills:**
Available skills appear as tags at the bottom of the chat input area. Click a tag to
activate or deactivate it. The active skill name is highlighted in the
**"Skills active:"** indicator.

**Selected nodes as context:**
When you select one or more nodes on the canvas, they appear as coloured pills in the
chat input (showing type and count). The assistant can then answer questions specifically
about those nodes without you having to name them.

> *Example: Select a ProductionSolution node, then ask "which datasets does this
> production system produce?" — the assistant already knows which node you mean.*

**Skills in context** — active skills and selected nodes are sent with every message.
You can switch skills or change the selection mid-conversation without starting over.

Skill nodes are created from the left toolbar (Skill node type) and edited like any
other node.

### 4.5 Node marking

The assistant can **visually annotate** nodes on the canvas with a colour dot and an
optional label. This is useful for highlighting a set of nodes by status, priority,
or any other temporary attribute.

> *"Mark all Actor nodes in red."*
> *"Mark Initiative nodes that relate to AI in green with the label 'AI-related'."*
> *"Remove all marks."*

A legend panel appears in the graph corner showing which colour maps to which label.

**Important:** Marks are session-only — they are never persisted to the graph and
disappear when you refresh the page.

### 4.6 Document upload

Click the **Upload** button (paperclip icon) in the chat input area to upload a
PDF, Word (.docx), or plain text file.

![Document upload with entity extraction](images/document-upload.png)
*After uploading a document, the assistant extracts entities and proposes them as new graph nodes, including suggested connections to existing content.*

Example workflow:
1. Upload a project description PDF.
2. The assistant extracts organisations, initiatives, and themes mentioned in the document.
3. It presents them as proposals with similarity scores against existing graph content.
4. You confirm which to add.

You can also ask questions about the uploaded document without extracting entities:

> *"What is the main goal of this document?"*
> *"List the organisations mentioned."*

### 4.7 Structured inputs in collection sessions

In an Active Knowledge Collection — the focused data-gathering assistant reached
through a collection link — the assistant can present interactive input controls
instead of asking for free text. When a question has a fixed set of choices or a
bounded numeric range, it renders **radio buttons**, **checkboxes**, **dropdowns**,
or a **slider** directly in the chat. This makes answering faster and keeps the
collected data consistent.

![Structured input form in a collection session](images/collection-form.png)
*The collection assistant presents a form with radio buttons, checkboxes, and a slider. The respondent answers with the controls and clicks Submit.*

When you submit a form, the assistant stores your answers as a structured
**CollectionResponse** — one record per submission, linked to the collection, with
a consistent field-by-field shape. Because every submission uses the same fields,
the responses for a collection can be compiled into simple aggregations or
statistics later (for example, counts per option). Ask the collection owner's
assistant to summarise the responses to see this in action.

Free-text answers still work at any time; the structured controls are offered only
where they make a question easier to answer.

---

## 5. Session menu and settings

Click the **☰** (hamburger) icon in the top-left corner to open the session menu —
a panel that slides out from the left and docks to the screen edge. While it is open,
the toolbar shifts right so it keeps floating next to the panel.

![Session menu open](images/hamburger-menu.png)
*The session menu with session navigation at the top, recent sessions in the middle,
and Settings at the bottom.*

Your work happens inside a **session** (the ID shown in the top bar). The session
menu lets you navigate between sessions, similar to how chats work in chat-based
AI apps:

| Item | Effect |
|------|--------|
| **Start new session** | Saves the current session automatically and opens a fresh, empty one |
| **Search previous sessions** | Filters the recent-session list by name or ID |
| **Connect to session (via ID)** | Joins an existing session by entering its ID (format `1234-5678`) |
| **Recent sessions** | The most recently used sessions — shown by name if you have named them, otherwise by ID. Click one to load it; the current session is saved automatically first. Use the pencil icon to name a session, or the trash icon to delete it. |
| **Recent activity** | Opens the activity panel — a read-only audit log of graph changes (see [5.2](#52-recent-activity-audit-log)) |
| **Settings** | Opens the Settings dialog (see below) |

Session content (node membership, positions, groups, and hidden nodes) is stored
**on the server**, so a session can be shared with others: the active session ID
is kept in the page URL (`?session=1234-5678`), and anyone who opens that URL —
or enters the ID via **Connect to session** — joins the same session. Your list
of recently visited sessions stays local to your browser; only the session
*content* lives on the server. The canvas is saved automatically while you work
and restored when you return to a session.

Deleting a session removes its content for everyone. If you delete the session
you are currently in, a fresh empty session is created and you are switched into
it automatically. When other people are connected to a session you delete, the
confirmation warns you how many are currently in it.

### 5.1 Collaborating in a session

Several people can work in the same session at the same time — just share the
session URL (or the session ID via **Connect to session**). Everyone sees the
same nodes, positions, groups and annotations, and changes appear for the others
within a fraction of a second. Each person still pans and zooms their own view;
only the content is shared, not the camera.

**Who else is here.** When at least one other person joins, a row of coloured
dots appears in the top bar next to the session ID — one dot per connected
person, each in that person's colour with the initial of their name. Hover a dot
to see the full name; your own dot is outlined in white.

![Presence roster in the top bar](images/presence-roster.png)
*Coloured presence dots show who else is currently in the session.*

**Who is working on what.** When someone selects one or more nodes, those nodes
get a coloured outline and a small name badge in that person's colour on every
other participant's screen. This lets you see at a glance what a collaborator is
holding, so you can avoid grabbing the same node. These markers are advisory —
they never lock anything — and they disappear automatically when the person
deselects, leaves, or a short time passes without activity.

**Your name.** By default you appear as *Guest-1*, *Guest-2*, and so on. Set a
recognisable name under **Settings → Your presence → Display name**; it is shown
to everyone in the roster and on your selection badges. The name is stored in
your browser and takes effect the next time you open or switch session.

### 5.2 Recent activity (audit log)

Open **Recent activity** from the bottom of the session menu to see a read-only
log of everything that has changed in the graph, newest first. The panel slides
in from the right edge of the screen.

![Recent activity panel](images/recent-activity.png)
*The activity panel lists graph changes newest-first, with an **AI** badge on
changes made by an agent.*

Each entry shows:

- **What happened** — node or connection created, updated, or deleted.
- **When** — a relative time (e.g. "5 min ago") with the exact date and time
  underneath.
- **Which item** — the entity's type and name (or ID).
- **Where it came from** — the origin of the change (for example `web-ui` or
  `mcp`), shown as *via …*.
- An **AI** badge when the change was made by an AI agent rather than a person.

For an update, a compact **before → after** diff lists the fields that changed.
Use **Load more** at the bottom to page further back through the history, and the
refresh icon in the header to reload from the top. This view never changes the
graph — it is purely for looking back at what happened.

History is also available per item: double-click a node (or open a connection's
editor) and switch to the **History** tab to see only that item's changes. When a
standalone graph has no recorded history yet, the panel simply shows that there is
no activity.

### Settings dialog

The settings that previously lived directly in the app menu are now in the
**Settings** dialog, opened from the bottom of the session menu.

#### Graph statistics

The top of the dialog shows the total number of nodes and edges currently in the graph,
followed by a colour-coded breakdown by node type. If there are more than five types,
a **Details** button opens a full node-type statistics dialog.

#### View settings

| Option | Effect |
|--------|--------|
| **Show minimap** | Toggle the minimap overlay in the bottom-right corner of the canvas |

#### Your presence

| Option | Effect |
|--------|--------|
| **Display name** | The name shown to other people collaborating in the same session (see [Collaborating in a session](#51-collaborating-in-a-session)). Leave it empty to appear as a numbered guest. |

#### Language

Switch between **English** and **Svenska**. The change takes effect immediately and
applies to all UI labels and chat placeholders.

The AI assistant always responds in the language you write in, regardless of the
UI language setting.

#### Admin

| Action | Description |
|--------|-------------|
| **Export graph** | Downloads the full graph as a JSON file |
| **Log out** | Signs you out (only relevant when authentication is enabled) |

---

## 6. Federation — searching across multiple graphs

When the application is connected to one or more remote graphs, a **depth selector**
appears next to the minimap in the lower-right corner of the canvas.

| Depth | What is searched |
|-------|-----------------|
| 0 | Local graph only |
| 1 | Local + directly configured remote graphs |
| 2+ | Local + graphs reachable via configured remotes |

**Federated results** are visually distinguished in search results and the graph:
- Node labels show `<GraphName>: <NodeName>` when results come from a remote graph.
- Federated nodes have a provenance badge indicating their source graph and distance.

Changing the depth only affects your current search and view — it does not change
what is permanently stored in the local graph.

**Node adoption:** If you want to permanently link a local node to an entity
from a remote graph, right-click the federated node and choose **Adopt** (if available).
This creates a local copy with an `ADOPTED_FROM` reference to the original.

---

## 7. Interactive guides

The app includes step-by-step guided tours triggered via URL parameter or by the
AI assistant.

**Start a guide via URL:**
```
https://your-app/web/?guide=first_intro
```

**Start a guide via the assistant:**
> *"Start the introduction guide."*

Guides display a floating tooltip that walks you through UI elements one step at a time.
Use **Next** / **Enter** to advance, **Escape** to cancel.

Available built-in guides are configured in `schema_config.json` under
`presentation.guides`. Administrators can add custom guides without code changes.

---

## 8. Connecting external AI tools via MCP

The application exposes its graph operations as a **Model Context Protocol (MCP)**
server. This lets external AI assistants (Claude, ChatGPT, Open WebUI, etc.)
interact with the graph directly — search, read, and write nodes — from their own
interface.

### 8.1 Available MCP tools

| Tool | Description |
|------|-------------|
| `search_graph` | Search nodes by query string |
| `get_node_details` | Get full details for a specific node |
| `get_related_nodes` | Get nodes connected to a given node |
| `find_similar_nodes` | Find nodes with similar names/descriptions |
| `add_nodes` | Add new nodes and edges |
| `update_node` | Update node properties |
| `delete_nodes` | Delete nodes by ID |
| `get_graph_stats` | Get summary statistics about the graph |
| `save_view` | Save a named canvas view |
| `get_capabilities` | Discover what the server supports |
| `connect_to_visualization_session` | Bind to a specific browser window by session ID |

### 8.2 Connecting

The MCP endpoint is available at:

```
https://your-server/mcp
```

Legacy SSE clients (older MCP protocol) can use:
```
https://your-server/mcp/sse
```

**SSPCloud users:** See [SSPCloud-setup.md](./SSPCloud-setup.md) for step-by-step
instructions on connecting from Claude or ChatGPT.

**Authentication:** If `MCP_BASIC_AUTH=true` is configured server-side, the MCP
endpoint requires Basic Auth credentials. The web GUI and REST API remain unprotected
in this mode (suitable for Cloud Run + IAP deployments).

### 8.3 Live visualization control via session ID

Each session has a **session ID**, shown in the top bar next to the application
title (e.g. `3953-2493`) and reflected in the page URL (`?session=3953-2493`). The
ID targets a shared, server-stored session: an external AI connected via MCP can
use it to push results directly into that session's canvas — in real time, while
you watch — and anyone who opens the same URL or connects by ID sees the same
content.

![External AI controlling the canvas via session ID](images/mcp-session-control.png)
*ChatGPT with the graph's MCP tool connected. The user tells it "Use session-id 3953-2493 — show the international statistical organisations here." The AI calls MCP tools and the nodes appear live in the browser on the right.*

**How it works:**

1. Open the app in your browser — note the session ID in the top bar.
2. In your external AI assistant (Claude, ChatGPT, etc.), connect to the MCP server.
3. Tell the assistant the session ID and what you want to see:
   > *"Use session-id 3953-2493. Show the international statistical organisations."*
4. The assistant calls `connect_to_visualization_session` with your session ID, then
   uses tools like `search_graph` and `get_related_nodes` to fetch data and push the
   result to your browser window.

The canvas updates in real time — nodes appear, edges are drawn, and the layout
adjusts automatically, all driven by the external AI without any manual work in
the browser.

**Use cases:**

- Use a powerful external assistant (e.g. a model with extended context or web access)
  to drive complex graph explorations while you observe and interact in the browser.
- Combine natural-language queries from one AI with graph navigation in another tool.
- Share your session ID with a colleague who can then direct the visualization from
  their own AI assistant, in a collaborative session.

---

## Appendix: Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| **Delete** | Delete selected node or edge |
| **Enter** (chat) | Send message |
| **Escape** | Cancel guide / close dialog |
| **Esc × 2** | Clear the canvas (remove all nodes from view) |
| **Ctrl+Z** / **Cmd+Z** | Undo (canvas layout changes only) |
| **Space + drag** | Pan the canvas |
| **Scroll wheel** | Zoom in/out |

---

## Appendix: Screenshot checklist

Screenshots for this guide are saved to `docs/images/` in PNG format.

| Filename | Status | What it shows |
|----------|--------|---------------|
| `ui-overview.png` | ✓ | Full app with graph, toolbar, search, and chat panel |
| `toolbar.png` | ✓ | Left toolbar with node type icons |
| `create-node.png` | ✓ | Create node dialog with fields filled in |
| `create-edge.png` | ✓ | Canvas showing edge creation in progress |
| `context-menu.png` | ✓ | Right-click menu on a node, custom action visible |
| `search.png` | ✓ | Search bar with dropdown results |
| `chat-panel.png` | ✓ | Chat panel with a multi-turn conversation |
| `node-proposal.png` | pending | Node proposal dialog with similarity matches |
| `node-marking.png` | pending | Canvas with colour marking dots and legend |
| `document-upload.png` | ✓ | Chat after file upload with extracted entity proposals |
| `hamburger-menu.png` | ✓ | Session menu (☰) — left panel with session navigation and Settings entry |
| `recent-activity.png` | pending | Recent activity panel (right side) with entries, an AI badge, and a before→after diff |
| `create-agent.png` | ✓ | Create Agent dialog with all configuration sections |
| `skills.png` | ✓ | Chat panel bottom showing active skill and selected nodes |
| `mcp-session-control.png` | ✓ | External AI (ChatGPT) controlling the canvas via session ID |
| `federation-depth.png` | pending | Canvas depth selector (requires federation-enabled instance) |
