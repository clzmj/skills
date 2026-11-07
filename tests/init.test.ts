import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { initialize, parseManifest, parseOptions, parseState, planCommands, resolveCoreSource, type Options } from "../scripts/init";

const manifest = JSON.parse(readFileSync(new URL("../favorites.json", import.meta.url), "utf8"));
const options: Options = { agents: ["codex", "claude-code"], dryRun: false, hooks: true };

describe("favorite installation", () => {
  test("hosted mode uses this extracted release while clone mode keeps GitHub tracking", () => {
    const release = new URL("../", import.meta.url).pathname.replace(/\/$/, "");
    expect(resolveCoreSource(undefined)).toBe("clzmj/skills");
    expect(resolveCoreSource(release)).toBe(release);
    expect(() => resolveCoreSource("relative")).toThrow("extracted release root");
    expect(() => resolveCoreSource("/tmp")).toThrow("extracted release root");
    const commands = planCommands(parseManifest(manifest), options, {}, "/agents", release);
    expect(commands[0][3]).toBe(release);
    expect(commands.find(command => command[3] === "mattpocock/skills")).toBeDefined();
    const logs: string[] = [];
    initialize(manifest, { ...options, dryRun: true }, { which: () => "/bin/tool", log: line => logs.push(line), run: () => { throw new Error("spawned"); } }, "/agents", release);
    expect(logs.some(line => line.includes(`skills add ${release}`))).toBe(true);
  });
  test("plans both local skills, independent uv hook installers, and complete upstream packs", () => {
    const commands = planCommands(parseManifest(manifest), options, {}, "/repo");
    expect(commands[0]).toEqual(["bunx", "skills", "add", "clzmj/skills", "--skill", "rex", "python-tooling", "-g", "-a", "codex", "claude-code", "-y"]);
    expect(commands[1]).toEqual(["uv", "run", "--script", "/repo/skills/rex/scripts/install_hooks.py", "--agents", "codex", "claude-code"]);
    expect(commands[2][3]).toBe("/repo/skills/python-tooling/scripts/install_hooks.py");
    expect(commands).toContainEqual(["codex", "plugin", "add", "egm@egm"]);
    expect(commands).toContainEqual(["claude", "plugin", "install", "ponytail@ponytail"]);
    expect(commands).toContainEqual(["bunx", "skills", "add", "mattpocock/skills", "--skill", "*", "-g", "-a", "codex", "claude-code", "-y"]);
    expect(JSON.stringify(commands)).not.toMatch(/plannotator/i);
  });

  test("keeps installed disabled plugins and versions untouched, adds only missing marketplaces", () => {
    const state = parseState("codex", { installed: [{ pluginId: "egm@egm", enabled: false, version: "0.1.0" }] }, { marketplaces: [{ name: "egm" }, { name: "ponytail" }] });
    const commands = planCommands(parseManifest(manifest), { ...options, agents: ["codex"], hooks: false }, { codex: state });
    expect(commands.filter(c => c[1] === "plugin")).toEqual([["codex", "plugin", "add", "ponytail@ponytail"]]);
    expect(planCommands(parseManifest(manifest), { ...options, agents: ["claude-code"], hooks: false }, {
      "claude-code": parseState("claude-code", [{ id: "ponytail@ponytail", enabled: false }], [{ name: "ponytail" }]),
    })).not.toContainEqual(["claude", "plugin", "install", "ponytail@ponytail"]);
  });

  test("dry run never spawns even inventory subprocesses", () => {
    const logs: string[] = [];
    initialize(manifest, { ...options, dryRun: true }, {
      which: () => "/bin/tool", log: line => logs.push(line),
      run: () => { throw new Error("dry run spawned a process"); },
    });
    expect(logs[0]).toContain("no subprocesses or writes");
    expect(logs.some(line => line.includes("conditional"))).toBe(true);
  });

  test("keeps an existing curated Ponytail alias without adding its native marketplace", () => {
    const state = parseState("codex", { installed: [{ pluginId: "engineering-suite-ponytail@openai-curated-remote", enabled: false, version: "2.0.0" }] }, { marketplaces: [] });
    const commands = planCommands(parseManifest(manifest), { ...options, agents: ["codex"] }, { codex: state });
    expect(commands).not.toContainEqual(["codex", "plugin", "add", "ponytail@ponytail"]);
    expect(commands).not.toContainEqual(["codex", "plugin", "marketplace", "add", "DietrichGebert/ponytail"]);
    expect(commands).toContainEqual(["codex", "plugin", "add", "egm@egm"]);
    for (const installedAs of [["invalid"], ["name@market@extra"], ["-bad@market"], "name@market", ["x@y", "x@y"]]) {
      expect(() => parseManifest({ favorites: [{ type: "plugin", source: "owner/repo", marketplace: "market", plugin: "name", installedAs }] })).toThrow("Invalid favorites entry");
    }
  });

  test("validates the entire manifest and all prerequisites before any subprocess", () => {
    let calls = 0;
    const runtime = { which: () => "/bin/tool", log: () => {}, run: () => { calls++; return ""; } };
    expect(() => initialize({ favorites: [...manifest.favorites, { type: "skills", source: "bad", skills: ["ok"] }] }, options, runtime)).toThrow("Invalid favorites entry");
    expect(() => initialize(manifest, options, { ...runtime, which: name => name === "uv" ? null : "/bin/tool" })).toThrow("Missing required tools: uv");
    expect(calls).toBe(0);
    expect(() => parseManifest({ favorites: [{ type: "skills", source: "a/b", skills: ["*", "foo"] }] })).toThrow();
    expect(() => parseManifest({ favorites: [manifest.favorites[0], manifest.favorites[0]] })).toThrow("Duplicate favorite");
  });

  test("reads all host inventories before mutations and stops immediately on a command failure", () => {
    const calls: string[][] = [];
    const logs: string[] = [];
    expect(() => initialize(manifest, options, {
      which: () => "/bin/tool", log: line => logs.push(line),
      run(command, capture) {
        calls.push(command);
        if (!capture) throw new Error("install failed");
        return command[0] === "codex" ? JSON.stringify(command.includes("marketplace") ? { marketplaces: [] } : { installed: [] }) : "[]";
      },
    })).toThrow("install failed");
    expect(calls).toHaveLength(5);
    expect(calls.slice(0, 4).every(c => c.at(-1) === "--json")).toBe(true);
    expect(logs.some(line => line.includes("Initialization completed"))).toBe(false);
  });

  test("rejects malformed inventory instead of treating it as an empty installation", () => {
    expect(() => parseState("codex", {}, { marketplaces: [] })).toThrow("Unexpected codex");
    expect(() => parseState("claude-code", [{ enabled: false }], [])).toThrow("Unexpected claude");
  });

  test("supports selected hosts and rejects unknown arguments", () => {
    expect(parseOptions(["--agents", "claude-code", "--no-hooks", "--dry-run"])).toEqual({ agents: ["claude-code"], hooks: false, dryRun: true });
    expect(() => parseOptions(["--agents"])).toThrow();
    expect(() => parseOptions(["--agents", "other"])).toThrow();
    expect(() => parseOptions(["--wat"])).toThrow();
  });
});
