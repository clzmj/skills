import { expect, test } from 'bun:test';
import { fetchEnvironment, policy } from '../scripts/with-doppler';

const env = { GITHUB_ACTIONS: 'true', GITHUB_REPOSITORY: policy.repository, GITHUB_REF: 'refs/heads/main', DOPPLER_TOKEN: 'dp.st.mock-only', CLOUDFLARE_API_TOKEN: 'stale' };
const values = { ...Object.fromEntries(policy.secrets.map(name => [name, `mock-${name}`])), DOPPLER_PROJECT: policy.project, DOPPLER_CONFIG: 'ci', DOPPLER_ENVIRONMENT: 'ci' };
const fetcher = (async () => Response.json(values)) as typeof fetch;

test('fetches a selected config, masks before returning, and strips the bootstrap token and stale secrets', async () => {
  const masked: string[] = [];
  const child = await fetchEnvironment({ config: 'ci', names: ['TF_STATE_BUCKET'], env, fetcher: (async (url, options) => {
    const requested = new URL(String(url));
    expect(requested.origin).toBe('https://api.doppler.com');
    expect(requested.searchParams.get('secrets')).toBe('TF_STATE_BUCKET,DOPPLER_PROJECT,DOPPLER_CONFIG,DOPPLER_ENVIRONMENT');
    expect(options?.redirect).toBe('error');
    return Response.json(values);
  }) as typeof fetch, mask: value => masked.push(value) });
  expect(masked).toEqual(['mock-TF_STATE_BUCKET']);
  expect(child.DOPPLER_TOKEN).toBeUndefined();
  expect(child.CLOUDFLARE_API_TOKEN).toBeUndefined();
  expect(child.TF_STATE_BUCKET).toBe('mock-TF_STATE_BUCKET');
});

test('rejects other repositories, operator tokens, and production outside main before fetching', async () => {
  for (const invalid of [{ ...env, GITHUB_REPOSITORY: 'other/repo' }, { ...env, DOPPLER_TOKEN: 'dp.ct.operator' }, { ...env, GITHUB_REF: 'refs/heads/feature' }]) {
    await expect(fetchEnvironment({ config: 'prd', env: invalid, fetcher, mask: () => {} })).rejects.toThrow();
  }
});

test('fails closed for denied fetches, wrong configs and missing required values', async () => {
  for (const result of [new Response('private response details', { status: 403 }), Response.json({ ...values, DOPPLER_CONFIG: 'prd' }), Response.json({ ...values, CLOUDFLARE_API_TOKEN: '' })]) {
    await expect(fetchEnvironment({ config: 'ci', env, fetcher: (async () => result) as typeof fetch, mask: () => {} })).rejects.toThrow();
  }
});
