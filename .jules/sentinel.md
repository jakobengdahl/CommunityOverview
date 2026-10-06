## 2025-02-23 - URL Path Substring Match Authentication Bypass
**Vulnerability:** Authentication bypass in FastAPI/Starlette middleware via combined `startswith()` and `endswith()` URL checks.
**Learning:** Combining `.startswith("/api/sessions/")` and `.endswith("/stream")` creates a wildcard-like bypass `^/api/sessions/.*/stream$`, allowing an attacker to inject arbitrary sub-paths (e.g., `/api/sessions/admin_actions/stream`) to circumvent authentication rules.
**Prevention:** Never use substring matching or unconstrained combination prefix/suffix matching for routing. Use strict regex (`re.match` with start/end anchors) or exact matching where possible.
