# Quibac Pilot Cloud Migration

The cloud pilot is a fresh KIBAK environment. Do not copy local secrets,
passwords, OAuth tokens, IMAP credentials, SMTP credentials, Communications,
RoutingDecisions or PromptExecutions.

Transfer only after review:

- department names and descriptions;
- semantic knowledge, exclusions and examples;
- RACI assignments;
- an evaluation set, if approved and scrubbed.

Create the tenant and administrator interactively in the client environment,
then configure OpenAI and Google OAuth manually. Keep the mailbox disabled until
the readonly connection test and safety policy are explicitly approved. Perform
backfill only as a separate, controlled change with a backup and a documented
rollback.
