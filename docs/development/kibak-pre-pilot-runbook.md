# KIBAK Pre-Pilot Runbook

## Local stack

Run only the independent KIBAK stack from `COMM_ROUTER`:

```bash
docker compose up -d postgres web worker
docker compose ps
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

The local PostgreSQL service listens on host port `5433`. It uses the
`kibak_master` and `kibak_tenant_test` databases, the `kibak_postgres_data`
volume, and the `kibak_local_network` network. Do not point this stack at an
ANCHI/GEMAVI resource.

## Synthetic volume

The generator is deterministic and restricted to a demo/test KIBAK company.
It never contacts IMAP, SMTP, Meta, OpenAI, or another external provider.

```bash
cd backend
python scripts/generate_kibak_load.py --count 100 --company-id 1 --batch prepilot100
python scripts/generate_kibak_load.py --count 500 --company-id 1 --batch prepilot500
python scripts/generate_kibak_load.py --count 1000 --company-id 1 --batch prepilot1000
```

The supported counts are `100`, `500`, and `1000`. Repeating the same batch is
safe and reports existing rows as skipped. Resetting requires an explicit
confirmation token:

```bash
python scripts/generate_kibak_load.py --reset --company-id 1 --batch prepilot100 --confirm-reset KIBAK_LOAD_RESET
```

## Operational checks

- `health/live` confirms the process is responding and exposes only aggregate
  metrics.
- `health/ready` checks database connectivity, the migration contract,
  writable temporary storage, and the jobs worker heartbeat.
- Jobs are claimed atomically, retried according to the configured limits, and
  stale running jobs are recoverable after `JOB_STALE_AFTER_SECONDS`.
- Workbench, History, Operations, Jobs, and Evaluation Lab lists are paginated;
  the Evaluation Lab keeps threshold simulation in SQL aggregates instead of
  loading every stored result into the page.
- A missing worker heartbeat is a warning when there are no pending jobs and a
  readiness failure when queued work requires processing.
- KIBAK uses the isolated route surface by default when `APP_SLUG=kibak`.
  Keep `KIBAK_ISOLATED_ROUTES=true` in deployments. The explicit `false` opt-out
  is reserved for compatibility/test environments and must not be used in a
  normal KIBAK deployment.

## Pilot exit criteria

Before using a controlled mailbox, verify the target tenant, mailbox, and
storage are KIBAK-owned; run the focused suite and the PostgreSQL smoke test;
confirm that no secrets or message bodies appear in logs; and keep automatic
routing and forwarding disabled until their provider and retry policies have
been explicitly reviewed.
