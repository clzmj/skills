import { existsSync, lstatSync, readFileSync, realpathSync, statSync, mkdirSync, writeFileSync, symlinkSync } from "node:fs";
import { isAbsolute, resolve, dirname } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";
import { InstallScope, prepareHostedRelease, registerLauncher, launcherPath, fingerprint } from "./self-update";
import { openTerminalUI, TerminalBack, TerminalCancelled, type TerminalUI } from "./terminal-ui";

export type Agent = "codex" | "claude-code";
export type Favorite = ({ type: "skills"; skills: string[] } | { type: "plugin"; marketplace: string; plugin: string; installedAs?: string[] }) & { source: string; description?: string };
export type Options = {
  agents: Agent[]; dryRun: boolean; hooks: boolean; scope: InstallScope;
  projectDir: string; method: "copy" | "symlink"; core: string[]; favorites?: string[];
  packSkills: Record<string, string[]>; yes: boolean; update: boolean; help: boolean; provided?: string[];
};
export type State = { plugins: Set<string>; marketplaces: Set<string> };
export type Command = string[];
export type Skill = { name: string; description: string };
export type InstallRecord = { version: 1; options: Options; ownedPaths: Record<string, string> };
export type PageUI = { [K in keyof TerminalUI]: TerminalUI[K] extends (...args: infer Args) => infer Result ? (...args: Args) => Result | Awaited<Result> : TerminalUI[K] };
export type Runtime = {
  which: (name: string) => string | null;
  run: (command: Command, capture: boolean, cwd?: string) => string | Promise<string>;
  log: (message: string) => void;
  exists?: (path: string) => boolean;
  ui?: PageUI;
  cancel?: () => void | Promise<void>;
  signal?: AbortSignal;
  discover?: (source: string, signal?: AbortSignal) => Promise<Skill[]>;
  save?: (path: string, record: InstallRecord) => void;
  load?: (path: string) => InstallRecord | undefined;
  prepare?: (options: Options, source: string) => Promise<string> | string;
  finish?: (options: Options, source: string) => Promise<void> | void;
  fingerprint?: (path: string) => string;
  link?: (source: string, destination: string) => void;
};
const root = fileURLToPath(new URL("../", import.meta.url));
const agents: Agent[] = ["codex", "claude-code"];
const CORE_SOURCE = "clzmj/skills";
const coreSkills: Skill[] = [
  { name: "rex", description: "Visible terminal work and conversational agent coordination" },
  { name: "python-tooling", description: "Python environments, dependency management, formatting and checks" },
];
const namePattern = /^[a-zA-Z0-9][a-zA-Z0-9._-]*$/;
const pathExists = (path: string) => { try { lstatSync(path); return true; } catch (error: any) { if (error.code === "ENOENT") return false; throw error; } };
const cli = (agent: Agent) => agent === "codex" ? "codex" : "claude";
export const settingsPath = (options: Pick<Options, "scope" | "projectDir">) => options.scope === InstallScope.Project
  ? resolve(options.projectDir, ".clzmj-skills/install.json") : resolve(homedir(), ".config/clzmj-skills/install.json");

export function parseOptions(args: string[], detected: Agent[] = [], cwd = process.cwd()): Options {
  const options: Options = { agents: [...detected], dryRun: false, hooks: false, scope: InstallScope.Global, projectDir: cwd, method: "symlink", core: coreSkills.map(s => s.name), favorites: [], packSkills: {}, yes: false, update: false, help: false, provided: [...args] };
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
      if (flag === "--scope") { if (value !== InstallScope.Global && value !== InstallScope.Project) throw new Error("--scope must be global or project"); options.scope = value; }
      else if (flag === "--method") { if (value !== "copy" && value !== "symlink") throw new Error("--method must be copy or symlink"); options.method = value; }
      else if (flag === "--project-dir") { if (!isAbsolute(value)) throw new Error("--project-dir must be an absolute directory"); options.projectDir = resolve(value); }
      else {
        const [source, names, extra] = value.split(":");
        if (!/^[\w.-]+\/[\w.-]+$/.test(source) || !names || extra || !names.split(",").every(name => namePattern.test(name))) throw new Error("--skills expects owner/repo:skill,skill");
        options.packSkills[source] = [...new Set(names.split(","))];
      }
    } else throw new Error(`Unknown argument: ${flag}`);
  }
  if (options.scope === InstallScope.Project && !args.includes("--project-dir") && options.yes) throw new Error("Unattended project installs require --project-dir /absolute/directory");
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
  if (value === undefined) return CORE_SOURCE;
  if (!isAbsolute(value) || realpathSync(value) !== realpathSync(directory)) throw new Error("CLZMJ_SKILLS_SOURCE must point to this extracted release root");
  for (const skill of coreSkills) if (!statSync(resolve(value, "skills", skill.name, "SKILL.md")).isFile()) throw new Error(`Missing bundled skill: ${skill.name}`);
  return realpathSync(value);
}

export function detectAgents(which: Runtime["which"], exists: (path: string) => boolean = existsSync): Agent[] {
  return agents.filter(agent => which(cli(agent)) || exists(agent === "codex" ? process.env.CODEX_HOME || resolve(homedir(), ".codex") : process.env.CLAUDE_CONFIG_DIR || resolve(homedir(), ".claude")));
}
export const skillDirectory = (agent: Agent, options: Options, home = homedir()) => options.scope === InstallScope.Project
  ? resolve(options.projectDir, agent === "codex" ? ".agents/skills" : ".claude/skills")
  : agent === "codex" ? resolve(home, ".agents/skills") : resolve(process.env.CLAUDE_CONFIG_DIR || resolve(home, ".claude"), "skills");
const skillsCommand = (source: string, names: string[], selected: Agent[], options: Options): Command => {
  // Skills CLI auto-copies when only one agent directory is targeted. Include
  // the universal canonical destination so Claude really receives a symlink.
  const targets = options.method === "symlink" && selected.includes("claude-code") && !selected.includes("codex") ? ["codex", ...selected] : selected;
  return ["bunx", "--bun", "skills", "add", source, "--skill", ...names, ...(options.scope === InstallScope.Global ? ["-g"] : []), "-a", ...targets, ...(options.method === "copy" ? ["--copy"] : []), "-y"];
};
export const displayCommand = (command: Command) => command.map(value => /^[\w./:@-]+$/.test(value) ? value : `'${value.replaceAll("'", "'\\''")}'`).join(" ");

export async function discoverPack(source: string, signal?: AbortSignal): Promise<Skill[]> {
  const get = async (url: string) => {
    const response = await fetch(url, { headers: { Accept: "application/vnd.github+json" }, signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(20_000)]) : AbortSignal.timeout(20_000) });
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

async function activity<T>(runtime: Runtime, label: string, action: () => T | Promise<T>, counts = { completed: 0, total: 0, failed: 0 }): Promise<T> {
  if (runtime.signal?.aborted) throw new TerminalCancelled();
  if (!runtime.ui) return await action();
  const started = Date.now();
  const draw = () => { runtime.ui!.pollCancel(); runtime.ui!.progress({ ...counts, title: counts.total ? "Installing" : label, label, elapsedMs: Date.now() - started }); };
  draw();
  let cancellationError: unknown, cleanup: Promise<void> | undefined;
  const timer = setInterval(() => {
    if (cancellationError) return;
    try { draw(); }
    catch (error) { cancellationError = runtime.signal?.aborted ? new TerminalCancelled() : error; cleanup = Promise.resolve(runtime.cancel?.()); }
  }, 100);
  try {
    const result = await action();
    if (cancellationError) { await cleanup; throw cancellationError; }
    return result;
  } catch (error) {
    if (cancellationError) { await cleanup; throw cancellationError; }
    throw error;
  } finally { clearInterval(timer); }
}

function validateCatalog(skills: Skill[], source: string): Skill[] {
  if (!skills.length || skills.some(skill => !namePattern.test(skill.name)) || new Set(skills.map(skill => skill.name)).size !== skills.length) throw new Error(`Invalid discovered skill catalog: ${source}`);
  return skills;
}

export async function chooseOptions(options: Options, favorites: Favorite[], runtime: Runtime, resume = false, catalogs = new Map<string, Skill[]>()): Promise<Options> {
  if (options.yes || options.dryRun) return options;
  const ui = runtime.ui;
  if (!ui) throw new Error("No controlling terminal. Run in a terminal or pass --yes with explicit choices for unattended installation.");
  const chosen = { ...options, packSkills: { ...options.packSkills } };
  let page = resume ? Number.MAX_SAFE_INTEGER : 0;
  while (true) {
    const pages: (() => void | Promise<void>)[] = [
      async () => { chosen.agents = await ui.multiselect("Agents", agents.map(name => ({ name, description: name === "codex" ? "OpenAI Codex" : "Claude Code" })), chosen.agents, undefined, 1) as Agent[]; },
      async () => { chosen.scope = await ui.select("Installation scope", [{ name: InstallScope.Global, description: "Install for your user account" }, { name: InstallScope.Project, description: "Install only in one project directory" }], chosen.scope) as Options["scope"]; },
      ...(chosen.scope === InstallScope.Project ? [async () => { chosen.projectDir = realpathSync(await ui.directory("Project directory", chosen.projectDir)); }] : []),
      async () => { chosen.method = await ui.select("Installation method", [{ name: "symlink", description: "Keep one shared canonical copy per scope" }, { name: "copy", description: "Copy ordinary skills into each selected agent" }], chosen.method, "Native plugins keep their complete host packaging.") as Options["method"]; },
      async () => {
        const available = favorites.filter(favorite => chosen.scope !== InstallScope.Project || favorite.type === "skills");
        chosen.favorites = await ui.multiselect("Favorite packs", available.map(favorite => ({ name: favorite.source, description: `${favorite.description || (favorite.type === "plugin" ? "Complete native plugin pack" : "Upstream skill pack")} · owner ${favorite.source.split("/")[0]}` })), chosen.favorites ?? available.map(favorite => favorite.source), chosen.scope === InstallScope.Project ? "Native plugins require global scope and are omitted here. Choose none for core-only." : "Choose none for core-only. Native plugins retain their host packaging.");
      },
      async () => {
        const rows = coreSkills.map(skill => ({ name: `${CORE_SOURCE}/${skill.name}`, source: CORE_SOURCE, skill: skill.name, description: skill.description }));
        const defaults = rows.filter(row => chosen.core.includes(row.skill)).map(row => row.name);
        const selectedPacks = favorites.filter(favorite => favorite.type === "skills" && (chosen.favorites === undefined || chosen.favorites.includes(favorite.source)));
        for (const favorite of selectedPacks) {
          if (favorite.type !== "skills") continue;
          let catalog = catalogs.get(favorite.source);
          if (!catalog) {
            catalog = favorite.skills.includes("*") ? validateCatalog(await activity(runtime, `Discovering ${favorite.source}`, () => (runtime.discover ?? discoverPack)(favorite.source, runtime.signal)), favorite.source) : favorite.skills.map(name => ({ name, description: `From ${favorite.source}` }));
            catalogs.set(favorite.source, catalog);
          }
          const selected = chosen.packSkills[favorite.source] ?? catalog.map(skill => skill.name);
          for (const skill of catalog) {
            const row = { name: `${favorite.source}/${skill.name}`, source: favorite.source, skill: skill.name, description: skill.description };
            rows.push(row);
            if (selected.includes(skill.name)) defaults.push(row.name);
          }
        }
        const keys = new Set(await ui.multiselect("Individual skills", rows, defaults, `${rows.length} skills · ${new Set(rows.map(row => row.source)).size} sources`));
        if ([...keys].some(key => !rows.some(row => row.name === key))) throw new Error("Unknown individual skill selection");
        chosen.core = rows.filter(row => row.source === CORE_SOURCE && keys.has(row.name)).map(row => row.skill);
        for (const favorite of selectedPacks) chosen.packSkills[favorite.source] = rows.filter(row => row.source === favorite.source && keys.has(row.name)).map(row => row.skill);
      },
      ...(chosen.scope === InstallScope.Global ? [async () => { chosen.hooks = await ui.confirm("Hooks", chosen.hooks, ["Install independent Rex and Python hooks in your home settings?", "Hooks are separate from ordinary skill selection."]); }] : []),
    ];
    page = Math.min(page, pages.length - 1);
    try { await pages[page](); }
    catch (error) { if (error instanceof TerminalBack) { page = Math.max(0, page - 1); continue; } throw error; }
    // Recompute pages after scope/pack changes; the next page follows this one.
    page++;
    const extraDirectory = chosen.scope === InstallScope.Project ? 1 : 0;
    const length = 5 + extraDirectory + (chosen.scope === InstallScope.Global ? 1 : 0);
    if (page >= length) break;
  }
  if (chosen.scope === InstallScope.Project) chosen.hooks = false;
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
      const canonical = resolve(options.scope === InstallScope.Global ? homedir() : options.projectDir, ".agents/skills", skill);
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
    if (options.scope === InstallScope.Project) { skipped.push(`unsupported project-native plugin: ${favorite.plugin} for ${agent}; no global changes`); continue; }
    if ([`${favorite.plugin}@${favorite.marketplace}`, ...(favorite.installedAs ?? [])].some(id => states[agent]?.plugins.has(id))) { skipped.push(`already-installed: ${favorite.plugin} for ${agent}; existing enabled/disabled/pinned state preserved`); continue; }
    if (!known.get(agent)!.has(favorite.marketplace)) {
      steps.push({ command: [cli(agent), "plugin", "marketplace", "add", favorite.source], label: `marketplace: ${favorite.marketplace} for ${agent}`, paths: [], kind: "plugin" });
      known.get(agent)!.add(favorite.marketplace);
    }
    steps.push({ command: [cli(agent), "plugin", agent === "codex" ? "add" : "install", `${favorite.plugin}@${favorite.marketplace}`], label: `install native plugin: ${favorite.plugin} for ${agent}`, paths: [], kind: "plugin" });
  }
  if (options.hooks && options.scope === InstallScope.Project) skipped.push("unsupported project hooks: global hook installers skipped; no home settings changed");
  else if (options.hooks) for (const { name: skill } of coreSkills) {
    // Run the bundled installer independently of whether the skill was already installed.
    steps.push({ command: ["uv", "run", "--script", resolve(root, "skills", skill, "scripts/install_hooks.py"), "--agents", ...options.agents], label: `global hooks: ${skill}`, paths: [], kind: "hook" });
  }
  return { steps, skipped };
}
export function planCommands(favorites: Favorite[], options: Options, states: Partial<Record<Agent, State>>, _directory?: string, coreSource = CORE_SOURCE): Command[] { return buildPlan(favorites, options, states, coreSource).steps.filter(step => !step.link).map(step => step.command); }

export async function initialize(manifest: unknown, initial: Options, runtime: Runtime, _directory?: string, coreSource = CORE_SOURCE): Promise<void> {
  const favorites = parseManifest(manifest);
  let options = { ...initial, packSkills: { ...initial.packSkills } };
  const saved = runtime.load?.(settingsPath(options));
  if (options.update && saved) {
    options = { ...saved.options, packSkills: { ...saved.options.packSkills }, dryRun: initial.dryRun, yes: initial.yes, update: true, help: initial.help, provided: initial.provided };
    const flags: [string[], keyof Options][] = [[["--agents"], "agents"], [["--core"], "core"], [["--core-only", "--all-favorites", "--favorites"], "favorites"], [["--skills"], "packSkills"], [["--hooks", "--no-hooks"], "hooks"], [["--scope"], "scope"], [["--project-dir"], "projectDir"], [["--method"], "method"]];
    for (const [names, key] of flags) if (names.some(flag => initial.provided?.includes(flag))) (options as any)[key] = key === "packSkills" ? { ...saved.options.packSkills, ...initial.packSkills } : initial[key];
  }
  const exists = runtime.exists ?? pathExists;
  let plan: ReturnType<typeof buildPlan>;
  let selected: Favorite[];
  let states: Partial<Record<Agent, State>>;
  let resume = false;
  const catalogs = new Map<string, Skill[]>();
  let interactiveSelections = { ...options.packSkills };
  while (true) {
    if (resume) options.packSkills = { ...interactiveSelections, ...options.packSkills };
    options = await chooseOptions(options, favorites, runtime, resume, catalogs);
    interactiveSelections = { ...interactiveSelections, ...options.packSkills };
    if (!options.agents.length) throw new Error("No agents selected/detected. Pass --agents codex and/or claude-code.");
    if (options.scope === InstallScope.Project && !statSync(options.projectDir).isDirectory()) throw new Error("Project directory must exist");
    if (options.favorites?.some(source => !favorites.some(favorite => favorite.source === source))) throw new Error("Unknown favorite source; use an owner/repo listed in favorites.json");
    selected = favorites.filter(favorite => options.favorites === undefined || options.favorites.includes(favorite.source)).map(favorite => ({ ...favorite }));
    for (const source of Object.keys(options.packSkills)) if (!selected.some(favorite => favorite.source === source && favorite.type === "skills")) {
      if (initial.provided?.includes("--skills") && initial.packSkills[source]) throw new Error(`--skills source is not a selected ordinary pack: ${source}`);
      delete options.packSkills[source];
    }
    options.favorites = selected.map(favorite => favorite.source);
    for (const favorite of selected) if (favorite.type === "skills") {
      if (options.packSkills[favorite.source]) favorite.skills = options.packSkills[favorite.source];
      else if (favorite.skills.includes("*")) {
        const discovered = validateCatalog(await activity(runtime, `Discovering ${favorite.source}`, () => (runtime.discover ?? discoverPack)(favorite.source, runtime.signal)), favorite.source);
        favorite.skills = discovered.map(skill => skill.name);
        runtime.log(`${favorite.source}: discovered ${discovered.length} skills`);
      }
      options.packSkills[favorite.source] = favorite.skills;
    }
    const tools = new Set<string>();
    if (options.core.length || selected.some(favorite => favorite.type === "skills" && favorite.skills.length)) { tools.add("bunx"); tools.add("git"); }
    if (options.hooks && options.scope === InstallScope.Global) tools.add("uv");
    if (options.scope === InstallScope.Global && selected.some(favorite => favorite.type === "plugin")) for (const agent of options.agents) tools.add(cli(agent));
    const missing = [...tools].filter(tool => !runtime.which(tool));
    if (missing.length) throw new Error(`Missing required tools: ${missing.join(", ")}`);
    states = {};
    if (!options.dryRun && options.scope === InstallScope.Global && selected.some(favorite => favorite.type === "plugin")) for (const agent of options.agents) {
      states[agent] = await activity(runtime, `Checking ${agent} native plugins`, async () => parseState(agent,
        JSON.parse(await runtime.run([cli(agent), "plugin", "list", "--json"], true, options.projectDir)),
        JSON.parse(await runtime.run([cli(agent), "plugin", "marketplace", "list", "--json"], true, options.projectDir))));
    }
    plan = buildPlan(selected, options, states, coreSource, exists);
    const summary = [
      `Agents: ${options.agents.join(", ")}`,
      `Scope: ${options.scope}${options.scope === InstallScope.Project ? ` at ${options.projectDir}` : " (user home)"}`,
      `Method: ${options.method}; native plugins retain complete host packaging`,
      ...(options.method === "symlink" ? [`Shared skill storage: ${resolve(options.scope === InstallScope.Global ? homedir() : options.projectDir, ".agents/skills")}${options.agents.includes("claude-code") ? "; Claude links to this directory" : ""}`] : []),
      `Core: ${options.core.join(", ") || "none"}`,
      ...selected.map(favorite => `Pack: ${favorite.source}${favorite.type === "skills" ? ` · ${favorite.skills.length} skills` : " · native plugin"}`),
      `Hooks: ${options.hooks && options.scope === InstallScope.Global ? "selected (home settings)" : "skipped"}`,
      `Plan: ${plan.steps.length} operations; ${plan.skipped.length} preserved or skipped`,
      ...plan.skipped.map(line => `Preserved/skipped: ${line}`),
      `Settings: ${settingsPath(options)}`,
      `Launcher: ${launcherPath({ scope: options.scope, projectRoot: options.projectDir })}`,
    ];
    if (process.env.CLZMJ_SKILLS_VERSION) summary.push(`Release cache: ${resolve(options.scope === InstallScope.Global ? resolve(homedir(), ".local/share/clzmj-skills/releases") : resolve(options.projectDir, ".clzmj-skills/releases"), process.env.CLZMJ_SKILLS_VERSION)}`);
    else summary.push(`Launcher source: ${coreSource === CORE_SOURCE ? root : coreSource}`);
    if (options.hooks && options.scope === InstallScope.Global) {
      summary.push(`Hook runtime: ${resolve(homedir(), ".local/share/agent-tooling")}`);
      for (const agent of options.agents) summary.push(`Hook settings: ${resolve(homedir(), agent === "codex" ? ".codex/hooks.json" : ".claude/settings.json")}`, `Rex instructions: ${resolve(homedir(), agent === "codex" ? ".codex/AGENTS.md" : ".claude/CLAUDE.md")}`);
    }
    if (!runtime.ui) {
      runtime.log(`\nInstallation preview: ${options.scope}${options.scope === InstallScope.Project ? ` at ${options.projectDir}` : " (user home)"}; ${options.method}; agents ${options.agents.join(", ")}`);
      summary.forEach(runtime.log);
      if (options.dryRun) runtime.log("Dry run: no subprocesses or writes. Native plugin commands are conditional on inventory; read-only network discovery may occur.");
      for (const step of plan.steps) { runtime.log(`${step.label}: ${step.link ? `${step.link.destination} -> ${step.link.source}` : displayCommand(step.command)}`); for (const path of step.paths) runtime.log(`  path: ${path}`); }
    }
    if (options.dryRun) return;
    if (!options.yes) {
      try {
        if (!await runtime.ui!.review("Review installation", summary)) { await runtime.ui!.finish("Installation cancelled", ["No installation changes made."]); return; }
      } catch (error) { if (error instanceof TerminalBack) { resume = true; continue; } throw error; }
    }
    break;
  }
  if (runtime.prepare) { coreSource = await activity(runtime, "Preparing verified release", () => runtime.prepare!(options, coreSource)); plan = buildPlan(selected!, options, states!, coreSource, exists); }
  const scopedSaved = runtime.load?.(settingsPath(options));
  const record: InstallRecord = { version: 1, options, ownedPaths: { ...(scopedSaved?.ownedPaths ?? {}) } };
  let completed = 0, failed = 0;
  const changed = new Set<string>(), errors: string[] = [];
  for (const step of plan!.steps) {
    const fresh = step.paths.filter(path => !exists(path));
    const previouslyOwned = step.paths.filter(path => record.ownedPaths[path]);
    if (!runtime.ui) runtime.log(step.label);
    try {
      await activity(runtime, step.label, async () => {
        if (step.link) { if (!runtime.link) throw new Error("Native symlink operation unavailable"); runtime.link(step.link.source, step.link.destination); }
        else await runtime.run(step.command, false, options.projectDir);
      }, { completed, total: plan!.steps.length, failed });
      completed++;
      for (const path of step.paths) if (exists(path)) changed.add(path);
      for (const path of [...fresh, ...previouslyOwned]) if (exists(path) && runtime.fingerprint) record.ownedPaths[path] = runtime.fingerprint(path);
    } catch (error) {
      if (error instanceof TerminalCancelled) { runtime.save?.(settingsPath(options), record); throw error; }
      failed++;
      const details = [`Failed ${step.label}: ${error instanceof Error ? error.message : String(error)}`];
      if (!step.link) details.push(`Retry from ${options.projectDir}: ${displayCommand(step.command)}`);
      details.push(`Installer retry (explicitly updates all saved ordinary selections): ${displayCommand(["bun", resolve(coreSource === CORE_SOURCE ? root : coreSource, "scripts/init.ts"), "--update", "--yes", "--scope", options.scope, ...(options.scope === InstallScope.Project ? ["--project-dir", options.projectDir] : [])])}`);
      errors.push(...details); if (!runtime.ui) details.forEach(runtime.log);
    }
    runtime.save?.(settingsPath(options), record);
    runtime.ui?.progress({ completed, total: plan!.steps.length, failed, label: completed + failed === plan!.steps.length ? "Finished" : "Preparing next operation", elapsedMs: 0 });
  }
  runtime.save?.(settingsPath(options), record);
  if (!failed) await runtime.finish?.(options, coreSource);
  const result = [`Summary: ${completed} commands completed; ${plan!.skipped.length} skipped; ${failed} failed.`, ...[...changed].map(path => `Changed path: ${path}`)];
  if (options.hooks && options.scope === InstallScope.Global) result.push("Hook settings may have changed; review each host's /hooks menu. Uninstall retains shared host hook settings.");
  if (!runtime.ui) result.forEach(runtime.log);
  if (failed) throw new Error(`${failed} installation command(s) failed; completed installations were preserved. Retry with the same choices.\n${errors.join("\n")}\n${result.join("\n")}`);
  if (runtime.ui) await runtime.ui.finish("Installation complete", [
    result[0],
    `Agents: ${options.agents.join(", ")}`,
    `Scope: ${options.scope}${options.scope === InstallScope.Project ? ` at ${options.projectDir}` : " (user home)"}`,
    `Method: ${options.method}`,
    `Core: ${options.core.join(", ") || "none"}`,
    ...selected!.map(favorite => `Pack: ${favorite.source}${favorite.type === "skills" ? ` · ${favorite.skills.length} skills` : " · native plugin"}`),
    `Launcher: ${launcherPath({ scope: options.scope, projectRoot: options.projectDir })}`,
    `Next: ${options.scope === InstallScope.Global ? "clzmj-skills help" : `${launcherPath({ scope: options.scope, projectRoot: options.projectDir })} help`}`,
    ...(options.hooks && options.scope === InstallScope.Global ? ["Review new hooks in each host's /hooks menu."] : []),
  ]);
}

export const helpText = `Usage: clzmj-skills [install|update] [options]\n  --agents codex claude-code   Select hosts (detected hosts preselected)\n  --scope global|project       Global default; project uses exact --project-dir\n  --project-dir /absolute/dir  Existing project working directory\n  --method symlink|copy        Ordinary skills; symlink default\n  --core rex python-tooling    Selected core skills, or --core none\n  --core-only                 No favorite packs (default)\n  --all-favorites             All curated packs\n  --favorites owner/repo ...  Select favorite packs\n  --skills owner/repo:a,b      Individual ordinary pack skills\n  --hooks / --no-hooks         Independent global hook choice (default off)\n  --update                    Explicitly reinstall selected ordinary skills\n  --yes                       Accept choices without a terminal\n  --dry-run                   Preview; no subprocesses or writes\n  --help                      Show this help\nProject scope skips unsupported native plugins and hooks. Existing skills and native plugins are preserved by default.`;

// Drain both pipes concurrently and retain only bounded diagnostics. Child output
// never writes into the terminal pages, including noisy upstream installers.
async function boundedOutput(stream: ReadableStream<Uint8Array>, maximum: number): Promise<{ text: string; overflow: boolean }> {
  const reader = stream.getReader();
  let bytes = Buffer.alloc(0), overflow = false;
  while (true) {
    const { value, done } = await reader.read(); if (done) break;
    if (bytes.length + value.length > maximum) overflow = true;
    bytes = Buffer.concat([bytes, Buffer.from(value)]).subarray(-maximum);
  }
  return { text: bytes.toString("utf8").replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g, "").replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, ""), overflow };
}

export async function runSubprocess(command: Command, capture: boolean, cwd?: string, active?: (child?: Bun.Subprocess) => void): Promise<string> {
  const child = Bun.spawn(command, { cwd, detached: process.platform !== "win32", stdin: "inherit", stdout: "pipe", stderr: "pipe" });
  active?.(child);
  try {
    const [code, stdout, stderr] = await Promise.all([child.exited, boundedOutput(child.stdout, capture ? 2 * 1024 * 1024 : 128 * 1024), boundedOutput(child.stderr, 128 * 1024)]);
    if (code !== 0) throw new Error(`Command failed (${code}): ${displayCommand(command)}\n${stderr.text || stdout.text}`);
    if (capture && stdout.overflow) throw new Error(`Inventory output exceeded the 2 MiB limit: ${displayCommand(command)}`);
    return capture ? stdout.text : "";
  } finally { active?.(); }
}

async function terminateProcessGroup(child: Bun.Subprocess): Promise<void> {
  const send = (signal: NodeJS.Signals) => {
    try { if (process.platform === "win32") child.kill(signal); else process.kill(-child.pid, signal); }
    catch (error: any) { if (error.code !== "ESRCH") throw error; }
  };
  send("SIGTERM");
  // A descendant may keep the pipes open after the leader exits or ignore TERM.
  await new Promise(resolve => setTimeout(resolve, 250));
  send("SIGKILL");
  await child.exited;
}

if (import.meta.main) {
  let terminal: TerminalUI | undefined;
  let child: Bun.Subprocess | undefined;
  let work: Promise<string> | undefined;
  const controller = new AbortController();
  let cancellation: Promise<void> | undefined, interrupted: NodeJS.Signals | undefined;
  const cancel = () => cancellation ??= (async () => {
    controller.abort();
    const pending = work, current = child;
    if (current) await terminateProcessGroup(current);
    if (pending) await pending.catch(() => {});
  })();
  const stop = (signal: NodeJS.Signals) => { interrupted = signal; terminal?.close(); void cancel(); };
  try {
    const args = process.argv.slice(2);
    const options = parseOptions(args, detectAgents(name => Bun.which(name)));
    if (options.help) console.log(helpText);
    else {
      if (!options.yes && !options.dryRun) terminal = openTerminalUI({ onSignal: stop });
      if (!terminal) for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"] as const) process.once(signal, stop);
      await initialize(JSON.parse(readFileSync(resolve(root, "favorites.json"), "utf8")), options, {
        which: name => Bun.which(name), log: message => { if (!terminal) console.log(message); }, ui: terminal,
        cancel, signal: controller.signal,
        load(path) { if (!existsSync(path)) return undefined; const record = JSON.parse(readFileSync(path, "utf8")); if (record.version !== 1 || !record.options || !record.ownedPaths) throw new Error(`Invalid installer settings: ${path}`); return record; },
        save(path, record) { mkdirSync(dirname(path), { recursive: true }); writeFileSync(path, JSON.stringify(record, null, 2) + "\n", { mode: 0o600 }); },
        prepare: (chosen, source) => source === CORE_SOURCE ? source : prepareHostedRelease({ scope: chosen.scope, projectRoot: chosen.projectDir, dryRun: false, source }),
        finish: async (chosen, source) => { const path = await registerLauncher({ scope: chosen.scope, projectRoot: chosen.projectDir, dryRun: false, source: source === CORE_SOURCE ? root : source }); if (path && !terminal) console.log(`Launcher: ${path}`); },
        fingerprint,
        link(source, destination) { mkdirSync(dirname(destination), { recursive: true }); symlinkSync(source, destination); },
        run: async (command, capture, cwd) => {
          const pending = runSubprocess(command, capture, cwd, current => { child = current; }); work = pending;
          try { return await pending; } finally { if (work === pending) work = undefined; }
        },
      }, undefined, resolveCoreSource(process.env.CLZMJ_SKILLS_SOURCE));
    }
  } catch (error) {
    if (interrupted) { await cancellation; process.exitCode = interrupted === "SIGINT" ? 130 : interrupted === "SIGTERM" ? 143 : 129; }
    else if (error instanceof TerminalCancelled) { terminal?.close(); console.error("Installation cancelled; completed installations were preserved."); process.exitCode = 130; }
    else {
      const message = `Initialization failed: ${error instanceof Error ? error.message : String(error)}`;
      if (terminal) { try { await terminal.finish("Installation failed", message.split("\n")); } catch {} }
      else console.error(message);
      process.exitCode = 1;
    }
  } finally {
    if (interrupted) { await cancellation; process.exitCode = interrupted === "SIGINT" ? 130 : interrupted === "SIGTERM" ? 143 : 129; }
    terminal?.close();
    for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"] as const) process.off(signal, stop);
  }
}
