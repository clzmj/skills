# Installer deployment

The repository is intended for public source distribution after its publication
checks pass. Repository visibility is a separate operator action; it is not
changed by deployment. The release archive is public through
`https://skills.clzmj.dev`; it contains only the installer payload selected by
`scripts/build-release.ts`. Terraform state, Git history, secrets, and CI files are
never release inputs.

## Source publication checks

Before changing repository visibility, refresh branches and tags and review all
reachable history, the current tree, and the release allowlist:

```sh
git fetch --all --prune
git ls-remote --heads --tags origin
python3 scripts/public-readiness.py
python3 scripts/public-readiness.py --hosted
```

The read-only script requires Gitleaks, scans all locally reachable history and
the current unignored source tree, inventories credential/state/artifact paths,
and prints only finding metadata. The optional `--hosted` pass inventories
GitHub Actions artifacts/releases and scans available Actions logs in memory;
it requires authenticated `gh`. Neither pass prints matching values. Review any
matches before publishing; pattern scanning cannot prove every possible secret
is absent. Deleted remote refs, dangling objects, hosted logs/artifacts, issue
content, and repository settings require separate review. A clean release
archive does not make source history safe to publish.

Keep the production environment restricted to the `main` branch. Enable and
verify main-branch protection when the repository becomes public if the current
GitHub plan does not support it while private. Fork PR checks must remain free
of deployment credentials; maintainers who can change same-repository PR code
remain inside the CI credential trust boundary. Repository publication does not
publish Doppler configs, the private R2 bucket, or Terraform state.

## Environments and secrets

Doppler project `skills` is the source of truth. GitHub fetches secrets at runtime
using a separate read-only service token for each repository environment:

| Doppler config | GitHub environment | Purpose |
| --- | --- | --- |
| `dev` | None | Local development and explicitly authorized operations |
| `ci` | `ci` | Tests and read-only Terraform plans |
| `prd` | `production` | Release upload, Worker deployment, Terraform apply |

The only stored GitHub environment secret is `DOPPLER_TOKEN`. Its dedicated
identity is `github-clzmj-skills-ci-runtime` or
`github-clzmj-skills-prd-runtime`, with read access to exactly the corresponding
Doppler config. Keep Cloudflare and state credentials in Doppler only.

Restrict GitHub `production` deployment branches to `main`. Both the workflow and
script reject other GitHub refs. `scripts/with-doppler.ts` fetches the required
names from the fixed Doppler HTTPS API, validates project/config metadata,
registers GitHub log masks before starting the command, and injects the values
only into that child. It removes the service token from the child environment.
There are no secret files, runner environment/output files, CLI caches, fallback
credentials, or config syncs. A failed fetch stops the command. Update provider
credentials in Doppler; the next job fetches their current values.

| Doppler secret | `ci` | `prd` |
| --- | --- | --- |
| `CLOUDFLARE_API_TOKEN` | Read Cloudflare resources and R2 bucket metadata | Manage this Worker's script, R2 release objects/bucket, and custom domain |
| `CLOUDFLARE_ACCOUNT_ID` | Account selector | Same account selector |
| `CLOUDFLARE_ZONE_ID` | `clzmj.dev` zone selector | Same zone selector |
| `TF_STATE_BUCKET` | `clzmj-skills-tfstate` | `clzmj-skills-tfstate` |
| `TF_STATE_R2_ACCESS_KEY_ID` | Read only, scoped to state bucket | Read/write, scoped to state bucket |
| `TF_STATE_R2_SECRET_ACCESS_KEY` | Paired state secret | Paired state secret |

The deployment script maps the state pair to `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`, the identifiers to Terraform's `TF_VAR_cloudflare_*`
variables, and derives `AWS_ENDPOINT_URL_S3` from the account ID. The backend is fixed to dedicated bucket `clzmj-skills-tfstate` and key `skills/terraform.tfstate`, with native S3 locks. Both the script guard and Terraform configuration reject cross-project state-bucket reuse. All CI/production credentials must be newly created for this project and environment; never copy or reference another project's keys. Release uploads use Wrangler's
Cloudflare API token, not the Terraform state's S3 credentials.

Isolation means separate credentials, configs, and resources within the existing
Cloudflare account, not a separate account. The state keys are restricted to the
dedicated state bucket. Cloudflare's Worker script and R2 management permissions
on the provider tokens cover the selected account; naming those tokens makes
them independently auditable and revocable, but does not restrict those
management permissions to this Worker's name or one bucket.

## Checks and production

Pull requests run Python/Bun tests, build the public archive, and validate
Terraform without a backend. Same-repository PRs then use `ci` credentials for
`terraform plan -lock=false`. Fork PRs receive no plan credentials. Plans are not
applied or uploaded as workflow artifacts, and PRs never upload release objects.

A `main` push, or manual run selecting `main`, passes the same checks, then:

1. Builds `dist/releases` with the full commit SHA as version.
2. Plans/applies only `cloudflare_r2_bucket.releases` so the Worker binding exists.
3. Uploads `releases/SHA/skills.tar.gz` and its `.sha256` object to the private
   `clzmj-skills-releases` bucket.
4. Deploys `clzmj-skills` using `distribution/wrangler.jsonc` and its `RELEASES`
   binding. Wrangler owns the script; Terraform owns its custom domain.
5. Plans/applies all Terraform, routing `skills.clzmj.dev` to the existing Worker.
6. Replaces `latest.json` as one atomic R2 object write after all preceding steps
   succeed. A failure before this step leaves the previous release discoverable.

Production runs are serialized and never cancel an active upload/apply. Terraform
applies use saved local plans and state locking. No state or plan artifact is
published. Action dependencies are pinned to commit SHAs; package dependencies
are installed from the committed Bun lockfile. Only the secret-consuming steps
receive `DOPPLER_TOKEN`; dependency installation and public packaging do not.

The token is a standing credential: anyone able to steal it or modify a trusted
secret-consuming workflow can read its config. Keep repository write access
restricted, preserve production branch rules and same-repository PR guards, and
do not inject production secrets into PR jobs. Runtime reads depend on Doppler
availability and fail closed during an outage.

To rotate a runtime token, mint a replacement with read-only access to the same
config, pipe its value directly into the matching GitHub environment's
`DOPPLER_TOKEN`, verify a job, then revoke the previous identity. Revoke a leaked
token immediately and rotate provider credentials it could have exposed. Token
values must stay in process memory or stdin; record identity metadata only.

For an explicitly authorized local deployment, supply the same environment
contract through Doppler and select a full commit SHA:

```sh
doppler run --no-fallback --project skills --config ci -- bun scripts/deploy.ts plan
GITHUB_SHA="$(git rev-parse HEAD)" doppler run --no-fallback --project skills --config prd -- bun scripts/deploy.ts production
```

Run important local commands through the repository's visible Rex workflow.
Verify `https://skills.clzmj.dev/latest.json`, its archive checksum, and a hosted
installer run after deployment before claiming live delivery. Re-run the
production workflow run from a known-good main commit to restore a previous release;
retain older versioned objects for that purpose.

## Public endpoint protection and costs

Cloudflare standard DDoS protection applies to the custom domain. The Worker allowlists paths/methods, rejects query cache-busting before R2, rate-limits anonymous clients to 120 requests/60 seconds, and returns Retry-After on limits or protection outages. Immutable releases cache for one year; installer/manifest cache for 60 seconds. Native rate limits are approximate per location and use a prefixed IP key, so clients behind the same NAT share an allowance. Worker cache hits still count as Worker requests.

Normal installer usage is expected to fit the account-shared free allowances: Workers Free has 100,000 requests/day; R2 Standard includes 10 GB-month, 1 million Class A operations, and 10 million Class B operations monthly, with free egress. Existing account usage, plan status, and abuse can affect cost/availability; rate limiting is not a global billing cap. No plan upgrade is performed by these scripts. The operator CLI verified that the current account subscriptions report zero base price. R2 remains usage-based and allowances are account-shared, so this is not a guarantee of a zero bill under unlimited usage.

Sources: [Workers limits](https://developers.cloudflare.com/workers/platform/limits/), [R2 pricing](https://developers.cloudflare.com/r2/pricing/), [DDoS coverage](https://developers.cloudflare.com/ddos-protection/), [Rate Limiting API](https://developers.cloudflare.com/workers/runtime-apis/bindings/rate-limit/).

## Dedicated credential bootstrap

Use the authenticated **Cloudflare CLI `cf`**, not Wrangler authentication, for operator provisioning. The operator profile needs `account_api_tokens:create`; routine deployment never uses that operator identity. The factory captures command responses in process memory, stores new values directly through Doppler stdin, and never logs or writes plaintext key values.

```sh
cf auth whoami
doppler run --project skills --config dev -- bun scripts/provision-credentials.ts plan --operator-cli
doppler run --project skills --config dev -- bun scripts/provision-credentials.ts apply --operator-cli
```

The explicit `--operator-cli` option strips service-token auth variables from the child CLI and uses its named operator profile. The optional API-token bootstrap mode remains available for operators who supply their own temporary `CLOUDFLARE_BOOTSTRAP_API_TOKEN` in this project; it is not required when `cf` is already authenticated. No operator/bootstrap token is synced to GitHub.

Required leaf identities: `skills-ci-cloudflare` (read-only provider operations), `skills-prd-cloudflare` (required Worker/R2 writes and zone read), `skills-ci-state` (object reads only in `clzmj-skills-tfstate`), and `skills-prd-state` (object reads/writes only in that bucket). Any optional temporary factory token should be revoked after setup. API-token administration is a prerequisite enforced by Cloudflare; deployment credentials cannot grant themselves this permission. Existing dedicated tokens are preserved only when their identities and policies match the project manifest.

## Initial bootstrap audit and state migration

The first public release (`7d01868c9aad18a90097b4f0d61c9907c9d3b827`) was provisioned before the user clarified the global dedicated-credential rule. It used the reference deployment credential in place and wrote only `skills/terraform.tfstate` in the reference state bucket. No credentials were copied to `skills` or GitHub, and no environment was deleted. The four dedicated credentials have now been provisioned. The live release bucket and custom domain have been imported into `clzmj-skills-tfstate` with a no-change plan. Read-only CI planning also reports no changes. No ongoing deployment or backend operation uses the reference credentials.

After creating the dedicated backend, initialize it with `-reconfigure` and import the already-created release bucket and custom domain using the new project keys; do not read the reference credentials again. The custom-domain import identifier is `$CLOUDFLARE_ACCOUNT_ID/af5477ad04031752e1152de25fd41380401078c0`; the bucket import is `$CLOUDFLARE_ACCOUNT_ID/clzmj-skills-releases/default`. Require a full no-change plan before retiring the initial skills-only state object using a dedicated project identity. Never modify the site state object.

The approved retirement is complete: both snapshots contained the same two
managed skills resources, then only the temporary
`clzmj-dev-tfstate/skills/terraform.tfstate` object was deleted using the dedicated
skills production identity. The site's state object was not selected.

## Zero-sync distribution

Each runtime service token belongs to one project/config and one GitHub
repository environment. Tokens and provider credentials are never shared
between projects. CLI/API reads do not consume Doppler config-sync slots; the
workflow uses direct API reads and requires no paid plan upgrade.

The previous native syncs and their copied GitHub secrets are retired only after
the runtime workflows pass. The GitHub integration connection may remain
authorized without any config mappings; it is not used to distribute secrets.

Sources: [service-token scope](https://docs.doppler.com/docs/service-tokens),
[download API](https://docs.doppler.com/reference/secrets-download),
[sync limits](https://docs.doppler.com/docs/platform-limits).
