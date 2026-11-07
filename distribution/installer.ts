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
const { mkdtempSync, mkdirSync, rmSync, renameSync, existsSync, readFileSync, writeFileSync, lstatSync } = await import("node:fs");
const { resolve, join } = await import("node:path");
const { homedir, tmpdir } = await import("node:os");
const { createHash } = await import("node:crypto");
const origin = "https://skills.clzmj.dev";
const args = process.argv.slice(2);
const dryRun = args.includes("--dry-run");
let temporary;
try {
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
  let source = extracted;
  if (dryRun) console.log("Bootstrap dry run: downloaded and verified in temporary storage; no persistent release or installation writes.");
  else {
    const releases = resolve(homedir(), ".local/share/clzmj-skills/releases");
    mkdirSync(releases, { recursive: true });
    source = join(releases, version);
    if (existsSync(source)) {
      if (!lstatSync(source).isDirectory() || lstatSync(source).isSymbolicLink() || readFileSync(join(source, ".release-sha256"), "utf8") !== sha256) throw new Error("Existing release directory does not match this release: " + source);
      // Reuse the persistent path, but verify every packaged file against the fresh archive.
      const { readdirSync } = await import("node:fs");
      const verify = directory => {
        for (const entry of readdirSync(directory, { withFileTypes: true })) {
          const relative = join(directory, entry.name).slice(extracted.length + 1);
          const actual = join(source, relative);
          if (lstatSync(actual).isSymbolicLink()) throw new Error("Unexpected symlink in existing release");
          if (entry.isDirectory()) verify(join(directory, entry.name));
          else if (!readFileSync(actual).equals(readFileSync(join(directory, entry.name)))) throw new Error("Existing release file differs: " + relative);
        }
      };
      verify(extracted);
    } else {
      writeFileSync(join(extracted, ".release-sha256"), sha256);
      // Stage on the destination filesystem so rename is atomic even when /tmp is separate.
      const staging = mkdtempSync(join(releases, ".install-"));
      try {
        const { cpSync } = await import("node:fs");
        cpSync(extracted, join(staging, "release"), { recursive: true, errorOnExist: true });
        renameSync(join(staging, "release"), source);
      } finally { rmSync(staging, { recursive: true, force: true }); }
    }
  }
  const result = Bun.spawnSync([process.execPath, join(source, "scripts/init.ts"), ...args], { env: { ...process.env, CLZMJ_SKILLS_SOURCE: source }, stdin: "inherit", stdout: "inherit", stderr: "inherit" });
  if (result.error) throw result.error;
  process.exitCode = result.exitCode;
} catch (error) {
  console.error("Installation failed: " + (error instanceof Error ? error.message : String(error)));
  process.exitCode = 1;
} finally { if (temporary) rmSync(temporary, { recursive: true, force: true }); }
`;
