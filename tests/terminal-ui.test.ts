import { describe, expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { TerminalBack, TerminalCancelled, TerminalUI, writeTerminalFrame } from "../scripts/terminal-ui";

function harness(keys: (string | null)[], columns = 80, rows = 24) {
  const output: string[] = [];
  let closes = 0;
  const ui = new TerminalUI({ read: () => { if (!keys.length) throw new Error("Test exhausted input"); return keys.shift()!; }, write: value => output.push(value), size: () => ({ columns, rows }), close: () => { closes++; } });
  return { ui, output, closes: () => closes };
}
const catalog = Array.from({ length: 45 }, (_, i) => ({ name: `owner/skill-${i + 1}`, description: `Detailed explanation for skill ${i + 1}` }));
const plain = (value: string) => value.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "");

describe("terminal pages", () => {
  test("long catalog scrolls to the end, handles split/repeated arrows, selects all and toggles", async () => {
    const { ui, output } = harness(["\x1b", "[", "F", "a", " ", "\x1b[A\x1b[A", " ", "\r"]);
    const selected = await ui.multiselect("Catalog", catalog);
    expect(selected.length).toBe(43);
    expect(selected).not.toContain("owner/skill-45");
    expect(selected).not.toContain("owner/skill-43");
    expect(output.some(value => value.includes("Detailed explanation for skill 45"))).toBe(true);
    expect(output.some(value => value.includes("45/45"))).toBe(true);
    ui.close();
  });
  test("n clears selection and y/n confirmations need no enter; review defaults no", async () => {
    const { ui } = harness(["a", "n", "\r", "y", "n", "\r", "\x1b[D", "\r"]);
    expect(await ui.multiselect("Items", catalog, [catalog[0]!.name])).toEqual([]);
    expect(await ui.confirm("Hooks")).toBe(true);
    expect(await ui.confirm("Hooks", true)).toBe(false);
    expect(await ui.review("Review", ["Install 3 skills"])).toBe(false);
    expect(await ui.confirm("Hooks")).toBe(true);
    ui.close();
  });
  test("bare escape returns Back, Ctrl-C and Ctrl-D restore exactly once", async () => {
    const back = harness(["\x1b", null]);
    await expect(back.ui.select("Scope", [{ name: "global" }], "global")).rejects.toBeInstanceOf(TerminalBack);
    back.ui.close();
    for (const key of ["\x03", "\x04"]) {
      const item = harness([key]);
      await expect(item.ui.multiselect("Items", catalog)).rejects.toBeInstanceOf(TerminalCancelled);
      item.ui.close();
      expect(item.closes()).toBe(1);
      expect(item.output.at(-1)).toContain("\x1b[?25h\x1b[?1049l");
    }
  });
  test("directory picker navigates children and parent without typed paths", async () => {
    const root = mkdtempSync(join(tmpdir(), "skills-picker-"));
    mkdirSync(join(root, "child"));
    const { ui } = harness(["\x1b[B\x1b[B\r", "\x1b[B\r", "\x1b[B\x1b[B\r", "\r"]);
    try { expect(await ui.directory("Project folder", root)).toBe(realpathSync(join(root, "child"))); }
    finally { ui.close(); rmSync(root, { recursive: true }); }
  });
  test("small viewport stays bounded with visible controls and terminal escape text is sanitized", async () => {
    const { ui, output } = harness(["\x1b[F", "\r"], 40, 16);
    await ui.multiselect("Catalog\x1b[31m", catalog);
    for (const frame of output.filter(text => text.includes("\x1b[2J"))) {
      const text = plain(frame), lines = text.split("\r\n");
      expect(lines.length).toBeLessThanOrEqual(15);
      expect(lines.every(line => [...line].length <= 39)).toBe(true);
      expect(text).toContain("Enter Next");
      expect(text).toContain("Esc Back");
      expect(text).toContain("Ctrl-C Cancel");
      expect(frame).not.toContain("\x1b[31m");
    }
    ui.close();
  });
  test("nonblocking cancellation also catches input queued behind confirmation", async () => {
    const { ui, closes } = harness(["y\x03"]);
    expect(await ui.confirm("Install?")).toBe(true);
    expect(() => ui.pollCancel()).toThrow(TerminalCancelled);
    expect(closes()).toBe(1);
  });
  test("arrow keys during active progress do not cancel", () => {
    const ui = new TerminalUI({ read: () => null, poll: () => "\x1b[A\x1b[B", write: () => {}, size: () => ({ columns: 80, rows: 24 }) });
    expect(() => ui.pollCancel()).not.toThrow();
    ui.close();
  });
  test("initial screen write failure still releases its terminal", () => {
    let released = 0;
    expect(() => new TerminalUI({ read: () => null, write: () => { throw new Error("tty write failed"); }, size: () => ({ columns: 80, rows: 24 }), close: () => { released++; } })).toThrow("tty write failed");
    expect(released).toBe(1);
  });
  test("required selections stay on the page until a choice is made", async () => {
    const { ui, output } = harness(["n", "\r", " ", "\r"]);
    expect(await ui.multiselect("Agents", catalog.slice(0, 2), [], undefined, 1)).toEqual([catalog[0]!.name]);
    expect(output.some(frame => frame.includes("Select at least 1 item to continue"))).toBe(true);
    expect(output.at(-1)).not.toContain("Select at least");
    expect(output.at(-1)).toContain("1 selected");
    ui.close();
  });
  test("closing a pending asynchronous selector cancels promptly", async () => {
    const ui = new TerminalUI({ read: async () => { await new Promise(done => setTimeout(done, 5)); return null; }, write: () => {}, size: () => ({ columns: 80, rows: 24 }) });
    const selection = ui.select("Agents", [{ name: "codex" }], "codex");
    ui.close();
    await expect(selection).rejects.toBeInstanceOf(TerminalCancelled);
  });
  test("terminal frames survive short writes and interrupted output without losing UTF-8 bytes", () => {
    const captured: Buffer[] = [];
    let attempts = 0;
    const frame = "\x1b[H↑↓ Complete footer: Enter Next  Esc Back  Ctrl-C Cancel";
    writeTerminalFrame(123, frame, (fd, buffer, offset, length) => {
      expect(fd).toBe(123);
      if (++attempts === 2) throw Object.assign(new Error("interrupted"), { code: "EINTR" });
      const count = Math.min(3, length);
      captured.push(Buffer.from(buffer.subarray(offset, offset + count)));
      return count;
    });
    expect(Buffer.concat(captured).toString()).toBe(frame);
    expect(attempts).toBeGreaterThan(2);
    expect(() => writeTerminalFrame(123, "screen", () => 0)).toThrow("Terminal output ended");
  });
  test("preflight progress honors its operation title", () => {
    const { ui, output } = harness([]);
    ui.progress({ title: "Discovering owner/pack", label: "Reading available skills", completed: 0, total: 0, failed: 0 });
    expect(output.at(-1)).toContain("Discovering owner/pack");
    expect(output.at(-1)).not.toContain("Installing your skills");
    ui.close();
  });
  test("progress reflects real success/failure counts and finish accepts Esc", async () => {
    const { ui, output } = harness(["\x1b", null]);
    ui.progress({ completed: 2, failed: 1, total: 4, label: "Installing owner/pack", elapsedMs: 62000 });
    expect(output.at(-1)).toContain("3/4");
    expect(output.at(-1)).toContain("2 completed   1 failed   1m 2s");
    await ui.finish("Completed", ["2 commands succeeded; 1 failed"]);
    expect(output.at(-1)).toContain("Esc Close");
    ui.close();
  });
});
