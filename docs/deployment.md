# Installer deployment

The repository stays private. The release archive is public through
`https://skills.clzmj.dev`; it contains only the installer payload selected by
`scripts/build-release.ts`. Terraform state, Git history, secrets, and CI files are
never release inputs.

## Environments and secrets

Doppler project `skills` is the source of truth. Configure its GitHub integration
for this private repository with these exact mappings:

| Doppler config | GitHub environment | Purpose |
| --- | --- | --- |
| `dev` | None | Local development and explicitly authorized operations |
| `ci` | `ci` | Tests and read-only Terraform plans |
| `prd` | `production` | Release upload, Worker deployment, Terraform apply |

Restrict GitHub `production` deployment branches to `main`. Both the workflow and
script reject other GitHub refs. No staging deployment or repository-level secret
fallback is configured. Update values in Doppler and verify its sync, rather than
editing the synced GitHub copies. Integration configuration and token policies
must be verified in the external services.

| GitHub environment secret | `ci` | `production` |
| --- | --- | --- |
| `CLOUDFLARE_API_TOKEN` | Read Cloudflare resources and R2 bucket metadata | Manage this Worker's script, R2 release objects/bucket, and custom domain |
| `CLOUDFLARE_ACCOUNT_ID` | Account selector | Same account selector |
| `CLOUDFLARE_ZONE_ID` | `clzmj.dev` zone selector | Same zone selector |
| `TF_STATE_BUCKET` | `clzmj-dev-tfstate` | `clzmj-dev-tfstate` |
| `TF_STATE_R2_ACCESS_KEY_ID` | Read only, scoped to state bucket | Read/write, scoped to state bucket |
| `TF_STATE_R2_SECRET_ACCESS_KEY` | Paired state secret | Paired state secret |

The deployment script maps the state pair to `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY`, the identifiers to Terraform's `TF_VAR_cloudflare_*`
variables, and derives `AWS_ENDPOINT_URL_S3` from the account ID. The backend bucket is supplied through `TF_STATE_BUCKET`; the current scoped credentials use the existing `clzmj-dev-tfstate` bucket with a separate `skills/terraform.tfstate` key and lockfile. The site state key is never selected. Dedicated `clzmj-skills-tfstate` credentials can be used later without changing the fixed state key. Release uploads use Wrangler's
Cloudflare API token, not the Terraform state's S3 credentials.

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
are installed from the committed Bun lockfile.

For an explicitly authorized local deployment, supply the same environment
contract through Doppler and select a full commit SHA:

```sh
doppler run --project skills --config ci -- bun scripts/deploy.ts plan
GITHUB_SHA="$(git rev-parse HEAD)" doppler run --project skills --config prd -- bun scripts/deploy.ts production
```

Run important local commands through the repository's visible Rex workflow.
Verify `https://skills.clzmj.dev/latest.json`, its archive checksum, and a hosted
installer run after deployment before claiming live delivery. Re-run the
production workflow run from a known-good main commit to restore a previous release;
retain older versioned objects for that purpose.

## Public endpoint protection and costs

Cloudflare standard DDoS protection applies to the custom domain. The Worker allowlists paths/methods, rejects query cache-busting before R2, rate-limits anonymous clients to 120 requests/60 seconds, and returns Retry-After on limits or protection outages. Immutable releases cache for one year; installer/manifest cache for 60 seconds. Native rate limits are approximate per location and use a prefixed IP key, so clients behind the same NAT share an allowance. Worker cache hits still count as Worker requests.

Normal installer usage is expected to fit the account-shared free allowances: Workers Free has 100,000 requests/day; R2 Standard includes 10 GB-month, 1 million Class A operations, and 10 million Class B operations monthly, with free egress. Existing account usage, plan status, and abuse can affect cost/availability; rate limiting is not a global billing cap. No plan upgrade is performed by these scripts. Billing-plan reads were not permitted by the existing deployment token; verify the account plan in the owner dashboard.

Sources: [Workers limits](https://developers.cloudflare.com/workers/platform/limits/), [R2 pricing](https://developers.cloudflare.com/r2/pricing/), [DDoS coverage](https://developers.cloudflare.com/ddos-protection/), [Rate Limiting API](https://developers.cloudflare.com/workers/runtime-apis/bindings/rate-limit/).
