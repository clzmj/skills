import { expect, test } from "bun:test";
import { bucket, inputs, provision, s3Credentials, specifications, type IO } from "../scripts/provision-credentials";

const account = "a".repeat(32), zone = "b".repeat(32);
const env = { DOPPLER_PROJECT: "skills", DOPPLER_CONFIG: "dev", CLOUDFLARE_ACCOUNT_ID: account, CLOUDFLARE_ZONE_ID: zone, CLOUDFLARE_BOOTSTRAP_API_TOKEN: "operator-secret" };
const groups = [
  ...["Workers Scripts Read", "Workers Scripts Write", "Workers R2 Storage Read", "Workers R2 Storage Write"].map(name => ({ name, scopes: ["com.cloudflare.api.account"] })),
  { name: "Zone Read", scopes: ["com.cloudflare.api.account.zone"] },
  ...["Read", "Write"].map(role => ({ name: `Workers R2 Storage Bucket Item ${role}`, scopes: ["com.cloudflare.edge.r2.bucket"] })),
].map((g, i) => ({ ...g, id: String(i) }));
function fixture() {
  const saved: Record<string, Record<string, string>> = { ci: { UNRELATED: "preserve" }, prd: {} };
  const tokens: any[] = [], calls: any[] = [], logs: string[] = [], writes: any[] = [];
  const io: IO = {
    async api(path, token, method = "GET", body: any) {
      calls.push({ path, token, method, body });
      if (path.endsWith("permission_groups")) return groups;
      if (path.endsWith("tokens/verify")) return { id: tokens.find(t => t.value === token)?.id, status: "active" };
      if (path.endsWith("/tokens") && method === "GET") return tokens;
      if (path.endsWith("/tokens") && method === "POST") {
        const result = { ...body, id: String(tokens.length + 1).padStart(32, "0"), value: `minted-secret-${tokens.length}`, status: "active" };
        tokens.push(result); return result;
      }
      if (path.includes("/tokens/")) return tokens.find(t => path.endsWith(t.id));
      if (path.endsWith("r2/buckets")) return method === "GET" ? { buckets: [] } : {};
      throw new Error("unexpected request");
    },
    async read(config) { return { ...saved[config] }; },
    async write(config, values) { writes.push({ config, values: { ...values } }); saved[config] = { ...values }; },
    log(line) { logs.push(line); },
  };
  return { io, tokens, saved, calls, logs, writes };
}
test("input validation happens before any IO and never falls back to deployment token", async () => {
  expect(() => inputs({ ...env, DOPPLER_PROJECT: "other" })).toThrow("project skills config dev");
  expect(() => inputs({ ...env, CLOUDFLARE_BOOTSTRAP_API_TOKEN: "" })).toThrow("Missing");
  expect(() => inputs({ ...env, CLOUDFLARE_ACCOUNT_ID: "bad" })).toThrow("Invalid");
  expect(inputs({ ...env, DOPPLER_CONFIG: "dev_personal", DOPPLER_ENVIRONMENT: "dev" }).account).toBe(account);
  expect(() => inputs({ ...env, DOPPLER_CONFIG: "dev_personal", DOPPLER_ENVIRONMENT: "prd" })).toThrow("development config");
});
test("policies use exact account, zone and own-bucket scopes; missing scope fails closed", () => {
  const specs = specifications(account, zone, groups);
  expect(specs.map(s => s.name)).toEqual(["skills-ci-cloudflare", "skills-ci-state", "skills-prd-cloudflare", "skills-prd-state"]);
  expect(specs[0].policies[1].resources).toEqual({ [`com.cloudflare.api.account.zone.${zone}`]: "*" });
  expect(specs[1].policies[0].resources).toEqual({ [`com.cloudflare.edge.r2.bucket.${account}_default_${bucket}`]: "*" });
  expect(() => specifications(account, zone, groups.filter(g => g.name !== "Zone Read"))).toThrow("permission");
  expect(s3Credentials("id", "abc").TF_STATE_R2_SECRET_ACCESS_KEY).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
});
test("plan is read-only and logs no credential values", async () => {
  const f = fixture(); await provision(env, "plan", f.io);
  expect(f.calls.every(c => c.method === "GET")).toBe(true);
  expect(f.writes).toHaveLength(0);
  expect(f.logs.join()).not.toContain("operator-secret");
});
test("apply creates four dedicated tokens, creates state bucket with new project token, stores no raw state token", async () => {
  const f = fixture(); await provision(env, "apply", f.io);
  expect(f.tokens).toHaveLength(4);
  expect(f.tokens[0].name).toBe("skills-prd-cloudflare");
  const createBucket = f.calls.find(c => c.path.endsWith("r2/buckets") && c.method === "POST");
  expect(createBucket.token).toBe(f.tokens[0].value);
  for (const config of ["ci", "prd"]) {
    const state = f.tokens.find(t => t.name === `skills-${config}-state`);
    expect(f.saved[config].TF_STATE_R2_ACCESS_KEY_ID).toBe(state.id);
    expect(f.saved[config].TF_STATE_R2_SECRET_ACCESS_KEY).toBe(s3Credentials(state.id, state.value).TF_STATE_R2_SECRET_ACCESS_KEY);
    expect(Object.values(f.saved[config])).not.toContain(state.value);
    expect(f.saved[config].TF_STATE_BUCKET).toBe(bucket);
  }
  expect(f.saved.ci.UNRELATED).toBe("preserve");
  expect(f.logs.join()).not.toMatch(/minted-secret|operator-secret/);
  const count = f.tokens.length;
  await provision(env, "apply", f.io);
  expect(f.tokens).toHaveLength(count);
});
test("orphan named tokens and wrong policies fail before all writes", async () => {
  const f = fixture(); const spec = specifications(account, zone, groups)[0];
  f.tokens.push({ ...spec, id: "orphan", status: "active" });
  await expect(provision(env, "apply", f.io)).rejects.toThrow("no saved Doppler value");
  expect(f.writes).toHaveLength(0);
  expect(f.calls.some(c => c.method === "POST")).toBe(false);
});
test("partial persistence failure stops immediately with sanitized diagnostic, never revokes", async () => {
  const f = fixture(); f.io.write = async () => { throw new Error("SECRET RESPONSE"); };
  await expect(provision(env, "apply", f.io)).rejects.toThrow("Provisioning stopped at skills-prd-cloudflare");
  expect(f.tokens).toHaveLength(1);
  expect(f.calls.some(c => c.method === "DELETE")).toBe(false);
});
