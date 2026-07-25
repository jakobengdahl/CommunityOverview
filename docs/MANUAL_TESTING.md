# Manual Testing Checklist

This checklist is used for manual verification before `dev` is merged to `preview`.
It is updated when PRs that require manual verification land on `dev`.

---

## How to use this list

1. Work through the relevant sections after a batch of features has been merged to `dev`
2. Check off and sign with a date in a copy — do not leave checkmarks in this file
3. If a test fails: open an issue and block the merge until it is resolved

---

## Security

### CORS configuration (PR #56)
**Purpose:** Verify that the CORS policy does not allow credentials with a wildcard origin.

- [ ] Start the app without `CORS_ALLOWED_ORIGINS` set (default `*`)
- [ ] Send a cross-origin request with credentials from another origin (e.g. via browser devtools or curl with `Origin` header)
- [ ] Verify that `Access-Control-Allow-Credentials` is **absent** in the response
- [ ] Set `CORS_ALLOWED_ORIGINS=https://your-test-domain.com` and restart
- [ ] Send a request from the allowed origin → credentials header should be present
- [ ] Send a request from a **disallowed** origin → no CORS headers in the response

### SSRF protection in webhook delivery (PR #103)
**Purpose:** Verify that webhook delivery blocks calls to internal/private IP addresses.

- [ ] Create an `EventSubscription` node with a webhook URL to a public test endpoint (e.g. `https://webhook.site/...`)
- [ ] Trigger a graph mutation → verify that the webhook is delivered (check the endpoint)
- [ ] Create an `EventSubscription` with URL `http://127.0.0.1:8000/health`
- [ ] Trigger a mutation → verify that the delivery is logged as `DROPPED` (not attempted)
- [ ] Also test with `http://169.254.169.254/` (AWS metadata endpoint)
- [ ] **Known limitation:** DNS rebinding attacks (TOCTOU) require infra-level egress filtering for full mitigation

### Authentication on execute_tool (PR #39)
**Purpose:** Verify that `/execute_tool` requires auth for write operations.

- [ ] Start the app with `AUTH_ENABLED=false` (or not set)
- [ ] `POST /execute_tool {"tool_name": "add_nodes", "arguments": {...}}` without credentials → expected: **403 Forbidden**
- [ ] `POST /execute_tool {"tool_name": "search_graph", "arguments": {...}}` without credentials → expected: **200 OK**
- [ ] Restart with `AUTH_ENABLED=true` and `AUTH_PASSWORD` set
- [ ] Same write call with valid credentials → **200 OK**
- [ ] Verify that the "Save View" button in the chat panel still works (now uses `api.addNodes` internally)

---

## UI / Frontend flows

### EventSubscription and Agent nodes (PR #142)
**Purpose:** Verify that nodes of these types are displayed correctly in the graph immediately after creation.

- [ ] Click "+" and select `EventSubscription` → the node should appear in the graph **without a page reload**
- [ ] Click "+" and select `Agent`, link to an EventSubscription → Agent node and edge appear immediately
- [ ] Open the node-type dropdown → `EventSubscription` and `Agent` should be in the list
- [ ] Ask the chat assistant about EventSubscription nodes → assistant responds with knowledge of the type
- [ ] Open the `/stats` endpoint (`GET /api/stats`) with EventSubscription/Agent nodes in the graph → no crash, correct count

---

## Add new sections here

When a PR that requires manual testing is merged to `dev`, add a section with:
- PR number and a one-line description
- Checklist with concrete steps and expected outcomes
