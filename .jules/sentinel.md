## 2025-02-14 - Prevent FastAPI route bypass in auth middleware
**Vulnerability:** Combining `.startswith()` and `.endswith()` for route matching in FastAPI middleware allows unauthorized access via path traversal/wildcard matching (e.g., allowing `/api/sessions/hack/stream/bypass`).
**Learning:** Using simple string operations for dynamic route checking in middleware is insecure when path variables are involved, especially when stripping strings or allowing variables to carry slashes.
**Prevention:** Use strict regex (`re.match`) with start/end anchors and `re.escape()` for configuration prefixes instead of substring matching.
