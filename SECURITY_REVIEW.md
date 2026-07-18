# Security Review

Date: 2026-07-18
Scope: `CommunityOverview` (public core), `CommunityOverview-SaaS`, `Community-Overview-corp`.
Method: manual source review of the backend API host, the graph REST/MCP surface,
the OAuth gateway, the frontend, CI/deploy workflows, and dependency pins. No
dynamic testing or dependency CVE scanning was run — see finding #9.

This is an action-tracking report. Each finding has a severity, the concrete
location, why it matters, and a **session-ready remediation**: enough detail that
a single Claude Code session can pick it up, implement it, and open a PR. Where a
fix requires a judgement call, the remediation spells out the **decision, its
consequences, and a recommendation**. Nothing here has been changed in code — each
fix should be its own branch/PR per the standard workflow.

## Summary

Overall the codebase is defensively written: parameterised access throughout (no
raw SQL, no shell/`eval`/`pickle`/`yaml.load` on untrusted input), Pydantic
validation at the HTTP boundary, constant-time credential comparison, PKCE
enforced on the gateway, and safe CORS defaults (empty allow-list = same-origin;
wildcard automatically disables credentials). No hardcoded secrets were found —
every credential is read from an environment variable, and `.env*` is gitignored.

The findings below are mostly in the OAuth gateway (`services/mcp_oauth_gateway/`)
and a few information-disclosure / hardening gaps in the API host. None is an
unauthenticated remote-code-execution class bug; the highest-impact items concern
OAuth token/redirect handling and an unauthenticated data-exposure surface whose
risk depends on deployment configuration.

| # | Severity | Area | Finding | Effort |
|---|----------|------|---------|--------|
| 1 | High | Gateway | `redirect_uri` never validated against the registered client → auth-code interception / open redirect | M |
| 2 | Medium | Gateway | Google ID token decoded with `verify_signature=False` (skips `aud`/`exp`); OIDC `nonce` unused | M |
| 3 | Medium | API host | 500 handlers leak full tracebacks / `print_exc` on `/execute_tool` and `/export_graph` | S |
| 4 | Medium | Gateway | 60-day access-token TTL with no revocation path | M |
| 5 | Medium | API host | Shared-session read/write endpoints bypass auth, guarded only by an 8-digit id | M |
| 6 | Low → resolved | Supply chain | `httpx2` investigated — legitimate (Pydantic's `httpx` continuation); no action needed | — |
| 7 | Low | Gateway | Unbounded in-memory DCR store; auth-code/DCR stores not replica-shared | S |
| 8 | Low | API host | `/export_graph` lacks the read-allow-list gate `/execute_tool` has when auth is inactive | S |
| 9 | Info | All repos | Dependency + CodeQL scanning already exist (advisory); close the remaining gaps — Python SAST, secret scanning, promote-to-blocking | S |

Effort key: XS = single line · S = ≤ ~30 lines / one file · M = multi-file or logic-heavy.

---

## Findings and remediations

### 1. `redirect_uri` is not validated against the registered client (High)

- **Files:** `services/mcp_oauth_gateway/main.py:147` (`/authorize`),
  `main.py:191` (`/callback`), `services/mcp_oauth_gateway/auth.py:165`
  (`issue_auth_code`), `auth.py:182` (`exchange_code_for_token`).
- **Issue:** `/authorize` accepts any `redirect_uri` from the query string,
  round-trips it through the Google `state`, and `/callback` redirects the browser
  to that value with a freshly issued authorization `code`. The `redirect_uris`
  recorded at Dynamic Client Registration (`/register`) are never consulted, and
  there is no allow-list of permitted redirect targets. An attacker who lures an
  allow-listed user through `/authorize?...&redirect_uri=https://evil.example`
  receives the authorization code at their own endpoint.
- **Why it matters:** Classic OAuth open-redirect / authorization-code interception.
  PKCE limits but does not remove the impact (mix-up and malicious-client variants
  remain), and the code + `state` are enough to complete a login as the victim in
  several attack shapes.

**Remediation (session-ready):**

1. Add a redirect allow-list config value in `config.py`, e.g.
   `ALLOWED_REDIRECT_URIS: List[str]` read from an env var
   (`ALLOWED_REDIRECT_URIS`, comma-separated). Support exact-match URIs; if
   prefix/wildcard matching is needed for local dev, gate it behind an explicit
   `ALLOW_LOOPBACK_REDIRECTS=true` that only permits `http://127.0.0.1:*` and
   `http://localhost:*` (RFC 8252 §7.3).
2. In `/authorize` (`main.py:147`), after the PKCE checks, reject any
   `redirect_uri` not on the allow-list (or, if `client_id` is present and known in
   `dcr_clients`, not in that client's registered `redirect_uris`) with
   `HTTPException(400, "redirect_uri not permitted")`. Validate **before**
   redirecting to Google.
3. In `/callback` (`main.py:191`), re-validate the `redirect_uri` decoded from
   `state` against the same allow-list before issuing the code — the state value is
   attacker-influenced and must not be trusted blindly.
4. Add tests in `test_oauth_flow.py`: (a) a disallowed `redirect_uri` → 400 at
   `/authorize`; (b) a tampered `redirect_uri` in the callback state → 400; (c) an
   allow-listed URI still completes the happy path.

**Decision — how strict should matching be?**
- *Option A (recommended): exact-match allow-list from env, plus RFC 8252 loopback
  exception behind a flag.* Consequence: any new client redirect URI is a config
  change (small ops overhead) but the interception vector is closed and the policy
  is auditable. Best fit for an allow-listed, small-user-base gateway.
- *Option B: trust the `redirect_uris` registered via `/register`.* Consequence:
  self-service, but `/register` is unauthenticated (finding #7), so an attacker can
  register their own malicious redirect and defeat the check. Only viable if
  `/register` is locked down first. **Not recommended** on its own.
- Recommendation: ship Option A now; revisit Option B only after #7 restricts
  registration.

---

### 2. Google ID token signature not verified; `nonce` unused (Medium)

- **File:** `services/mcp_oauth_gateway/auth.py:100-149` (`exchange_google_code`,
  `verify_signature=False` at `:134`); `nonce` generated at `main.py:180` and
  never checked.
- **Issue:** The ID token is decoded with
  `jwt.decode(id_token, options={"verify_signature": False})`, which also disables
  `exp` and `aud` validation. The `email_verified`/`email` decision is then made on
  unverified claims. The OIDC `nonce` is generated and sent to Google but never
  validated against the returned token.
- **Why it matters:** Defence-in-depth. The token currently comes straight from
  Google's token endpoint over TLS, so today the exposure is low — but any refactor
  that introduces caching, a second IdP, or a test seam would silently trust forged
  claims. `aud`/`exp`/`nonce` are cheap, standard OIDC guarantees.

**Remediation (session-ready):**

1. `PyJWT[crypto]` is already pinned in the gateway requirements, so use
   `jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")` to fetch Google's
   signing keys (construct it once at module load; it caches keys internally).
2. Replace the unverified decode in `exchange_google_code` with a verified one:
   ```python
   signing_key = _google_jwks.get_signing_key_from_jwt(id_token)
   claims = jwt.decode(
       id_token,
       signing_key.key,
       algorithms=["RS256"],
       audience=config.GOOGLE_OAUTH_CLIENT_ID,
       issuer=["https://accounts.google.com", "accounts.google.com"],
   )
   ```
   Keep the existing `email` / `email_verified` handling after verification.
3. Thread the `nonce` through the flow: store the issued `nonce` in the server-side
   flow state (see #4/#7 for where flow state should live) and assert
   `claims.get("nonce") == expected_nonce` in the callback.
4. Handle `PyJWKClientError` / `PyJWTError` by returning `None` (same failure shape
   as today) and logging a warning.
5. Tests: a token with a bad signature, wrong `aud`, expired `exp`, or mismatched
   `nonce` must all be rejected; a valid Google-signed token still passes. Mock the
   JWKS client so tests stay offline (CI has no network to Google).

**Decision — network dependency on Google JWKS in tests/CI.**
- Verifying signatures means production must reach Google's JWKS endpoint (it
  already reaches Google's token endpoint, so no new egress). Consequence: mock the
  JWKS client in tests so CI stays hermetic. Recommendation: proceed — the JWKS
  fetch is cached and the added assurance is worth it.

---

### 3. Full tracebacks returned / printed on server errors (Medium — info disclosure)

- **File:** `backend/api_host/tool_routes.py:98` (`traceback.print_exc()`) and
  `:114-116` (`{"error": str(e), "traceback": error_trace}` returned with
  `status_code=500`).
- **Issue:** `/export_graph` serialises the full Python traceback into the HTTP
  response body; `/execute_tool` prints stack traces to stdout. This leaks internal
  file paths, dependency versions, and code structure to any caller that can
  trigger a 500. `print_exc` is also on the CLAUDE.md "never stage debug artifacts"
  list, so it is a lint/policy violation as well.

**Remediation (session-ready):**

1. In `tool_routes.py`, replace `traceback.print_exc()` (`:98`) with
   `logger.exception("execute_tool failed for %s", tool_name)`.
2. In the `/export_graph` handler (`:113-116`), remove the `traceback` field and
   the `traceback.format_exc()` call; return
   `JSONResponse({"error": "internal error", "request_id": <id>}, status_code=500)`
   and `logger.exception(...)` server-side. Drop the now-unused
   `import traceback` at `:5` if nothing else needs it.
3. Optionally generate a short `request_id` (`secrets.token_hex(8)`) and log it
   alongside the exception so an operator can correlate a user-reported error to the
   server log without exposing internals.
4. Grep the rest of the backend for the same pattern
   (`grep -rn "traceback.format_exc\|print_exc\|traceback.*response" backend/`) and
   fix any sibling handlers so the response contract is consistent.
5. Test: force an exception in an executed tool and assert the response body
   contains no `traceback`/file-path substring and status is 500.

No decision required — this is a straight hardening fix. Effort S.

---

### 4. 60-day access-token TTL, no revocation (Medium)

- **File:** `services/mcp_oauth_gateway/config.py:61`
  (`ACCESS_TOKEN_TTL_SECONDS = 60 * 24 * 3600`); validation at
  `auth.py:232` (`validate_token`), enforcement at `main.py:321`
  (`_require_valid_token`).
- **Issue:** Gateway JWTs live 60 days and are stateless HMAC tokens, so there is
  no way to revoke one before expiry. Removing a user from `TEST_USERS` does **not**
  invalidate already-issued tokens — a de-listed or leaked token keeps working for
  up to two months. The long TTL exists to survive Cloud Run scale-to-zero without
  re-auth (a real UX need), so simply shortening it regresses that.

**Remediation (session-ready):** two complementary layers; do at least layer A.

- **Layer A — live allow-list re-check (small, high value).** In
  `_require_valid_token` (`main.py:321`), after `auth.validate_token` succeeds,
  call `auth.is_user_allowed(claims["sub"])` and raise `401` if the user is no
  longer allow-listed. Consequence: de-listing takes effect on the next request
  even with long-lived tokens; cost is one set-membership check per proxied
  request. Add a test: a valid token whose `sub` was removed from `TEST_USERS` is
  rejected.
- **Layer B — refresh-token model (larger).** Shorten `ACCESS_TOKEN_TTL_SECONDS`
  to minutes/hours and issue a longer-lived refresh token at `/token`, adding a
  `grant_type=refresh_token` branch. This restores the reconnect UX while shrinking
  the access-token exposure window.

**Decision — how far to go on revocation.**
- *Option 1 (recommended first step): keep the long-lived access token but add
  Layer A.* Consequence: no client changes, no new endpoints; de-listing works
  immediately; a leaked token of a still-allowed user remains valid until expiry.
  Good cost/benefit for the current small allow-listed audience.
- *Option 2: Layer A + a `jti` deny-list* (store revoked token ids, checked in
  `validate_token`). Consequence: enables per-token revocation, but reintroduces
  server-side state that must be shared across replicas (ties into #7). Adopt when
  per-token revocation becomes a real requirement.
- *Option 3: full short-lived access + refresh tokens (Layer B).* Consequence:
  standards-aligned and smallest exposure window, but the most client/flow work and
  more moving parts. Recommended target state once the gateway has more than a
  handful of users.
- Recommendation: implement Layer A now (this PR-sized change), and record Options
  2/3 as roadmap items in the SaaS repo.

---

### 5. Shared-session endpoints bypass auth, guarded only by an 8-digit id (Medium)

- **Files:** `backend/api_host/middleware.py:64-76` (auth bypass for `/sessions/`
  and `/api/sessions/*/stream`), `backend/core/session_store.py:38`
  (`SESSION_ID_RE = ^\d{4}-\d{4}$`) and `:356` (`_new_id`, ~10^8 space),
  session CRUD/ops/stream in `backend/service/rest_api.py:662-832`, rate limiter in
  `backend/core/session_manager.py:170` (`check_lookup_rate`) with defaults
  `lookup_bucket_capacity=60`, `lookup_refill_per_sec=2` (`:66-67`).
- **Issue:** Shared multi-user sessions are readable and mutable
  (`GET/PATCH/DELETE /api/sessions/{id}`, `POST .../ops`, SSE stream) with **no**
  Authorization header — by design, because `EventSource` cannot send one. The only
  protection is an unguessable 8-digit id plus per-IP lookup rate limiting. With the
  current bucket (2 guesses/sec sustained per key, burst 60), a single source needs
  a long time to hit a *specific* id, but hitting *any* live session out of a
  populated store is far cheaper, and a distributed source multiplies the rate. The
  session state carries graph node references, so a hit leaks or lets an attacker
  mutate session-scoped graph selections.
- **Why it matters:** On an instance where the main graph is authenticated, this is
  an unauthenticated side channel into session-scoped data. The rate limit is the
  right idea but the id is the weak part of the design.

**Remediation (session-ready):**

1. **Widen the id (main mitigation).** Change `_new_id` (`session_store.py:356`) to
   `secrets.token_urlsafe(16)` (≈128 bits) and update `SESSION_ID_RE` (`:38`) to
   match the new alphabet (`^[A-Za-z0-9_-]{16,}$`). This makes enumeration
   infeasible regardless of rate limiting. **Migration:** existing `NNNN-NNNN` ids
   in `data/sessions/` must keep resolving — either broaden the regex to accept
   *both* the legacy and new formats, or add a one-time migration; document the
   choice in the PR. This is the breaking-surface consideration to call out.
2. **Keep and verify the rate limit.** Confirm `check_lookup_rate` is called on
   every auth-bypassed session endpoint (it is, on `GET` and stream; verify
   `PATCH`/`DELETE`/`ops` paths too) and that the client key is the real client IP —
   review `_lookup_rate_key` in `rest_api.py:34` together with `TRUSTED_PROXY_HOPS`
   so that behind Cloud Run the key is the client, not a shared proxy address (a
   shared key collapses everyone into one bucket). Consider lowering
   `lookup_refill_per_sec` if step 1 is deferred.
3. **Tests:** id format round-trips through create → get → stream; legacy id still
   resolves (if kept); rate-limit returns 429 after the bucket is drained.

**Decision — widen the id vs. rely on the rate limit alone.**
- *Option A (recommended): widen to a 128-bit token.* Consequence: closes the
  enumeration risk permanently; requires a small migration/regex change for legacy
  ids. The ids also appear in shareable URLs (they are meant to be shared), so a
  longer id is slightly less pretty but semantically unchanged.
- *Option B: keep the short id, only tighten the rate limit.* Consequence: no
  migration, but the id space is still small and a distributed attacker can churn
  through it; you are trading a permanent fix for a tunable one. **Not recommended**
  as the primary control.
- Recommendation: Option A, accepting both id formats during a transition window.
  Document the trust model explicitly in `docs/MULTI_USER_SESSIONS_DESIGN.md`.

---

### 6. All HTTP uses `httpx2` (Low — supply chain) — RESOLVED: provenance confirmed

- **Files:** `backend/requirements.txt` (`httpx2>=2.0.0`),
  `services/mcp_oauth_gateway/requirements.txt` (`httpx2==2.5.0`), imported as
  `import httpx2 as httpx` across federation, skills loader, agents, and the
  gateway proxy/auth.
- **Original concern:** `httpx2` looked like an off-mainstream package carrying
  **all** outbound HTTP including the OAuth token exchange, raising a
  supply-chain / dependency-confusion question.
- **Investigation result (2026-07-18, PyPI):** `httpx2` is **legitimate and
  reputable**, not a typosquat. It is the continuation of `httpx` under
  **Pydantic's** stewardship (author Tom Christie — the original `httpx`/`encode`
  author; maintainer Pydantic Services Inc.; source
  `https://github.com/pydantic/httpx2`; BSD-3-Clause). Pydantic picked up
  `httpx` stewardship under the `httpx2` name because upstream `httpx` activity
  slowed. The project's newness (first release 2026) is why it was unfamiliar —
  it postdates this reviewer's knowledge cutoff. `pydantic` is already a core
  dependency of this codebase, so this does not add a new trust root.
- **Conclusion:** No code change required. The concentration/typosquat concern is
  withdrawn; this is a maintained package from a trusted vendor.
- **Optional hardening (not required):** the gateway already exact-pins `httpx2`
  by version. If defence-in-depth against a compromised re-publish is later
  wanted, add `--hash=` pinning (via `pip-compile --generate-hashes`) to the
  gateway requirements. Tracked as an optional item, not a fix.

---

### 7. In-memory DCR/auth-code stores: unbounded DCR, not replica-shared (Low)

- **Files:** `services/mcp_oauth_gateway/main.py:62` (`dcr_clients`),
  `services/mcp_oauth_gateway/auth.py:44` (`_code_store`).
- **Issue:** `dcr_clients` grows without bound and without TTL — the unauthenticated
  `/register` endpoint (`main.py:76`) lets anyone add entries indefinitely
  (memory-growth DoS, and it feeds the redirect-trust option rejected in #1). Auth
  codes are pruned, but both stores are per-process, so behind more than one Cloud
  Run instance a code issued on one replica cannot be redeemed on another
  (correctness; also pushes operators toward sticky sessions).

**Remediation (session-ready):**

1. Bound `dcr_clients`: add a max-entries cap and a TTL (evict oldest / expired on
   insert), mirroring `_prune_expired_codes`. Add a `client_id_expires_at` and drop
   stale registrations.
2. Consider requiring the `/register` endpoint to be reachable only during
   onboarding, or rate-limit it per IP (reuse the token-bucket pattern from
   `session_manager.py`).
3. For multi-instance correctness, either (a) document and enforce single-instance
   operation for the gateway (min instances = max instances = 1), or (b) move
   `_code_store` and `dcr_clients` to a shared store (Redis/Firestore). Auth codes
   are short-lived (5 min TTL), so single-instance is a legitimate, low-cost choice.

**Decision — shared state vs. single instance.**
- *Single instance (recommended near-term):* zero new infra; the 5-minute code TTL
  and OAuth flow tolerate it. Consequence: the gateway cannot horizontally scale,
  which is fine at current volume. Document it in the deploy contract.
- *Shared store:* enables scale-out and per-token revocation (ties into #4 Option
  2), at the cost of a Redis/Firestore dependency and its own security surface.
  Adopt when the gateway needs more than one instance.
- Recommendation: bound `dcr_clients` now (always correct), and pick single-instance
  operation until scale demands a shared store.

---

### 8. `/export_graph` has no read-allow-list gate when auth is inactive (Low)

- **File:** `backend/api_host/tool_routes.py:101` (`/export_graph`) vs. the
  `SAFE_TOOLS` gate at `:74-81` (`/execute_tool`).
- **Issue:** `/execute_tool` refuses non-safe tools when `auth_active` is false, but
  the sibling `/export_graph` endpoint has no equivalent check — on an instance with
  auth disabled it dumps the entire graph unauthenticated. This may be intended for
  fully-open standalone instances, but it is inconsistent with `/execute_tool` and
  easy to overlook when hardening a deployment.

**Remediation (session-ready):** decide the intended posture, then make it explicit.

- *If export should follow the same rule as unsafe tools:* add the `auth_active`
  guard to `/export_graph` (return 403 when `not auth_active`), matching
  `/execute_tool`. Add a test for both auth-inactive (403) and auth-active (200)
  cases.
- *If export is deliberately a public read on open instances:* add `export_graph`
  to the conceptual read-safe set, and document in `backend/DEVELOPMENT.md` that a
  full graph export is unauthenticated on auth-disabled instances so operators can
  make an informed choice.

**Decision — is a full graph export "read-safe"?**
- A whole-graph dump is materially more sensitive than a scoped `search_graph`, so
  treating it like the other `SAFE_TOOLS` reads is arguably too permissive.
- Recommendation: **gate it behind `auth_active`** (treat export as privileged) and
  document the choice. This is the safer default and keeps the two endpoints
  consistent; operators who truly want open export can still disable auth-scoping
  deliberately.

---

### 9. CI / repo hardening (Info)

- **Observations:** No hardcoded secrets found; `.env*` is gitignored; deploy
  workflows are `workflow_dispatch`-only and delegate real deployment to the infra
  repo; CORS and auth defaults are safe. Security scanning already exists and is
  broader than a first pass suggests: `.github/workflows/security-scan.yml` runs
  `pip-audit` over `backend/requirements.txt`, `backend/requirements-dev.txt` and
  the gateway requirements, plus `npm audit --omit=dev` over the JS workspaces, on
  every PR to `main`/`preview`/`dev` and on a weekly schedule; **CodeQL** runs via
  GitHub's repository-level default setup (`Analyze (python|javascript-typescript|
  actions)`). All of these passed on this PR. The dependency-audit steps are
  deliberately **advisory** (`continue-on-error: true`) so a fresh upstream advisory
  never blocks a merge (documented intent: flip to blocking once the baseline is
  understood).
- **Remaining gaps (this is what #9 is really about):** no Python **SAST** (e.g.
  `bandit`), no explicit **secret scanning** in CI (GitHub secret scanning / push
  protection status is a repo setting to confirm, not a committed workflow), and
  the dependency audits are still advisory rather than required checks.

**Remediation (session-ready):**

1. Add a Python SAST pass with `bandit -r backend services` (configured to skip
   `*/tests/` and known false positives) as a new job in `security-scan.yml`,
   non-blocking to start.
2. Confirm GitHub **secret scanning + push protection** is enabled on all three
   repos (repo Settings → Code security). If a committed control is preferred, add
   a `gitleaks` step to `security-scan.yml` instead of/alongside the native feature.
3. Once the `pip-audit`/`npm audit` baseline is clean, drop `continue-on-error`
   from those steps and add them to `dev` branch protection so a newly-introduced
   vulnerable dependency actually blocks a merge.
4. Mirror an equivalent `security-scan.yml` (dependency audit + SAST for any
   Python validators) and branch protection onto the SaaS/corp `main` branches.

**Decision — required vs. advisory scanners.** Making `pip-audit`/`bandit` required
can block merges on an upstream CVE with no fix available. The repo already made the
right initial call (advisory first — see the `security-scan.yml` header comment).
Recommendation: keep the advisory posture, triage the current baseline into
`SMALL_FIXES.md`, then promote the **dependency-audit** job to required once clean;
keep `bandit` advisory longer since SAST tends to be noisier.

---

## Notes on the two private repos

`CommunityOverview-SaaS` and `Community-Overview-corp` are documentation / planning
/ contract repos: YAML prototypes, validators (`scripts/validate_*.py`), and graph
JSON. No executable service code, no dynamic input handling, and no secrets were
found — placeholder values only, consistent with their stated policy. The
`scripts/validate_*.py` files parse in-repo files, not untrusted input. The main
cross-cutting recommendation for these repos is #9 (secret scanning / branch
protection). When the SaaS repo grows real services, revisit this review against
that code.

## What was NOT covered

- No dynamic/DAST testing or live exploitation was performed.
- No dependency CVE scan was run *as part of this review* — the repo's own CI
  already runs `pip-audit` + `npm audit` (see finding #9), which passed on this PR.
- Third-party package internals (including `httpx2`) were not audited beyond the
  provenance flag in #6.
- The frontend was checked for obvious sinks (`dangerouslySetInnerHTML`,
  `innerHTML`, `eval` — none found) but not exhaustively for DOM XSS.
