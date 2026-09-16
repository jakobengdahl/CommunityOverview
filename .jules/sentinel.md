## YYYY-MM-DD - [Title]
**Vulnerability:** Prefix-based authentication bypass
**Learning:** Using `request.url.path.startswith("/route")` to conditionally skip authentication in FastAPI middleware is vulnerable to prefix bypasses. For example, an attacker requesting `/route_bypass` (a hypothetical sensitive endpoint) would inadvertently bypass authentication because it starts with `/route`.
**Prevention:** Use a combination of exact string matching (`path == "/route"`) and strict subdirectory matching (`path.startswith("/route/")`) to accurately and securely identify the intended routes for auth exclusion/inclusion.
## 2026-08-24 - Strict Mount Prefix Matching in Sub-application Path Routing
**Weakness (hardening, no bypass observed):** Non-strict prefix matching when stripping a root path in an ASGI mount.
**Learning:** In `backend/api_host/mcp_mount.py`, the sub-application path was derived with a bare prefix check (`if root_path and path.startswith(root_path):`). Since `root_path` does not end in a slash, a sibling path that merely shares the prefix (e.g. `/mcp-bypass`, `/mcpadmin/keys`) would have been rewritten as if it were a subpath of the mount. No such route exists here and no auth bypass was demonstrated, but the rewrite is wrong in principle and would become exploitable if a sibling prefix route were ever added.
**Prevention:** Always combine exact match with strict trailing slash prefix matching (`path == root_path or path.startswith(root_path + "/")`) when stripping root paths for sub-application routing in ASGI handlers.
## 2026-08-26 - Prevent Information Disclosure in 500 Errors
**Vulnerability:** API endpoints returned `str(e)` in 500 error responses, potentially leaking sensitive internal information (for example file paths, database schemas, or stack traces).
**Learning:** Passing the raw exception string directly to the `HTTPException` detail parameter is a common CWE-209 pattern.
**Prevention:** Use `logger.exception()` to log the full traceback server-side, and return a generic message such as `Internal server error` to the client for unexpected 500 responses.
## 2026-08-31 - Fix Authentication Bypass in Middleware Substring Matching
**Vulnerability:** In `backend/api_host/middleware.py`, authentication exclusion for the `/api/sessions/{id}/stream` endpoint was implemented using `"/api/sessions/" in request.url.path`. This allowed an attacker to bypass authentication for an unrelated endpoint by including the substring anywhere in the URL (e.g., `/admin/api/sessions/stream`).
**Learning:** Using substring matching (`in`) for authentication and authorization logic on URL paths is a critical vulnerability that allows attackers to craft paths that bypass security controls while still routing to sensitive handlers.
**Prevention:** Always use strict prefix matching (e.g. `.startswith()`) and/or exact matching, anchored to the configured base URL paths (like `config.api_prefix`), when writing security middleware rules.
## 2025-02-27 - Server-Side Request Forgery (SSRF) in MCPLoader fetch
**Vulnerability:** The `fetch` tool in `backend/agents/mcp_loader.py` passed unsanitized URLs to `httpx.get()` without checking against internal IPs, and it explicitly followed redirects using `follow_redirects=True`.
**Learning:** This permitted an agent (and potentially an attacker via agent prompt injection) to interact with internal infrastructure such as AWS metadata endpoints or localhost servers, bypassing the application's intended SSRF protections (like `backend.core.events.delivery.is_safe_url`), because even if validated before calling `httpx.get()`, `follow_redirects=True` lets a malicious server redirect to internal IPs unchecked.
**Prevention:** Always validate external URL requests against a strict `is_safe_url` allowlist. To handle redirects securely, use `follow_redirects=False` inside a manual loop that re-evaluates `is_safe_url(next_url)` before following the redirect.
