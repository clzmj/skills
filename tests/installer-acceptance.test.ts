import { afterAll, expect, test } from "bun:test";
import { appendFileSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { buildRelease } from "../scripts/build-release";
import { installer } from "../distribution/installer";

// Boundary acceptance: real CLI/launcher subprocesses, disposable HOME, and a
// fake Skills CLI which records arguments and makes only fixture skill trees.
const root = fileURLToPath(new URL("../", import.meta.url));
const temporary = realpathSync(mkdtempSync(join(tmpdir(), "skills-acceptance-")));
afterAll(() => rmSync(temporary, { recursive: true, force: true }));
const built = buildRelease("acceptance-release", join(temporary, "build"));
const manifest = join(temporary, "manifest.json");
writeFileSync(manifest, JSON.stringify(built.manifest));
const preload = join(temporary, "fetch.ts");
writeFileSync(preload, `import { readFileSync, appendFileSync } from "node:fs";
globalThis.fetch = async (url) => {
  appendFileSync(process.env.FIXTURE_FETCH_LOG, String(url) + "\\n");
  if (String(url) === "https://skills.clzmj.dev/latest.json") {
    const value = JSON.parse(readFileSync(process.env.FIXTURE_MANIFEST, "utf8"));
    if (process.env.FIXTURE_BAD_CHECKSUM) value.sha256 = "0".repeat(64);
    return new Response(JSON.stringify(value));
  }
  if (String(url) === "https://skills.clzmj.dev/releases/acceptance-release/skills.tar.gz") return new Response(readFileSync(process.env.FIXTURE_ARCHIVE));
  throw new Error("Acceptance fixture forbids network: " + url);
};`);
const fake = join(temporary, "skills.ts");
writeFileSync(fake, `import { appendFileSync, existsSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { join, relative, dirname } from "node:path";
const args = process.argv.slice(2);
appendFileSync(process.env.FIXTURE_COMMAND_LOG, JSON.stringify({args,cwd:process.cwd()}) + "\\n");
if (args[0] !== "--bun" || args[1] !== "skills" || args[2] !== "add") process.exit(92);
const names = args.slice(args.indexOf("--skill") + 1).filter((_, i, tail) => i < tail.findIndex(value => value.startsWith("-")));
const agent = args[args.indexOf("-a") + 1];
for (const skill of names) {
const base = args.includes("-g") ? process.env.HOME : process.cwd();
const canonical = join(base, ".agents/skills", skill);
const destination = agent === "codex" ? canonical : join(base, ".claude/skills", skill);
const copy = args.includes("--copy");
const target = copy ? destination : canonical;
mkdirSync(target, {recursive:true}); writeFileSync(join(target,"SKILL.md"), "fixture " + skill + "\\n");
if (process.env.FIXTURE_FAIL_AGENT === agent) process.exit(17);
if (!copy && destination !== canonical) { mkdirSync(dirname(destination), {recursive:true}); rmSync(destination, {force:true,recursive:true}); symlinkSync(relative(dirname(destination),canonical), destination); }
}
`);

function fixture(name: string) {
  const base = join(temporary, name), home = join(base, "home"), project = join(base, "project"), bin = join(base, "bin");
  for (const path of [home, project, bin, join(base, "tmp")]) mkdirSync(path, { recursive: true });
  const quote = (s: string) => "'" + s.replaceAll("'", "'\\''") + "'";
  writeFileSync(join(bin, "bun"), `#!/bin/sh\nexec ${quote(process.execPath)} --preload ${quote(preload)} "$@"\n`, { mode: 0o755 });
  writeFileSync(join(bin, "bunx"), `#!/bin/sh\nexec ${quote(process.execPath)} ${quote(fake)} "$@"\n`, { mode: 0o755 });
  for (const name of ["codex", "claude", "uv", "git"]) writeFileSync(join(bin, name), "#!/bin/sh\necho 'Unexpected external command' >&2\nexit 93\n", { mode: 0o755 });
  const log = join(base, "commands.jsonl"), fetchLog = join(base, "fetch.log");
  const env = { HOME: home, PATH: bin + ":/usr/bin:/bin", TMPDIR: join(base, "tmp"),
    BUN_RUNTIME_TRANSPILER_CACHE_PATH: join(base, "bun-transpile"),
    CODEX_HOME: join(home, ".codex"), CLAUDE_CONFIG_DIR: join(home, ".claude"),
    XDG_CONFIG_HOME: join(home, ".config"), XDG_STATE_HOME: join(home, ".state"),
    PYTHONDONTWRITEBYTECODE: "1", PYTHONUSERBASE: join(base, "python"), PYTHONPYCACHEPREFIX: join(base, "pycache"),
    FIXTURE_COMMAND_LOG: log, FIXTURE_FETCH_LOG: fetchLog, FIXTURE_MANIFEST: manifest, FIXTURE_ARCHIVE: built.archivePath,
    CLZMJ_SKILLS_SOURCE: root, DISABLE_TELEMETRY: "1", DO_NOT_TRACK: "1" };
  const run = (args: string[], extra = {}) => Bun.spawnSync(args, { cwd: project, env: { ...env, ...extra }, timeout: 20_000 });
  const commands = () => existsSync(log) ? readFileSync(log, "utf8").trim().split("\n").filter(Boolean).map(line => JSON.parse(line)) : [];
  return { base, home, project, bin, env, run, commands, fetchLog };
}

for (const scope of ["global", "project"] as const) for (const method of ["copy", "symlink"] as const) {
  test(`real subprocess ${scope} ${method}: both agents, preserve existing, saved update, launcher`, () => {
    const f = fixture(scope + "-" + method);
    const args = ["--yes", "--scope", scope, "--project-dir", f.project, "--method", method, "--agents", "codex", "claude-code", "--core-only"];
    const installed = f.run([process.execPath, join(root, "scripts/init.ts"), ...args]);
    expect(installed.stderr.toString()).toBe(""); expect(installed.exitCode).toBe(0);
    const base = scope === "global" ? f.home : f.project;
    const config = scope === "global" ? join(f.home, ".config/clzmj-skills/install.json") : join(f.project, ".clzmj-skills/install.json");
    const launcher = scope === "global" ? join(f.home, ".local/bin/clzmj-skills") : join(f.project, ".clzmj-skills/bin/clzmj-skills");
    expect(f.commands()).toHaveLength(2);
    for (const command of f.commands()) {
      expect(command.cwd).toBe(f.project);
      expect(command.args.slice(command.args.indexOf("--skill") + 1, command.args.indexOf("--skill") + 3)).toEqual(["rex", "python-tooling"]);
      expect(command.args.includes("-g")).toBe(scope === "global");
      expect(command.args.includes("--copy")).toBe(method === "copy");
    }
    for (const skill of ["rex", "python-tooling"]) {
      expect(existsSync(join(base, ".agents/skills", skill, "SKILL.md"))).toBe(true);
      expect(existsSync(join(base, ".claude/skills", skill, "SKILL.md"))).toBe(true);
      expect(lstatSync(join(base, ".claude/skills", skill)).isSymbolicLink()).toBe(method === "symlink");
    }
    if (scope === "project") expect(readdirSync(f.home, { recursive: true })).toEqual([]);
    const saved = JSON.parse(readFileSync(config, "utf8")).options;
    const unchanged = f.run([launcher, "install", ...args]); expect(unchanged.exitCode).toBe(0); expect(f.commands()).toHaveLength(2);
    const updated = f.run([launcher, "update", "--yes"]); expect(updated.stderr.toString()).toBe(""); expect(updated.exitCode).toBe(0);
    expect(f.commands()).toHaveLength(4);
    const restored = JSON.parse(readFileSync(config, "utf8")).options;
    for (const key of ["agents", "scope", "projectDir", "method", "core", "favorites", "hooks"]) expect(restored[key]).toEqual(saved[key]);
    if (scope === "project") expect(readdirSync(f.home)).toEqual([]);
    if (method !== "copy") return;
    const configBefore = readFileSync(config, "utf8"), launcherBefore = readFileSync(launcher, "utf8");
    const failed = f.run([launcher, "self-update"], { FIXTURE_BAD_CHECKSUM: "1" }); expect(failed.exitCode).toBe(1);
    expect(readFileSync(launcher, "utf8")).toBe(launcherBefore);
    const dry = f.run([launcher, "self-update", "--dry-run"]); expect(dry.exitCode).toBe(0); expect(readFileSync(launcher, "utf8")).toBe(launcherBefore);
    const self = f.run([launcher, "self-update"]); expect(self.stderr.toString()).toBe(""); expect(self.exitCode).toBe(0);
    expect(readFileSync(launcher, "utf8")).toContain("acceptance-release/scripts/self-update.ts");
    expect(readFileSync(config, "utf8")).toBe(configBefore); expect(f.commands()).toHaveLength(4);
    const current = f.run([launcher, "self-update"]); expect(current.exitCode).toBe(0); expect(current.stdout.toString()).toContain("already current");
    const owned = join(base, ".agents/skills/rex/SKILL.md"); appendFileSync(owned, "user edit\n");
    const removal = f.run([launcher, "uninstall"]); expect(removal.exitCode).toBe(0); expect(existsSync(owned)).toBe(true); expect(existsSync(config)).toBe(true);
  });
}

test("existing canonical skill gains only a missing agent link and remains unowned", () => {
  const f = fixture("existing-canonical"), canonical = join(f.home, ".agents/skills/rex"), linked = join(f.home, ".claude/skills/rex");
  mkdirSync(canonical, { recursive: true }); writeFileSync(join(canonical, "SKILL.md"), "user-owned core\n");
  const result = f.run([process.execPath, join(root, "scripts/init.ts"), "--yes", "--agents", "codex", "claude-code", "--core", "rex", "--core-only", "--method", "symlink"]);
  expect(result.stderr.toString()).toBe(""); expect(result.exitCode).toBe(0);
  expect(f.commands()).toEqual([]); expect(lstatSync(linked).isSymbolicLink()).toBe(true);
  expect(readFileSync(join(canonical, "SKILL.md"), "utf8")).toBe("user-owned core\n");
  const config = join(f.home, ".config/clzmj-skills/install.json"), record = JSON.parse(readFileSync(config, "utf8"));
  expect(Object.keys(record.ownedPaths)).toEqual([linked]);
  const removed = f.run([join(f.home, ".local/bin/clzmj-skills"), "uninstall"]);
  expect(removed.exitCode).toBe(0); expect(existsSync(linked)).toBe(false);
  expect(readFileSync(join(canonical, "SKILL.md"), "utf8")).toBe("user-owned core\n");
});

test("partial install fails without a launcher and explicit saved update can complete", () => {
  const f = fixture("partial-failure"), args = ["--yes", "--scope", "project", "--project-dir", f.project, "--agents", "codex", "claude-code", "--core-only"];
  const failed = f.run([process.execPath, join(root, "scripts/init.ts"), ...args], { FIXTURE_FAIL_AGENT: "codex" });
  expect(failed.exitCode).toBe(1); expect(failed.stdout.toString()).toContain("failed");
  const launcher = join(f.project, ".clzmj-skills/bin/clzmj-skills"); expect(existsSync(launcher)).toBe(false);
  const completed = f.run([process.execPath, join(root, "scripts/init.ts"), ...args, "--update"]);
  expect(completed.exitCode).toBe(0); expect(existsSync(launcher)).toBe(true);
  expect(existsSync(join(f.project, ".agents/skills/python-tooling/SKILL.md"))).toBe(true);
  expect(readdirSync(f.home)).toEqual([]);
});

test("hosted shell pipeline controlling terminal cancellation and EOF make no persistent writes", () => {
  const f = fixture("terminal");
  const shell = join(f.base, "install.sh"); writeFileSync(shell, installer());
  const python = Bun.which("python3"); expect(python).not.toBeNull();
  const result = f.run([python!, join(root, "tests/installer-pty.py"), shell, f.project]);
  expect(result.stderr.toString()).toBe(""); expect(result.exitCode).toBe(0);
  expect(result.stdout.toString()).toContain("cancel and EOF passed");
  expect(f.commands()).toEqual([]); expect(readdirSync(f.home)).toEqual([]);
});
