#!/usr/bin/env bun

export const policy = {
  repository: 'clzmj/skills',
  project: 'skills',
  secrets: ['CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_ZONE_ID', 'TF_STATE_BUCKET', 'TF_STATE_R2_ACCESS_KEY_ID', 'TF_STATE_R2_SECRET_ACCESS_KEY'],
};

type Options = {
  config: string;
  names?: string[];
  env: Record<string, string | undefined>;
  fetcher?: typeof fetch;
  mask?: (value: string) => void;
};

export async function fetchEnvironment({ config, names = policy.secrets, env, fetcher = fetch, mask = value => process.stdout.write(`::add-mask::${value.replaceAll('%', '%25').replaceAll('\r', '%0D').replaceAll('\n', '%0A')}\n`) }: Options) {
  if (env.GITHUB_ACTIONS !== 'true' || env.GITHUB_REPOSITORY !== policy.repository) throw Error('Runtime fetch is restricted to this repository in GitHub Actions');
  if (!['ci', 'prd'].includes(config)) throw Error('Expected config ci or prd');
  if (config === 'prd' && env.GITHUB_REF !== 'refs/heads/main') throw Error('Production requires main');
  if (!env.DOPPLER_TOKEN?.startsWith('dp.st.')) throw Error('A dedicated Doppler service token is required');
  if (!names.length || names.some(name => !policy.secrets.includes(name))) throw Error('Unexpected runtime secret selection');
  const metadata = ['DOPPLER_PROJECT', 'DOPPLER_CONFIG', 'DOPPLER_ENVIRONMENT'];
  const url = new URL('https://api.doppler.com/v3/configs/config/secrets/download');
  url.search = new URLSearchParams({ project: policy.project, config, format: 'json', secrets: [...names, ...metadata].join(',') }).toString();
  let values: Record<string, unknown>;
  try {
    const response = await fetcher(url, { headers: { Authorization: `Bearer ${env.DOPPLER_TOKEN}` }, redirect: 'error', signal: AbortSignal.timeout(30_000) });
    if (!response.ok) throw Error('Fetch rejected');
    values = await response.json() as Record<string, unknown>;
  } catch { throw Error('Doppler runtime fetch failed; command was not started'); }
  if (!values || values.DOPPLER_PROJECT !== policy.project || values.DOPPLER_CONFIG !== config) throw Error('Doppler token/config mismatch');
  for (const name of [...names, ...metadata]) {
    if (typeof values[name] !== 'string' || !values[name]) throw Error(`Missing runtime secret: ${name}`);
  }
  // Register masks before any child output, without exporting secrets to runner files.
  for (const name of names) mask(values[name] as string);
  const childEnv = { ...env };
  delete childEnv.DOPPLER_TOKEN;
  for (const name of [...policy.secrets, ...metadata]) delete childEnv[name];
  for (const name of [...names, ...metadata]) childEnv[name] = values[name] as string;
  return childEnv;
}

if (import.meta.main) {
  try {
    const [config, ...args] = process.argv.slice(2);
    const divider = args.indexOf('--');
    const options = args.slice(0, divider);
    if (divider < 0 || !args[divider + 1] || (options.length && (options.length !== 2 || options[0] !== '--only'))) throw Error('Usage: bun scripts/with-doppler.ts ci|prd [--only NAME,NAME] -- command [args]');
    const env = await fetchEnvironment({ config, names: options.length ? options[1].split(',') : undefined, env: process.env });
    const child = Bun.spawn(args.slice(divider + 1), { env, stdin: 'inherit', stdout: 'inherit', stderr: 'inherit' });
    process.exitCode = await child.exited;
  } catch (error) {
    console.error(error instanceof Error ? error.message : 'Runtime fetch failed');
    process.exitCode = 1;
  }
}
