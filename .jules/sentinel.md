## YYYY-MM-DD - [Title]
**Vulnerability:** Prefix-based authentication bypass
**Learning:** Using `request.url.path.startswith("/route")` to conditionally skip authentication in FastAPI middleware is vulnerable to prefix bypasses. For example, an attacker requesting `/route_bypass` (a hypothetical sensitive endpoint) would inadvertently bypass authentication because it starts with `/route`.
**Prevention:** Use a combination of exact string matching (`path == "/route"`) and strict subdirectory matching (`path.startswith("/route/")`) to accurately and securely identify the intended routes for auth exclusion/inclusion.
