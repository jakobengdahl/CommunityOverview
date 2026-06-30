# Agent Scheduling

AI agents can be triggered in two complementary ways:

1. **Event subscriptions** — react to graph mutations (node create/update/delete)
2. **Time-based schedules** — fire on a recurring day and time (this document)

Both trigger paths produce an event payload that the agent processes using the same MCP tools.

## Overview

Each agent can have at most one schedule: a day of the week plus a time of day, with an optional timezone.

Scheduling is designed to work with scale-to-zero hosting (e.g. GCP Cloud Run). The in-process scheduler is opt-in and off by default. When disabled, an external scheduler such as GCP Cloud Scheduler calls the `POST /agents/{id}/trigger` HTTP endpoint to fire agents on demand, and the app instance starts only when triggered.

```
                     ┌─────────────────────────────┐
                     │  External scheduler          │
                     │  (e.g. GCP Cloud Scheduler)  │
                     └───────────┬─────────────────┘
                                 │ POST /agents/{id}/trigger
                     ┌───────────▼─────────────────┐
                     │  GET /agents/schedules        │◄── SaaS/infra layer reads
                     │  POST /agents/{id}/trigger    │    and reconciles jobs
                     │                               │
                     │  AgentRegistry                │
                     │    AgentScheduler (optional)  │
                     └───────────┬─────────────────┘
                                 │ enqueue
                     ┌───────────▼─────────────────┐
                     │  AgentWorker                 │
                     │  (processes payload via LLM) │
                     └─────────────────────────────┘
```

## Configuration

### Agent schedule

Add a `schedule` block to the Agent node's `metadata` field:

```json
{
  "schedule": {
    "day_of_week": "tuesday",
    "time": "14:00",
    "timezone": "Europe/Stockholm"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `day_of_week` | string or int | Day name (`"monday"` … `"sunday"`, case-insensitive) or integer (0 = Monday … 6 = Sunday) |
| `time` | string | Time in `HH:MM` format |
| `hour` / `minute` | int | Alternative to `time`; provide both |
| `timezone` | string | IANA timezone (e.g. `"Europe/Stockholm"`). Defaults to `"UTC"` |

Invalid values (unknown timezone, day out of range, etc.) cause the schedule field to be ignored — the agent still runs but will not be scheduled.

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTS_SCHEDULER_ENABLED` | `false` | Enable the in-process background scheduler |

All other agent env vars (`AGENTS_ENABLED`, `AGENTS_LLM_PROVIDER`, etc.) are unchanged.

## In-process scheduler

When `AGENTS_SCHEDULER_ENABLED=true`, the app runs a background daemon thread that wakes every 30 seconds and fires any agent whose schedule matches the current wall-clock time.

Deduplication: an agent fires at most once per calendar minute (based on the schedule's timezone). After firing, it will fire again the following week.

**Use this mode for development or single-instance deployments** where always-on processes are acceptable.

```bash
AGENTS_ENABLED=true
AGENTS_SCHEDULER_ENABLED=true
AGENTS_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

## External scheduler (recommended for scale-to-zero)

When `AGENTS_SCHEDULER_ENABLED=false` (the default), no background thread runs. An external scheduler fires agents by calling:

```
POST /agents/{agent_id}/trigger
```

This endpoint enqueues a `scheduled_trigger` event for the agent and returns immediately.

### GCP Cloud Scheduler setup

1. Deploy the app to Cloud Run.
2. Read the schedule contract from the app:
   ```bash
   curl https://your-app/agents/schedules
   ```
3. Create a Cloud Scheduler job per agent using the returned `cron` and `trigger_path`.
4. Authenticate with OIDC using a service account that has `roles/run.invoker`.

Example Cloud Scheduler job (via gcloud):
```bash
gcloud scheduler jobs create http my-agent-job \
  --schedule="0 14 * * 2" \
  --uri="https://your-app/agents/agent-abc123/trigger" \
  --http-method=POST \
  --oidc-service-account-email=scheduler@my-project.iam.gserviceaccount.com \
  --location=europe-west1
```

### Schedule export API

`GET /agents/schedules` returns the schedule for every active agent that has one configured. Intended for infrastructure layers that need to reconcile external scheduler jobs.

**Response:**
```json
[
  {
    "agent_id": "agent-abc123",
    "agent_name": "Weekly Status Report",
    "trigger_path": "/agents/agent-abc123/trigger",
    "schedule": {
      "day_of_week": 1,
      "day_name": "Tuesday",
      "hour": 14,
      "minute": 0,
      "timezone": "Europe/Stockholm",
      "cron": "0 14 * * 2"
    }
  }
]
```

Note: `cron` uses the standard 5-field format with `0 = Sunday` (not Monday as in Python's `weekday()`).

## Trigger endpoint

`POST /agents/{agent_id}/trigger` fires an agent immediately, regardless of schedule.

- Returns `200 OK` when the event is enqueued.
- Returns `404` when no agent with that ID exists, or when the agent has no schedule configured.

No request body is required. Authentication is handled at the infrastructure level (OIDC for Cloud Run; no app-level auth token needed).

## Trigger payload

Regardless of whether the agent is fired by the in-process scheduler or the HTTP trigger endpoint, the agent receives the same payload shape:

```json
{
  "event_id": "sched-550e8400-e29b-41d4-a716-446655440000",
  "event_type": "scheduled_trigger",
  "occurred_at": "2026-06-29T14:00:00Z",
  "origin": {
    "event_origin": "scheduler",
    "event_session_id": null,
    "event_correlation_id": null
  },
  "schedule": {
    "day_of_week": 1,
    "day_name": "Tuesday",
    "hour": 14,
    "minute": 0,
    "timezone": "Europe/Stockholm",
    "cron": "0 14 * * 2"
  },
  "entity": null,
  "subscription": null
}
```

Key differences from event-subscription payloads:
- `event_type` is `"scheduled_trigger"` (not `"node.create"` etc.)
- `origin.event_origin` is `"scheduler"`
- `entity` and `subscription` are `null`
- `schedule` is populated with the agent's configured schedule

## Cron day-of-week mapping

Python's `weekday()` uses Monday = 0. Standard cron uses Sunday = 0. The conversion applied when producing the `cron` field is:

```
cron_dow = (python_dow + 1) % 7
```

| Day | Python | Cron |
|-----|--------|------|
| Monday | 0 | 1 |
| Tuesday | 1 | 2 |
| Wednesday | 2 | 3 |
| Thursday | 3 | 4 |
| Friday | 4 | 5 |
| Saturday | 5 | 6 |
| Sunday | 6 | 0 |

## Plugin / SaaS integration

The open-core app stores and exposes schedules but does not manage external infrastructure. A SaaS layer or plugin can:

1. Call `GET /agents/schedules` to read the current schedule contract.
2. Reconcile GCP Cloud Scheduler jobs (create/update/delete) to match.
3. Each job posts to `POST /agents/{id}/trigger` when its cron fires.

This keeps scheduling configuration in the UI (the user never touches Cloud Scheduler directly) while the SaaS layer transparently manages the infrastructure.
