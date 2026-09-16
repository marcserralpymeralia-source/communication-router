# KIBAK Pilot Cloud Deployment

Runbook for a fresh, client-owned pilot. It does not use GEMAVI/ANCHI
resources and does not migrate local credentials or operational data.

## 1. Create client resources

Create, in the client's accounts:

1. Render project/environment for staging.
2. Neon project and protected staging branch.
3. `kibak_master` database.
4. One PostgreSQL database per tenant, starting with `kibak_tenant_pilot`.
5. Private S3-compatible bucket with versioning/retention.
6. Google Cloud project and OAuth Web client when Gmail is ready.
7. Pilot domain after the generated Render URL has been validated.

Use `sslmode=require` in Neon connection strings. Use pooled endpoints for
normal web/worker traffic and direct endpoints for migrations/provisioning when
the operation requires a session-level connection.

## 2. Generate secrets

Generate unique values in the client's secret manager for `SECRET_KEY`,
`AUTH_SECRET` and `ENCRYPTION_KEY`. Keep a recovery copy of the encryption key.
Do not place them in this repository, shell history, tickets or chat.

## 3. Configure staging

Set `APP_ENV=staging`, `APP_SLUG=kibak`, an HTTPS `APP_URL`, explicit
`ALLOWED_HOSTS` and explicit `CORS_ALLOWED_ORIGINS`. Set
`RUN_WORKERS_IN_WEB=false` and use the private S3-compatible storage settings.
Set the same `RELEASE_SHA`, master URL, encryption key and job settings on web
and worker. Do not set `OPENAI_API_KEY` globally when tenant-scoped settings are
the source of truth.

## 4. Deploy services

Create `kibak-pilot-web` and `kibak-pilot-worker` from the same commit. Use the
Docker commands from `deploy/render/README.md`. Configure `/health/live` as the
web health path. The worker has no public port.

Before accepting traffic, verify:

```text
GET /health/live
GET /health/ready
GET /health/tenant (with a tenant-admin session)
```

## 5. Migrate and provision

Back up master and tenant databases first. Run only the KIBAK migration command
appropriate for the release; never run the legacy schema baseline command.
Verify the `kibak.master.*` and `kibak.tenant.*` ledgers. Then run from the
release image, interactively:

```bash
cd /app/backend
python scripts/provision_kibak_staging_tenant.py
```

The script asks for tenant name, slug, admin email and password via `getpass`.
The tenant database must already exist in `TENANT_DATABASE_URL`. It requires
typing `KIBAK_STAGING_TENANT_PROVISION`, is idempotent, rejects collisions and
never accepts passwords on the command line.

## 6. Pilot defaults

Every newly provisioned tenant starts with simulation enabled, forwarding and
SMTP disabled, no active mailbox, no auto-sync and no mark-as-read. Configure
OpenAI manually in the tenant UI only after the safety policy is verified.

## 7. Google OAuth

After a stable HTTPS domain exists, register exactly:

```text
https://<client-domain>/settings/mailboxes/oauth/google/callback
```

Set the same value in `GOOGLE_OAUTH_REDIRECT_URI`. The application uses
`APP_URL` for staging/production callback generation, avoiding internal proxy
HTTP and localhost URLs. Configure the consent screen, OAuth Web client,
authorized redirect URI and allowed test users in the client's Google Cloud
project. Do not run OAuth as part of this deployment runbook.

## 8. Client handoff and ownership

The client owns Render, Neon, object storage, Google Cloud, OpenAI billing and
the domain. Developer access is delegated, least-privilege and revocable.

## 9. Not included

No automatic IMAP connection, sync, backfill, SMTP, forwarding, OpenAI call,
DNS change, cloud API call or migration of local secrets/data is performed by
this runbook.
