import { archiveExtractor } from "./archive-source";

export const origin = "https://skills.clzmj.dev";
export const versionPattern = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
export const validVersion = (version: string) => versionPattern.test(version) && !version.includes("..");

export function installer(): string {
  return `#!/bin/sh
set -eu
command -v bun >/dev/null 2>&1 || { echo 'Install Bun first: https://bun.sh' >&2; exit 1; }
# Bun performs HTTPS downloads and checksum verification; no repository credentials are needed.
bun - "$@" <<'CLZMJ_INSTALLER'
const extractArchive = ${archiveExtractor};
${bootstrap}
CLZMJ_INSTALLER
`;
}

const bootstrap = String.raw`
const { mkdtempSync, rmSync } = await import("node:fs");
const { join } = await import("node:path");
const { tmpdir } = await import("node:os");
const { createHash } = await import("node:crypto");
const origin = "https://skills.clzmj.dev";
const args = process.argv.slice(2);
const dryRun = args.includes("--dry-run");
let temporary;
try {
  if (args.includes("--help") || args.includes("-h")) {
    console.log("clzmj-skills installer: --scope global|project --project-dir PATH --method copy|symlink --agents codex claude-code --core rex python-tooling --favorites SOURCE --skills SOURCE:SKILL --hooks --no-hooks --yes --dry-run --update\nAfter installation: clzmj-skills install|update|self-update|uninstall|help");
    process.exit(0);
  }
  const booleans = new Set(["--core-only", "--all-favorites", "--hooks", "--no-hooks", "--yes", "-y", "--update", "--dry-run"]);
  const choices = { "--scope": ["global", "project"], "--method": ["copy", "symlink"] };
  const lists = new Set(["--agents", "--core", "--favorites", "--skills"]);
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (booleans.has(flag)) continue;
    if (flag in choices || flag === "--project-dir") {
      const value = args[++index];
      if (!value || value.startsWith("--") || (flag in choices && !choices[flag].includes(value)) || (flag === "--project-dir" && !value.startsWith("/"))) throw new Error("Invalid value for " + flag);
    } else if (lists.has(flag)) {
      let count = 0;
      while (args[index + 1] && !args[index + 1].startsWith("-")) {
        const value = args[++index]; count++;
        if ((flag === "--agents" && !value.split(",").every(agent => ["codex", "claude-code"].includes(agent))) || (flag === "--core" && !value.split(",").every(skill => ["rex", "python-tooling", "none"].includes(skill)))) throw new Error("Invalid value for " + flag);
        if (flag === "--skills" && !/^[\w.-]+\/[\w.-]+:[A-Za-z0-9][A-Za-z0-9._-]*(,[A-Za-z0-9][A-Za-z0-9._-]*)*$/.test(value)) throw new Error("Invalid value for --skills");
      }
      if (!count) throw new Error("Missing value for " + flag);
    } else throw new Error("Unknown argument: " + flag);
  }
  const manifestResponse = await fetch(origin + "/latest.json", { redirect: "error" });
  if (!manifestResponse.ok) throw new Error("Release manifest download failed: " + manifestResponse.status);
  const manifest = await manifestResponse.json();
  const { version, sha256, url } = manifest;
  if (typeof version !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(version) || version.includes("..") || !/^[a-f0-9]{64}$/.test(sha256) || url !== "/releases/" + version + "/skills.tar.gz") throw new Error("Invalid release manifest");
  const archiveResponse = await fetch(origin + url, { redirect: "error" });
  if (!archiveResponse.ok) throw new Error("Release download failed: " + archiveResponse.status);
  if (Number(archiveResponse.headers.get("content-length")) > 16 * 1024 * 1024) throw new Error("Release is too large");
  const archive = new Uint8Array(await archiveResponse.arrayBuffer());
  if (archive.length > 16 * 1024 * 1024 || createHash("sha256").update(archive).digest("hex") !== sha256) throw new Error("Release checksum mismatch");
  temporary = mkdtempSync(join(tmpdir(), "clzmj-skills-"));
  const extracted = join(temporary, "release");
  await extractArchive(archive, extracted);
  const source = extracted;
  if (dryRun) console.log("Bootstrap dry run: downloaded and verified in temporary storage; no persistent release or installation writes.");
  // The shell/Bun heredoc consumes stdin; reconnect interactive children to the terminal.
  let input = "inherit";
  try { input = (await import("node:fs")).openSync("/dev/tty", "r"); } catch {}
  const result = Bun.spawnSync([process.execPath, join(source, "scripts/init.ts"), ...args], { env: { ...process.env, CLZMJ_SKILLS_SOURCE: source, CLZMJ_SKILLS_VERSION: version, CLZMJ_SKILLS_SHA256: sha256 }, stdin: input, stdout: "inherit", stderr: "inherit" });
  if (result.error) throw result.error;
  process.exitCode = result.exitCode;
} catch (error) {
  console.error("Installation failed: " + (error instanceof Error ? error.message : String(error)));
  process.exitCode = 1;
} finally { if (temporary) rmSync(temporary, { recursive: true, force: true }); }
`;
