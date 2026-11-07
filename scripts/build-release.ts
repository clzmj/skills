import { readFileSync, mkdirSync, writeFileSync, lstatSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { createHash } from "node:crypto";
import { validVersion } from "../distribution/installer";

const root = fileURLToPath(new URL("../", import.meta.url));

export function buildRelease(version: string, output = resolve(root, "dist/releases")) {
  if (!validVersion(version)) throw new Error("Unsafe release version");
  const inventory = Bun.spawnSync(["git", "ls-files", "-z", "--", "skills/rex", "skills/python-tooling"], { cwd: root });
  if (inventory.exitCode !== 0) throw new Error("Cannot read tracked release files");
  const paths = ["scripts/init.ts", "favorites.json", "LICENSE", ...inventory.stdout.toString().split("\0").filter(path => path && !path.split("/").some(part => part.startsWith(".") || part.startsWith("test") || part === "__pycache__"))].sort();
  const chunks: Buffer[] = [];
  for (const path of paths) {
    const metadata = lstatSync(resolve(root, path));
    if (Buffer.byteLength(path) > 100 || !metadata.isFile() || metadata.isSymbolicLink()) throw new Error(`Unsupported release file: ${path}`);
    const contents = readFileSync(resolve(root, path));
    const header = Buffer.alloc(512);
    header.write(path);
    const octal = (value: number, offset: number, length: number) => header.write(value.toString(8).padStart(length - 1, "0") + "\0", offset, length, "ascii");
    octal(metadata.mode & 0o111 ? 0o755 : 0o644, 100, 8); octal(0, 108, 8); octal(0, 116, 8); octal(contents.length, 124, 12); octal(0, 136, 12);
    header.fill(32, 148, 156); header[156] = 48; header.write("ustar\0", 257); header.write("00", 263);
    const checksum = header.reduce((sum, byte) => sum + byte, 0);
    header.write(checksum.toString(8).padStart(6, "0") + "\0 ", 148, 8, "ascii");
    chunks.push(header, contents, Buffer.alloc((512 - contents.length % 512) % 512));
  }
  chunks.push(Buffer.alloc(1024));
  const archive = gzipSync(Buffer.concat(chunks), { level: 9 });
  const sha256 = createHash("sha256").update(archive).digest("hex");
  const manifest = { version, sha256, url: `/releases/${version}/skills.tar.gz` };
  const archivePath = resolve(output, `releases/${version}/skills.tar.gz`);
  mkdirSync(dirname(archivePath), { recursive: true });
  writeFileSync(archivePath, archive);
  writeFileSync(`${archivePath}.sha256`, `${sha256}  skills.tar.gz\n`);
  writeFileSync(resolve(output, "latest.json"), JSON.stringify(manifest, null, 2) + "\n");
  return { manifest, archivePath, paths };
}

if (import.meta.main) {
  let version = "";
  let output = resolve(root, "dist/releases");
  for (let index = 2; index < process.argv.length; index++) {
    const flag = process.argv[index];
    if (flag === "--version") version = process.argv[++index] ?? "";
    else if (flag === "--out") output = resolve(process.argv[++index] ?? "");
    else throw new Error(`Unknown argument: ${flag}`);
  }
  if (!version) throw new Error("Usage: bun scripts/build-release.ts --version VERSION [--out DIRECTORY]");
  console.log(JSON.stringify(buildRelease(version, output).manifest));
}
