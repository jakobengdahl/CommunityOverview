# ADR 0005 — Sessions may stage graph changes

- **Status:** Accepted
- **Date:** 2026-09-18
- **Scope:** Open-source core
- **Related:** [`SESSION_OVERLAY_CONTRACT.md`](../SESSION_OVERLAY_CONTRACT.md)
  (the contract this decision adopts);
  [`MULTI_USER_SESSIONS_DESIGN.md`](../MULTI_USER_SESSIONS_DESIGN.md) decision D4,
  which this decision revises for one mode of work

## Context

Every write to the graph lands in the graph at once. That holds for a node edited
on the canvas, an agent's tool call and a chat suggestion alike. A session
changes what people see together, but not what the graph holds. Decision D4 of the
shared-sessions design made that explicit: session state stores node references,
layout and annotations, never node copies.

That leaves no way to work on a graph without changing it. A workshop cannot
sketch a restructuring and decide afterwards what to keep. A controlled edit
cannot be reviewed before it takes effect. The closest thing today, agent
governance proposals, holds one tool call at a time. It carries no base version
and replays blindly, so a later change to the same node is silently overwritten.

Three facts about the current code shape the decision:

- **Nodes and edges carry nothing a change could be compared against.** Nodes
  have a wall-clock `updated_at` that is client-settable on create and not
  ordered across instances. Edges have no timestamp at all. The PostgreSQL
  backend writes with a blind upsert.
- **The session document is loaded, persisted and replayed whole.** It is also
  shared between instances only when the sessions directory is.
- **Under PostgreSQL there is no mutation history**, so an audit of what a
  merge did cannot be read back from history.

## Decision

1. **A session may hold a staged layer of node and edge changes.** In *staged*
   write mode, writes made in the session go to that layer instead of the graph.
   Reads made in the session see the graph with the layer applied. The graph is
   unchanged until someone explicitly merges. *Direct* mode, which is today's
   behaviour, stays the default and stays unchanged.
2. **The layer lives beside the graph, not in the session document.** The
   persistence backend that holds the graph also holds the layers, behind a
   capability it declares. That makes a merge one atomic unit together with the
   graph writes it causes, keeps the session document small, and puts the layers
   wherever the graph is shared. The session document keeps D4's shape:
   references, layout and annotations.
3. **Every node and edge carries a `revision` that only the storage assigns.** It
   starts at 1 and advances by one on every applied write. Staged changes record
   the entity they were based on, and main-graph writes can require the revision
   they expect. A backend shared by several writers enforces that expectation
   itself, rather than trusting any one instance.
4. **A merge never overwrites silently.** Conflicts are detected per field
   against the values the staged change was based on. An unresolved conflict
   stops the merge, and the merge applies completely or not at all.
5. **Composition is scoped to the request.** Only a request that acts in a staged
   session sees the composed view. System consumers keep reading the graph:
   event subscriptions, agent scheduling, federation peers and history.

## Consequences

- **D4 is revised for staged mode only.** A session in staged mode owns content
  that is not in the graph, but that content lives in the layer store, not in the
  session document. D4's rule about the document itself still holds.
- **Deleting a session can now lose work that exists nowhere else.** Deleting a
  session whose layer is not empty therefore needs an explicit discard.
- **The graph model grows a field.** An older build ignores `revision` and drops
  it from every entity it rewrites; a whole-graph save rewrites them all. The
  contract makes that fail safe: an entity without a
  revision reads as revision 0, and a staged change based on a later revision
  then conflicts instead of passing.
- **Every user-facing read path must compose.** That includes lexical search,
  semantic search and traversal, and it costs work per query in proportion to
  the size of the layer. The contract bounds the layer size.
- **Merge authority becomes a real question.** The core has no accounts (D7).
  The authorization hook gains merge and discard actions, and the default hook
  treats them like any other mutation.

The contract that implements this decision is
[`SESSION_OVERLAY_CONTRACT.md`](../SESSION_OVERLAY_CONTRACT.md).
