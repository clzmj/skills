import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";

export type Agent = "codex" | "claude-code";
type Favorite =
  | { type: "skills"; source: string; skills: string[] }
  | { type: "plugin"; source: string; marketplace: string; plugin: string; installedAs?: string[] };
export type Options = { agents: Agent[]; dryRun: boolean; hooks: boolean };
export type State = { plugins: Set<string>; marketplaces: Set<string> };
type Command = string[];
type Runtime = {
  which: (name: string) => string | null;
  run: (command: Command, capture: boolean) => string;
  log: (message: string) => void;
};
const root = fileURLToPath(new URL("../", import.meta.url));
const agents: Agent[] = ["codex", "claude-code"];
const namePattern = /^[a-zA-Z0-9][a-zA-Z0-9._-]*$/;

export function parseOptions(args: string[]): Options {
  const options: Options = { agents: [...agents], dryRun: false, hooks: true };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--dry-run") options.dryRun = true;
    else if (args[i] === "--no-hooks") options.hooks = false;
    else if (args[i] === "--agents") {
      const selected: Agent[] = [];
      while (args[i + 1] && !args[i + 1].startsWith("--")) {
        const value = args[++i];
        if (!agents.includes(value as Agent)) throw new Error(`Unsupported agent: ${value}`);
        if (!selected.includes(value as Agent)) selected.push(value as Agent);
      }
      if (!selected.length) throw new Error("--agents requires codex and/or claude-code");
      options.agents = selected;
    } else throw new Error(`Unknown argument: ${args[i]}`);
  }
  return options;
}

export function parseManifest(value: unknown): Favorite[] {
  if (!value || typeof value !== "object" || !Array.isArray((value as any).favorites))
    throw new Error("favorites.json must contain a favorites array");
  const seen = new Set<string>();
  const marketplaceSources = new Map<string, string>();
  return (value as any).favorites.map((entry: any, index: number) => {
    const fail = () => { throw new Error(`Invalid favorites entry ${index + 1}`); };
    if (!entry || typeof entry !== "object" || typeof entry.source !== "string" ||
        !/^[\w.-]+\/[\w.-]+$/.test(entry.source)) fail();
    let key: string;
    if (entry.type === "skills") {
      if (!Array.isArray(entry.skills) || !entry.skills.length ||
          !entry.skills.every((skill: unknown) => typeof skill === "string" && (skill === "*" || namePattern.test(skill))) ||
          new Set(entry.skills).size !== entry.skills.length ||
          (entry.skills.includes("*") && entry.skills.length !== 1) ||
          Object.keys(entry).some(k => !["type", "source", "skills"].includes(k))) fail();
      key = `skills:${entry.source}`;
    } else if (entry.type === "plugin") {
      if (typeof entry.marketplace !== "string" || !namePattern.test(entry.marketplace) ||
          typeof entry.plugin !== "string" || !namePattern.test(entry.plugin) ||
          (entry.installedAs !== undefined && (!Array.isArray(entry.installedAs) ||
            !entry.installedAs.every((id: unknown) => typeof id === "string" && /^[a-zA-Z0-9][a-zA-Z0-9._-]*@[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(id)) ||
            new Set(entry.installedAs).size !== entry.installedAs.length)) ||
          Object.keys(entry).some(k => !["type", "source", "marketplace", "plugin", "installedAs"].includes(k))) fail();
      key = `plugin:${entry.plugin}@${entry.marketplace}`;
      const previous = marketplaceSources.get(entry.marketplace);
      if (previous && previous !== entry.source)
        throw new Error(`Conflicting sources for marketplace: ${entry.marketplace}`);
      marketplaceSources.set(entry.marketplace, entry.source);
    } else return fail();
    if (seen.has(key)) throw new Error(`Duplicate favorite: ${key}`);
    seen.add(key);
    return entry;
  });
}

const cli = (agent: Agent) => agent === "codex" ? "codex" : "claude";
const skillsCommand = (source: string, skills: string[], selected: Agent[]): Command =>
  ["bunx", "skills", "add", source, "--skill", ...skills, "-g", "-a", ...selected, "-y"];

export function parseState(agent: Agent, plugins: unknown, marketplaces: unknown): State {
  const pluginRows = agent === "codex" ? (plugins as any)?.installed : plugins;
  const marketplaceRows = agent === "codex" ? (marketplaces as any)?.marketplaces : marketplaces;
  if (!Array.isArray(pluginRows) || !Array.isArray(marketplaceRows))
    throw new Error(`Unexpected ${cli(agent)} plugin inventory JSON`);
  const pluginIds = pluginRows.map((row: any) => agent === "codex" ? row.pluginId : row.id);
  const marketplaceNames = marketplaceRows.map((row: any) => row.name);
  if (![...pluginIds, ...marketplaceNames].every(value => typeof value === "string" && value.length))
    throw new Error(`Unexpected ${cli(agent)} plugin inventory entry`);
  // Disabled installations and pinned versions count as installed; never enable/update them.
  return { plugins: new Set(pluginIds), marketplaces: new Set(marketplaceNames) };
}

export function planCommands(favorites: Favorite[], options: Options, states: Partial<Record<Agent, State>>, directory = resolve(homedir(), ".agents")): Command[] {
  const commands: Command[] = [skillsCommand("clzmj/skills", ["rex", "python-tooling"], options.agents)];
  if (options.hooks) for (const skill of ["rex", "python-tooling"])
    commands.push(["uv", "run", "--script", resolve(directory, "skills", skill, "scripts/install_hooks.py"), "--agents", ...options.agents]);
  const knownMarketplaces = new Map(options.agents.map(agent => [agent, new Set(states[agent]?.marketplaces)]));
  for (const favorite of favorites) {
    if (favorite.type === "skills") commands.push(skillsCommand(favorite.source, favorite.skills, options.agents));
    else for (const agent of options.agents) {
      if ([`${favorite.plugin}@${favorite.marketplace}`, ...(favorite.installedAs ?? [])].some(id => states[agent]?.plugins.has(id))) continue;
      if (!knownMarketplaces.get(agent)!.has(favorite.marketplace)) {
        commands.push([cli(agent), "plugin", "marketplace", "add", favorite.source]);
        knownMarketplaces.get(agent)!.add(favorite.marketplace);
      }
      commands.push([cli(agent), "plugin", agent === "codex" ? "add" : "install", `${favorite.plugin}@${favorite.marketplace}`]);
    }
  }
  return commands;
}

export const displayCommand = (command: Command) => command.map(value => /^[\w./:@-]+$/.test(value) ? value : `'${value.replaceAll("'", "'\\''")}'`).join(" ");

export function initialize(manifest: unknown, options: Options, runtime: Runtime, directory = resolve(homedir(), ".agents")): void {
  const favorites = parseManifest(manifest);
  // Complete manifest and executable checks before the first subprocess or mutation.
  const tools = new Set(["bunx", "git"]);
  if (options.hooks) tools.add("uv");
  if (favorites.some(f => f.type === "plugin")) for (const agent of options.agents) tools.add(cli(agent));
  if (favorites.some(f => f.type === "plugin" && f.source === "DietrichGebert/ponytail")) tools.add("node");
  const missing = [...tools].filter(tool => !runtime.which(tool));
  if (missing.length) throw new Error(`Missing required tools: ${missing.join(", ")}`);
  const states: Partial<Record<Agent, State>> = {};
  if (options.dryRun) {
    runtime.log("Dry run: no subprocesses or writes. Plugin commands below are conditional on missing installations/marketplaces.");
  } else if (favorites.some(f => f.type === "plugin")) {
    for (const agent of options.agents) {
      const plugins = JSON.parse(runtime.run([cli(agent), "plugin", "list", "--json"], true));
      const marketplaces = JSON.parse(runtime.run([cli(agent), "plugin", "marketplace", "list", "--json"], true));
      states[agent] = parseState(agent, plugins, marketplaces);
    }
  }
  const commands = planCommands(favorites, options, states, directory);
  for (const command of commands) {
    runtime.log(displayCommand(command));
    if (!options.dryRun) runtime.run(command, false);
  }
  if (!options.dryRun) runtime.log("Initialization completed. Review new hooks in each host's /hooks menu and start a new session.");
}

if (import.meta.main) {
  try {
    initialize(JSON.parse(readFileSync(resolve(root, "favorites.json"), "utf8")), parseOptions(process.argv.slice(2)), {
      which: name => Bun.which(name),
      log: console.log,
      run(command, capture) {
        const result = Bun.spawnSync(command, { cwd: root, stdout: capture ? "pipe" : "inherit", stderr: "inherit", stdin: "inherit" });
        if (result.error) throw result.error;
        if (result.exitCode !== 0) throw new Error(`Command failed (${result.exitCode}): ${displayCommand(command)}`);
        return capture ? result.stdout.toString() : "";
      },
    });
  } catch (error) {
    console.error(`Initialization failed: ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  }
}
