# KIBAK Pilot Recovery

## Database

1. Stop worker and web writes.
2. Identify the exact release and tenant database.
3. Restore Neon PITR or the latest verified logical backup into an isolated
   recovery target.
4. Validate KIBAK ledger, tenant isolation and safety policy.
5. Promote only after a read-only smoke test.

Back up `kibak_master` and every tenant independently before migrations. A
schema rollback is not implied by an application rollback; migrations must be
forward-compatible or restored from backup.

## Application rollback

Redeploy the previous verified release to both web and worker. Keep
`RELEASE_SHA` identical. Do not run a destructive schema command during an
application-only rollback.

## Storage

Restore objects from bucket versioning/retention. Object keys are tenant-scoped;
do not copy objects between tenants without an explicit migration.

## Encryption key loss

Loss of `ENCRYPTION_KEY` makes encrypted tenant database URLs, mailbox
credentials, OAuth refresh tokens and LLM credentials unreadable. Recover the
original key from the client's secret manager; do not generate a replacement
and expect old ciphertext to work.

## Credential recovery

Rotate OpenAI keys in the tenant UI, reconnect Google OAuth if refresh tokens
were revoked, and re-enter mailbox credentials. Never recover or print old
plaintext secrets.
