#!/usr/bin/env bun
import { createHash } from "node:crypto";

// Operator usage: doppler run --project skills --config dev -- bun scripts/provision-credentials.ts plan|apply
// All secret reads/writes are restricted to this project. Never print provider/CLI responses.
export const project = "skills";
export const bucket = "clzmj-skills-tfstate";
export const operatorCLI = "skills:operator-cli";
type Secrets = Record<string, string>;
type Group = { id: string; name: string; scopes: string[]; is_selectable?: boolean };
type Policy = { effect: string; resources: Record<string, string>; permission_groups: { id: string }[] };
type Token = { id: string; name: string; status: string; policies: Policy[]; expires_on?: string; not_before?: string };
type Spec = { name: string; config: "ci" | "prd"; key: string; policies: Policy[] };
export type IO = {
  api(path: string, token: string, method?: string, body?: unknown): Promise<any>;
  read(config: "ci" | "prd"): Promise<Secrets>;
  write(config: "ci" | "prd", values: Secrets): Promise<void>;
  log(line: string): void;
};
export function inputs(env: Secrets, useOperatorCLI = false) {
  const development = env.DOPPLER_CONFIG === "dev" || (env.DOPPLER_ENVIRONMENT === "dev" && env.DOPPLER_CONFIG?.startsWith("dev_"));
  if (env.DOPPLER_PROJECT !== project || !development) throw new Error("Run with Doppler project skills config dev or its private development config");
  for (const key of ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ZONE_ID"]) {
    if (!/^[a-f0-9]{32}$/.test(env[key] ?? "")) throw new Error(`Invalid ${key}`);
  }
  if (!useOperatorCLI && !env.CLOUDFLARE_BOOTSTRAP_API_TOKEN?.trim()) throw new Error("Missing CLOUDFLARE_BOOTSTRAP_API_TOKEN in skills/dev");
  return { account: env.CLOUDFLARE_ACCOUNT_ID, zone: env.CLOUDFLARE_ZONE_ID, bootstrap: useOperatorCLI ? operatorCLI : env.CLOUDFLARE_BOOTSTRAP_API_TOKEN };
}
// Only non-secret resource identifiers and policies may appear in CLI arguments.
export function operatorCommand(path: string, method = "GET", body?: any): { args: string[]; account: string } {
  const match = /^\/accounts\/([a-f0-9]{32})\/tokens(?:\/(permission_groups|[a-f0-9]{32}))?$/.exec(path);
  if (!match) throw new Error("Unsupported operator CLI endpoint");
  let action: string[];
  if (method === "GET" && match[2] === "permission_groups") action = ["permission-groups", "list"];
  else if (method === "GET" && match[2]) action = ["get", match[2]];
  else if (method === "GET") action = ["list", "--include-expired", "true"];
  else if (method === "POST" && !match[2] && /^skills-(ci|prd)-(cloudflare|state)$/.test(body?.name) && Array.isArray(body?.policies)) {
    action = ["create", "--name", body.name, "--policies", JSON.stringify(body.policies)];
  } else throw new Error("Unsupported operator CLI operation");
  return { args: ["cf", "--profile", "default", "accounts", "tokens", ...action], account: match[1] };
}
export function operatorEnvironment(env: Record<string, string | undefined>, account: string) {
  return { ...Object.fromEntries(Object.entries(env).filter(([key]) =>
    !key.startsWith("CLOUDFLARE_") && !key.startsWith("CF_") && !key.startsWith("TF_STATE_") && !key.startsWith("AWS_"))),
    CLOUDFLARE_ACCOUNT_ID: account };
}
export function operatorResult(raw: string) {
  try {
    const parsed = JSON.parse(raw);
    if (parsed?.success === false) throw new Error();
    return parsed?.result ?? parsed;
  } catch { throw new Error("Invalid operator CLI response; output suppressed"); }
}
export function s3Credentials(id: string, value: string) {
  return { TF_STATE_R2_ACCESS_KEY_ID: id, TF_STATE_R2_SECRET_ACCESS_KEY: createHash("sha256").update(value).digest("hex") };
}
export function specifications(account: string, zone: string, groups: Group[]): Spec[] {
  function policy(names: string[], scope: string, resource: string): Policy {
    return { effect: "allow", resources: { [resource]: "*" }, permission_groups: names.map(name => {
      const matches = groups.filter(g => g.name === name && g.scopes.includes(scope) && g.is_selectable !== false);
      if (matches.length !== 1) throw new Error(`Missing or ambiguous selectable permission: ${name}`);
      return { id: matches[0].id };
    }) };
  }
  return (["ci", "prd"] as const).flatMap(config => {
    const role = config === "ci" ? "Read" : "Write";
    return [
      { name: `skills-${config}-cloudflare`, config, key: "CLOUDFLARE_API_TOKEN", policies: [
        policy([`Workers Scripts ${role}`, `Workers R2 Storage ${role}`], "com.cloudflare.api.account", `com.cloudflare.api.account.${account}`),
        policy(["Zone Read"], "com.cloudflare.api.account.zone", `com.cloudflare.api.account.zone.${zone}`),
      ] },
      { name: `skills-${config}-state`, config, key: "TF_STATE_R2_ACCESS_KEY_ID", policies: [
        policy([`Workers R2 Storage Bucket Item ${role}`], "com.cloudflare.edge.r2.bucket", `com.cloudflare.edge.r2.bucket.${account}_default_${bucket}`),
      ] },
    ];
  });
}
function normalized(policies: Policy[]) {
  return JSON.stringify(policies.map(p => ({ effect: p.effect,
    resources: Object.entries(p.resources).sort(), permissions: p.permission_groups.map(g => g.id).sort(),
  })).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))));
}
export function validateExisting(spec: Spec, token: Token, stored: Secrets) {
  if (!stored[spec.key] || (spec.key === "TF_STATE_R2_ACCESS_KEY_ID" && (stored[spec.key] !== token.id || !/^[a-f0-9]{64}$/.test(stored.TF_STATE_R2_SECRET_ACCESS_KEY ?? "")))) throw new Error(`${spec.name}: named token has no saved Doppler value; recover it manually before retrying`);
  if (token.status !== "active" || normalized(token.policies) !== normalized(spec.policies)
    || (token.expires_on && Date.parse(token.expires_on) <= Date.now())
    || (token.not_before && Date.parse(token.not_before) > Date.now())) {
    throw new Error(`${spec.name}: existing token is inactive or has different scope; resolve manually`);
  }
}
export async function provision(env: Secrets, mode: "plan" | "apply", io: IO, useOperatorCLI = false) {
  const { account, zone, bootstrap } = inputs(env, useOperatorCLI);
  const base = `/accounts/${account}`;
  const groups: Group[] = await io.api(`${base}/tokens/permission_groups`, bootstrap);
  const specs = specifications(account, zone, groups);
  const existing: Token[] = await io.api(`${base}/tokens`, bootstrap);
  const saved = { ci: await io.read("ci"), prd: await io.read("prd") };
  // Complete preflight before any write, including orphan/duplicate names.
  const found = new Map<string, Token>();
  for (const spec of specs) {
    const matches = existing.filter(t => t.name === spec.name);
    if (matches.length > 1) throw new Error(`${spec.name}: duplicate named tokens require manual resolution`);
    if (matches.length) {
      const detail: Token = await io.api(`${base}/tokens/${matches[0].id}`, bootstrap);
      validateExisting(spec, detail, saved[spec.config]);
      if (spec.key === "CLOUDFLARE_API_TOKEN") {
        const verified = await io.api(`${base}/tokens/verify`, saved[spec.config][spec.key]);
        if (verified.id !== detail.id || verified.status !== "active") throw new Error(`${spec.name}: Doppler value does not match active named token`);
      }
      found.set(spec.name, detail);
    } else if (saved[spec.config][spec.key] || (spec.key === "TF_STATE_R2_ACCESS_KEY_ID" && saved[spec.config].TF_STATE_R2_SECRET_ACCESS_KEY)) {
      throw new Error(`${spec.name}: Doppler has an unrecognized value; resolve manually before provisioning`);
    }
    io.log(`${found.has(spec.name) ? "preserve" : "create"} ${spec.name}: ${JSON.stringify(spec.policies)}`);
  }
  if (mode === "plan") return;
  // Mint production API token first so the operator token only manages tokens.
  const ordered = [...specs].sort((a, b) => Number(b.name === "skills-prd-cloudflare") - Number(a.name === "skills-prd-cloudflare"));
  let current = "preflight";
  try {
    for (const spec of ordered) {
      current = spec.name;
      let token = found.get(spec.name);
      if (!token) {
        const created = await io.api(`${base}/tokens`, bootstrap, "POST", { name: spec.name, policies: spec.policies });
        if (!created.id || !created.value) throw new Error("Token response missing required fields");
        // Persist immediately as one update; retain only the derived S3 pair for state tokens.
        const values = spec.key === "CLOUDFLARE_API_TOKEN" ? { CLOUDFLARE_API_TOKEN: created.value } : s3Credentials(created.id, created.value);
        Object.assign(saved[spec.config], values);
        await io.write(spec.config, saved[spec.config]);
        token = created;
      }
      if (spec.name === "skills-prd-cloudflare") {
        const tokenValue = saved.prd.CLOUDFLARE_API_TOKEN;
        // List with the fresh project token; a missing bucket is the sole create condition.
        const listing = await io.api(`${base}/r2/buckets`, tokenValue);
        if (!Array.isArray(listing.buckets)) throw new Error("Invalid bucket listing");
        if (!listing.buckets.some((b: { name: string }) => b.name === bucket)) {
          await io.api(`${base}/r2/buckets`, tokenValue, "POST", { name: bucket, locationHint: "wnam" });
        }
      }
      io.log(`stored ${spec.name} in skills/${spec.config}`);
    }
    for (const config of ["ci", "prd"] as const) {
      Object.assign(saved[config], { CLOUDFLARE_ACCOUNT_ID: account, CLOUDFLARE_ZONE_ID: zone, TF_STATE_BUCKET: bucket });
      await io.write(config, saved[config]);
    }
  } catch {
    throw new Error(`Provisioning stopped at ${current}; prior project tokens are preserved. Check skills Doppler before retrying. If a named token lacks its saved value, recover manually; no token was revoked.`);
  }
}

async function doppler(args: string[], value?: string): Promise<string> {
  try {
    const child = Bun.spawn(["doppler", "--no-check-version", ...args], {
      stdin: value === undefined ? "ignore" : new Blob([value]), stdout: "pipe", stderr: "pipe",
      // Avoid unrelated Cloudflare credentials reaching the CLI subprocess.
      env: Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith("CLOUDFLARE_") && !key.startsWith("TF_STATE_") && !key.startsWith("AWS_"))),
    });
    const [output] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text()]);
    if (await child.exited !== 0) throw new Error();
    return output;
  } catch { throw new Error("Doppler operation failed for project skills; output suppressed"); }
}
export const liveIO: IO = {
  async api(path, token, method = "GET", body) {
    try {
      if (token === operatorCLI) {
        const command = operatorCommand(path, method, body);
        const results: any[] = [];
        // Explicit pages avoid silently missing an existing named credential.
        const list = method === "GET" && /\/tokens$/.test(path);
        for (let page = 1; page <= 1000; page++) {
          const child = Bun.spawn([...command.args, ...(list ? ["--per-page", "50", "--page", String(page)] : [])], {
            env: operatorEnvironment(process.env, command.account), stdin: "ignore", stdout: "pipe", stderr: "pipe",
          });
          const [stdout] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text()]);
          if (await child.exited !== 0) throw new Error();
          const result = operatorResult(stdout);
          if (!list) return result;
          if (!Array.isArray(result)) throw new Error();
          results.push(...result);
          if (result.length < 50) return results;
        }
        throw new Error();
      }
      const response = await fetch(`https://api.cloudflare.com/client/v4${path}`, { method,
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const data = await response.json() as any;
      if (!response.ok || !data.success) {
        const safePath = path.replace(/accounts\/[a-f0-9]{32}/, "accounts/<account>").replace(/tokens\/[a-f0-9]{32}/, "tokens/<token>");
        throw new Error(`Cloudflare ${method} ${safePath} failed (HTTP ${response.status}); response suppressed`);
      }
      // Fail closed if pagination would hide an existing named token.
      if (data.result_info?.total_pages > 1) throw new Error();
      return data.result;
    } catch (error) {
      if (error instanceof Error && error.message.startsWith("Cloudflare ")) throw error;
      throw new Error(`Cloudflare ${method} failed; response suppressed`);
    }
  },
  async read(config) {
    const raw = await doppler(["secrets", "download", "--project", project, "--config", config, "--no-file", "--no-fallback", "--format", "json"]);
    try { return JSON.parse(raw); } catch { throw new Error("Invalid skills Doppler response; output suppressed"); }
  },
  async write(config, values) {
    // Download includes reserved Doppler selectors, which cannot be uploaded as user secrets.
    const secrets = Object.fromEntries(Object.entries(values).filter(([key]) => !key.startsWith("DOPPLER_")));
    await doppler(["secrets", "upload", "/dev/stdin", "--project", project, "--config", config, "--silent"], JSON.stringify(secrets));
  },
  log: console.log,
};
if (import.meta.main) {
  const mode = process.argv[2];
  if ((mode !== "plan" && mode !== "apply") || process.argv.slice(3).some(arg => arg !== "--operator-cli")) { console.error("Usage: bun scripts/provision-credentials.ts plan|apply [--operator-cli]"); process.exit(1); }
  try { await provision(process.env as Secrets, mode, liveIO, process.argv.includes("--operator-cli")); }
  catch (error) { console.error(error instanceof Error ? error.message : "Credential provisioning failed; details suppressed"); process.exit(1); }
}
