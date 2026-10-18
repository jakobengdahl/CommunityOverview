# Threat model for OSS Scanner

Guidance for automated security scanning of this repository. The human-facing
reporting policy is [`SECURITY.md`](../SECURITY.md); this file describes what
the code is meant to guarantee and how we rate findings.

## What the project is

A self-hostable knowledge-graph service: a FastAPI backend (`backend/`) that
stores nodes and edges, serves a REST API under `/api`, an MCP server under
`/mcp`, a React web app (`frontend/web`) and an embeddable widget
(`frontend/widget`), plus LLM-backed chat and background agents. A separate
OAuth 2.1 gateway (`services/mcp_oauth_gateway/`) can sit in front of the MCP
endpoint for remote MCP clients.

## Deployment assumptions

- One deployment serves one graph to one trusted community. There is no
  per-user role model inside the core: authenticated callers are equally
  privileged, and `COMMUNITYOVERVIEW_AUTHORIZATION_MODE` only switches the
  whole instance between permissive, read-only and deny-all.
- Authentication is opt-in (`AUTH_ENABLED`, Basic / Bearer / signed session
  cookie, see `backend/api_host/middleware.py`). An instance run with auth off
  is a deliberate operator choice; "anyone can write when auth is off" is not a
  finding. A bypass of auth **when it is enabled** is.
- Operator-owned inputs are trusted: environment variables, files under
  `config/` (profiles, schema, federation topology), and the federation config
  that names remote graph URLs.
- The actor/scope headers (`x-communityoverview-*`) are meant to be set by a
  trusted gateway in front of the core (`docs/EXTERNAL_ADMIN_AND_AUTOMATION_SEAMS.md`).

## Where untrusted input enters

Treat everything below as attacker-controlled when auth is enabled and the
attacker has no credentials, or when the attacker is a remote party the
instance talks to:

- HTTP requests to unauthenticated routes: health/info endpoints, login,
  `/sessions/*` and `/api/sessions/{id}/stream` (protected only by the session
  id being unguessable), the read-only `SAFE_TOOLS` path in
  `backend/api_host/tool_routes.py`, `/collect/{short_name}` (public
  knowledge-collection kiosk).
- Files: graph import and archive import (`backend/service/import_service.py`,
  `graph_import.py`, `graph_archive.py`), uploads and attachments, and the
  document/PDF/image extractors in `backend/core/` (`attachment_extract.py`,
  `pdf_extractor.py`, `image_ingest.py`).
- Outbound fetches whose target is graph data rather than operator config:
  webhook delivery (`backend/core/events/delivery.py`), image-by-URL ingest,
  SKILL.md fetching (`backend/skills/loader.py`), remote MCP integrations
  configured on Agent nodes (`backend/agents/mcp_loader.py`). These carry SSRF
  guards; a bypass of them is in scope.
- Content returned by remote federated graphs and remote MCP servers.
- Graph content and fetched skills that reach LLM prompts (prompt injection
  leading to tool calls the user did not ask for).
- The web app and widget rendering graph content (stored XSS), and the widget
  being embedded on third-party pages.
- The OAuth gateway: the authorization-code / PKCE flow, redirect-URI checks,
  issued JWTs, the user allow-list, and header forwarding to the upstream core.

## In scope, by priority

1. Authentication bypass when auth is enabled, on the core or the gateway.
2. Remote code execution, command or argument injection (including via stdio
   MCP integration config or the attachment extractor subprocess), path
   traversal on import, archive or attachment paths.
3. SSRF guard bypasses that reach private, loopback or metadata addresses.
4. Secret disclosure: LLM/API keys, `secret://` references resolved for the
   wrong caller, signing keys, tokens in logs or responses.
5. Stored XSS through graph content in the web app or widget.
6. Session-id predictability or leakage that lets a stranger read or drive a
   visualization session.
7. Cross-graph or cross-workspace data access where scoping is enforced.

## Out of scope

- Anything that requires operator-level access: editing env vars, `config/`
  files or the federation config, or a compromised host.
- Behaviour of an instance with auth disabled, other than the unauthenticated
  routes listed above being reachable by design.
- Equal-privilege actions between authenticated users (no roles exist), e.g.
  one authenticated user editing or archiving another's nodes, or creating an
  EventSubscription or Agent node (graph writers are trusted to configure them).
- Volumetric denial of service; missing hardening headers with no concrete
  impact; vulnerabilities only in third-party dependencies that this code does
  not make reachable.
- Prompt injection whose only effect is a wrong or odd chat answer, with no
  unauthorized tool call or data exfiltration.
- `frontend/xr` (WebXR spike), test code, `scripts/`, and example data under
  `config/*/graph.json`.

## Severity guidance

- **Critical:** unauthenticated RCE; unauthenticated full read/write of the
  graph on an auth-enabled instance; gateway token forgery.
- **High:** SSRF to internal or metadata endpoints; path traversal writing
  files; secret disclosure to an unauthenticated caller; stored XSS reachable by
  an unauthenticated party (e.g. via the public kiosk) that runs in an
  authenticated user's browser.
- **Medium:** stored XSS that needs an authenticated writer; prompt injection
  that triggers an unintended write tool call; session-stream access needing a
  leaked session id; information disclosure of non-secret internal state.
- **Low:** issues needing unusual operator configuration, or with no
  demonstrated exploit path.

Memory-safety is not a concern here (Python and JavaScript only); cap
resource-exhaustion findings at Low unless a single small request causes it.

## Reports and patches

- Report in English, against the `main` branch.
- A reproducer as a pytest test (backend, `backend/<module>/tests/`) or a
  vitest test (frontend) is the most useful form; patches should be minimal and
  include such a regression test.
- Build and test commands are those in `.oss-scanner/Dockerfile`.
