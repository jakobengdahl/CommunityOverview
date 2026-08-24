## YYYY-MM-DD - [Title]
**Vulnerability:** Prefix-based authentication bypass
**Learning:** Using `request.url.path.startswith("/route")` to conditionally skip authentication in FastAPI middleware is vulnerable to prefix bypasses. For example, an attacker requesting `/route_bypass` (a hypothetical sensitive endpoint) would inadvertently bypass authentication because it starts with `/route`.
**Prevention:** Use a combination of exact string matching (`path == "/route"`) and strict subdirectory matching (`path.startswith("/route/")`) to accurately and securely identify the intended routes for auth exclusion/inclusion.
## 2026-08-24 - Strict Mount Prefix Matching in Sub-application Path Routing
**Weakness (hardening, no bypass observed):** Non-strict prefix matching when stripping a root path in an ASGI mount.
**Learning:** In `backend/api_host/mcp_mount.py`, the sub-application path was derived with a bare prefix check (`if root_path and path.startswith(root_path):`). Since `root_path` does not end in a slash, a sibling path that merely shares the prefix (e.g. `/mcp-bypass`, `/mcpadmin/keys`) would have been rewritten as if it were a subpath of the mount. No such route exists here and no auth bypass was demonstrated, but the rewrite is wrong in principle and would become exploitable if a sibling prefix route were ever added.
**Prevention:** Always combine exact match with strict trailing slash prefix matching (`path == root_path or path.startswith(root_path + "/")`) when stripping root paths for sub-application routing in ASGI handlers.
