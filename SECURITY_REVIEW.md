# Security Review

Date: 2026-07-18
Scope: `CommunityOverview` (public core), `CommunityOverview-SaaS`, `Community-Overview-corp`.
Method: manual source review of the backend API host, the graph REST/MCP surface,
the OAuth gateway, the frontend, CI/deploy workflows, and dependency pins. No
dynamic testing or dependency CVE scanning was run — see "Recommended tooling".

This is an action-tracking report. Each finding has a severity, the concrete
location, why it matters, and a proposed fix. Nothing here has been changed in
code; fixes should be scoped as their own branches/PRs per the workflow.

## Summary

Overall the codebase is defensively written: parameterised access throughout (no
raw SQL, no shell/`eval`/`pickle`/`yaml.load` on untrusted input), Pydantic
validation at the HTTP boundary, constant-time credential comparison, PKCE
enforced on the gateway, and safe CORS defaults (empty allow-list = same-origin;
wildcard automatically disables credentials). No hardcoded secrets were found —
every credential is read from an environment variable, and `.env*` is gitignored.

The findings below are mostly in the OAuth gateway (`services/mcp_oauth_gateway/`)
and a few information-disclosure / hardening gaps in the API host. None is an
unauthenticated remote-code-execution class bug; the highest-impact items are
around OAuth token/redirect handling and an unauthenticated data-exposure surface
that depends on deployment configuration.

| # | Severity | Area | Finding |
|---|----------|------|---------|
| 1 | High | Gateway | `redirect_uri` is never validated against the registered client → auth-code interception / open redirect |
| 2 | Medium | Gateway | Google ID token decoded with `verify_signature=False` (also disables `aud`/`exp`); OIDC `nonce` generated but never checked |
| 3 | Medium | API host | 500 handlers leak full tracebacks / `print_exc` to the client on `/execute_tool` and `/export_graph` |
| 4 | Medium | Gateway | 60-day access-token TTL with no revocation path |
| 5 | Medium | API host | Shared-session read/write endpoints bypass auth, guarded only by an 8-digit ID (100M space) |
| 6 | Low | Supply chain | All HTTP traffic (incl. the security-sensitive gateway) uses `httpx2`, an off-mainstream package — verify provenance |
| 7 | Low | Gateway | In-memory auth-code and DCR stores are unbounded for DCR and not shared across replicas |
| 8 | Low | API host | `/export_graph` has no read-tool allow-list gate when auth is inactive |
| 9 | Info | Both repos | Verify branch protection / secret-scanning is enabled; add automated dependency + SAST scanning to CI |

---

## Findings

### 1. `redirect_uri` is not validated against the registered client (High)

- **Files:** `services/mcp_oauth_gateway/main.py:147` (`/authorize`),
  `services/mcp_oauth_gateway/main.py:191` (`/callback`),
  `services/mcp_oauth_gateway/auth.py:165` (`issue_auth_code`).
- **Issue:** `/authorize` accepts any `redirect_uri` from the query string,
  round-trips it through the Google `state`, and `/callback` redirects the
  browser to that value with a freshly issued authorization `code`. The
  `redirect_uris` recorded at Dynamic Client Registration (`/register`) are never
  consulted, and there is no allow-list of permitted redirect targets. An attacker
  who lures an allow-listed user through `/authorize?...&redirect_uri=https://evil`
  receives the authorization code at their own endpoint.
- **Why it matters:** This is the classic OAuth open-redirect / code-interception
  vector. PKCE reduces (but does not eliminate) the impact: the code is bound to a
  `code_challenge` the attacker also controls when they initiate the flow, so a
  mix-up/redirect attack can still yield a usable token for the victim's identity.
- **Fix:** Validate `redirect_uri` against a configured allow-list (and/or the
  `redirect_uris` registered for the presented `client_id`) in `/authorize`, and
  reject unknown values with `400`. Persist the validated `redirect_uri` in the
  server-side flow state rather than trusting the callback round-trip.

### 2. Google ID token signature is not verified; `nonce` unused (Medium)

- **File:** `services/mcp_oauth_gateway/auth.py:134` (and `nonce` at
  `main.py:180`).
- **Issue:** `exchange_google_code` decodes the ID token with
  `jwt.decode(id_token, options={"verify_signature": False})`. The docstring argues
  this is safe because the code came from Google over TLS, but disabling signature
  verification also disables `exp` and `aud` validation, and the `email_verified`
  decision is then made on unverified claims. Separately, a `nonce` is generated in
  `/authorize` and sent to Google but never checked against the returned token.
- **Why it matters:** Defence-in-depth: any future refactor that lets an
  externally-influenced token reach this path (caching, a second IdP, a test seam)
  would trust forged claims. `aud`/`exp`/`nonce` checks are the standard OIDC
  guarantees and cost little.
- **Fix:** Verify the ID token against Google's JWKS (`aud` = client id, issuer =
  `accounts.google.com`, `exp` enforced) using the `PyJWT[crypto]` +
  `PyJWKClient` already available, and validate the returned `nonce` matches the
  one issued for the flow.

### 3. Full tracebacks returned / printed on server errors (Medium — info disclosure)

- **File:** `backend/api_host/tool_routes.py:98` (`traceback.print_exc()`) and
  `:114-116` (`{"error": str(e), "traceback": error_trace}` returned to the client
  with `status_code=500`).
- **Issue:** The `/export_graph` handler serialises the full Python traceback into
  the HTTP response body, and `/execute_tool` prints stack traces to stdout. This
  leaks internal file paths, dependency versions, and code structure to any caller
  that can trigger a 500. `print_exc` is also on the CLAUDE.md "never stage debug
  artifacts" list.
- **Fix:** Log the traceback server-side (`logger.exception(...)`) and return a
  generic `{"error": "internal error"}` with a correlation id. Remove the
  `traceback` field from the response and the `print_exc()` call.

### 4. 60-day access-token TTL, no revocation (Medium)

- **File:** `services/mcp_oauth_gateway/config.py:61`
  (`ACCESS_TOKEN_TTL_SECONDS = 60 * 24 * 3600`).
- **Issue:** Gateway JWTs live 60 days, and because they are stateless HMAC tokens
  there is no way to revoke one before expiry (removing a user from `TEST_USERS`
  does not invalidate already-issued tokens). A leaked token grants two months of
  access.
- **Why it matters:** The comment explains the long TTL avoids re-auth after Cloud
  Run scale-to-zero, which is a real UX need — but the trade-off is a large
  exposure window with no kill switch.
- **Fix:** Issue a short-lived access token (minutes–hours) plus a refresh token,
  or add a server-side token version / `jti` deny-list keyed on the user so the
  allow-list change takes effect. At minimum, re-check `is_user_allowed(sub)` on
  every proxied request in `_require_valid_token` so de-listing is enforced live.

### 5. Shared-session endpoints bypass auth, guarded only by an 8-digit id (Medium)

- **Files:** `backend/api_host/middleware.py:64-76` (auth bypass for `/sessions/`
  and `/api/sessions/*/stream`), `backend/core/session_store.py:356`
  (`_new_id` → `NNNN-NNNN`, ~10^8 space), session CRUD/ops in
  `backend/service/rest_api.py:662-832`.
- **Issue:** Shared multi-user sessions are readable and mutable
  (`GET/PATCH/DELETE /api/sessions/{id}`, `POST .../ops`, SSE stream) without any
  Authorization header — by design, because `EventSource` cannot send one. The only
  protection is an unguessable 8-digit session id plus per-IP lookup rate limiting
  (`_rate_limit_lookup`). 100M ids is a modest space for a determined enumerator,
  and the session state carries graph node references.
- **Why it matters:** On an instance where the main graph is otherwise
  authenticated, this side channel can leak or corrupt session-scoped graph
  selections to an unauthenticated party who guesses/enumerates an id.
- **Fix:** The rate limiter is the right mitigation — confirm it is enabled and
  low-threshold by default, key it on the pre-proxy client IP correctly (see
  `_lookup_rate_key` / `TRUSTED_PROXY_HOPS`), and consider widening the id to a
  full `secrets.token_urlsafe` value. Document the trust model explicitly in
  `docs/MULTI_USER_SESSIONS_DESIGN.md`.

### 6. All HTTP uses `httpx2`, an off-mainstream package (Low — supply chain)

- **Files:** `backend/requirements.txt` (`httpx2>=2.0.0`),
  `services/mcp_oauth_gateway/requirements.txt` (`httpx2==2.5.0`), imported as
  `import httpx2 as httpx` across federation, skills loader, agents, and the
  gateway proxy/auth.
- **Issue:** The mainstream, widely-audited HTTP client is `httpx` (encode/httpx).
  `httpx2` is a different, far less prominent PyPI package and is used for **all**
  outbound HTTP including the security-sensitive OAuth token exchange. This is a
  concentrated supply-chain dependency on a low-profile package.
- **Fix:** Confirm `httpx2` is the intended, vetted dependency (pin by hash, review
  the publisher and source). If it is a fork chosen for a specific reason, document
  that reason; otherwise migrate to `httpx`. The gateway already hash-pins-by-version
  — extend that to full hash pinning for this package.

### 7. In-memory DCR/auth-code stores: unbounded DCR, not replica-shared (Low)

- **Files:** `services/mcp_oauth_gateway/main.py:62` (`dcr_clients`),
  `services/mcp_oauth_gateway/auth.py:44` (`_code_store`).
- **Issue:** `dcr_clients` grows without bound and without TTL — an open
  `/register` endpoint lets anyone add entries indefinitely (memory-growth DoS).
  Auth codes are pruned, but both stores are per-process, so behind more than one
  Cloud Run instance a code issued on one replica cannot be redeemed on another
  (correctness, and it pushes operators toward sticky sessions).
- **Fix:** Add a size cap + TTL to `dcr_clients` (or require the endpoint be
  reachable only during onboarding), and move both stores to a shared backend
  (or accept single-instance operation and document it).

### 8. `/export_graph` has no read-allow-list gate when auth is inactive (Low)

- **File:** `backend/api_host/tool_routes.py:101` vs the `SAFE_TOOLS` gate at
  `:74-81`.
- **Issue:** `/execute_tool` refuses non-safe tools when `auth_active` is false,
  but the sibling `/export_graph` endpoint has no equivalent check — on an instance
  with auth disabled it dumps the entire graph unauthenticated. This may be
  intended for fully-open standalone instances, but it is inconsistent with the
  `/execute_tool` gate and easy to overlook.
- **Fix:** Decide the intended posture and make it explicit: either gate
  `/export_graph` behind `auth_active` (like unsafe tools) or document that export
  is a public read on open instances, consistent with `SAFE_TOOLS`.

### 9. CI / repo hardening (Info)

- **Observations:** No hardcoded secrets found; `.env*` is gitignored; deploy
  workflows are `workflow_dispatch`-only and delegate real deployment to the infra
  repo; CORS and auth defaults are safe. CI runs tests + ruff/eslint but no
  security scanning.
- **Fix / recommended tooling:**
  - Add dependency vulnerability scanning (`pip-audit` for backend + gateway,
    `npm audit`/Dependabot for the frontend) as a CI job.
  - Add a Python SAST pass (`bandit`) and secret scanning (GitHub secret scanning
    / `gitleaks`) to CI.
  - Confirm branch protection + required checks on `dev` (documented in CLAUDE.md)
    and enable GitHub secret scanning + push protection on all three repos.

---

## Notes on the two private repos

`CommunityOverview-SaaS` and `Community-Overview-corp` are documentation / planning
/ contract repos: YAML prototypes, validators (`scripts/validate_*.py`), and graph
JSON. No executable service code, no dynamic input handling, and no secrets were
found — placeholder values only, consistent with their stated policy. The
`scripts/validate_*.py` files use `json`/`yaml.safe_load`-style parsing over
in-repo files, not untrusted input. The main cross-cutting recommendation for
these repos is #9 (secret scanning / branch protection).

## What was NOT covered

- No dynamic/DAST testing or live exploitation was performed.
- No dependency CVE scan was run (recommended as CI tooling — finding #9).
- Third-party package internals (including `httpx2`) were not audited beyond
  provenance flagging.
- Frontend was reviewed for obvious sinks (`dangerouslySetInnerHTML`, `innerHTML`,
  `eval` — none found) but not exhaustively for DOM XSS.
