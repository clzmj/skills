import { existsSync, lstatSync, readFileSync, realpathSync, statSync, mkdirSync, writeFileSync, openSync, readSync, writeSync, closeSync, symlinkSync } from "node:fs";
import { isAbsolute, resolve, dirname } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";
import { prepareHostedRelease, registerLauncher, launcherPath, fingerprint } from "./self-update";

export type Agent = "codex" | "claude-code";
export type Favorite = ({ type: "skills"; skills: string[] } | { type: "plugin"; marketplace: string; plugin: string; installedAs?: string[] }) & { source: string; description?: string };
export type Options = {
  agents: Agent[]; dryRun: boolean; hooks: boolean; scope: "global" | "project";
  projectDir: string; method: "copy" | "symlink"; core: string[]; favorites?: string[];
  packSkills: Record<string, string[]>; yes: boolean; update: boolean; help: boolean; provided?: string[];
};
export type State = { plugins: Set<string>; marketplaces: Set<string> };
export type Command = string[];
export type Skill = { name: string; description: string };
export type InstallRecord = { version: 1; options: Options; ownedPaths: Record<string, string> };
export type Runtime = {
  which: (name: string) => string | null;
  run: (command: Command, capture: boolean, cwd?: string) => string;
  log: (message: string) => void;
  exists?: (path: string) => boolean;
  ask?: (question: string, defaultValue: string) => string;
  discover?: (source: string) => Promise<Skill[]>;
  save?: (path: string, record: InstallRecord) => void;
  load?: (path: string) => InstallRecord | undefined;
  prepare?: (options: Options, source: string) => Promise<string> | string;
  finish?: (options: Options, source: string) => Promise<void> | void;
  fingerprint?: (path: string) => string;
  link?: (source: string, destination: string) => void;
};
const root = fileURLToPath(new URL("../", import.meta.url));
const agents: Agent[] = ["codex", "claude-code"];
const coreSkills: Skill[] = [
  { name: "rex", description: "Visible terminal work and conversational agent coordination" },
  { name: "python-tooling", description: "Python environments, dependency management, formatting and checks" },
];
const namePattern = /^[a-zA-Z0-9][a-zA-Z0-9._-]*$/;
const pathExists = (path: string) => { try { lstatSync(path); return true; } catch (error: any) { if (error.code === "ENOENT") return false; throw error; } };
const cli = (agent: Agent) => agent === "codex" ? "codex" : "claude";
export const settingsPath = (options: Pick<Options, "scope" | "projectDir">) => options.scope === "project"
  ? resolve(options.projectDir, ".clzmj-skills/install.json") : resolve(homedir(), ".config/clzmj-skills/install.json");

export function parseOptions(args: string[], detected: Agent[] = [], cwd = process.cwd()): Options {
  const options: Options = { agents: [...detected], dryRun: false, hooks: false, scope: "global", projectDir: cwd, method: "copy", core: coreSkills.map(s => s.name), favorites: [], packSkills: {}, yes: false, update: false, help: false, provided: [...args] };
  const list = (index: number): [string[], number] => {
    const values: string[] = [];
    while (args[index + 1] && !args[index + 1].startsWith("-")) values.push(...args[++index].split(",").filter(Boolean));
    if (!values.length) throw new Error(`${args[index]} requires a value`);
    return [[...new Set(values)], index];
  };
  for (let i = 0; i < args.length; i++) {
    const flag = args[i];
    if (flag === "--dry-run") options.dryRun = true;
    else if (flag === "--yes" || flag === "-y") options.yes = true;
    else if (flag === "--help" || flag === "-h") options.help = true;
    else if (flag === "--update") options.update = true;
    else if (flag === "--hooks") options.hooks = true;
    else if (flag === "--no-hooks") options.hooks = false;
    else if (flag === "--core-only") options.favorites = [];
    else if (flag === "--all-favorites") options.favorites = undefined;
    else if (["--agents", "--core", "--favorites"].includes(flag)) {
      let values: string[]; [values, i] = list(i);
      if (flag === "--agents") {
        if (!values.every(value => agents.includes(value as Agent))) throw new Error("--agents supports codex and claude-code");
        options.agents = values as Agent[];
      } else if (flag === "--core") {
        if (values.includes("none")) { if (values.length !== 1) throw new Error("Use --core none alone"); options.core = []; }
        else { if (!values.every(value => coreSkills.some(s => s.name === value))) throw new Error("Unknown core skill"); options.core = values; }
      } else options.favorites = values;
    } else if (["--scope", "--project-dir", "--method", "--skills"].includes(flag)) {
      const value = args[++i];
      if (!value || value.startsWith("--")) throw new Error(`${flag} requires a value`);
      if (flag === "--scope") { if (value !== "global" && value !== "project") throw new Error("--scope must be global or project"); options.scope = value; }
      else if (flag === "--method") { if (value !== "copy" && value !== "symlink") throw new Error("--method must be copy or symlink"); options.method = value; }
      else if (flag === "--project-dir") { if (!isAbsolute(value)) throw new Error("--project-dir must be an absolute directory"); options.projectDir = resolve(value); }
      else {
        const [source, names, extra] = value.split(":");
        if (!/^[\w.-]+\/[\w.-]+$/.test(source) || !names || extra || !names.split(",").every(name => namePattern.test(name))) throw new Error("--skills expects owner/repo:skill,skill");
        options.packSkills[source] = [...new Set(names.split(","))];
      }
    } else throw new Error(`Unknown argument: ${flag}`);
  }
  if (options.scope === "project" && !args.includes("--project-dir") && options.yes) throw new Error("Unattended project installs require --project-dir /absolute/directory");
  return options;
}

export function parseManifest(value: unknown): Favorite[] {
  if (!value || typeof value !== "object" || !Array.isArray((value as any).favorites)) throw new Error("favorites.json must contain a favorites array");
  const seen = new Set<string>();
  const marketplaces = new Map<string, string>();
  return (value as any).favorites.map((entry: any, index: number) => {
    const fail = () => { throw new Error(`Invalid favorites entry ${index + 1}`); };
    if (!entry || typeof entry !== "object" || typeof entry.source !== "string" || !/^[\w.-]+\/[\w.-]+$/.test(entry.source) || (entry.description !== undefined && (typeof entry.description !== "string" || /[\x00-\x1f\x7f]/.test(entry.description)))) fail();
    let key: string;
    if (entry.type === "skills") {
      if (!Array.isArray(entry.skills) || !entry.skills.length || !entry.skills.every((s: unknown) => typeof s === "string" && (s === "*" || namePattern.test(s))) || new Set(entry.skills).size !== entry.skills.length || (entry.skills.includes("*") && entry.skills.length !== 1) || Object.keys(entry).some(k => !["type", "source", "skills", "description"].includes(k))) fail();
      key = `skills:${entry.source}`;
    } else if (entry.type === "plugin") {
      if (typeof entry.marketplace !== "string" || !namePattern.test(entry.marketplace) || typeof entry.plugin !== "string" || !namePattern.test(entry.plugin) || (entry.installedAs !== undefined && (!Array.isArray(entry.installedAs) || !entry.installedAs.every((id: unknown) => typeof id === "string" && /^[a-zA-Z0-9][a-zA-Z0-9._-]*@[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(id)) || new Set(entry.installedAs).size !== entry.installedAs.length)) || Object.keys(entry).some(k => !["type", "source", "marketplace", "plugin", "installedAs", "description"].includes(k))) fail();
      key = `plugin:${entry.plugin}@${entry.marketplace}`;
      const previous = marketplaces.get(entry.marketplace);
      if (previous && previous !== entry.source) throw new Error(`Conflicting sources for marketplace: ${entry.marketplace}`);
      marketplaces.set(entry.marketplace, entry.source);
    } else return fail();
    if (seen.has(key)) throw new Error(`Duplicate favorite: ${key}`);
    seen.add(key);
    return entry;
  });
}

export function parseState(agent: Agent, plugins: unknown, marketplaces: unknown): State {
  const pluginRows = agent === "codex" ? (plugins as any)?.installed : plugins;
  const marketplaceRows = agent === "codex" ? (marketplaces as any)?.marketplaces : marketplaces;
  if (!Array.isArray(pluginRows) || !Array.isArray(marketplaceRows)) throw new Error(`Unexpected ${cli(agent)} plugin inventory JSON`);
  const pluginIds = pluginRows.map((row: any) => agent === "codex" ? row.pluginId : row.id);
  const marketplaceNames = marketplaceRows.map((row: any) => row.name);
  if (![...pluginIds, ...marketplaceNames].every(value => typeof value === "string" && value.length)) throw new Error(`Unexpected ${cli(agent)} plugin inventory entry`);
  return { plugins: new Set(pluginIds), marketplaces: new Set(marketplaceNames) };
}

export function resolveCoreSource(value: string | undefined, directory = root): string {
  if (value === undefined) return "clzmj/skills";
  if (!isAbsolute(value) || realpathSync(value) !== realpathSync(directory)) throw new Error("CLZMJ_SKILLS_SOURCE must point to this extracted release root");
  for (const skill of coreSkills) if (!statSync(resolve(value, "skills", skill.name, "SKILL.md")).isFile()) throw new Error(`Missing bundled skill: ${skill.name}`);
  return realpathSync(value);
}

export function detectAgents(which: Runtime["which"], exists: (path: string) => boolean = existsSync): Agent[] {
  return agents.filter(agent => which(cli(agent)) || exists(agent === "codex" ? process.env.CODEX_HOME || resolve(homedir(), ".codex") : process.env.CLAUDE_CONFIG_DIR || resolve(homedir(), ".claude")));
}
export const skillDirectory = (agent: Agent, options: Options, home = homedir()) => options.scope === "project"
  ? resolve(options.projectDir, agent === "codex" ? ".agents/skills" : ".claude/skills")
  : agent === "codex" ? resolve(home, ".agents/skills") : resolve(process.env.CLAUDE_CONFIG_DIR || resolve(home, ".claude"), "skills");
const skillsCommand = (source: string, names: string[], selected: Agent[], options: Options): Command =>
  ["bunx", "--bun", "skills", "add", source, "--skill", ...names, ...(options.scope === "global" ? ["-g"] : []), "-a", ...selected, ...(options.method === "copy" ? ["--copy"] : []), "-y"];
export const displayCommand = (command: Command) => command.map(value => /^[\w./:@-]+$/.test(value) ? value : `'${value.replaceAll("'", "'\\''")}'`).join(" ");

export async function discoverPack(source: string): Promise<Skill[]> {
  const get = async (url: string) => {
    const response = await fetch(url, { headers: { Accept: "application/vnd.github+json" }, signal: AbortSignal.timeout(20_000) });
    if (!response.ok) throw new Error(`Could not discover ${source}: HTTP ${response.status}. Retry or use --skills ${source}:name,name`);
    return response;
  };
  const repo = await (await get(`https://api.github.com/repos/${source}`)).json() as any;
  const tree = await (await get(`https://api.github.com/repos/${source}/git/trees/${encodeURIComponent(repo.default_branch)}?recursive=1`)).json() as any;
  if (tree.truncated || !Array.isArray(tree.tree)) throw new Error(`Incomplete skill discovery for ${source}`);
  const paths = tree.tree.filter((row: any) => row.type === "blob" && /(^|\/)SKILL\.md$/.test(row.path) && !row.path.split("/").some((part: string) => part.startsWith("."))).map((row: any) => row.path as string);
  if (!paths.length || paths.length > 150) throw new Error(`Unexpected skill count for ${source}`);
  const skills = await Promise.all(paths.map(async path => {
    const text = await (await get(`https://raw.githubusercontent.com/${source}/${encodeURIComponent(repo.default_branch)}/${path}`)).text();
    const frontmatter = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
    const name = frontmatter?.[1].match(/^name:\s*["']?([\w.-]+)["']?\s*$/m)?.[1];
    const description = frontmatter?.[1].match(/^description:\s*(.+)$/m)?.[1]?.replace(/^["']|["']$/g, "") || "Upstream skill";
    if (!name || !namePattern.test(name)) throw new Error(`Cannot read skill name in ${source}/${path}; use --skills for explicit selection`);
    return { name, description: description.replace(/[\x00-\x1f\x7f]/g, " ").slice(0, 240) };
  }));
  if (new Set(skills.map(s => s.name)).size !== skills.length) throw new Error(`Duplicate discovered names in ${source}`);
  return skills.sort((a, b) => a.name.localeCompare(b.name));
}

function select(ask: NonNullable<Runtime["ask"]>, log: Runtime["log"], title: string, rows: Skill[], defaults: string[], shortcuts = false): string[] {
  log(title);
  rows.forEach((row, index) => log(`  ${index + 1}. ${row.name}${defaults.includes(row.name) ? " [selected]" : ""} — ${row.description}`));
  const answer = ask(`Select numbers/names separated by commas${shortcuts ? ", all or none (core-only)" : ", all or none"}`, defaults.join(","));
  if (answer === "all") return rows.map(row => row.name);
  if (answer === "none" || answer === "core-only") return [];
  const selected = answer.split(/[\s,]+/).filter(Boolean).map(value => /^\d+$/.test(value) ? rows[Number(value) - 1]?.name : value);
  if (!selected.every(name => rows.some(row => row.name === name))) throw new Error(`Invalid selection for ${title}`);
  return [...new Set(selected)] as string[];
}

export function chooseOptions(options: Options, favorites: Favorite[], runtime: Runtime): Options {
  if (options.yes || options.dryRun) return options;
  if (!runtime.ask) throw new Error("No controlling terminal. Run in a terminal or pass --yes with explicit choices for unattended installation.");
  const ask = runtime.ask;
  const chosen = { ...options, packSkills: { ...options.packSkills } };
  chosen.agents = select(ask, runtime.log, "Agents", agents.map(name => ({ name, description: name === "codex" ? "OpenAI Codex" : "Claude Code" })), chosen.agents) as Agent[];
  const scope = ask("Scope: global or project", chosen.scope);
  if (scope !== "global" && scope !== "project") throw new Error("Scope must be global or project");
  chosen.scope = scope;
  if (scope === "project") {
    const directory = ask("Exact absolute project directory", chosen.projectDir);
    if (!isAbsolute(directory) || !statSync(directory).isDirectory()) throw new Error("Project directory must be an existing absolute directory");
    chosen.projectDir = realpathSync(directory);
  }
  const method = ask("Ordinary skills: copy or symlink (native plugins keep their packaging)", chosen.method);
  if (method !== "copy" && method !== "symlink") throw new Error("Method must be copy or symlink");
  chosen.method = method;
  chosen.core = select(ask, runtime.log, "Core skills — owner clzmj", coreSkills, chosen.core);
  chosen.favorites = select(ask, runtime.log, "Favorite packs — select all, individual packs, or core-only", favorites.map(f => ({ name: f.source, description: `${f.description || (f.type === "plugin" ? "Complete native plugin pack" : "Upstream skill pack")} (owner ${f.source.split("/")[0]})` })), chosen.favorites ?? favorites.map(f => f.source), true);
  if (chosen.scope === "project") { runtime.log("Project hooks are unsupported; global hooks skipped without changing home settings."); chosen.hooks = false; }
  else chosen.hooks = /^(y|yes)$/i.test(ask("Install independent Rex/Python hooks? yes or no", chosen.hooks ? "yes" : "no"));
  return chosen;
}

type Step = { command: Command; link?: { source: string; destination: string }; label: string; paths: string[]; kind: "skill" | "plugin" | "hook" };
export function buildPlan(favorites: Favorite[], options: Options, states: Partial<Record<Agent, State>>, coreSource: string, exists: (path: string) => boolean = pathExists): { steps: Step[]; skipped: string[] } {
  const steps: Step[] = [], skipped: string[] = [];
  const ordinary = [{ source: coreSource, skills: options.core }, ...favorites.filter(f => f.type === "skills") as Extract<Favorite, { type: "skills" }>[]];
  for (const pack of ordinary) for (const agent of options.agents) {
    const names: string[] = [], paths: string[] = [], statuses: string[] = [];
    for (const skill of pack.skills) {
      const path = resolve(skillDirectory(agent, options), skill);
      const canonical = resolve(options.scope === "global" ? homedir() : options.projectDir, ".agents/skills", skill);
      if (!options.update && exists(path)) { skipped.push(`already-installed: ${skill} for ${agent} (${path}); version status unknown, preserved (use --update to explicitly reinstall)`); continue; }
      if (!options.update && options.method === "symlink" && exists(canonical)) {
        if (!exists(resolve(canonical, "SKILL.md"))) { skipped.push(`existing shared path preserved: ${canonical}; no verified SKILL.md, cannot link for ${agent}`); continue; }
        steps.push({ command: [], link: { source: canonical, destination: path }, label: `link existing: ${skill} for ${agent} (canonical preserved)`, paths: [path], kind: "skill" });
        continue;
      }
      names.push(skill);
      statuses.push(`${skill} (${options.update && exists(path) ? "explicit update" : "new install"})`);
      paths.push(path, ...(options.method === "symlink" ? [canonical] : []));
    }
    if (names.length) steps.push({ command: skillsCommand(pack.source, names, [agent], options), label: `skills for ${agent}: ${statuses.join(", ")}`, paths: [...new Set(paths)], kind: "skill" });
  }
  const known = new Map(options.agents.map(agent => [agent, new Set(states[agent]?.marketplaces)]));
  for (const favorite of favorites) if (favorite.type === "plugin") for (const agent of options.agents) {
    if (options.scope === "project") { skipped.push(`unsupported project-native plugin: ${favorite.plugin} for ${agent}; no global changes`); continue; }
    if ([`${favorite.plugin}@${favorite.marketplace}`, ...(favorite.installedAs ?? [])].some(id => states[agent]?.plugins.has(id))) { skipped.push(`already-installed: ${favorite.plugin} for ${agent}; existing enabled/disabled/pinned state preserved`); continue; }
    if (!known.get(agent)!.has(favorite.marketplace)) {
      steps.push({ command: [cli(agent), "plugin", "marketplace", "add", favorite.source], label: `marketplace: ${favorite.marketplace} for ${agent}`, paths: [], kind: "plugin" });
      known.get(agent)!.add(favorite.marketplace);
    }
    steps.push({ command: [cli(agent), "plugin", agent === "codex" ? "add" : "install", `${favorite.plugin}@${favorite.marketplace}`], label: `install native plugin: ${favorite.plugin} for ${agent}`, paths: [], kind: "plugin" });
  }
  if (options.hooks && options.scope === "project") skipped.push("unsupported project hooks: global hook installers skipped; no home settings changed");
  else if (options.hooks) for (const { name: skill } of coreSkills) {
    // Run the bundled installer independently of whether the skill was already installed.
    steps.push({ command: ["uv", "run", "--script", resolve(root, "skills", skill, "scripts/install_hooks.py"), "--agents", ...options.agents], label: `global hooks: ${skill}`, paths: [], kind: "hook" });
  }
  return { steps, skipped };
}
export function planCommands(favorites: Favorite[], options: Options, states: Partial<Record<Agent, State>>, _directory?: string, coreSource = "clzmj/skills"): Command[] { return buildPlan(favorites, options, states, coreSource).steps.filter(step => !step.link).map(step => step.command); }

export async function initialize(manifest: unknown, initial: Options, runtime: Runtime, _directory?: string, coreSource = "clzmj/skills"): Promise<void> {
  const favorites = parseManifest(manifest);
  let options = { ...initial, packSkills: { ...initial.packSkills } };
  const saved = runtime.load?.(settingsPath(options));
  if (options.update && saved) {
    options = { ...saved.options, dryRun: initial.dryRun, yes: initial.yes, update: true, help: initial.help, provided: initial.provided };
    const flags: [string[], keyof Options][] = [[["--agents"], "agents"], [["--core"], "core"], [["--core-only", "--all-favorites", "--favorites"], "favorites"], [["--skills"], "packSkills"], [["--hooks", "--no-hooks"], "hooks"], [["--scope"], "scope"], [["--project-dir"], "projectDir"], [["--method"], "method"]];
    for (const [names, key] of flags) if (names.some(flag => initial.provided?.includes(flag))) (options as any)[key] = key === "packSkills" ? { ...saved.options.packSkills, ...initial.packSkills } : initial[key];
  }
  options = chooseOptions(options, favorites, runtime);
  if (!options.agents.length) throw new Error("No agents selected/detected. Pass --agents codex and/or claude-code.");
  if (options.scope === "project" && !statSync(options.projectDir).isDirectory()) throw new Error("Project directory must exist");
  const exists = runtime.exists ?? pathExists;
  if (options.favorites?.some(source => !favorites.some(f => f.source === source))) throw new Error("Unknown favorite source; use an owner/repo listed in favorites.json");
  let selected = favorites.filter(f => options.favorites === undefined || options.favorites.includes(f.source)).map(f => ({ ...f }));
  for (const [source] of Object.entries(options.packSkills)) if (!selected.some(f => f.source === source && f.type === "skills")) {
    if (initial.provided?.includes("--skills") && initial.packSkills[source]) throw new Error(`--skills source is not a selected ordinary pack: ${source}`);
    delete options.packSkills[source];
  }
  // Persist the exact approved packs, so a future catalog addition never expands update scope.
  options.favorites = selected.map(f => f.source);
  for (const favorite of selected) if (favorite.type === "skills") {
    if (options.packSkills[favorite.source]) favorite.skills = options.packSkills[favorite.source];
    else if (favorite.skills.includes("*")) {
      const discovered = await (runtime.discover ?? discoverPack)(favorite.source);
      if (!discovered.length || discovered.some(s => !namePattern.test(s.name)) || new Set(discovered.map(s => s.name)).size !== discovered.length) throw new Error(`Invalid discovered skill catalog: ${favorite.source}`);
      runtime.log(`${favorite.source}: discovered ${discovered.length} skills`);
      favorite.skills = options.yes || options.dryRun ? discovered.map(s => s.name) : select(runtime.ask!, runtime.log, `Individual skills from ${favorite.source}`, discovered, discovered.map(s => s.name));
      runtime.log(`  ${favorite.skills.join(", ")}`);
    }
    options.packSkills[favorite.source] = favorite.skills;
  }
  const tools = new Set<string>();
  if (options.core.length || selected.some(f => f.type === "skills")) { tools.add("bunx"); tools.add("git"); }
  if (options.hooks && options.scope === "global") tools.add("uv");
  if (options.scope === "global" && selected.some(f => f.type === "plugin")) for (const agent of options.agents) tools.add(cli(agent));
  const missing = [...tools].filter(tool => !runtime.which(tool));
  if (missing.length) throw new Error(`Missing required tools: ${missing.join(", ")}`);
  const states: Partial<Record<Agent, State>> = {};
  if (!options.dryRun && options.scope === "global" && selected.some(f => f.type === "plugin")) for (const agent of options.agents) {
    states[agent] = parseState(agent, JSON.parse(runtime.run([cli(agent), "plugin", "list", "--json"], true, options.projectDir)), JSON.parse(runtime.run([cli(agent), "plugin", "marketplace", "list", "--json"], true, options.projectDir)));
  }
  let plan = buildPlan(selected, options, states, coreSource, exists);
  runtime.log(`\nInstallation preview: ${options.scope}${options.scope === "project" ? ` at ${options.projectDir}` : " (user home)"}; ${options.method}; agents ${options.agents.join(", ")}`);
  runtime.log(`Core: ${options.core.join(", ") || "none"}; favorites: ${selected.map(f => f.source).join(", ") || "none"}; hooks: ${options.hooks && options.scope === "global" ? "selected (home settings)" : "skipped"}`);
  if (options.dryRun) runtime.log("Dry run: no subprocesses or writes. Native plugin commands are conditional on inventory; read-only network discovery may occur.");
  for (const line of plan.skipped) runtime.log(`Skip ${line}`);
  for (const step of plan.steps) { runtime.log(`${step.label}: ${step.link ? `${step.link.destination} -> ${step.link.source}` : displayCommand(step.command)}`); for (const path of step.paths) runtime.log(`  path: ${path}`); }
  runtime.log(`Settings: ${settingsPath(options)}; native plugins retain complete host packaging.`);
  runtime.log(`Launcher: ${launcherPath({ scope: options.scope, projectRoot: options.projectDir })}`);
  if (process.env.CLZMJ_SKILLS_VERSION) runtime.log(`Release cache: ${resolve(options.scope === "global" ? resolve(homedir(), ".local/share/clzmj-skills/releases") : resolve(options.projectDir, ".clzmj-skills/releases"), process.env.CLZMJ_SKILLS_VERSION)}`);
  else runtime.log(`Launcher source: ${coreSource === "clzmj/skills" ? root : coreSource}`);
  if (options.scope === "global" && selected.some(f => f.type === "plugin")) for (const agent of options.agents) runtime.log(`Native plugin store (${agent}): ${resolve(agent === "codex" ? process.env.CODEX_HOME || resolve(homedir(), ".codex") : process.env.CLAUDE_CONFIG_DIR || resolve(homedir(), ".claude"), "plugins")}`);
  if (options.hooks && options.scope === "global") {
    runtime.log(`Hook runtime: ${resolve(homedir(), ".local/share/agent-tooling")} (Rex runtime and Python hook files)`);
    for (const agent of options.agents) {
      runtime.log(`Hook settings: ${resolve(homedir(), agent === "codex" ? ".codex/hooks.json" : ".claude/settings.json")}`);
      runtime.log(`Rex instructions: ${resolve(homedir(), agent === "codex" ? ".codex/AGENTS.md" : ".claude/CLAUDE.md")}`);
    }
  }
  if (options.dryRun) return;
  if (!options.yes && !/^(y|yes)$/i.test(runtime.ask!("Install this plan? yes or no", "no"))) { runtime.log("Cancelled; no installation changes made."); return; }
  if (runtime.prepare) { coreSource = await runtime.prepare(options, coreSource); plan = buildPlan(selected, options, states, coreSource, exists); }
  const scopedSaved = runtime.load?.(settingsPath(options));
  const record: InstallRecord = { version: 1, options, ownedPaths: { ...(scopedSaved?.ownedPaths ?? {}) } };
  let completed = 0, failed = 0;
  const changed = new Set<string>();
  for (const step of plan.steps) {
    const fresh = step.paths.filter(path => !exists(path));
    const previouslyOwned = step.paths.filter(path => record.ownedPaths[path]);
    runtime.log(step.label);
    try {
      if (step.link) { if (!runtime.link) throw new Error("Native symlink operation unavailable"); runtime.link(step.link.source, step.link.destination); }
      else runtime.run(step.command, false, options.projectDir);
      completed++;
      for (const path of step.paths) if (exists(path)) changed.add(path);
      for (const path of [...fresh, ...previouslyOwned]) if (exists(path) && runtime.fingerprint) record.ownedPaths[path] = runtime.fingerprint(path);
    } catch (error) {
      failed++;
      runtime.log(`Failed ${step.label}: ${error instanceof Error ? error.message : String(error)}`);
      if (!step.link) runtime.log(`Retry from ${options.projectDir}: ${displayCommand(step.command)}`);
      runtime.log(`Installer retry (explicitly updates all saved ordinary selections): ${displayCommand(["bun", resolve(coreSource === "clzmj/skills" ? root : coreSource, "scripts/init.ts"), "--update", "--yes", "--scope", options.scope, ...(options.scope === "project" ? ["--project-dir", options.projectDir] : [])])}`);
      // Skills CLI can partially create a destination before failing; do not claim ownership of partial output.
    }
  }
  runtime.save?.(settingsPath(options), record);
  if (!failed) await runtime.finish?.(options, coreSource);
  runtime.log(`\nSummary: ${completed} commands completed; ${plan.skipped.length} skipped; ${failed} failed.`);
  for (const path of changed) runtime.log(`Changed path: ${path}`);
  if (options.hooks && options.scope === "global") runtime.log("Hook settings may have changed; review each host's /hooks menu. Uninstall retains shared host hook settings.");
  if (failed) throw new Error(`${failed} installation command(s) failed; completed installations were preserved. Retry with the same choices.`);
}

export const helpText = `Usage: clzmj-skills [install|update] [options]\n  --agents codex claude-code   Select hosts (detected hosts preselected)\n  --scope global|project       Global default; project uses exact --project-dir\n  --project-dir /absolute/dir  Existing project working directory\n  --method copy|symlink        Ordinary skills; copy default\n  --core rex python-tooling    Selected core skills, or --core none\n  --core-only                 No favorite packs (default)\n  --all-favorites             All curated packs\n  --favorites owner/repo ...  Select favorite packs\n  --skills owner/repo:a,b      Individual ordinary pack skills\n  --hooks / --no-hooks         Independent global hook choice (default off)\n  --update                    Explicitly reinstall selected ordinary skills\n  --yes                       Accept choices without a terminal\n  --dry-run                   Preview; no subprocesses or writes\n  --help                      Show this help\nProject scope skips unsupported native plugins and hooks. Existing skills and native plugins are preserved by default.`;

export function readTerminalAnswer(fd: number, fallback: string): string {
  const bytes: number[] = [], byte = Buffer.alloc(1);
  while (true) {
    if (readSync(fd, byte, 0, 1, null) === 0) throw new Error("Terminal input ended; installation cancelled. Pass --yes for unattended installation.");
    if (byte[0] === 10) break;
    if (byte[0] !== 13) bytes.push(byte[0]);
  }
  return Buffer.from(bytes).toString("utf8").trim() || fallback;
}
export function openTerminal(): { ask: NonNullable<Runtime["ask"]>; close: () => void } | undefined {
  let fd: number;
  try { fd = openSync("/dev/tty", "r+"); } catch { return undefined; }
  return {
    ask(question, fallback) {
      writeSync(fd, `${question}${fallback ? ` [${fallback}]` : ""}: `);
      return readTerminalAnswer(fd, fallback);
    },
    close: () => closeSync(fd),
  };
}

if (import.meta.main) {
  let terminal: ReturnType<typeof openTerminal>;
  try {
    const args = process.argv.slice(2);
    const options = parseOptions(args, detectAgents(name => Bun.which(name)));
    if (options.help) console.log(helpText);
    else {
      if (!options.yes && !options.dryRun) terminal = openTerminal();
      await initialize(JSON.parse(readFileSync(resolve(root, "favorites.json"), "utf8")), options, {
        which: name => Bun.which(name), log: console.log, ask: terminal?.ask,
        load(path) { if (!existsSync(path)) return undefined; const record = JSON.parse(readFileSync(path, "utf8")); if (record.version !== 1 || !record.options || !record.ownedPaths) throw new Error(`Invalid installer settings: ${path}`); return record; },
        save(path, record) { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, JSON.stringify(record, null, 2) + "\n", { mode: 0o600 }); },
        prepare: (chosen, source) => source === "clzmj/skills" ? source : prepareHostedRelease({ scope: chosen.scope, projectRoot: chosen.projectDir, dryRun: false, source }),
        finish: async (chosen, source) => { const path = await registerLauncher({ scope: chosen.scope, projectRoot: chosen.projectDir, dryRun: false, source: source === "clzmj/skills" ? root : source }); if (path) console.log(`Launcher: ${path}`); },
        fingerprint,
        link(source, destination) { mkdirSync(dirname(destination), { recursive: true }); symlinkSync(source, destination); },
        run(command, capture, cwd) {
          const result = Bun.spawnSync(command, { cwd, stdout: capture ? "pipe" : "inherit", stderr: "inherit", stdin: "inherit" });
          if (result.error) throw result.error;
          if (result.exitCode !== 0) throw new Error(`Command failed (${result.exitCode}): ${displayCommand(command)}`);
          return capture ? result.stdout.toString() : "";
        },
      }, undefined, resolveCoreSource(process.env.CLZMJ_SKILLS_SOURCE));
    }
  } catch (error) {
    console.error(`Initialization failed: ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  } finally { terminal?.close(); }
}
