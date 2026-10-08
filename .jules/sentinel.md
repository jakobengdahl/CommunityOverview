
## 2024-10-07 - Middleware Authentication Bypass via .startswith() and .endswith()
**Vulnerability:** The API auth middleware bypassed authentication for SSE streams using `.endswith("/stream")` combined with `.startswith("/api/sessions/")`. This created a wildcard path traversal vulnerability allowing unauthorized access to arbitrary paths ending in `/stream` within the sessions scope (e.g. `/api/sessions/bypass/stream`).
**Learning:** The use of string suffix/prefix matching methods like `startswith` and `endswith` for route matching in authentication middleware is inherently dangerous, as attackers can easily inject the required strings into malicious requests.
**Prevention:** Always use strict Regex (`re.match`) with both start (`^`) and end (`$`) anchors along with `re.escape()` to precisely specify configuration-dependent paths that require bypass logic, preventing any unwanted traversal or parameter injection.
