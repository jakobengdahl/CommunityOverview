# MCP server threat model

This model describes the MCP surface on main. Planned skills are identified
separately below. It covers the host mount and optional OAuth gateway, through
tool execution and responses to the client.

## Assets

- Graph nodes, edges, schema, presentation, attachments and their integrity.
- Visualization sessions, staged changes, annotations and templates.
- Credentials, authenticated identity and graph access decisions.
- Server availability and the integrity of instructions and tool results used
  by a client model.

## Actors

A legitimate MCP client and its user invoke tools. The API host serves the
mount and graph service; an optional OAuth gateway can stand in front of it.
Graph and attachment authors can contribute content later returned to a client.
An adversary may be an unauthenticated requester, a client with limited access,
or an author of malicious content consumed by a model.

## Trust boundaries

1. Client requests cross the network into the host mount, directly or through
   the optional gateway. Gateway authentication and host authentication are
   separate boundaries; the gateway is not automatically present.
2. Request identity and arguments cross from the transport into tool and graph
   authorization, storage and session operations.
3. Graph content, attachments, schema and presentation cross back into the
   client's model context. Data from an author can be mistaken for instructions.
4. A client model's proposed tool calls cross back into operations that can
   change graph or session state. A staged session separates proposed graph
   changes from their eventual merge.

## Entry points

The `MCPServer` mount supports protocol 2026-07-28 and legacy versions
2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05. Streamable HTTP uses
`/mcp`; legacy SSE uses `/mcp/sse` and `/mcp/messages`. Negotiation and
compatibility are exercised in
[protocol compatibility tests](../../../backend/api_host/tests/test_mcp_protocol_compatibility.py);
[mount_mcp](../../../backend/api_host/mcp_mount.py) routes the transports.

[register_mcp_tools](../../../backend/service/mcp_tools.py) registers these
families (availability can depend on configuration):

- Reads: graph search, details, related and similar nodes, statistics, merge
  preview, schema and type information, runtime and request context; session
  state, activity, layout, notes, annotations, templates and attachment reads.
- Writes: graph node and edge creation, update, deletion, archive and restore,
  including system-node creation; attachment add and delete; visualization
  session, layout, membership, auto-add agent, annotation and template changes;
  staged-change merge and discard. Some operations affect the shared graph,
  while session operations can change session state or stage graph writes.
- Visualization tools also send graph or viewport content to a session or widget.
  Tool arguments, session identifiers and content returned by these operations
  are inputs across the boundaries above.

The model-facing server instructions are built from schema type descriptions,
presentation title and prompt prefix, and language policy at construction by
[build_mcp_instructions](../../../backend/api_host/mcp_mount.py). They guide
search and data management; they are not an authorization policy. Graph content
and attachment text returned to clients can also carry adversarial instructions.

**Planned, not yet on main:** serving skills through the protocol is described
in [ADR 0016, Security and Phasing](../../adr/0016-mcp-2026-07-28-and-skills.md#security).
It calls for filtering skills and related results by node-read access, private
cache scope when access varies, sanitizing known injection patterns and dangerous
HTML, and limits of 512 files and 16 MiB per skill. Digests establish consistency,
not source trust. These are planned controls, not present MCP skill enforcement;
Skill nodes created by existing tools are not the planned protocol capability.

## Threats (STRIDE-style)

- **Spoofing:** a requester impersonates a user or forges actor headers or a
  session credential to invoke tools.
- **Tampering:** a caller modifies graph or session data, or places instructions
  in graph content, attachments or configuration to influence a client model.
- **Repudiation:** a caller disputes a mutation or uses misleading attribution.
- **Information disclosure:** a caller reads graph content, attachment text or
  session data beyond its authority, or a client model exposes it after prompt
  injection.
- **Denial of service:** large requests, expensive searches, attachment extraction
  or repeated session mutations exhaust resources.
- **Elevation of privilege:** a model follows untrusted content and invokes a
  write tool, or a client bypasses the intended graph access policy.

## Mitigations with code references

- Host authentication is conditional: [compute_auth_active and
  add_auth_middleware](../../../backend/api_host/middleware.py) support configured
  Basic credentials, bearer tokens and signed login sessions, and can guard MCP
  paths. [mint_session_cookie and request_has_valid_session](../../../backend/api_host/session_auth.py)
  sign and validate session cookies. The middleware checks unsafe cookie-authenticated
  requests for same-origin or requested-with signals, and handles Basic browser
  writes separately. `ActorHeaderMiddleware` strips inbound actor headers unless
  explicitly configured to trust a front layer that replaces them.
- When used, the gateway validates bearer tokens in
  [_require_valid_token](../../../services/mcp_oauth_gateway/main.py). OAuth token
  subjects must also pass the user allow-list; a matching static API key is
  accepted before that check. The gateway forwards requests via
  [proxy](../../../services/mcp_oauth_gateway/proxy.py).
  Its registration endpoint has a per-client rate limit in `_check_register_rate`.
- [bind_request_authorization_to_asgi_app](../../../backend/api_host/mcp_mount.py)
  carries request authorization into the mounted transports.
  [authorize_graph_access](../../../backend/service/access.py) evaluates the
  configured hook; session tools call it for reads and mutations in
  [register_mcp_tools](../../../backend/service/mcp_tools.py). The
  [default hook](../../../backend/runtime/authorization.py) supports permissive,
  read-only and deny-all modes and denies admin actions. Attachment tools are
  separately gated by configuration and authorization in `register_tool`;
  [create_app](../../../backend/api_host/server.py) refuses enabled attachment
  tools when MCP authentication is inactive.
- [register_mcp_tools](../../../backend/service/mcp_tools.py) uses session-aware
  wrappers for staged writes; `add_nodes`, for example, documents sequence and
  existence checks. Session mutation tools such as `apply_visualization_layout`
  and `add_nodes_to_session` apply operation caps and rate budgets; these are
  tool-specific controls, not a universal MCP request rate limit.
- The MCP SDK dependency in [requirements.txt](../../../backend/requirements.txt)
  supplies the transport body limit, exercised for both transports by
  [test_mcp_integration](../../../backend/api_host/tests/test_mcp_integration.py)
  at 4 MiB. [ReadLimits.from_env, AttachmentReadTools.extracted and
  AttachmentReadTools.search](../../../backend/service/attachment_tools.py) bound
  extraction and search work. `ReadLimits.from_env` caps the decoded attachment
  limit at 3,000,000 bytes (also its default), and `_store_attachment` enforces
  it for content carried in tool arguments.
- Mutation context and attribution flow through graph operations; for example,
  `add_nodes` sets its event origin to MCP in
  [mcp_tools.py](../../../backend/service/mcp_tools.py). This records an origin,
  not proof that a model's choice was intended by its user.

## Residual risk

- Authentication is not automatically active: it depends on credentials and
  configuration, and MCP authentication can be explicitly bypassed. The default
  graph authorization mode is permissive and does not narrow graph visibility.
  A trusted front layer must actually replace actor headers when that trust is
  enabled. The optional gateway does not protect a separately reachable mount. A
  static API key grants gateway access without a user allow-list check.
- [mount_mcp](../../../backend/api_host/mcp_mount.py) disables the SDK's DNS
  rebinding protection for the mounted transports. Authentication and network
  placement are separate considerations; the mount itself supplies no host or
  origin allow-list through that SDK control.
- No control here establishes that graph content, attachments, schema descriptions
  or presentation text are safe instructions for a client model. An authorized
  client can still be induced to disclose data or make unwanted writes. The
  server instructions are guidance, not enforcement, and the planned skill
  sanitization does not protect current tool results.
- Tool-specific limits do not provide a general rate limit for every MCP read or
  write, and size limits do not prevent all expensive valid requests. Attribution
  depends on the authenticated context and does not prevent an authorized actor
  from repudiating intent. Planned skill controls remain unimplemented on this
  surface.
