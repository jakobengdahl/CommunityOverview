# Migration Notes – Deployment Model Refactor

This document records what changed when deployment orchestration was moved from
this repository to the infra repository. Use it to update the infra repo and to
audit any backward-compatibility concerns.

---

## What Changed

### Workflows removed

| File | Reason |
|------|--------|
| `.github/workflows/deploy-dev.yml` | Contained pilot-specific Cloud Run deployment logic that now belongs in infra |
| `.github/workflows/deploy-prod.yml` | Same as above for the prod environment |

These workflows built application images **and** deployed them to all pilots in
a single job. That coupling meant this repo was both an artifact publisher and a
deployment control plane.

### Workflows updated

| File | Change |
|------|--------|
| `.github/workflows/ci.yml` | Now builds and publishes **both** images (`core` + `gateway`) on pushes to `dev` and `main`. Added `notify-infra` job. Trigger coverage extended to include `dev` branch. |
| `.github/workflows/deploy.yml` | Replaced commented-out cloud templates with a clear explanation of the new model and optional platform stubs. |

### New documentation

| File | Purpose |
|------|---------|
| `docs/DEPLOYMENT_CONTRACT.md` | Defines the artifact interface between this repo and the infra repo |
| `docs/MIGRATION_NOTES.md` | This file |

---

## Deployment Logic Removed from This Repo

The following responsibilities no longer live here:

- **Pilot enumeration** – `pilots.txt` was read by `deploy-dev.yml` and
  `deploy-prod.yml` to loop over `esam`, `unece`, `scb`. Pilot configuration
  must now live exclusively in the infra repo.

- **Cloud Run `gcloud run deploy` calls** – all `gcloud run deploy` invocations
  for core and gateway services across all pilots.

- **GCS volume mounting** – `--add-volume` / `--add-volume-mount` flags for
  per-pilot Cloud Storage buckets.

- **Per-pilot Secret Manager lookups** – conditional `gcloud secrets describe`
  checks and `--set-secrets` flags for `OPENAI_API_KEY_<PILOT>` and
  `ANTHROPIC_API_KEY_<PILOT>`.

- **Gateway URL resolution** – `gcloud secrets versions access` calls for
  `GW_<PILOT>_<ENV>_UPSTREAM_URL` and `GW_<PILOT>_<ENV>_PUBLIC_BASE_URL`.

- **GCP authentication in deploy context** – Workload Identity Federation setup
  (`google-github-actions/auth`) and `gcloud` SDK installation are no longer
  needed in this repo.

---

## What the Infra Repo Now Needs to Satisfy

The infra repo must:

1. **Store the pilot list.** Replace `pilots.txt` in this repo with an
   authoritative pilot configuration (e.g. a YAML manifest or Terraform
   variable) that lists `esam`, `unece`, `scb` and any future pilots.

2. **Listen for `app-release` dispatch events.** Configure a workflow trigger:
   ```yaml
   on:
     repository_dispatch:
       types: [app-release]
   ```

3. **Consume the artifact payload.** See `docs/DEPLOYMENT_CONTRACT.md` for
   the full field reference. At minimum the infra workflow needs:
   - `github.event.client_payload.core_image` + `core_digest`
   - `github.event.client_payload.gateway_image` + `gateway_digest`
   - `github.event.client_payload.channel` (to select environment)
   - `github.event.client_payload.image_tag` (for logging / labelling)

4. **Re-implement the Cloud Run deploy loop.** The logic previously in
   `deploy-dev.yml` and `deploy-prod.yml` (minus the image build steps) should
   move to the infra repo. The deploy steps themselves are largely reusable –
   the `deploy_core()` and `deploy_gateway()` shell functions can be copied
   and adapted.

5. **Manage GCP secrets and IAM.** Continue using Workload Identity Federation.
   The secrets `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT_CORE`,
   `GCP_SERVICE_ACCOUNT_GW`, `GCP_PROJECT_ID`, `GCP_REGION`, and `GCP_AR_REPO`
   should move to the infra repo's secret store. They are no longer required
   in this repo.

---

## Backward Compatibility Notes

- **Image registry location changes.** The removed deploy workflows pushed
  images to GCP Artifact Registry
  (`<region>-docker.pkg.dev/<project>/<repo>/core:<sha>`). The new CI workflow
  pushes to GHCR (`ghcr.io/<owner>/communityoverview:sha-<sha>`). The infra
  repo must be updated to pull from GHCR, **or** an infra-side workflow can
  re-tag and push to Artifact Registry before deploying to Cloud Run.

- **`pilots.txt` is now unused in this repo.** It remains in the repository for
  reference but is no longer read by any workflow. It should be removed once the
  infra repo has its own pilot list.

- **The `dev` branch now triggers a build.** Previously, pushing to `dev`
  triggered `deploy-dev.yml` (build + deploy). Now it triggers only the build
  and publish steps in `ci.yml`. Deployment must be initiated from the infra
  repo in response to the `app-release` dispatch event.

- **No GCP credentials needed here.** Remove the following secrets from this
  repo once infra deployment is confirmed to work end-to-end:
  - `GCP_WORKLOAD_IDENTITY_PROVIDER`
  - `GCP_SERVICE_ACCOUNT_CORE`
  - `GCP_SERVICE_ACCOUNT_GW`
  - `GCP_PROJECT_ID`
  - `GCP_REGION`
  - `GCP_AR_REPO`
  - `GOOGLE_OAUTH_CLIENT_ID`
  - `TEST_USERS`

---

## How a Branch Merge Now Flows

```
Developer pushes to dev
         │
         ▼
CI: test job runs (pytest)
         │
         ▼
CI: build job (on success)
  – builds core image → ghcr.io/<owner>/communityoverview:sha-<sha>
  – builds gateway image → ghcr.io/<owner>/communityoverview-gateway:sha-<sha>
  – pushes floating tag: dev
         │
         ▼
CI: notify-infra job (if INFRA_DISPATCH_TOKEN configured)
  – sends repository_dispatch app-release to infra repo
  – payload includes image digests, channel=dev, commit SHA
         │
         ▼
Infra repo: deployment workflow triggers
  – reads pilot list from infra config
  – pulls images by digest
  – runs gcloud run deploy for each pilot
  – manages secrets, volumes, scaling
```

For `main` merges, the same flow applies with `channel=prod` and the floating
`latest` tag.
