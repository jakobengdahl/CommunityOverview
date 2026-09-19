## 2026-09-19 - SSRF via Automatic Redirects in Skills Loader
**Vulnerability:** External skill URLs could redirect to internal IPs, causing SSRF because httpx was configured with follow_redirects=True which bypassed initial domain validation.
**Learning:** Validating only the initial URL is insufficient if the HTTP client automatically follows redirects. An attacker can set up a trusted external domain (or use an open redirect) to point to an internal resource (like AWS metadata or localhost).
**Prevention:** When fetching external URLs, always set follow_redirects=False. Manually walk the redirect chain, checking each new Location header against the SSRF validation function (e.g., is_safe_url) before making the request.
