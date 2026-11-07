#!/usr/bin/env bun
import { resolve } from "node:path";

const root = resolve(import.meta.dir, "..");
const mode = process.argv[2];
if (!['plan', 'production'].includes(mode ?? '')) {
  throw new Error('Usage: bun scripts/deploy.ts plan|production');
}
const required = [
  'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_ZONE_ID',
  'TF_STATE_BUCKET', 'TF_STATE_R2_ACCESS_KEY_ID', 'TF_STATE_R2_SECRET_ACCESS_KEY',
];
for (const name of required) {
  if (!process.env[name]) throw new Error(`Missing required environment variable: ${name}`);
}
if (!['clzmj-skills-tfstate', 'clzmj-dev-tfstate'].includes(process.env.TF_STATE_BUCKET!)) {
  throw new Error('TF_STATE_BUCKET must be an approved personal Terraform-state bucket');
}
if (!/^[0-9a-f]{32}$/.test(process.env.CLOUDFLARE_ACCOUNT_ID!)) {
  throw new Error('Invalid CLOUDFLARE_ACCOUNT_ID');
}
if (mode === 'production' && process.env.GITHUB_ACTIONS === 'true' && process.env.GITHUB_REF !== 'refs/heads/main') {
  throw new Error('Production deployment requires refs/heads/main');
}
const env = {
  ...process.env,
  TF_IN_AUTOMATION: 'true',
  TF_VAR_cloudflare_account_id: process.env.CLOUDFLARE_ACCOUNT_ID!,
  TF_VAR_cloudflare_zone_id: process.env.CLOUDFLARE_ZONE_ID!,
  AWS_ACCESS_KEY_ID: process.env.TF_STATE_R2_ACCESS_KEY_ID!,
  AWS_SECRET_ACCESS_KEY: process.env.TF_STATE_R2_SECRET_ACCESS_KEY!,
  AWS_ENDPOINT_URL_S3: `https://${process.env.CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com`,
  AWS_EC2_METADATA_DISABLED: 'true',
};
async function run(args: string[]) {
  const child = Bun.spawn(args, { cwd: root, env, stdin: 'inherit', stdout: 'inherit', stderr: 'inherit' });
  const code = await child.exited;
  if (code !== 0) throw new Error(`${args[0]} ${args[1] ?? ''} failed (${code})`);
}
const tf = (...args: string[]) => run(['terraform', '-chdir=infra', ...args]);
const wrangler = (...args: string[]) => run(['bun', 'x', '--no-install', 'wrangler', ...args]);
await tf('fmt', '-check', '-recursive');
await tf('init', '-input=false', '-no-color', `-backend-config=bucket=${process.env.TF_STATE_BUCKET}`);
await tf('validate', '-no-color');
if (mode === 'plan') {
  // Read-only CI credentials cannot create a state lock. Never apply this plan.
  await tf('plan', '-lock=false', '-input=false', '-no-color');
} else {
  const version = process.env.GITHUB_SHA;
  if (!/^[0-9a-f]{40}$/.test(version ?? '')) throw new Error('GITHUB_SHA must be a full commit SHA');
  await run(['bun', 'scripts/build-release.ts', '--version', version!, '--out', 'dist/releases']);
  // Bootstrap the binding's bucket before the Worker; Terraform owns the custom domain.
  await tf('plan', '-input=false', '-no-color', '-target=cloudflare_r2_bucket.releases', '-out=release-bucket.tfplan');
  await tf('apply', '-input=false', '-auto-approve', 'release-bucket.tfplan');
  for (const suffix of ['', '.sha256']) {
    const key = `releases/${version}/skills.tar.gz${suffix}`;
    await wrangler('r2', 'object', 'put', `clzmj-skills-releases/${key}`, '--file', `dist/releases/${key}`, '--remote');
  }
  await wrangler('deploy', '--config', 'distribution/wrangler.jsonc');
  await tf('plan', '-input=false', '-no-color', '-out=production.tfplan');
  await tf('apply', '-input=false', '-auto-approve', 'production.tfplan');
  // R2 replaces one object atomically. Publish discovery only after every prior step succeeds.
  await wrangler('r2', 'object', 'put', 'clzmj-skills-releases/latest.json', '--file', 'dist/releases/latest.json', '--content-type', 'application/json', '--remote');
}
