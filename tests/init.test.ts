import { describe, expect, test } from "bun:test";
import { readFileSync, mkdtempSync, rmSync, mkdirSync, symlinkSync, openSync, closeSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { initialize, parseManifest, parseOptions, parseState, buildPlan, resolveCoreSource, detectAgents, skillDirectory, settingsPath, readTerminalAnswer, type Runtime, type Options, type InstallRecord } from "../scripts/init";

const manifest = JSON.parse(readFileSync(new URL("../favorites.json", import.meta.url), "utf8"));
const options = (args: string[] = []) => parseOptions(["--agents", "codex", "claude-code", "--yes", ...args]);
function mock(existing = new Set<string>()) {
  const logs: string[] = [], calls: { command: string[]; capture: boolean; cwd?: string }[] = [];
  const saved: { path: string; record: InstallRecord }[] = [];
  const runtime: Runtime = {
    which: () => "/bin/tool", exists: path => existing.has(path), log: line => logs.push(line),
    discover: async () => [{ name: "tdd", description: "Test first" }, { name: "research", description: "Research primary sources" }],
    run(command, capture, cwd) {
      calls.push({ command, capture, cwd });
      if (capture) return command[0] === "codex" ? JSON.stringify(command.includes("marketplace") ? { marketplaces: [] } : { installed: [] }) : "[]";
      return "";
    },
    save: (path, record) => saved.push({ path, record }), fingerprint: () => "a".repeat(64), link: (_source, destination) => existing.add(destination),
  };
  return { logs, calls, runtime, saved, existing };
}

describe("installer choices and plans", () => {
  test("defaults to detected agents, global symlink, core-only and opt-in hooks", () => {
    const parsed = parseOptions([], ["codex"], "/project");
    expect(parsed).toMatchObject({ agents: ["codex"], scope: "global", method: "symlink", core: ["rex", "python-tooling"], favorites: [], hooks: false, yes: false, projectDir: "/project" });
    expect(detectAgents(name => name === "claude" ? "/claude" : null, () => false)).toEqual(["claude-code"]);
    expect(parseOptions(["--agents", "codex", "-y"])).toMatchObject({ agents: ["codex"], yes: true });
    expect(parseOptions(["--core", "none", "-y"])).toMatchObject({ core: [], yes: true });
    expect(() => parseOptions(["--agents"])).toThrow();
    expect(() => parseOptions(["--agents", "other"])).toThrow();
    expect(() => parseOptions(["--method", "hardlink"])).toThrow();
    expect(() => parseOptions(["--scope", "project", "--yes"])).toThrow("--project-dir");
    expect(() => parseOptions(["--project-dir", "relative"])).toThrow("absolute");
    expect(() => parseOptions(["--wat"])).toThrow();
  });
  test("uses verified copy flags, selected hosts and exact project cwd without global options", async () => {
    const dir = mkdtempSync(resolve(tmpdir(), "skills-project-"));
    try {
      const { runtime, calls } = mock();
      await initialize(manifest, options(["--scope", "project", "--project-dir", dir, "--all-favorites", "--hooks", "--method", "copy"]), runtime);
      expect(calls.length).toBe(8); // Four ordinary sources, one batch per selected agent
      expect(calls.every(call => call.cwd === dir && !call.capture && call.command.includes("--copy") && !call.command.includes("-g") && call.command[1] !== "plugin" && call.command[0] !== "uv")).toBe(true);
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
  test("symlink method omits --copy, native plugins retain complete host packaging", () => {
    const chosen = options(["--all-favorites", "--method", "symlink"]);
    const plan = buildPlan(parseManifest(manifest).filter(f => f.type === "plugin" || !f.skills.includes("*")), chosen, {}, "clzmj/skills", () => false);
    expect(plan.steps.filter(s => s.kind === "skill").every(s => !s.command.includes("--copy") && s.command.includes("-g"))).toBe(true);
    expect(plan.steps.map(s => s.command)).toContainEqual(["codex", "plugin", "add", "egm@egm"]);
    expect(plan.steps.map(s => s.command)).toContainEqual(["claude", "plugin", "install", "ponytail@ponytail"]);
  });
  test("retains disabled, pinned and curated-alias native installations even on explicit skill update", () => {
    const state = parseState("codex", { installed: [{ pluginId: "egm@egm", enabled: false, version: "0.1" }, { pluginId: "engineering-suite-ponytail@openai-curated-remote", enabled: false }] }, { marketplaces: [] });
    const plan = buildPlan(parseManifest(manifest).filter(f => f.type === "plugin"), options(["--update"]), { codex: state }, "clzmj/skills", () => false);
    expect(plan.steps.filter(s => s.command[0] === "codex" && s.kind === "plugin")).toEqual([]);
    expect(plan.skipped.filter(s => s.includes("already-installed"))).toHaveLength(2);
    expect(() => parseState("codex", {}, {})).toThrow("Unexpected codex");
  });
  test("preserves existing skills per host and does not silently overwrite shared symlink canonical paths", () => {
    const chosen = options(["--method", "copy"]);
    const codexRex = resolve(skillDirectory("codex", chosen), "rex");
    const plan = buildPlan([], chosen, {}, "clzmj/skills", path => path === codexRex);
    expect(plan.steps.some(s => s.command.includes("rex") && s.command.includes("claude-code"))).toBe(true);
    expect(plan.steps.some(s => s.command.includes("rex") && s.command.includes("codex"))).toBe(false);
    expect(plan.skipped[0]).toContain("already-installed");
    const linked = buildPlan([], { ...chosen, method: "symlink" }, {}, "clzmj/skills", path => path === codexRex || path === resolve(codexRex, "SKILL.md"));
    expect(linked.steps.some(step => step.link?.source === codexRex && step.link.destination.endsWith(".claude/skills/rex"))).toBe(true);
    const updated = buildPlan([], { ...chosen, update: true }, {}, "clzmj/skills", () => true);
    expect(updated.steps.filter(s => s.kind === "skill")).toHaveLength(2);
  });
  test("adding a symlink agent preserves canonical contents and owns only the new link", async () => {
    const chosen = options(["--agents", "claude-code", "--core", "rex", "--method", "symlink"]);
    const canonical = resolve(skillDirectory("codex", chosen), "rex");
    const destination = resolve(skillDirectory("claude-code", chosen), "rex");
    const fixture = mock(new Set([canonical, resolve(canonical, "SKILL.md")]));
    await initialize(manifest, chosen, fixture.runtime);
    expect(fixture.calls).toEqual([]);
    expect(fixture.existing.has(destination)).toBe(true);
    expect(fixture.saved[0].record.ownedPaths).toEqual({ [destination]: "a".repeat(64) });
  });
  test("preserves broken symlinks by default", () => {
    const dir = mkdtempSync(resolve(tmpdir(), "skills-broken-"));
    try {
      mkdirSync(resolve(dir, ".agents/skills"), { recursive: true });
      symlinkSync(resolve(dir, "missing"), resolve(dir, ".agents/skills/rex"));
      const plan = buildPlan([], options(["--scope", "project", "--project-dir", dir]), {}, "clzmj/skills");
      expect(plan.skipped.some(line => line.includes("rex for codex"))).toBe(true);
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
  test("independent hook choice can install hooks without ordinary core skills", () => {
    const plan = buildPlan([], options(["--core", "none", "--hooks"]), {}, "clzmj/skills", () => false);
    expect(plan.steps).toHaveLength(2);
    expect(plan.steps.every(s => s.kind === "hook" && s.command[0] === "uv" && s.command[3].includes("install_hooks.py"))).toBe(true);
  });
});

describe("safe installer execution", () => {
  test("dry run expands wildcard into concrete names and never spawns or persists", async () => {
    const { runtime, calls, logs, saved } = mock();
    runtime.run = () => { throw new Error("spawned"); };
    runtime.prepare = () => { throw new Error("prepared release"); };
    await initialize(manifest, options(["--all-favorites", "--dry-run"]), runtime);
    expect(calls).toEqual([]); expect(saved).toEqual([]);
    expect(logs.some(s => s.includes("discovered 2 skills"))).toBe(true);
    expect(logs.some(s => s.includes("--skill tdd"))).toBe(true);
    expect(logs.some(s => s.includes("--skill '*'"))).toBe(false);
    expect(logs.some(s => s.includes("no subprocesses or writes"))).toBe(true);
  });
  test("noninteractive execution requires explicit --yes and rejects before discovery or inventories", async () => {
    const { runtime, calls } = mock();
    runtime.discover = async () => { throw new Error("discovered"); };
    await expect(initialize(manifest, parseOptions(["--agents", "codex", "--all-favorites"]), runtime)).rejects.toThrow("No controlling terminal");
    expect(calls).toEqual([]);
  });
  test("interactive preview is default-no and no installation mutations happen before approval", async () => {
    const { runtime, calls, logs, saved } = mock();
    const answers = ["codex", "global", "copy", "rex", "none", "no", "no"];
    runtime.ask = (question, fallback) => { if (question.startsWith("Install this plan")) expect(logs.some(s => s.includes("Installation preview"))).toBe(true); return answers.shift() ?? fallback; };
    runtime.prepare = () => { throw new Error("mutation before confirmation"); };
    await initialize(manifest, parseOptions([], ["codex"]), runtime);
    expect(calls).toEqual([]); expect(saved).toEqual([]);
    expect(logs.at(-1)).toContain("Cancelled");
  });
  test("interactive wildcard selection shows count and accepts individual skills", async () => {
    const { runtime, calls } = mock();
    const answers = ["codex", "global", "copy", "none", "mattpocock/skills", "no", "tdd", "yes"];
    runtime.ask = (_question, fallback) => answers.shift() ?? fallback;
    await initialize(manifest, parseOptions([], ["codex"]), runtime);
    expect(calls).toHaveLength(1);
    expect(calls[0].command).toContain("tdd");
    expect(calls[0].command).not.toContain("research");
  });
  test("reads all native inventories before any writes and reports failed commands with exact retry", async () => {
    const { runtime, calls, logs } = mock();
    const run = runtime.run;
    runtime.run = (command, capture, cwd) => { const output = run(command, capture, cwd); if (!capture) throw new Error("install failed"); return output; };
    await expect(initialize(manifest, options(["--all-favorites"]), runtime)).rejects.toThrow("installation command(s) failed");
    expect(calls.slice(0, 4).every(call => call.capture)).toBe(true);
    expect(logs.some(s => s.startsWith("Retry from "))).toBe(true);
    expect(logs.some(s => s.includes("failed."))).toBe(true);
  });
  test("hosted failure retry uses the durable staged installer path", async () => {
    const { runtime, logs } = mock();
    runtime.prepare = () => "/durable/release";
    runtime.run = () => { throw new Error("failed install"); };
    await expect(initialize(manifest, options(), runtime, undefined, "/temporary/extraction")).rejects.toThrow("installation command(s) failed");
    expect(logs.some(line => line.startsWith("Installer retry") && line.includes("/durable/release/scripts/init.ts"))).toBe(true);
    expect(logs.some(line => line.startsWith("Installer retry") && line.includes("/temporary/extraction"))).toBe(false);
  });
  test("validates full manifest, selections and required tools before commands", async () => {
    const { runtime, calls } = mock();
    await expect(initialize({ favorites: [...manifest.favorites, { type: "skills", source: "bad", skills: ["ok"] }] }, options(), runtime)).rejects.toThrow("Invalid favorites entry");
    await expect(initialize(manifest, options(["--favorites", "other/repo"]), runtime)).rejects.toThrow("Unknown favorite");
    runtime.which = name => name === "uv" ? null : "/tool";
    await expect(initialize(manifest, options(["--hooks"]), runtime)).rejects.toThrow("Missing required tools: uv");
    expect(calls).toEqual([]);
    expect(() => parseManifest({ favorites: [{ type: "skills", source: "a/b", skills: ["*", "foo"] }] })).toThrow();
  });
  test("persists only newly created ownership and refreshes only previously owned paths", async () => {
    const chosen = options(["--agents", "codex", "--core", "rex"]);
    const { runtime, saved, existing } = mock();
    const path = resolve(skillDirectory("codex", chosen), "rex");
    runtime.run = () => { existing.add(path); return ""; };
    await initialize(manifest, chosen, runtime);
    expect(saved[0].record.ownedPaths).toEqual({ [path]: "a".repeat(64) });
    const another = mock(new Set([path]));
    await initialize(manifest, { ...chosen, update: true }, another.runtime);
    expect(another.saved[0].record.ownedPaths).toEqual({});
  });
  test("explicit update loads saved selections without resetting them to defaults", async () => {
    const { runtime, calls } = mock();
    const savedOptions = options(["--agents", "claude-code", "--core", "rex", "--favorites", "UditAkhourii/adhd", "--method", "copy"]);
    runtime.load = () => ({ version: 1, options: savedOptions, ownedPaths: {} });
    await initialize(manifest, parseOptions(["--yes", "--update"], ["codex"]), runtime);
    expect(calls).toHaveLength(2);
    expect(calls.every(c => c.command.includes("claude-code") && c.command.includes("--copy"))).toBe(true);
    expect(calls.some(c => c.command.includes("adhd"))).toBe(true);
  });
  test("saved all-favorites becomes exact selections and narrowing update prunes inherited pack choices", async () => {
    const first = mock();
    await initialize(manifest, options(["--all-favorites"]), first.runtime);
    expect(first.saved[0].record.options.favorites).toEqual(manifest.favorites.map((f: any) => f.source));
    const second = mock();
    second.runtime.load = () => first.saved[0].record;
    await initialize(manifest, parseOptions(["--yes", "--update", "--core-only", "--core", "rex", "--method", "symlink", "--agents", "codex"]), second.runtime);
    expect(second.calls).toHaveLength(1);
    expect(second.calls[0].command).not.toContain("--copy");
    expect(second.saved[0].record.options).toMatchObject({ favorites: [], packSkills: {}, core: ["rex"], agents: ["codex"], method: "symlink" });
  });
  test("terminal EOF aborts even when defaults exist", () => {
    const dir = mkdtempSync(resolve(tmpdir(), "skills-eof-"));
    try {
      const path = resolve(dir, "input");
      writeFileSync(path, ""); const empty = openSync(path, "r");
      try { expect(() => readTerminalAnswer(empty, "yes")).toThrow("Terminal input ended"); } finally { closeSync(empty); }
      writeFileSync(path, "\n"); const newline = openSync(path, "r");
      try { expect(readTerminalAnswer(newline, "no")).toBe("no"); } finally { closeSync(newline); }
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
  test("hosted source validation only accepts the current extracted release", () => {
    const release = new URL("../", import.meta.url).pathname.replace(/\/$/, "");
    expect(resolveCoreSource(undefined)).toBe("clzmj/skills");
    expect(resolveCoreSource(release)).toBe(release);
    expect(() => resolveCoreSource("relative")).toThrow("extracted release root");
    expect(() => resolveCoreSource("/tmp")).toThrow("extracted release root");
  });
});
