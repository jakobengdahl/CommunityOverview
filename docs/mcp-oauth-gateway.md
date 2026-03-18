# MCP OAuth Gateway

The MCP OAuth Gateway is a small FastAPI service that wraps the CommunityOverview MCP endpoint
with OAuth 2.1 Authorization Code + PKCE authentication, using Google as the Identity Provider.
It runs as a separate Cloud Run service per pilot (esam / unece / scb) and per environment
(prod / dev).

---

## How it works

```
ChatGPT ──GET /sse──► MCP OAuth Gateway ──proxy──► CommunityOverview /mcp/sse
              OAuth 2.1 (PKCE + Google)
```

1. ChatGPT discovers the OAuth endpoints via `GET /.well-known/oauth-authorization-server`.
2. The gateway redirects the user to Google for login.
3. After login, Google redirects back to `GET /callback`; the gateway verifies the email
   against the `TEST_USERS` allowlist.
4. If allowed, the gateway issues a short-lived JWT (30 min) and proxies MCP traffic.

---

## Connecting the gateway in ChatGPT

1. Open **ChatGPT** → **Settings** → **Connectors** (or the relevant MCP plugin settings).
2. Set the **MCP Server URL** to:
   ```
   https://<PUBLIC_BASE_URL>/sse
   ```
   For example: `https://mcp.esam.communityoverview.example.com/sse`
3. ChatGPT will automatically fetch `/.well-known/oauth-authorization-server` and start
   the OAuth flow the first time you use the connector.
4. Sign in with your Google account when prompted.

> **Note:** If your Google account is not in the `TEST_USERS` list you will see a
> **403 Forbidden** error after login. Ask an administrator to add your email address.

---

## User allowlist (`TEST_USERS`)

Only users whose email addresses are listed in the `TEST_USERS` environment variable can log in.

### Check the current list

```bash
gcloud run services describe mcp-gateway-esam-prod \
  --region europe-north1 \
  --format "value(spec.template.spec.containers[0].env)"
```

### Add more users

Update the `TEST_USERS` variable in Cloud Run:

```bash
gcloud run services update mcp-gateway-esam-prod \
  --region europe-north1 \
  --update-env-vars TEST_USERS="alice@example.com,bob@example.com,carol@scb.se"
```

The change takes effect within a few seconds (no redeployment needed).

---

## Environment variables reference

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | Yes | OAuth 2.0 Client ID from GCP Console |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Yes | Injected from Secret Manager |
| `GW_JWT_SIGNING_KEY` | Yes | Random secret used to sign gateway JWTs (Secret Manager) |
| `TEST_USERS` | Yes | Comma-separated allowed emails, e.g. `a@gmail.com,b@scb.se` |
| `UPSTREAM_MCP_BASE_URL` | Yes | URL of the CommunityOverview instance, e.g. `https://communityoverview-esam-prod-xxx.run.app` |
| `PUBLIC_BASE_URL` | Yes | Public URL of this gateway, e.g. `https://mcp.esam.communityoverview.example.com` |
| `PORT` | No | TCP port (default `8080`) |

---

## Deploying a new gateway instance

```bash
# Build and push the container image
gcloud builds submit services/mcp_oauth_gateway \
  --tag europe-north1-docker.pkg.dev/MY_PROJECT/mcp-gateway/mcp-oauth-gateway:latest

# Deploy to Cloud Run
gcloud run deploy mcp-gateway-esam-prod \
  --image europe-north1-docker.pkg.dev/MY_PROJECT/mcp-gateway/mcp-oauth-gateway:latest \
  --region europe-north1 \
  --allow-unauthenticated \
  --set-env-vars "GOOGLE_OAUTH_CLIENT_ID=...,TEST_USERS=...,UPSTREAM_MCP_BASE_URL=...,PUBLIC_BASE_URL=..." \
  --set-secrets "GOOGLE_OAUTH_CLIENT_SECRET=google-oauth-secret:latest,GW_JWT_SIGNING_KEY=gw-jwt-key:latest"
```

> `--allow-unauthenticated` is required so ChatGPT can reach the OAuth metadata and token
> endpoints without a Google-signed identity token. The gateway enforces its own auth.

---

## Security notes

- **PKCE S256 is mandatory.** Requests without `code_challenge` / `code_challenge_method=S256`
  are rejected with HTTP 400.
- Authorization codes are single-use and expire after **5 minutes**.
- Gateway JWTs expire after **30 minutes**.
- `redirect_uri` is validated at the token endpoint: the value must match the one sent in the
  original authorization request (per RFC 6749 §4.1.3). External OAuth clients may use their
  own callback URLs.
- The `TEST_USERS` allowlist is enforced after successful Google login – a valid Google
  account that is not in the list receives HTTP 403.
