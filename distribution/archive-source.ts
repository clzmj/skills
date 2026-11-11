// Keep this as JavaScript text: bundlers must not rewrite its Node imports or function body.
export const archiveExtractor = String.raw`async function extractArchive(compressed, directory) {
  const { gunzipSync } = await import("node:zlib");
  const { mkdirSync, writeFileSync, chmodSync } = await import("node:fs");
  const { dirname, resolve } = await import("node:path");
  const bytes = gunzipSync(compressed, { maxOutputLength: 32 * 1024 * 1024 });
  const files = new Map();
  const field = (header, start, length) => header.subarray(start, start + length).toString("utf8").replace(/\0.*$/s, "");
  let offset = 0;
  for (; offset + 512 <= bytes.length; ) {
    const header = bytes.subarray(offset, offset + 512);
    if (header.every(byte => byte === 0)) break;
    const name = field(header, 0, 100);
    const sizeText = field(header, 124, 12).trim();
    const modeText = field(header, 100, 8).trim();
    const checksumText = field(header, 148, 8).trim();
    const checksum = header.reduce((sum, byte, index) => sum + (index >= 148 && index < 156 ? 32 : byte), 0);
    if (!/^[0-7]+$/.test(checksumText) || checksum !== parseInt(checksumText, 8)) throw new Error("Invalid archive checksum");
    if (!/^[0-7]+$/.test(sizeText)) throw new Error("Invalid archive size");
    const size = parseInt(sizeText, 8);
    const mode = /^[0-7]+$/.test(modeText) ? parseInt(modeText, 8) : -1;
    if (mode !== 0o644 && mode !== 0o755) throw new Error("Unsafe archive mode");
    if ((header[156] !== 0 && header[156] !== 48) || field(header, 157, 100) || field(header, 345, 155)) throw new Error("Archive links and special entries are forbidden");
    if (!/^(scripts\/(init|self-update|terminal-ui)\.ts|distribution\/archive(-source)?\.ts|favorites\.json|LICENSE|skills\/(rex|python-tooling)\/[A-Za-z0-9_./-]+)$/.test(name) || name.split("/").some(part => !part || part === "." || part === ".." || part.startsWith(".")) || /(^|\/)(test[^/]*|__pycache__)(\/|$)/.test(name) || files.has(name)) throw new Error("Unsafe archive path: " + name);
    const end = offset + 512 + size;
    if (!Number.isSafeInteger(size) || end > bytes.length) throw new Error("Truncated archive");
    files.set(name, { contents: bytes.subarray(offset + 512, end), mode });
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  if (bytes.length - offset < 1024 || !bytes.subarray(offset).every(byte => byte === 0)) throw new Error("Invalid archive trailer");
  for (const required of ["scripts/init.ts", "scripts/self-update.ts", "scripts/terminal-ui.ts", "distribution/archive.ts", "distribution/archive-source.ts", "favorites.json", "skills/rex/SKILL.md", "skills/python-tooling/SKILL.md"]) if (!files.has(required)) throw new Error("Archive missing " + required);
  for (const name of files.keys()) {
    const parts = name.split("/");
    for (let index = 1; index < parts.length; index++) if (files.has(parts.slice(0, index).join("/"))) throw new Error("Conflicting archive paths");
  }
  // Validate everything before the first write, including malicious late entries.
  for (const [name, { contents, mode }] of files) {
    const destination = resolve(directory, name);
    mkdirSync(dirname(destination), { recursive: true });
    writeFileSync(destination, contents, { mode, flag: "wx" });
    chmodSync(destination, mode);
  }
  return [...files.keys()];
}`;
