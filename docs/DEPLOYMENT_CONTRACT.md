# Deployment Contract

This document defines the interface between this repository (the application
repo) and the infra repository. It is the single source of truth for what this
repo produces and what the infra repo must consume.

---

## Responsibilities

| Concern | Owner |
|---------|-------|
| Build the application | **This repo** |
| Run tests | **This repo** |
| Publish versioned container images | **This repo** |
| Signal infra after a successful build | **This repo** |
| Decide which pilots exist | **Infra repo** |
| Configure Cloud Run services per pilot | **Infra repo** |
| Manage GCS volumes and per-pilot secrets | **Infra repo** |
| Orchestrate rollouts across environments | **Infra repo** |

---

## Images Published

Both images are published to GitHub Container Registry (GHCR) on every
successful push to `preview` or `prod` (and on version tags). `main` is the
integration branch: pushes to it run the tests but publish no image.

| Image | Registry path |
|-------|---------------|
| Core application | `ghcr.io/<owner>/communityoverview` |
| MCP OAuth Gateway | `ghcr.io/<owner>/communityoverview-gateway` |

### Tags

| Tag | Description |
|-----|-------------|
| `sha-<commit>` | Immutable. Canonical reference for a specific build. |
| `dev` | Floating. Latest successful build from the `preview` branch. |
| `latest` | Floating. Latest successful build from the `prod` branch. |
| `v<semver>` | Applied when a version tag (`v*`) is pushed. |

Infra deployments **must** reference the immutable `sha-<commit>` tag (or the
image digest) to guarantee reproducible rollouts. Floating tags (`dev`,
`latest`) may be used for monitoring but not for production deployments.

---

## Release SBOM Evidence

The image build job scans both published images by their immutable digest with
Syft and produces SPDX 2.3 JSON and CycloneDX 1.6 JSON for each image. The core
reports cover installed Python dependencies (including the default ML extras),
OS packages, and the production npm dependency tree for the web and widget
bundles. The gateway reports cover its installed Python dependencies and OS
packages. The XR workspace is not shipped by this workflow.

The core image retains npm `package.json` metadata under
`/usr/share/communityoverview/frontend-dependencies`, copied from the installed
production dependency tree in the same builder stage as the bundles. Module
code and development-only packages are not copied. This lets Syft identify
frontend package versions despite bundling/minification. Coverage is conservative:
production dependencies can include code removed by tree shaking.

Every successful image build retains one GitHub Actions artifact named
`release-evidence-<commit_sha>-<run_id>-<run_attempt>` for 90 days (subject to the
repository retention limit). The build summary links to it. It contains:

- `core-sha-<commit_sha>.spdx.json` and `.cyclonedx.json`;
- `gateway-sha-<commit_sha>.spdx.json` and `.cyclonedx.json`;
- `release.json`, binding the full commit SHA, source ref/version tag, immutable
  `sha-<commit_sha>` version, workflow run/attempt, and both image digest references;
- `SHA256SUMS`, covering all JSON evidence files.

Each SBOM also identifies its digest-pinned image and full source commit. A scan
or evidence upload failure fails the build job and prevents infra notification.
Images may already have been pushed when a scan fails. Evidence stays with the
existing CI run; this workflow does not create GitHub Releases, publish SBOMs to
another service, sign, or attest them.

---

## Release Channels

| Branch | Channel |
|--------|---------|
| `preview` | `preview` |
| `prod` (or version tag) | `prod` |

`main` is the integration branch and maps to no channel — it does not deploy.

---

## `repository_dispatch` Payload

When `INFRA_DISPATCH_TOKEN` and `INFRA_REPO` are configured as repository
secrets, CI sends a `repository_dispatch` event of type `app-release` to the
infra repo after a successful build. The payload is:

```json
{
  "channel":        "preview | prod",
  "commit_sha":     "<full 40-char git SHA>",
  "ref":            "refs/heads/preview | refs/heads/prod | refs/tags/v1.2.3",
  "image_tag":      "sha-<commit>",
  "core_image":     "ghcr.io/<owner>/communityoverview",
  "core_digest":    "sha256:<digest>",
  "gateway_image":  "ghcr.io/<owner>/communityoverview-gateway",
  "gateway_digest": "sha256:<digest>",
  "repository":     "<owner>/communityoverview",
  "run_id":         "<GitHub Actions run ID>"
}
```

### Field reference

| Field | Type | Description |
|-------|------|-------------|
| `channel` | string | Release channel: `preview` or `prod` |
| `commit_sha` | string | Full 40-character git SHA of the build |
| `ref` | string | Git ref that triggered the build |
| `image_tag` | string | Immutable image tag (`sha-<commit>`) to deploy |
| `core_image` | string | GHCR path of the core application image (no tag) |
| `core_digest` | string | Content-addressable digest of the core image |
| `gateway_image` | string | GHCR path of the gateway image (no tag) |
| `gateway_digest` | string | Content-addressable digest of the gateway image |
| `repository` | string | Source repository (`<owner>/<repo>`) |
| `run_id` | string | GitHub Actions run ID (for audit/traceability) |

To pull a specific image by digest the infra repo should use:

```
ghcr.io/<owner>/communityoverview@sha256:<digest>
```

---

## Enabling Infra Notifications

Add the following secrets to this repository:

| Secret | Description |
|--------|-------------|
| `INFRA_DISPATCH_TOKEN` | GitHub PAT with `repo:write` scope on the infra repo |
| `INFRA_REPO` | Infra repository in `owner/repo` format |

If these secrets are not set the `notify-infra` CI job exits cleanly with a
skip message – no other CI behaviour is affected.

---

## Pull Policy for the Infra Repo

The infra repo should:

1. Listen for `repository_dispatch` events with `event_type == 'app-release'`.
2. Extract `core_image`, `core_digest`, `gateway_image`, `gateway_digest`, and
   `channel` from `github.event.client_payload`.
3. Use the digest-pinned image reference for Cloud Run deployments to guarantee
   immutability.
4. Map `channel` to its target environment(s) and pilot list.
5. Perform any environment-specific configuration (secrets, volumes, scaling)
   independently of this repo.

---

## Local Verification

To verify the images locally after a build:

```bash
# Pull the core image by SHA tag
docker pull ghcr.io/<owner>/communityoverview:sha-<commit>

# Run the core image locally
docker run --rm -p 8000:8000 \
  -e ANTHROPIC_API_KEY=<key> \
  ghcr.io/<owner>/communityoverview:sha-<commit>

# Pull the gateway image
docker pull ghcr.io/<owner>/communityoverview-gateway:sha-<commit>
```

Images are publicly readable from GHCR once the package visibility is set to
public. If the package is private, authenticate first:

```bash
echo $GITHUB_TOKEN | docker login ghcr.io -u <github-username> --password-stdin
```
