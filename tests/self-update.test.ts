import { afterAll, expect, test } from "bun:test";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, lstatSync, symlinkSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { buildRelease } from "../scripts/build-release";
import { extractArchive } from "../distribution/archive";
import { fingerprint, launcherPath, registerLauncher, selfUpdate, stageRelease, uninstall } from "../scripts/self-update";
const temporary = mkdtempSync(join(tmpdir(), "skills-self-update-"));
afterAll(() => rmSync(temporary, { recursive: true, force: true }));
const built = buildRelease("update-test", join(temporary, "built"));
const archive = readFileSync(built.archivePath);
const location = (name: string) => ({ scope: "project" as const, projectRoot: join(temporary, name) });
const runtime = (manifest = built.manifest, check = () => true) => ({ fetch: (async (url: string | URL | Request) => new Response(String(url).endsWith("latest.json") ? JSON.stringify(manifest) : archive)) as typeof fetch, check, log: () => {} });
test("self-update stages verified immutable release, switches only launcher, and same version is a no-op", async () => {
  const options = location("success");
  mkdirSync(join(options.projectRoot, ".clzmj-skills"), { recursive: true });
  const settings = join(options.projectRoot, ".clzmj-skills/install.json"); writeFileSync(settings, "saved choices");
  const source = await selfUpdate(options, runtime());
  const launcher = launcherPath(options), before = readFileSync(launcher, "utf8");
  const executed = Bun.spawnSync([launcher, "--help"]); expect(executed.exitCode).toBe(0); expect(executed.stdout.toString()).toContain("Usage: clzmj-skills");
  expect(before).toContain(source + "/scripts/self-update.ts");
  expect(before).toContain("CLZMJ_SKILLS_SCOPE='project'");
  expect(readFileSync(settings, "utf8")).toBe("saved choices");
  let downloads = 0;
  const nextRuntime = runtime(); const fetcher = nextRuntime.fetch;
  nextRuntime.fetch = (async (...args: Parameters<typeof fetch>) => { downloads++; return fetcher(...args); }) as typeof fetch;
  expect(await selfUpdate({ ...options, source: source + "/" }, nextRuntime)).toBe(source + "/");
  expect(downloads).toBe(1);
  expect(readFileSync(launcher, "utf8")).toBe(before);
});
test("checksum, manifest, and new installer failure keep the working launcher", async () => {
  const options = location("failure"); registerLauncher({ ...options, source: "/previous/release" });
  const before = readFileSync(launcherPath(options), "utf8");
  await expect(selfUpdate(options, runtime({ ...built.manifest, sha256: "0".repeat(64) }))).rejects.toThrow("checksum");
  await expect(selfUpdate(options, runtime({ ...built.manifest, url: "https://untrusted.example/archive" }))).rejects.toThrow("manifest");
  await expect(selfUpdate(options, runtime(built.manifest, () => false))).rejects.toThrow("validation failed");
  expect(readFileSync(launcherPath(options), "utf8")).toBe(before);
  expect(existsSync(join(options.projectRoot, ".clzmj-skills/releases"))).toBe(false);
});
test("dry-run verifies but writes no project state or launcher", async () => {
  const options = location("dry"); await selfUpdate({ ...options, dryRun: true }, runtime());
  expect(existsSync(options.projectRoot)).toBe(false);
});
test("existing releases reject added files and tampering before launcher changes", async () => {
  const options = location("tampered"); const extracted = join(temporary, "extracted"); await extractArchive(archive, extracted);
  const source = stageRelease(extracted, built.manifest.version, built.manifest.sha256, options);
  writeFileSync(join(source, "unexpected"), "extra");
  expect(() => stageRelease(extracted, built.manifest.version, built.manifest.sha256, options)).toThrow("contents differ");
});
test("uninstall keeps modified paths and removes unchanged owned paths", async () => {
  const options = location("uninstall"); registerLauncher({ ...options, source: "/previous/release" });
  const owned = join(options.projectRoot, ".agents/skills/sample"); mkdirSync(join(options.projectRoot, ".agents/skills"), { recursive: true }); writeFileSync(owned, "original");
  const config = join(options.projectRoot, ".clzmj-skills/install.json");
  writeFileSync(config, JSON.stringify({ ownedPaths: { [owned]: fingerprint(owned) } }));
  writeFileSync(owned, "user edit"); uninstall(options);
  expect(existsSync(owned)).toBe(true); expect(existsSync(config)).toBe(true);
  writeFileSync(owned, "original"); uninstall(options);
  expect(existsSync(owned)).toBe(false); expect(existsSync(config)).toBe(false); expect(existsSync(launcherPath(options))).toBe(false);
});

test("uninstall rejects arbitrary recorded paths before removing anything", () => {
  const options = location("unsafe-uninstall"); registerLauncher({ ...options, source: "/previous/release" });
  const arbitrary = join(options.projectRoot, "private-file"); writeFileSync(arbitrary, "user data");
  writeFileSync(join(options.projectRoot, ".clzmj-skills/install.json"), JSON.stringify({ ownedPaths: { [arbitrary]: fingerprint(arbitrary) } }));
  expect(() => uninstall(options)).toThrow("Unsafe owned path"); expect(readFileSync(arbitrary, "utf8")).toBe("user data");
});

test("uninstall removes owned dangling agent links after canonical skill directories", () => {
  const options = location("links"); registerLauncher({ ...options, source: "/previous/release" });
  const canonical = join(options.projectRoot, ".agents/skills/rex"), link = join(options.projectRoot, ".claude/skills/rex");
  mkdirSync(canonical, { recursive: true }); mkdirSync(join(options.projectRoot, ".claude/skills"), { recursive: true });
  writeFileSync(join(canonical, "SKILL.md"), "skill"); symlinkSync(canonical, link);
  writeFileSync(join(options.projectRoot, ".clzmj-skills/install.json"), JSON.stringify({ ownedPaths: { [canonical]: fingerprint(canonical), [link]: fingerprint(link) } }));
  uninstall(options); expect(existsSync(canonical)).toBe(false); expect(() => lstatSync(link)).toThrow();
});
test("uninstall preserves an edited launcher even with its managed marker", () => {
  const options = location("edited-launcher"); registerLauncher({ ...options, source: "/previous/release" });
  const launcher = launcherPath(options), config = join(options.projectRoot, ".clzmj-skills/install.json");
  writeFileSync(config, JSON.stringify({ ownedPaths: {} })); writeFileSync(launcher, readFileSync(launcher, "utf8") + "# User changes\n");
  uninstall(options); expect(existsSync(launcher)).toBe(true); expect(existsSync(config)).toBe(true);
});
test("default smoke check executes both CLI and initializer help from the packaged archive", async () => {
  const options = location("default-smoke"); const mocked = runtime();
  const source = await selfUpdate(options, { fetch: mocked.fetch, log: () => {} });
  expect(existsSync(join(source, "scripts/init.ts"))).toBe(true);
});
test("launcher receipt commit failure restores the previous working launcher", () => {
  const options = location("receipt-failure"); registerLauncher({ ...options, source: "/previous/release" });
  const launcher = launcherPath(options), previous = readFileSync(launcher, "utf8"), receipt = join(options.projectRoot, ".clzmj-skills/launcher.sha256");
  rmSync(receipt); mkdirSync(receipt);
  expect(() => registerLauncher({ ...options, source: "/new/release" })).toThrow();
  expect(readFileSync(launcher, "utf8")).toBe(previous);
});
