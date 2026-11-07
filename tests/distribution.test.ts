import { afterAll, describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, rmSync, existsSync, writeFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { gzipSync, gunzipSync } from "node:zlib";
import { buildRelease } from "../scripts/build-release";
import { extractArchive } from "../distribution/archive";
import { installer } from "../distribution/installer";
import worker, { handleRequest, type EdgeCache, type Env } from "../distribution/worker";

const temporary = mkdtempSync(join(tmpdir(), "skills-distribution-test-"));
afterAll(() => rmSync(temporary, { recursive: true, force: true }));
const release = buildRelease("test-123", join(temporary, "built"));
const archive = readFileSync(release.archivePath);

describe("public distribution", () => {
  test("builds deterministic allowlisted artifacts with a matching public manifest", async () => {
    const second = buildRelease("test-123", join(temporary, "second"));
    expect(readFileSync(second.archivePath)).toEqual(archive);
    expect(release.manifest.sha256).toBe(createHash("sha256").update(archive).digest("hex"));
    expect(JSON.parse(readFileSync(join(temporary, "built/latest.json"), "utf8"))).toEqual(release.manifest);
    expect(readFileSync(release.archivePath + ".sha256", "utf8")).toStartWith(release.manifest.sha256);
    const paths = await extractArchive(archive, join(temporary, "extracted"));
    expect(paths).toEqual(release.paths);
    expect(statSync(join(temporary, "extracted/skills/python-tooling/scripts/install_hooks.py")).mode & 0o7777).toBe(0o755);
    expect(statSync(join(temporary, "extracted/skills/rex/scripts/install_hooks.py")).mode & 0o7777).toBe(0o755);
    expect(statSync(join(temporary, "extracted/favorites.json")).mode & 0o7777).toBe(0o644);
    expect(paths).toContain("skills/rex/scripts/agents/complex-worker.toml");
    expect(paths.some(path => /test|\.git|runtime|\.state|__pycache__|infra|distribution/.test(path))).toBe(false);
    expect(paths.every(path => path === "scripts/init.ts" || path === "favorites.json" || path === "LICENSE" || path.startsWith("skills/"))).toBe(true);
    for (const version of ["", "../escape", "a..b", "/abs", "a/b", "x".repeat(129)]) expect(() => buildRelease(version, temporary)).toThrow("Unsafe release version");
  });

  test("rejects traversal, symlinks, duplicate entries, corruption and truncation before writes", async () => {
    const raw = gunzipSync(archive);
    const mutate = (change: (header: Buffer) => void) => {
      const changed = Buffer.from(raw);
      const header = changed.subarray(0, 512);
      change(header);
      header.fill(32, 148, 156);
      header.write(header.reduce((sum, byte) => sum + byte, 0).toString(8).padStart(6, "0") + "\0 ", 148, 8, "ascii");
      return gzipSync(changed);
    };
    const destination = join(temporary, "unsafe");
    for (const name of ["../escape", "/absolute", "skills/rex/../../escape", "skills/rex/.state/token"]) {
      await expect(extractArchive(mutate(header => { header.fill(0, 0, 100); header.write(name); }), destination)).rejects.toThrow("Unsafe archive path");
    }
    await expect(extractArchive(mutate(header => { header[156] = 50; }), destination)).rejects.toThrow("special entries");
    for (const mode of [0o4755, 0o2755, 0o1755, 0o777, 0o600]) {
      await expect(extractArchive(mutate(header => { header.write(mode.toString(8).padStart(7, "0") + "\0", 100, 8, "ascii"); }), destination)).rejects.toThrow("Unsafe archive mode");
    }
    const corrupted = Buffer.from(raw); corrupted[0] ^= 1;
    await expect(extractArchive(gzipSync(corrupted), destination)).rejects.toThrow("checksum");
    await expect(extractArchive(gzipSync(raw.subarray(0, 600)), destination)).rejects.toThrow();
    // A repeated valid first entry is still rejected before writing any files.
    const size = parseInt(raw.subarray(124, 136).toString().replace(/\0.*$/s, ""), 8);
    const firstEntry = raw.subarray(0, 512 + Math.ceil(size / 512) * 512);
    await expect(extractArchive(gzipSync(Buffer.concat([firstEntry, raw])), destination)).rejects.toThrow("Unsafe archive path");
    const collision = Buffer.from(firstEntry);
    const collisionHeader = collision.subarray(0, 512);
    collisionHeader.fill(0, 0, 100); collisionHeader.write("skills/rex/scripts"); collisionHeader.fill(32, 148, 156);
    collisionHeader.write(collisionHeader.reduce((sum, byte) => sum + byte, 0).toString(8).padStart(6, "0") + "\0 ", 148, 8, "ascii");
    await expect(extractArchive(gzipSync(Buffer.concat([collision, raw])), destination)).rejects.toThrow("Conflicting archive paths");
    expect(existsSync(destination)).toBe(false);
  });

  test("serves only the public allowlist from the private binding", async () => {
    const keys: string[] = [];
    const env: Env = { RATE_LIMITER: { limit: async () => ({ success: true }) }, RELEASES: { async get(key) { keys.push(key); return key === "latest.json" || key === "releases/test-123/skills.tar.gz" ? { body: new Response("release").body!, size: 7, httpEtag: '"sha"' } : null; } } };
    const get = (path: string, method = "GET") => worker.fetch(new Request(`https://skills.clzmj.dev${path}`, { method }), env);
    expect(await (await get("/health")).text()).toBe("ok\n");
    expect(await (await get("/install")).text()).toStartWith("#!/bin/sh");
    expect((await get("/latest.json")).headers.get("Cache-Control")).toBe("public, max-age=60");
    expect((await get("/releases/test-123/skills.tar.gz")).headers.get("Cache-Control")).toContain("immutable");
    expect(await (await get("/latest.json", "HEAD")).text()).toBe("");
    for (const path of ["/", "/secrets", "/releases/a..b/skills.tar.gz", "/releases/test-123/other", "/releases/test-123/skills.tar.gz/extra", "/releases/a%2Fb/skills.tar.gz"]) expect((await get(path)).status).toBe(404);
    expect((await get("/latest.json", "POST")).status).toBe(405);
    expect((await get("/releases/missing/skills.tar.gz")).status).toBe(404);
    expect(keys).toEqual(["latest.json", "releases/test-123/skills.tar.gz", "latest.json", "releases/missing/skills.tar.gz"]);
  });

  test("rate limits before all storage and fails closed when protection is unavailable", async () => {
    const keys: string[] = [];
    let reads = 0;
    let cacheReads = 0;
    const cache: EdgeCache = { match: async () => { cacheReads++; return undefined; }, put: async () => {} };
    const env: Env = {
      RATE_LIMITER: { limit: async options => { keys.push(options.key); return { success: false }; } },
      RELEASES: { get: async () => { reads++; return null; } },
    };
    const request = new Request("https://skills.clzmj.dev/latest.json", { headers: { "CF-Connecting-IP": "203.0.113.1" } });
    const limited = await handleRequest(request, env, undefined, cache);
    expect(limited.status).toBe(429);
    expect(limited.headers.get("Retry-After")).toBe("60");
    expect(limited.headers.get("Cache-Control")).toBe("no-store");
    expect(keys).toEqual(["clzmj-skills:203.0.113.1"]);
    env.RATE_LIMITER.limit = async () => { throw new Error("unavailable"); };
    expect((await handleRequest(request, env, undefined, cache)).status).toBe(503);
    expect(reads).toBe(0);
    expect(cacheReads).toBe(0);
  });

  test("uses canonical edge cache keys and rejects query bypasses before storage", async () => {
    let reads = 0;
    let limits = 0;
    const objects = new Map<string, Response>();
    const pending: Promise<unknown>[] = [];
    const cache: EdgeCache = {
      match: async request => objects.get(request.url)?.clone(),
      put: async (request, response) => { expect(request.method).toBe("GET"); objects.set(request.url, response.clone()); },
    };
    const env: Env = {
      RATE_LIMITER: { limit: async () => { limits++; return { success: true }; } },
      RELEASES: { get: async () => { reads++; return { body: new Response("release").body! }; } },
    };
    const get = (path: string, method = "GET") => handleRequest(new Request(`https://skills.clzmj.dev${path}`, { method }), env, { waitUntil: promise => pending.push(promise) }, cache);
    const path = "/releases/test-123/skills.tar.gz";
    expect(await (await get(path)).text()).toBe("release");
    await Promise.all(pending);
    expect(await (await get(path)).text()).toBe("release");
    expect(await (await get(path, "HEAD")).text()).toBe("");
    expect(reads).toBe(1);
    for (const suffix of ["?v=1", "?v=2", "?random=xyz"]) expect((await get(path + suffix)).status).toBe(400);
    expect(limits).toBe(3);
    expect(reads).toBe(1);
    expect([...objects.keys()]).toEqual(["https://skills.clzmj.dev" + path]);
    expect((await get("/latest.json")).headers.get("Cache-Control")).toBe("public, max-age=60");
    expect((await get("/install")).headers.get("Cache-Control")).toBe("public, max-age=60");
    const before = reads;
    for (const bad of ["/bucket/private", "/releases/x/secret", "/releases/a..b/skills.tar.gz"]) expect((await get(bad)).status).toBe(404);
    expect((await get("/latest.json", "POST")).status).toBe(405);
    expect(reads).toBe(before);
  });

  test("config disables public alternate routes and uses only native rate limiting", () => {
    const config = JSON.parse(readFileSync(new URL("../distribution/wrangler.jsonc", import.meta.url), "utf8"));
    expect(config.workers_dev).toBe(false);
    expect(config.preview_urls).toBe(false);
    expect(config.routes).toBeUndefined();
    expect(config.ratelimits).toEqual([{ name: "RATE_LIMITER", namespace_id: "2026110601", simple: { limit: 120, period: 60 } }]);
    expect(config.r2_buckets).toEqual([{ binding: "RELEASES", bucket_name: "clzmj-skills-releases" }]);
    expect(config.kv_namespaces).toBeUndefined();
    expect(config.durable_objects).toBeUndefined();
  });

  test("runs the bootstrap, preserves local source, and keeps dry run temporary", () => {
    const preload = join(temporary, "preload.ts");
    writeFileSync(preload, `
      import { readFileSync, writeFileSync } from "node:fs";
      globalThis.fetch = async input => new Response(String(input).endsWith("/latest.json") ? process.env.TEST_MANIFEST : readFileSync(process.env.TEST_ARCHIVE));
      Bun.spawnSync = (command, options) => { writeFileSync(process.env.TEST_CALL, JSON.stringify({ command, source: options.env.CLZMJ_SKILLS_SOURCE })); return { exitCode: 0 }; };
    `);
    const body = installer().split("<<'CLZMJ_INSTALLER'\n")[1].split("\nCLZMJ_INSTALLER")[0];
    const run = (name: string, args: string[], manifest = release.manifest) => {
      const home = join(temporary, name);
      const call = join(temporary, name + "-call.json");
      const result = Bun.spawnSync([process.execPath, "--preload", preload, "-", ...args], { stdin: Buffer.from(body), env: { ...process.env, HOME: home, TEST_MANIFEST: JSON.stringify(manifest), TEST_ARCHIVE: release.archivePath, TEST_CALL: call } });
      return { home, call, result };
    };
    const dry = run("dry-home", ["--dry-run", "--agents", "codex"]);
    expect(dry.result.exitCode).toBe(0);
    expect(dry.result.stdout.toString()).toContain("no persistent release");
    expect(existsSync(join(dry.home, ".local/share/clzmj-skills"))).toBe(false);
    const dryCall = JSON.parse(readFileSync(dry.call, "utf8"));
    expect(dryCall.command.slice(2)).toEqual(["--dry-run", "--agents", "codex"]);
    expect(existsSync(dryCall.source)).toBe(false);
    const installed = run("installed-home", ["--no-hooks"]);
    expect(installed.result.stderr.toString()).toBe("");
    expect(installed.result.exitCode).toBe(0);
    const call = JSON.parse(readFileSync(installed.call, "utf8"));
    expect(call.source).toBe(join(installed.home, ".local/share/clzmj-skills/releases/test-123"));
    expect(existsSync(join(call.source, "skills/rex/SKILL.md"))).toBe(true);
    expect(run("installed-home", []).result.exitCode).toBe(0);
    const invalid = run("bad-checksum", [], { ...release.manifest, sha256: "0".repeat(64) });
    expect(invalid.result.exitCode).toBe(1);
    expect(invalid.result.stderr.toString()).toContain("checksum mismatch");
    expect(existsSync(invalid.call)).toBe(false);
    expect(existsSync(join(invalid.home, ".local/share/clzmj-skills"))).toBe(false);
  });

  test("installer is valid shell and contains checksum validation and persistent hosted source", () => {
    const script = installer();
    const syntax = Bun.spawnSync(["sh", "-n"], { stdin: Buffer.from(script) });
    expect(syntax.exitCode).toBe(0);
    expect(script).toContain('CLZMJ_SKILLS_SOURCE: source');
    expect(script).toContain('createHash("sha256")');
    expect(script).toContain('.local/share/clzmj-skills/releases');
    expect(script).toContain('no persistent release or installation writes');
    expect(script).not.toMatch(/gh auth|GITHUB_TOKEN|CLOUDFLARE_API_TOKEN|git clone/);
  });
});
