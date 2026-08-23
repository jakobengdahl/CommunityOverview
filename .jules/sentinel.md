## YYYY-MM-DD - [Title]
**Vulnerability:** Prefix-based authentication bypass
**Learning:** Using `request.url.path.startswith("/route")` to conditionally skip authentication in FastAPI middleware is vulnerable to prefix bypasses. For example, an attacker requesting `/route_bypass` (a hypothetical sensitive endpoint) would inadvertently bypass authentication because it starts with `/route`.
**Prevention:** Use a combination of exact string matching (`path == "/route"`) and strict subdirectory matching (`path.startswith("/route/")`) to accurately and securely identify the intended routes for auth exclusion/inclusion.
## 2025-03-05 - Auth Bypass via Sub-application Path Routing
**Vulnerability:** Prefix-based routing vulnerability in ASGI mounts.
**Learning:** In `backend/api_host/mcp_mount.py`, determining the sub-application path by stripping the root path used a prefix check (`if root_path and path.startswith(root_path):`). Since `root_path` doesn't typically end in a slash, this could allow a request matching the prefix but not being a true subpath (e.g., `/mcp-bypass`) to be incorrectly passed to the mounted app, potentially bypassing intended route handling or authorization.
**Prevention:** Always combine exact match with strict trailing slash prefix matching (`path == root_path or path.startswith(root_path + "/")`) when stripping root paths for sub-application routing in ASGI handlers.
