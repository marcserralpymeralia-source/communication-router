# KIBAK Free Pilot Runtime

This is an explicit staging profile for a controlled pilot. It uses one Render
Free Web Service, Neon Free and a private Cloudflare R2 bucket. It is not a
production topology and it never changes GEMAVI/ANCHI resources.

## Runtime

Set `APP_ENV=staging`, `APP_SLUG=kibak`, `DEPLOYMENT_MODE=free_pilot` and
`PILOT_FREE_MODE=true`. The web process must run with
`RUN_WORKERS_IN_WEB=true`. The existing jobs worker is started once inside the
web process with one job per cycle. The continuous email listener is not
started, so the service does not poll IMAP 24/7 and no keep-alive or health-ping
job is used. Render sleep and cold starts are expected.

The standard profile remains unchanged: deploy the web service and the
background worker with `DEPLOYMENT_MODE=standard` and
`RUN_WORKERS_IN_WEB=false`. Both profiles use the same `Procfile`, Docker image
and release SHA; only the topology and safety mode differ.

## Safety defaults

Free pilot runtime forces the effective tenant routing policy to simulation,
forces automatic forwarding off and prevents SMTP configuration/tests. Newly
created mailboxes remain disabled, with auto-sync and mark-as-read disabled.
The administrator can enable a mailbox only for a deliberate manual window.
The UI labels the runtime as `Entorno piloto` and explains that continuous
automatic processing is inactive.

Manual email actions reuse the existing job and IMAP pipeline. A free pilot
run defaults to 10 messages and is capped at 20 messages, including recent
read, backfill and mailbox operations. Processing is sequential and the
persistent `BackgroundJob` record, dedupe key, retry state and tenant-scoped
communication uniqueness remain the source of truth across restarts.

## Data services

Use one isolated Neon project with `kibak_master` and the pilot tenant database
`kibak_tenant_quibac`. Use pooled URLs for web traffic and direct/session URLs
for migrations or provisioning, always with `sslmode=require`. Run only the
KIBAK migration path and verify both ledgers before traffic.

Use `STORAGE_BACKEND=s3` with a private R2 bucket and the existing
tenant-scoped storage adapter. Communications, mailbox attachments and tenant
branding are durable in PostgreSQL plus R2. Import previews are temporary and
safe to lose; they are cleaned after 24 hours. No critical state is kept in
the Render filesystem.

## Render setup

Create only `kibak-pilot-web` as a Docker Web Service for this profile. Use the
same repository branch/release SHA as the approved pilot release and the
variables in `deploy/render/free-pilot.env.example`. Use the Docker command:

```text
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Set the health check to `/health/live`. The service's generated HTTPS URL is
the initial public URL. After it is stable, set `APP_URL` and
`GOOGLE_OAUTH_REDIRECT_URI` to that exact origin plus
`/settings/mailboxes/oauth/google/callback`. Never use localhost in staging.

## Pilot workflow

1. Run KIBAK migrations after a Neon backup.
2. Run `python scripts/provision_kibak_staging_tenant.py` interactively.
3. Verify readiness, tenant isolation and safe policy.
4. Configure OpenAI and Google OAuth in the client-owned secret/configuration
   stores, never in Git or chat.
5. Keep the mailbox disabled until a deliberate manual test window.
6. Enable it, run the existing manual processing action in a bounded batch,
   review Communications and routing decisions, then disable it again.

There is no automatic 24/7 polling in this profile. A restart-safe job can be
retried manually from its persisted state; duplicate Communications are
rejected by the existing tenant-scoped uniqueness contract.

## Upgrade path

Moving to the standard pilot/production topology requires configuring the
existing Render Background Worker with the same release SHA, setting
`DEPLOYMENT_MODE=standard` and `RUN_WORKERS_IN_WEB=false`, and validating the
standard readiness checks. It does not require a functional rewrite.

## Limitations and stop conditions

Render Free may sleep and has limited CPU/RAM. Do not run Evaluation Sets,
large backfills, continuous polling or concurrent jobs here. Do not enable
SMTP or automatic forwarding. External Render, Neon, R2, Google Cloud, DNS
and provider credentials are provisioning steps owned by the pilot operator;
this document creates none of them.
