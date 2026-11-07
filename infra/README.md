# Infrastructure

Terraform owns the private release bucket and `skills.clzmj.dev` custom domain. Wrangler owns the
`clzmj-skills` Worker script and its `RELEASES` R2 binding. The Worker exposes only the public
installer/package routes; neither bucket has a public domain or `r2.dev` access configured here.
This follows the ownership and Doppler environment pattern in `clzmj.dev` without using its Terraform state file.

| Resource | Name / address | Owner |
| --- | --- | --- |
| Worker | `clzmj-skills` | Wrangler |
| Custom domain | `skills.clzmj.dev`, `cloudflare_workers_custom_domain.skills` | Terraform; creates routing and DNS |
| Private releases | `clzmj-skills-releases`, `cloudflare_r2_bucket.releases` | Terraform; `prevent_destroy` |
| Private Terraform state | `clzmj-dev-tfstate`, key `skills/terraform.tfstate` | Existing backend bucket; isolated object key |
| Existing zone | `clzmj.dev` | Read-only data source; verifies the zone name |

The backend uses native S3 lockfiles. Production operations need state read/write/delete access
including `skills/terraform.tfstate.tflock`. The current credential set is bucket-scoped to the existing private state bucket. The skills state and lock use their own object keys; the site state key is never selected. A dedicated backend bucket can be supplied when dedicated credentials are available.
No token resource is managed by Terraform, so token values do not enter this state.

## Credentials

Doppler project `skills` is the source of truth. Sync `ci` to GitHub environment `ci` and `prd`
to `production`; production is restricted to `main`. Verify those external sync mappings when
provisioning. No repository-level secrets are needed.

| Name | Use / scope |
| --- | --- |
| `CLOUDFLARE_ACCOUNT_ID` | Personal Cloudflare account; Wrangler and `TF_VAR_cloudflare_account_id` |
| `CLOUDFLARE_ZONE_ID` | Existing `clzmj.dev` zone; `TF_VAR_cloudflare_zone_id` |
| `CLOUDFLARE_API_TOKEN` | Account R2 and Workers, zone read; CI read-only, production write including R2 object uploads |
| `TF_STATE_R2_ACCESS_KEY_ID`, `TF_STATE_R2_SECRET_ACCESS_KEY` | Only `clzmj-dev-tfstate`; map to AWS credentials for Terraform |

R2 runtime reads use the `RELEASES` binding and need no S3 key. The publisher uploads with Wrangler
using `CLOUDFLARE_API_TOKEN`; no separate release S3 key is needed. The state bucket is supplied by `TF_STATE_BUCKET` at init; the state key remains fixed to `skills/terraform.tfstate`. The deployment helper accepts only the two approved personal state bucket names.

Export these mappings inside the same `doppler run --project skills --config prd -- sh -c '…'`
process for every remote-state operation:

```sh
export TF_VAR_cloudflare_account_id="$CLOUDFLARE_ACCOUNT_ID"
export TF_VAR_cloudflare_zone_id="$CLOUDFLARE_ZONE_ID"
export AWS_ACCESS_KEY_ID="$TF_STATE_R2_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$TF_STATE_R2_SECRET_ACCESS_KEY"
export AWS_ENDPOINT_URL_S3="https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"
export AWS_DEFAULT_REGION=auto
```

## First deployment

Run important commands through the existing main Codex Rex session's visible runner. From the
repository root, with the personal account selected:

1. Select the existing private state bucket and its scoped credentials in Doppler. For a dedicated
   `clzmj-skills-tfstate` backend instead, create it and dedicated credentials outside this state first.
2. Export the mappings above and run `terraform -chdir=infra init -reconfigure -backend-config="bucket=$TF_STATE_BUCKET"`.
3. The release bucket must exist before Wrangler can bind it. Run
   `terraform -chdir=infra plan -target=cloudflare_r2_bucket.releases -out=bootstrap.tfplan`, review
   the saved plan, then `terraform -chdir=infra apply bootstrap.tfplan`. This is the only targeted
   apply: it breaks the first-deploy dependency between the bucket and script.
4. Publish a package and deploy the Worker through the repository deployment command.
5. Run `terraform -chdir=infra plan -out=production.tfplan`, review it, then
   `terraform -chdir=infra apply production.tfplan`. The deployed script now exists for custom
   domain attachment. Verify `https://skills.clzmj.dev` through its installer routes.

The deployment command publishes the latest-release pointer only after successful full apply.
Subsequent deploys upload the release, deploy the Worker, then use a complete Terraform plan/apply.
Do not run a separate Wrangler custom-domain route: Terraform owns that binding. Do not create an
extra DNS record or change existing apex DNS/zone settings.

If the release bucket already exists, adopt it instead of creating another:
`terraform -chdir=infra import cloudflare_r2_bucket.releases "$CLOUDFLARE_ACCOUNT_ID/clzmj-skills-releases/default"`.
Existing custom-domain import uses `ACCOUNT_ID/DOMAIN_ID`.

## Checks and plans

```sh
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra init -backend=false -lockfile=readonly
terraform -chdir=infra validate
```

The committed provider lockfile is the site's verified Cloudflare 5.28.1 selection and platform
checksums. Backend-free validation needs no credentials. An authenticated full plan still needs
the backend and Cloudflare read credentials. CI with read-only state credentials may use
`plan -lock=false` because it cannot create/delete the lockfile; serialize CI plans separately.
Production plans and applies keep locking enabled.

Schema references: [R2 bucket](https://github.com/cloudflare/terraform-provider-cloudflare/blob/v5.28.1/docs/resources/r2_bucket.md),
[Workers custom domain](https://github.com/cloudflare/terraform-provider-cloudflare/blob/v5.28.1/docs/resources/workers_custom_domain.md),
[S3 backend and environment endpoint](https://developer.hashicorp.com/terraform/language/backend/s3).
