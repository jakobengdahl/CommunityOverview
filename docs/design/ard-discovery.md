# ARD discovery of the MCP server and its skills

**Status:** Design (target state). Nothing here is implemented yet.
**Scope:** Open-source core, plus the OAuth gateway in `services/`.
**Builds on:** [ADR 0016](../adr/0016-mcp-2026-07-28-and-skills.md) (skills in
three layers, `SkillProvider`, `SchemaSource`).

An instance can publish an [Agentic Resource Discovery](#external-facts) (ARD)
manifest at `/.well-known/ard.json`, so that crawlers and agent hosts can find
its MCP server and, where the server is open, its skills. Discovery is opt-in
and has three modes:

| `discovery.mode` | `/.well-known/ard.json` | Lists |
|---|---|---|
| `off` (default) | not served (see [C1](#changes-to-the-direction-owner-decision-needed)) | nothing |
| `listed` | served without authentication | one MCP server entry, text from configuration only |
| `public` | served without authentication | the MCP server entry and the skills an anonymous caller can read |

`listed` announces that a protected server exists, without saying anything
about what is in it. `public` is for instances whose MCP endpoint is already
reachable without credentials, so the manifest reveals nothing a caller could
not already read over MCP.

## External facts

Checked on 2026-11-28 against the primary sources listed at the end. Where a
source could not be read, the fact is marked unverified.

- **Version and status.** The ARD specification page states "Version: v0.91,
  Status: Proposal, Date: August 26, 2026" [ARD]. It is not a standard. The
  manifest path is `/.well-known/ard.json`; the earlier `ai-catalog.json` path
  and `ai-catalog` link relation are optional for consumers (§5.1).
- **Manifest.** "A JSON document with an `entries` array of ARD entries; any
  other top-level members are transport-defined and ignored by ARD" (§5.1).
- **Entry (§4.2).** MUST carry `identifier`, `displayName`, `type` (a media
  type) and exactly one of `url` or `data` (§4.3: "An entry MUST NOT carry
  both"). `representativeQueries` SHOULD hold 2–5 examples. `capabilities` MAY
  be given. Optional terms are `description`, `tags`, `version`, `updatedAt`,
  `metadata` and `trustManifest`. There is no `publisher` field: the publisher
  is derived from the identifier (§5.3.1).
- **Identifier (Appendix C).** `urn:air:<publisher>:[<namespace>:]<agent-name>`,
  where `<publisher>` is a fully qualified domain name and the namespace may
  have several colon-separated segments.
- **Normative schema (Appendix D).** Appendix D.1 names
  `spec/schemas/ard-entry.schema.json` (JSON Schema 2020-12, `ArdEntry` and
  `ArdManifest`, `additionalProperties: true` by design) as authoritative. The
  schema file itself returned HTTP 503 on 2026-11-28, so field types, the
  `updatedAt` format and whether `url` must be an HTTP(S) URL are, as of
  2026-11-28, unverified. The first implementation task pins them.
- **Media types (§3.3).** ARD lists `application/mcp-server-card+json`,
  `application/agent-skills+md` (or `text/markdown;
  profile="urn:air:agent-skills"`), `application/agent-skills+json` and others,
  and says their IANA registrations are pending and "the format may change".
- **Skills media type conflict.** The MCP skills extension says a
  `skill://<path>/SKILL.md` resource's `mimeType` SHOULD be `text/markdown`
  [EXT-SKILLS]. ARD uses `application/agent-skills+md` for the same kind of
  file. A third name, `application/ai-skill+md`, appears in neither source;
  whether any other source uses it is, as of 2026-11-28, unverified. This spec
  uses each name in its own layer: `text/markdown` on MCP resources (unchanged),
  `application/agent-skills+md` as the ARD entry `type`.
- **MCP server card.** Not part of any released MCP specification: the
  2026-07-28 changelog does not mention it [MCP-CHANGELOG]. The server card
  extension is listed as Beta [MCP-EXT], its repository README says
  "Experimental … not an accepted or official MCP extension" [SERVER-CARD], and
  its draft discovery document uses `application/mcp-server-card+json` with the
  card at `<streamable-http-url>/server-card`. The card's field schema was not
  read and is, as of 2026-11-28, unverified.
- **Restricted collections.** ARD v0.91 defines no restricted, private or
  access-controlled collection or entry. It says "Authentication is Delegated:
  agent authentication is handled by the specific artifact protocol, not the
  discovery layer" (§3.6). `listed` therefore relies on the MCP endpoint's own
  authentication; it is this project's convention, not an ARD feature.
- **Caching, content type, CORS.** ARD gives no guidance on HTTP caching
  headers, CORS or the manifest's `Content-Type`.
- **trustManifest (§4.5).** Optional. ARD constrains only
  `trustManifest.identity`, which "MUST align with the `<publisher>` domain",
  and defines no signing procedure of its own.

## Configuration

Discovery has its own file, `config/<profile>/discovery_config.json`, next to
`federation_config.json`. It is not part of `schema_config.json`, so no schema
or profile text can reach the manifest by mistake. A missing file means `off`.

```json
{
  "discovery": {
    "mode": "listed",
    "mcp_front": "oauth_gateway",
    "public_base_url": "https://graph.example.org",
    "publisher_domain": "graph.example.org",
    "entry": {
      "slug": "research-graph",
      "display_name": "Research knowledge graph",
      "description": "Knowledge graph of research actors, initiatives and resources.",
      "representative_queries": [
        "Which initiatives work on open research data?",
        "Find guidelines about data sharing"
      ],
      "tags": ["knowledge-graph"]
    },
    "cache_max_age_seconds": 300
  }
}
```

| Key | Default | Rule |
|---|---|---|
| `mode` | `off` | `off`, `listed` or `public`. Anything else fails startup. |
| `mcp_front` | none; required unless `off` | `direct` or `oauth_gateway`: what clients reach the MCP endpoint through. The backend cannot detect a gateway in front of it, so the operator states it. |
| `public_base_url` | none; required unless `off` | Absolute `https` origin without path, query or fragment (`http` only for `localhost`). The MCP URL is `<public_base_url>/mcp`. Never derived from the request's `Host` header. |
| `publisher_domain` | none; required unless `off` | Lower-case FQDN (IDNA A-labels, no port). Must equal the host of `public_base_url` exactly; a parent domain is refused. Sibling hosts under a shared parent, or under a public suffix, therefore get distinct publishers and cannot mint each other's identifiers. This only binds the identifier to the host that serves the manifest over `https`; it proves nothing about the wider domain, which is left to a later `trustManifest`. |
| `entry.slug` | none; required unless `off` | `^[a-z0-9]+(-[a-z0-9]+)*$`, at most 63 characters. It is the namespace for every identifier this instance publishes. |
| `entry.display_name` | none; required | 1–100 characters. |
| `entry.description` | none; required | 1–500 characters. |
| `entry.representative_queries` | none; required | 2–5 items, each 1–200 characters (ARD's SHOULD, enforced). |
| `entry.tags` | `[]` | At most 10 items matching the slug pattern, each at most 40 characters. |
| `cache_max_age_seconds` | `300` | 0–86400. |

All `entry` text is plain text. It goes through the same checks as
`SkillsLoader` content (the prompt-injection patterns and dangerous-HTML
removal in `backend/skills/loader.py`), and control characters are rejected. A
value that fails is a startup error, never silently cleaned. The entry text is
the only free text in a `listed` manifest.

### Validation matrix

`public` is allowed only when the MCP endpoint really accepts calls without
credentials. The check uses the configured flags, not just whether
`compute_auth_active` installs the middleware, so an instance where
`AUTH_ENABLED=true` but the password is missing does not count as open.

MCP is *open by configuration* when either:

- `MCP_AUTH_ENABLED=false` (MCP bypasses auth whatever the other flags say), or
- `AUTH_ENABLED` and `MCP_BASIC_AUTH` are both false.

| `AUTH_ENABLED` | `MCP_BASIC_AUTH` | `MCP_AUTH_ENABLED` | `mcp_front` | MCP open | `public` | `listed` |
|---|---|---|---|---|---|---|
| false | false | unset / true / false | `direct` | yes | allowed | allowed, warning |
| true | any | unset / true | `direct` | no | **refused** | allowed |
| true | any | false | `direct` | yes | allowed | allowed, warning |
| false | true | unset / true | `direct` | no | **refused** | allowed |
| false | true | false | `direct` | yes | allowed | allowed, warning |
| any | any | any | `oauth_gateway` | no (gateway) | **refused** | allowed |

Two further rules:

- `TRUST_FRONT_LAYER_ACTOR=true` means a front layer authenticates callers and
  sets the actor headers, so `public` is refused there too.
- If the MCP skills extension is disabled (`skills_config.mcp.enabled: false`),
  `public` lists only the MCP server entry.

"Refused" means the app fails at startup with a configuration error naming the
rule that failed, and does not start. It does not fall back to another mode.
"Allowed, warning" means `listed` on an open server: the manifest is correct
but says less than it could, so a startup warning suggests `public`.

## Manifests

The manifest is built from an allowlist: each entry is assembled field by field
from configuration, fixed core strings and, in `public` only, the skills
catalogue. Nothing is copied from the schema, the presentation config, MCP
instructions, tool lists or graph content. Layer 3 content knowledge
(`describe_graph`, `graph://profile`) never appears in either mode.

Identifiers are `urn:air:<publisher_domain>:<slug>:mcp` for the server and
`urn:air:<publisher_domain>:<slug>:skill:<skill-name>` for skills.

### `listed`

```json
{
  "entries": [
    {
      "identifier": "urn:air:example.org:research-graph:mcp",
      "displayName": "Research knowledge graph",
      "type": "application/mcp-server-card+json",
      "data": {
        "name": "Research knowledge graph",
        "description": "Knowledge graph of research actors, initiatives and resources.",
        "remotes": [
          {"type": "streamable-http", "url": "https://graph.example.org/mcp"}
        ]
      },
      "description": "Knowledge graph of research actors, initiatives and resources.",
      "representativeQueries": [
        "Which initiatives work on open research data?",
        "Find guidelines about data sharing"
      ],
      "tags": ["knowledge-graph"],
      "updatedAt": "2026-11-28T09:00:00Z"
    }
  ]
}
```

The `data` object is an inline server card holding only a name, a description
and the remote endpoint. Its field names follow the server card schema, which
was not verified (see above). The implementation pins them to the server card
extension's schema at that point, keeping the same three facts. The card never
carries tools, resources, prompts, instructions or capabilities, in either
mode. Clients get those by connecting, and on a `listed` instance the endpoint
answers with its normal `401` and, behind the gateway, its OAuth metadata.

`updatedAt` in `listed` changes only when the discovery configuration changes:
it is the modification time of `discovery_config.json`, so neither a restart
nor a schema change moves it, and schema changes do not touch a manifest that
contains no schema-derived text.

### `public`

`public` returns the same MCP entry, followed by one entry per discoverable
skill, in catalogue order:

```json
{
  "identifier": "urn:air:example.org:research-graph:skill:graph-working-method",
  "displayName": "Graph working method",
  "type": "application/agent-skills+md",
  "url": "skill://graph-working-method/SKILL.md",
  "description": "How to search, inspect and change this knowledge graph with its MCP tools.",
  "metadata": {
    "mcpServer": "urn:air:example.org:research-graph:mcp",
    "sha256": "<digest of the served SKILL.md bytes>"
  },
  "updatedAt": "2026-11-28T09:00:00Z"
}
```

Skills listed in `public`:

| Skill | Listed when | `displayName` / `description` from |
|---|---|---|
| `graph-working-method` (layer 1) | skills extension enabled | fixed core strings |
| `graph-semantics` (layer 2) | skills extension enabled | fixed core strings; no node or relationship type names |
| Curated or generated Skill nodes | `discoverable: true`, served as a skill, and readable by an anonymous caller | node name and `when_to_use`, after the skill sanitisation |

Skill entries point at the `skill://` resource the MCP server already serves,
and `metadata.mcpServer` names the server entry that resolves it. The `sha256`
is the digest the skills extension already advertises, so a host can tell
whether the file it approved has changed. Plain HTTP URLs to `SKILL.md` files
are not offered (see [C3](#changes-to-the-direction-owner-decision-needed)).
Whether ARD accepts a non-HTTP URI in `url` is unverified; if the schema
requires HTTP(S), the fallback is `data` with an `application/agent-skills+json`
descriptor carrying the same name, `skill://` URI and digest.

## The `discoverable` flag on Skill nodes

`SkillInput` (`backend/service/system_nodes.py`) gains
`discoverable: bool = False`, stored in the node's metadata like the other
Skill fields and settable through `create_skill` and the normal node update.

- It defaults to false, so no existing Skill node becomes discoverable when
  discovery is switched on.
- It only has an effect in `public`. `listed` ignores it.
- It does not grant access. A discoverable node is listed only if the same
  read check used for MCP skills (`SkillProvider.can_read` with an anonymous
  context) allows it, and only once curated Skill nodes are served as skills.
- Changing the flag, the name or `when_to_use` is a node change, which
  rebuilds the manifest and moves that entry's `updatedAt`.

## `updatedAt`, ETag and rebuilds

The manifest is built once and held in memory. It is rebuilt only when one of
its inputs changes:

| Input | Event | Entries affected |
|---|---|---|
| Discovery configuration | process start (the file is read once) | all |
| Schema and presentation | `SchemaSource` schema-changed event | the layer 2 skill entry (its digest) |
| Skill catalogue | the skills catalogue change that already drives `notifications/resources/list_changed` | skill entries |
| Discoverable Skill nodes | node create, update or delete of a `Skill` node | that entry |

Each entry's `updatedAt` is the time its content last changed. The builder
compares each rebuilt entry, without `updatedAt`, with the previous build and
keeps the old timestamp when nothing differs. Timestamps are RFC 3339 UTC with
a `Z` suffix; ARD gives no format, so this is the conservative choice. After a
restart, the server entry takes the modification time of `discovery_config.json`
(in both modes, so it follows the configuration only), the layer 1 and layer 2
skill entries take the time of the schema load, and Skill node entries take the
node's `updated_at`. In `listed` the server entry is the only entry, so its
`updatedAt` and `Last-Modified` stay the same across restarts and schema loads
while the configuration file is unchanged.

Requests never touch storage or take a storage lock: they serialise the cached
build. That keeps an unauthenticated path from adding load to graph reads.

## HTTP behaviour

- **Route:** `GET` and `HEAD /.well-known/ard.json` at the origin root, outside
  `api_prefix`. Registered only when `mode` is not `off`. Other methods get `405`.
- **Content type:** `application/json` (ARD names none), with
  `X-Content-Type-Options: nosniff`.
- **Caching:** `Cache-Control: public, max-age=<cache_max_age_seconds>`, a
  strong `ETag` (SHA-256 of the serialised body), `Last-Modified` set to the
  latest `updatedAt`, and `304 Not Modified` for a matching `If-None-Match`.
  `public` caching is safe because the body does not depend on the caller.
- **Caller independence:** the manifest is always built for an anonymous
  caller. Credentials, cookies and actor headers on the request are ignored, and
  the response is byte-identical with or without them.
- **CORS:** none added. Discovery consumers are crawlers and hosts, not browser
  pages; the existing same-origin default stays.
- **Size:** at most 100 entries and 64 KiB serialised. Skill entries beyond
  that are dropped in catalogue order and a warning is logged.

### Middleware bypass

`add_auth_middleware` gets one more public route, checked before every other
rule:

```python
if (
    discovery_mode != "off"
    and request.method in {"GET", "HEAD"}
    and request.scope.get("raw_path") == b"/.well-known/ard.json"
    and request.url.path == "/.well-known/ard.json"
):
    return await call_next(request)
```

- An exact match on the raw request path. Starlette's `request.url.path` is
  the percent-decoded `scope["path"]`, so `/.well-known%2Fard.json` or
  `/.well-known/ard%2Ejson` would match a check on it alone and then reach the
  route. The check therefore compares the undecoded ASGI `raw_path` bytes
  (which exclude the query string), and also the decoded path, so a non-empty
  `root_path` cannot shift it. A server that does not set `raw_path` gets no
  bypass. `/.well-known/ard.json/`, `/.well-known//ard.json`,
  `/.well-known/ARD.json`, percent-encoded variants and any other
  `/.well-known/` path stay guarded.
- `discovery_mode` is read once from the validated configuration at app
  construction, not per request.
- With `mode: off` there is no bypass and no route (see C1).

### OAuth gateway

The gateway only proxies explicit routes, so `/.well-known/ard.json` on its
origin returns `404` today. For `listed` behind the gateway it gets one route,
`GET`/`HEAD /.well-known/ard.json`, that forwards to the upstream without a
gateway token. It forwards only the method, path and `If-None-Match`, and
passes back only the status, body, `Content-Type`, `Cache-Control`, `ETag`
and `Last-Modified`. The gateway's own OAuth metadata routes do not change.

## `DiscoveryProvider` seam

Core builds the manifest for exactly one instance. A deployment can register
its own provider through `RuntimeExtensions`, for example to add a
`trustManifest` or adjust tags in `public`, but never to list other instances.
In `listed` the provider is not called at all (see C5): the manifest is the
core-built server entry, so its text comes from the discovery configuration
only.

```python
class DiscoveryProvider(Protocol):
    def entries(
        self, core_entries: Sequence[Mapping[str, Any]], context: DiscoveryContext
    ) -> Sequence[Mapping[str, Any]]: ...
```

- `DiscoveryContext` is frozen and holds the validated discovery configuration,
  the mode and the `SchemaSource` revision. It has no storage handle, no
  request, no principal and no schema object.
- The default provider returns `core_entries` unchanged.
- Core validates everything a provider returns and fails closed, per entry:
  - The required ARD fields are present, with exactly one of `url` and `data`.
  - The identifier starts with `urn:air:<publisher_domain>:<slug>:`.
  - Every URL in the entry passes a scheme allowlist. Core walks every string
    value at any depth, in every field (`url`, `data`, `description`,
    `metadata`, `trustManifest` and any other key), and treats as a URL each
    match of `[A-Za-z][A-Za-z0-9+.-]*://` and each value starting with `//`.
    Each must be either `http`/`https` with exactly the scheme, host and port of
    `public_base_url` after normalisation, or `skill://` naming a skill in the
    current `public` build. Any other scheme (`wss://`, `mcp+https://`, `ftp://`,
    …) or a scheme-relative `//` value drops the entry, even when the host
    matches.
  - `type` is one of the media types listed here.
  - The size limits above hold.
  Entries that fail are dropped and logged by identifier only. A provider that
  raises leaves the core entries in place.
- A provider never widens the mode. In `listed` core skips the provider and
  publishes the core entries, so it cannot add entries, rewrite `description`,
  `displayName`, `tags` or `metadata`, or add keys such as `trustManifest`.
  Validating a rewritten entry field by field could not tell configured text
  from schema or content text, so skipping is the only check that holds.
- Providers run on rebuild, not per request.

## Tests

One test per guarantee, in the module's own test files. The no-leak test is
required.

1. **Off:** with `mode: off` the path behaves like any unregistered path:
   `404` without auth, `401` with full auth, and no bypass. Parametrised over
   both auth setups.
2. **Validation matrix:** every row of the matrix, plus the
   `TRUST_FRONT_LAYER_ACTOR` rule, through one parametrised test asserting the
   resulting allowed set or startup refusal.
3. **Config validation:** invalid `public_base_url`, a `publisher_domain` that
   differs from the base URL's host (including a parent domain and a public
   suffix such as `example.org` for `graph.example.org`), query counts outside 2–5, and entry
   text that matches an injection pattern each fail startup (parametrised).
4. **`listed` manifest:** equals the exact expected structure built from a
   fixture configuration.
5. **No-leak:** a protected instance (`AUTH_ENABLED=true` with a credential)
   whose schema, node ids, node names and a `discoverable` Skill node all use
   random sentinel tokens, in `listed` mode. An unauthenticated GET returns `200`.
   Walking every key and string value of the parsed manifest finds none of the
   sentinels, the only identifier is the MCP server's, and every entry key is
   in the allowlist.
6. **`public` manifest:** layer 1 and 2 entries, a discoverable readable Skill
   node, no non-discoverable or unreadable node, and no layer 3 entry.
7. **Bypass exactness:** the path variants listed under *Middleware bypass*,
   including `/.well-known%2Fard.json` and `/.well-known/ard%2Ejson` sent with
   their raw bytes, a request without `raw_path`, and a `POST` to the exact
   path all stay `401` on a guarded instance.
8. **Caller independence:** bodies and ETags are identical with and without
   valid credentials.
9. **Caching and `updatedAt`:** a matching `If-None-Match` gets `304`; a
   schema-changed event moves the layer 2 entry's `updatedAt` and the ETag and
   leaves the server entry's `updatedAt` alone; in `listed`, rebuilding after a
   simulated restart and schema load with an unchanged configuration file gives
   the same `updatedAt`, `Last-Modified` and ETag.
10. **Provider seam:** in `public`, one parametrised test where a provider
    returns an own-namespaced entry carrying, in turn, a foreign identifier, a
    foreign `https` origin in `url`, a `wss://`, an `mcp+https://` and a
    scheme-relative URL on the own host, a foreign URL inside `metadata`,
    `description` and `trustManifest`, and a `skill://` name not in the build;
    each such entry is dropped and the core entries remain.
    In `listed`, a provider that adds an entry, rewrites the server entry's
    text or adds a `trustManifest` is never called, and the manifest equals the
    build without a provider (parametrised over both modes).
11. **Gateway:** the pass-through route forwards without a token and returns
    only the allowed headers, and the other gateway routes still require a token
    (in the gateway's existing test file).

## Later: `trustManifest`

ARD's `trustManifest` can carry identity binding and attestations, verified by
whatever trust framework it declares. Core does not emit one. A deployment that
needs one adds it through its `DiscoveryProvider`, in `public` only (C5).
Core validation then checks that `trustManifest.identity` names `publisher_domain`, so the identity matches
the identifier as ARD requires. Signing stays outside core.

## Changes to the direction (owner decision needed)

The direction is kept: `discovery.mode` is `off` (default) | `public` |
`listed`; `public` only when MCP is open; startup refuses it otherwise. These
points refine it in ways the owner should confirm:

- **C1 — `off` is not always a `404`.** With no bypass in `off`, an instance
  whose auth guards every path answers `401`, as it does for any unknown path.
  An unguarded instance answers `404`. Returning `404` everywhere would need a
  bypass in `off` too, which the leak guards rule out. Proposed: accept the
  `401`, which reveals nothing.
- **C2 — `mcp_front` is a required key.** The backend cannot see an OAuth
  gateway in front of it, so the operator must state it. Without that key, an
  instance behind the gateway would pass the `public` check while its MCP is in
  fact protected.
- **C3 — `skill://` only, no HTTP `SKILL.md` URLs.** Serving `SKILL.md` over
  plain HTTP would add a second unauthenticated path, beyond the one this spec
  allows.
- **C4 — `listed` lists only the MCP server.** Listing skill entries would
  publish skill names and descriptions from the graph for a protected server.
- **C5 — no `DiscoveryProvider` in `listed`.** The direction gives deployments a
  provider seam; this spec does not call it in `listed`, so a protected
  instance cannot carry a provider-added `trustManifest` or adjusted tags. A
  `listed` `trustManifest`, if needed, would come from the discovery
  configuration with the same text checks, as a later change.

## Implementation tasks

1. **Discovery configuration and startup validation:** a model and loader for
   `discovery_config.json`, the text checks, the validation matrix and the
   refusal at startup. Pin the ARD entry schema and the server card fields
   against their published schemas first.
2. **Manifest builder, route and bypass:** the allowlist builder for `listed`
   and `public`, the cached build, the `/.well-known/ard.json` route, the
   exact raw-path bypass in `add_auth_middleware`, and the caching headers.
3. **`DiscoveryProvider` seam:** the protocol, `DiscoveryContext`,
   registration on `RuntimeExtensions`, core validation of provider output in
   `public`, and skipping the provider in `listed`.
4. **Skill entries and the `discoverable` flag:** the `SkillInput` field and
   skill entries in `public`, built from the skills catalogue and filtered by
   anonymous `can_read`. Depends on curated Skill nodes being served as skills.
5. **Rebuilds and `updatedAt`:** rebuilds on `SchemaSource` schema-changed
   events, skill catalogue changes and Skill node changes, with per-entry
   `updatedAt` and the ETag. Depends on `SchemaSource`.
6. **Gateway pass-through:** the `GET`/`HEAD /.well-known/ard.json` route in
   `services/mcp_oauth_gateway` and its section in
   [mcp-oauth-gateway.md](../mcp-oauth-gateway.md).
7. **Documentation:** the endpoint in `backend/DEVELOPMENT.md`, the discovery
   file in [PROFILES.md](../PROFILES.md) and the
   [deployment guide](../DEPLOYMENT_GUIDE.md), and the new unauthenticated
   path in the [threat models](../governance/threat-models/README.md).
8. **Later:** optional `trustManifest` through a deployment's provider, with
   the identity check described above.

## Sources

All read on 2026-11-28.

- [ARD] Agentic Resource Discovery specification v0.91:
  <https://agenticresourcediscovery.org/spec/>. Schema named in Appendix D.1,
  not retrievable on that date:
  <https://github.com/ards-project/ard-spec/blob/main/spec/schemas/ard-entry.schema.json>
- [EXT-SKILLS] MCP skills extension, beta specification:
  <https://github.com/modelcontextprotocol/ext-skills/blob/main/specification/beta/skills.mdx>;
  overview: <https://modelcontextprotocol.io/extensions/skills/overview.md>
- [MCP-EXT] MCP extensions overview:
  <https://modelcontextprotocol.io/extensions/overview.md>
- [MCP-CHANGELOG] MCP 2026-07-28 changelog:
  <https://modelcontextprotocol.io/specification/2026-07-28/changelog>
- [SERVER-CARD] MCP server card extension:
  <https://github.com/modelcontextprotocol/ext-server-card> and
  <https://github.com/modelcontextprotocol/ext-server-card/blob/main/docs/discovery.md>;
  SEP-2127: <https://modelcontextprotocol.io/seps/2127-mcp-server-cards.md>
- Agent Skills format (no media type defined):
  <https://agentskills.io/specification>
