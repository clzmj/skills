# Credential isolation

Use dedicated, named credentials for every project and environment. Never copy,
reuse, or reference another project's deployment tokens or storage keys. Names
must identify the project, environment, and purpose so audit logs support incident
investigation. Apply least privilege and keep CI read-only and production writes
separate. Human operator login may provision the dedicated credentials; routine
deployment must use the resulting project-specific credentials.

Doppler is the secret source of truth. Keep values out of Git, release packages,
Terraform configuration/state, command arguments, and logs. Configure GitHub
environment secrets as one dedicated read-only `DOPPLER_TOKEN` per config. Fetch
provider secrets at runtime through `scripts/with-doppler.ts`, with production
restricted to main. Use zero config syncs and no shared project credentials.
The public installer package must never contain credentials or infrastructure
state.
