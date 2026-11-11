import { describe, expect, test } from "bun:test";
import { TerminalBack, TerminalCancelled } from "../scripts/terminal-ui";
import { readFileSync, mkdtempSync, rmSync, mkdirSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { initialize, parseManifest, parseOptions, parseState, buildPlan, resolveCoreSource, detectAgents, skillDirectory, settingsPath, runSubprocess, type PageUI, type Runtime, type Options, type InstallRecord } from "../scripts/init";

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

function pages(overrides: Partial<PageUI> = {}): PageUI {
  return { multiselect: (_title, _rows, defaults) => defaults, select: (_title, _rows, defaultValue) => defaultValue,
    directory: (_title, start) => start, confirm: (_title, defaultValue = false) => defaultValue,
    review: () => false, page: () => {}, progress: () => {}, finish: () => {}, error: () => {}, pollCancel: () => {}, close: () => {}, ...overrides };
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
  test("Claude-only symlink batches include the universal canonical target required by Skills CLI", () => {
    const chosen = options(["--agents", "claude-code", "--core", "rex", "--method", "symlink"]);
    const plan = buildPlan([], chosen, {}, "clzmj/skills", () => false);
    expect(plan.steps).toHaveLength(1);
    expect(plan.steps[0].command.slice(plan.steps[0].command.indexOf("-a"))).toEqual(["-a", "codex", "claude-code", "-y"]);
    expect(plan.steps[0].paths).toContain(resolve(skillDirectory("codex", chosen), "rex"));
    const copied = buildPlan([], { ...chosen, method: "copy" }, {}, "clzmj/skills", () => false);
    expect(copied.steps[0].command.slice(copied.steps[0].command.indexOf("-a"))).toEqual(["-a", "claude-code", "--copy", "-y"]);
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
    let cancelled = false;
    runtime.ui = pages({ review(_title, lines) { expect(lines.some(line => line.includes("Scope: global"))).toBe(true); return false; }, finish(title) { cancelled = title === "Installation cancelled"; } });
    runtime.prepare = () => { throw new Error("mutation before confirmation"); };
    await initialize(manifest, parseOptions([], ["codex"]), runtime);
    expect(calls).toEqual([]); expect(saved).toEqual([]);
    expect(cancelled).toBe(true);
  });
  test("interactive wildcard selection shows count and accepts individual skills", async () => {
    const { runtime, calls } = mock();
    runtime.ui = pages({ multiselect(title, _rows, defaults) { if (title === "Favorite packs") return ["mattpocock/skills"]; if (title === "Individual skills") return ["mattpocock/skills/tdd"]; return defaults; }, review: () => true });
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
  test("hosted source validation only accepts the current extracted release", () => {
    const release = new URL("../", import.meta.url).pathname.replace(/\/$/, "");
    expect(resolveCoreSource(undefined)).toBe("clzmj/skills");
    expect(resolveCoreSource(release)).toBe(release);
    expect(() => resolveCoreSource("relative")).toThrow("extracted release root");
    expect(() => resolveCoreSource("/tmp")).toThrow("extracted release root");
  });
});

describe("terminal page flow and asynchronous work", () => {
  test("Back revisits a page with its prior choice and review stays default-no", async () => {
    const fixture = mock(); let skillVisits = 0; let reviews = 0; let hookVisits = 0;
    fixture.runtime.ui = pages({
      multiselect(title, _rows, defaults) {
        if (title === "Individual skills") { skillVisits++; if (skillVisits === 2) expect(defaults).toEqual(["clzmj/skills/rex"]); return ["clzmj/skills/rex"]; }
        return defaults;
      },
      confirm() { if (++hookVisits === 1) throw new TerminalBack(); return false; },
      review() { reviews++; if (reviews === 1) throw new TerminalBack(); return false; },
    });
    await initialize(manifest, parseOptions([], ["codex"]), fixture.runtime);
    expect(skillVisits).toBe(2); expect(reviews).toBe(2); expect(fixture.saved).toEqual([]); expect(fixture.calls).toEqual([]);
  });
  test("project favorite page excludes global native plugins", async () => {
    const directory = mkdtempSync(resolve(tmpdir(), "skills-pages-project-"));
    try {
      const fixture = mock(); let shown: string[] = [];
      fixture.runtime.ui = pages({ multiselect(title, rows, defaults, context) { if (title === "Favorite packs") { shown = rows.map(row => row.name); expect(context).toContain("global scope"); } return defaults; } });
      await initialize(manifest, parseOptions(["--scope", "project", "--project-dir", directory], ["codex"]), fixture.runtime);
      expect(shown).toEqual(parseManifest(manifest).filter(favorite => favorite.type === "skills").map(favorite => favorite.source));
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
  test("awaits inventory and installation promises while repainting honest progress", async () => {
    const fixture = mock(); const frames: {completed:number;total:number}[] = []; const original = fixture.runtime.run;
    fixture.runtime.run = async (command, capture, cwd) => { const output = await original(command, capture, cwd); if (!capture) await new Promise(resolve => setTimeout(resolve, 230)); return output; };
    fixture.runtime.ui = pages({ progress: state => { frames.push(state); } });
    await initialize(manifest, options(["--agents", "codex", "--core", "rex"]), fixture.runtime);
    expect(frames.filter(frame => frame.total === 1 && frame.completed === 0).length).toBeGreaterThanOrEqual(3);
    expect(frames.at(-1)).toMatchObject({ total: 1, completed: 1 });
  });
  test("interactive completion summarizes choices without dumping installed file paths", async () => {
    const fixture = mock(); let completed: string[] = [];
    fixture.runtime.ui = pages({ finish(_title, lines) { completed = lines ?? []; } });
    await initialize(manifest, options(["--agents", "codex", "--core", "rex"]), fixture.runtime);
    expect(completed[0]).toContain("Summary:");
    expect(completed).toContain("Agents: codex");
    expect(completed).toContain("Core: rex");
    expect(completed.some(line => line.startsWith("Launcher:"))).toBe(true);
    expect(completed.some(line => line.startsWith("Changed path:"))).toBe(false);
  });
  test("Ctrl-C during work aborts later steps and preserves only completed ownership", async () => {
    const fixture = mock(); let polls = 0, cancelled = false, finished = false;
    const chosen = options(["--core", "rex"]); const first = resolve(skillDirectory("codex", chosen), "rex");
    fixture.runtime.ui = pages({ pollCancel() { if (++polls === 3) throw new TerminalCancelled(); } });
    fixture.runtime.cancel = () => { cancelled = true; };
    fixture.runtime.finish = () => { finished = true; };
    fixture.runtime.run = async () => { if (!fixture.existing.has(first)) fixture.existing.add(first); else await new Promise(resolve => setTimeout(resolve, 250)); return ""; };
    await expect(initialize(manifest, chosen, fixture.runtime)).rejects.toThrow("Installation cancelled");
    expect(cancelled).toBe(true); expect(finished).toBe(false); expect(fixture.saved[0].record.ownedPaths).toEqual({ [first]: "a".repeat(64) });
  });
  test("async child pipes drain concurrently, bounded errors retain exit status, and timers run", async () => {
    let ticks = 0; const timer = setInterval(() => ticks++, 25);
    try {
      await expect(runSubprocess([process.execPath, "-e", 'process.stdout.write("x".repeat(300000));process.stderr.write("e".repeat(300000));await Bun.sleep(150);process.exit(17)'], false)).rejects.toThrow("Command failed (17)");
      expect(ticks).toBeGreaterThan(2);
    } finally { clearInterval(timer); }
  });
});

describe("one aggregate ordinary-skills page", () => {
  const sources = ["alpha/repo", "beta/repo"];
  const catalogManifest = { favorites: sources.map(source => ({ type: "skills", source, skills: ["*"] })) };
  const catalog = async (source: string) => [{ name: "shared", description: "Same skill name, different source" }, { name: source.split("/")[0] + "-only", description: "Source-specific skill" }];
  test("bundles core and all catalogs on one metadata-qualified page without conflating duplicate names", async () => {
    const fixture = mock(); const titles: string[] = []; fixture.runtime.discover = catalog;
    fixture.runtime.ui = pages({
      multiselect(title, rows, defaults) {
        titles.push(title);
        if (title === "Favorite packs") return sources;
        if (title === "Individual skills") {
          expect(rows.map(row => row.name)).toEqual(["clzmj/skills/rex", "clzmj/skills/python-tooling", "alpha/repo/shared", "alpha/repo/alpha-only", "beta/repo/shared", "beta/repo/beta-only"]);
          expect(rows[2]).toMatchObject({ source: "alpha/repo", skill: "shared" });
          expect(defaults).toHaveLength(6);
          return ["clzmj/skills/rex", "alpha/repo/shared", "beta/repo/beta-only"];
        }
        return defaults;
      }, review: () => true,
    });
    await initialize(catalogManifest, parseOptions([], ["codex"]), fixture.runtime);
    expect(titles.filter(title => title === "Individual skills")).toHaveLength(1);
    expect(titles.some(title => title === "Core skills" || title.startsWith("Individual skills:"))).toBe(false);
    expect(fixture.saved.at(-1)!.record.options).toMatchObject({ core: ["rex"], packSkills: { "alpha/repo": ["shared"], "beta/repo": ["beta-only"] } });
    expect(fixture.calls.map(call => call.command[4])).toEqual(["clzmj/skills", "alpha/repo", "beta/repo"]);
  });
  test("saved update defaults and empty deselections survive aggregate mapping", async () => {
    const fixture = mock(); fixture.runtime.discover = catalog;
    const savedOptions = options(["--core", "none", "--favorites", ...sources]);
    savedOptions.packSkills = { "alpha/repo": [], "beta/repo": ["shared"] };
    fixture.runtime.load = () => ({ version: 1, options: savedOptions, ownedPaths: {} });
    fixture.runtime.ui = pages({ multiselect(title, _rows, defaults) { if (title === "Individual skills") { expect(defaults).toEqual(["beta/repo/shared"]); return []; } return defaults; }, review: () => true });
    await initialize(catalogManifest, parseOptions(["--update"], ["codex"]), fixture.runtime);
    expect(fixture.calls).toEqual([]);
    expect(fixture.saved.at(-1)!.record.options).toMatchObject({ core: [], packSkills: { "alpha/repo": [], "beta/repo": [] } });
  });
  test("Back and review revisits cache catalogs and preserve a deselected source's empty choices", async () => {
    const fixture = mock(); const discovered: string[] = []; let favoriteVisits = 0, skillVisits = 0, hookVisits = 0, reviews = 0;
    fixture.runtime.discover = async source => { discovered.push(source); return catalog(source); };
    fixture.runtime.ui = pages({
      multiselect(title, _rows, defaults) {
        if (title === "Favorite packs") return ++favoriteVisits === 2 ? [sources[0]] : sources;
        if (title === "Individual skills") {
          skillVisits++;
          if ([2, 4].includes(skillVisits)) throw new TerminalBack();
          if (skillVisits === 5) expect(defaults).toEqual(["clzmj/skills/rex", "alpha/repo/shared"]);
          return ["clzmj/skills/rex", "alpha/repo/shared"];
        }
        return defaults;
      },
      confirm() { if ([2, 4].includes(++hookVisits)) throw new TerminalBack(); return false; },
      review() { if (++reviews < 3) throw new TerminalBack(); return false; },
    });
    await initialize(catalogManifest, parseOptions([], ["codex"]), fixture.runtime);
    expect(discovered).toEqual(sources); expect(skillVisits).toBe(5); expect(favoriteVisits).toBe(3);
    expect(fixture.saved).toEqual([]); expect(fixture.calls).toEqual([]);
  });
});
