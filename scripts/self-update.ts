import { cpSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readlinkSync, readdirSync, renameSync, rmSync, writeFileSync, chmodSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { extractArchive } from "../distribution/archive";

const origin = "https://skills.clzmj.dev";
const root = fileURLToPath(new URL("../", import.meta.url));
type Location = { scope: "global" | "project"; projectRoot: string; dryRun?: boolean; source?: string };
const shellQuote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";
export function launcherPath(options: Location) { return options.scope === "global" ? join(homedir(), ".local/bin/clzmj-skills") : join(options.projectRoot, ".clzmj-skills/bin/clzmj-skills"); }
const pathPresent = (path: string) => { try { lstatSync(path); return true; } catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return false; throw error; } };
function launcherReceipt(options: Location) { return options.scope === "global" ? join(homedir(), ".local/share/clzmj-skills/launcher.sha256") : join(options.projectRoot, ".clzmj-skills/launcher.sha256"); }
function releaseBase(options: Location) { return options.scope === "global" ? join(homedir(), ".local/share/clzmj-skills/releases") : join(options.projectRoot, ".clzmj-skills/releases"); }
export function fingerprint(path: string): string {
  const hash = createHash("sha256");
  function visit(current: string, relative: string) {
    const info = lstatSync(current);
    if (info.isSymbolicLink()) { hash.update("link:" + relative + ":" + readlinkSync(current)); }
    else if (info.isDirectory()) { hash.update("dir:" + relative); for (const name of readdirSync(current).sort()) visit(join(current, name), relative + "/" + name); }
    else if (info.isFile()) hash.update("file:" + relative + ":" + (info.mode & 0o777) + ":").update(readFileSync(current));
    else throw new Error("Unsupported installed file: " + current);
  }
  visit(path, ""); return hash.digest("hex");
}
export function stageRelease(source: string, version: string, sha256: string, options: Location): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(version) || version.includes("..") || !/^[a-f0-9]{64}$/.test(sha256)) throw new Error("Invalid release metadata");
  const base = releaseBase(options), destination = join(base, version);
  if (options.dryRun) return source;
  if (existsSync(destination)) {
    if (lstatSync(destination).isSymbolicLink() || !lstatSync(destination).isDirectory() || readFileSync(join(destination, ".release-sha256"), "utf8") !== sha256) throw new Error("Existing release directory does not match this release");
    // Compare exact packaged contents, including unexpected additions, without the local marker.
    const verify = (expected: string, actual: string) => {
      const names = readdirSync(expected).sort();
      if (JSON.stringify(names) !== JSON.stringify(readdirSync(actual).filter(name => actual !== destination || name !== ".release-sha256").sort())) throw new Error("Existing release contents differ");
      for (const name of names) { const a = join(expected, name), b = join(actual, name); if (lstatSync(b).isSymbolicLink()) throw new Error("Unexpected symlink in existing release"); if (lstatSync(a).isDirectory()) { if (!lstatSync(b).isDirectory()) throw new Error("Existing release contents differ"); verify(a, b); } else if (!readFileSync(a).equals(readFileSync(b)) || (lstatSync(a).mode & 0o777) !== (lstatSync(b).mode & 0o777)) throw new Error("Existing release file differs: " + name); }
    };
    verify(source, destination); return destination;
  }
  mkdirSync(base, { recursive: true });
  const staging = mkdtempSync(join(base, ".install-"));
  try { cpSync(source, join(staging, "release"), { recursive: true, errorOnExist: true }); writeFileSync(join(staging, "release/.release-sha256"), sha256); renameSync(join(staging, "release"), destination); }
  finally { rmSync(staging, { recursive: true, force: true }); }
  return destination;
}
export function prepareHostedRelease(options: Location): string {
  const source = options.source ?? process.env.CLZMJ_SKILLS_SOURCE ?? root;
  if (!process.env.CLZMJ_SKILLS_VERSION || !process.env.CLZMJ_SKILLS_SHA256) return source;
  return stageRelease(source, process.env.CLZMJ_SKILLS_VERSION, process.env.CLZMJ_SKILLS_SHA256, options);
}
export function registerLauncher(options: Location): string | undefined {
  if (options.dryRun) return;
  const source = options.source ?? process.env.CLZMJ_SKILLS_SOURCE ?? root;
  const path = launcherPath(options);
  const body = `#!/bin/sh\n# Managed by clzmj-skills\nexport CLZMJ_SKILLS_SCOPE=${shellQuote(options.scope)}\nexport CLZMJ_SKILLS_PROJECT=${shellQuote(resolve(options.projectRoot))}\nexec bun ${shellQuote(join(source, "scripts/self-update.ts"))} "$@"\n`;
  if (existsSync(path) && !readFileSync(path, "utf8").includes("# Managed by clzmj-skills\n")) throw new Error("Refusing to replace an unmanaged launcher: " + path);
  const receipt = launcherReceipt(options);
  mkdirSync(dirname(path), { recursive: true }); mkdirSync(dirname(receipt), { recursive: true });
  const temporary = mkdtempSync(join(dirname(path), ".clzmj-skills-"));
  let receiptTemporary: string | undefined;
  try {
    receiptTemporary = mkdtempSync(join(dirname(receipt), ".launcher-"));
    const staged = join(temporary, "launcher"), previous = join(temporary, "previous"), stagedReceipt = join(receiptTemporary, "sha256");
    writeFileSync(staged, body, { mode: 0o755 }); chmodSync(staged, 0o755);
    writeFileSync(stagedReceipt, createHash("sha256").update(body).digest("hex"));
    const hadLauncher = pathPresent(path);
    if (hadLauncher) cpSync(path, previous, { dereference: false });
    renameSync(staged, path);
    try { renameSync(stagedReceipt, receipt); }
    catch (error) { if (hadLauncher) renameSync(previous, path); else rmSync(path); throw error; }
  } finally { rmSync(temporary, { recursive: true, force: true }); if (receiptTemporary) rmSync(receiptTemporary, { recursive: true, force: true }); }
  return path;
}
type UpdateRuntime = { fetch: typeof fetch; check: (source: string) => boolean; log: (message: string) => void };
export async function selfUpdate(options: Location, overrides: Partial<UpdateRuntime> = {}): Promise<string> {
  const runtime: UpdateRuntime = { fetch: globalThis.fetch, check: source => ["self-update.ts", "init.ts"].every(script => Bun.spawnSync([process.execPath, join(source, "scripts", script), "--help"]).exitCode === 0), log: console.log, ...overrides };
  const response = await runtime.fetch(origin + "/latest.json", { redirect: "error" });
  if (!response.ok) throw new Error("Release manifest download failed: " + response.status);
  const { version, sha256, url } = await response.json() as any;
  if (typeof version !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(version) || version.includes("..") || !/^[a-f0-9]{64}$/.test(sha256) || url !== `/releases/${version}/skills.tar.gz`) throw new Error("Invalid release manifest");
  const current = options.source ?? root;
  if (resolve(current) === join(releaseBase(options), version) && existsSync(join(current, ".release-sha256")) && readFileSync(join(current, ".release-sha256"), "utf8") === sha256) { runtime.log("Installer is already current (" + version + ")."); return current; }
  const archiveResponse = await runtime.fetch(origin + url, { redirect: "error" });
  if (!archiveResponse.ok) throw new Error("Release download failed: " + archiveResponse.status);
  if (Number(archiveResponse.headers.get("content-length")) > 16 * 1024 * 1024) throw new Error("Release is too large");
  const archive = new Uint8Array(await archiveResponse.arrayBuffer());
  if (archive.length > 16 * 1024 * 1024 || createHash("sha256").update(archive).digest("hex") !== sha256) throw new Error("Release checksum mismatch");
  const temporary = mkdtempSync(join(tmpdir(), "clzmj-skills-update-"));
  try {
    const extracted = join(temporary, "release"); await extractArchive(archive, extracted);
    if (!runtime.check(extracted)) throw new Error("New installer validation failed; previous launcher retained");
    const source = stageRelease(extracted, version, sha256, options);
    registerLauncher({ ...options, source });
    runtime.log(options.dryRun ? "Self-update dry run: verified release; no persistent writes." : "Installer updated to " + version + ". Run clzmj-skills update explicitly to update your selected skills.");
    return source;
  } finally { rmSync(temporary, { recursive: true, force: true }); }
}
export const help = `Usage: clzmj-skills install [OPTIONS]\n       clzmj-skills update [OPTIONS]\n       clzmj-skills self-update [--dry-run]\n       clzmj-skills uninstall [--dry-run]\n       clzmj-skills help\n\ninstall configures selected skills; update reuses saved selections.\nself-update updates only this installer, preserving settings and installed skills.\nGlobal launcher: ~/.local/bin/clzmj-skills (add ~/.local/bin to PATH).\nProject launcher: .clzmj-skills/bin/clzmj-skills.\nRun install --help for installation options.`;
export function uninstall(options: Location) {
  const config = options.scope === "global" ? join(homedir(), ".config/clzmj-skills/install.json") : join(options.projectRoot, ".clzmj-skills/install.json");
  if (!existsSync(config)) throw new Error("No saved installation found");
  const state = JSON.parse(readFileSync(config, "utf8"));
  const base = options.scope === "global" ? homedir() : resolve(options.projectRoot);
  const allowed = [".agents/skills", ".codex/skills", ".claude/skills"].map(path => resolve(base, path));
  if (options.scope === "global" && process.env.CLAUDE_CONFIG_DIR) allowed.push(resolve(process.env.CLAUDE_CONFIG_DIR, "skills"));
  for (const path of Object.keys(state.ownedPaths ?? {})) {
    const absolute = resolve(path);
    if (absolute !== path || !allowed.some(directory => dirname(absolute) === directory)) throw new Error("Unsafe owned path in installation settings: " + path);
    let parent = dirname(absolute);
    const boundary = absolute.startsWith(base + "/") ? base : dirname(resolve(process.env.CLAUDE_CONFIG_DIR!));
    while (parent !== boundary && parent !== dirname(parent)) { if (existsSync(parent) && lstatSync(parent).isSymbolicLink()) throw new Error("Unsafe symlink parent in owned path: " + path); parent = dirname(parent); }
  }
  const launcher = launcherPath(options), receipt = launcherReceipt(options);
  if (pathPresent(launcher) && (!existsSync(receipt) || lstatSync(launcher).isSymbolicLink() || createHash("sha256").update(readFileSync(launcher)).digest("hex") !== readFileSync(receipt, "utf8"))) {
    console.log("Preserving modified or unverified launcher and settings: " + launcher); return;
  }
  let skipped = false;
  for (const [path, expected] of Object.entries(state.ownedPaths ?? {})) {
    if (!pathPresent(path)) continue;
    if (typeof expected !== "string" || fingerprint(path) !== expected) { console.log("Preserving modified or unverified path: " + path); skipped = true; continue; }
    console.log("Remove " + path); if (!options.dryRun) rmSync(path, { recursive: true });
  }
  if (skipped) { console.log("Retaining settings and launcher for paths needing review."); return; }
  if (!options.dryRun) { if (pathPresent(launcher)) rmSync(launcher); if (existsSync(receipt)) rmSync(receipt); rmSync(config); }
  console.log(options.dryRun ? "Uninstall dry run: no writes." : "Removed owned skills, launcher and settings. Hooks/plugins and cached releases are retained.");
}
if (import.meta.main) {
  try {
    const args = process.argv.slice(2), command = args[0] ?? "help";
    const options: Location = { scope: process.env.CLZMJ_SKILLS_SCOPE === "project" ? "project" : "global", projectRoot: process.env.CLZMJ_SKILLS_PROJECT ?? process.cwd(), source: root, dryRun: args.includes("--dry-run") };
    if (["help", "--help", "-h"].includes(command)) console.log(help);
    else if (["self-update", "uninstall"].includes(command)) { if (args.slice(1).some(arg => arg !== "--dry-run")) throw new Error("Unsupported " + command + " option"); if (command === "self-update") await selfUpdate(options); else uninstall(options); }
    else if (["install", "update"].includes(command)) {
      const forwarded = [...args.slice(1)];
      if (options.scope === "project" && !forwarded.includes("--scope")) forwarded.push("--scope", "project", "--project-dir", options.projectRoot);
      if (command === "update") forwarded.push("--update");
      const result = Bun.spawnSync([process.execPath, join(root, "scripts/init.ts"), ...forwarded], { env: { ...process.env, CLZMJ_SKILLS_SOURCE: root }, stdin: "inherit", stdout: "inherit", stderr: "inherit" });
      if (result.error) throw result.error; process.exitCode = result.exitCode;
    } else throw new Error("Unknown command: " + command);
  } catch (error) { console.error(error instanceof Error ? error.message : error); process.exitCode = 1; }
}
