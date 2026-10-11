## YYYY-MM-DD - Path Traversal Bypass in Middleware
**Vulnerability:** Combining `.startswith("/api/sessions/")` and `.endswith("/stream")` to bypass authentication allowed wildcard path traversal (e.g., `/api/sessions/hack/stream/bypass`).
**Learning:** Checking ends-with and starts-with separately leaves the middle and exact endpoints unconstrained.
**Prevention:** Use strict regex matching (`re.match(f"^{re.escape(prefix)}/sessions/[^/]+/stream$")`) to ensure the entire path structure is exactly as intended before bypassing auth.
