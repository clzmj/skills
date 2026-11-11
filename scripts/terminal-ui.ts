import { closeSync, constants, openSync, readSync, readdirSync, realpathSync, statSync, writeSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { ReadStream } from "node:tty";

export type Choice = { name: string; description?: string; source?: string; skill?: string };
type SkillLine = { prefix: string; source: string; skill: string };
export type Progress = { title?: string; completed: number; total: number; failed: number; label: string; elapsedMs?: number };
export class TerminalBack extends Error { constructor() { super("Back"); this.name = "TerminalBack"; } }
export class TerminalCancelled extends Error { constructor() { super("Installation cancelled."); this.name = "TerminalCancelled"; } }
export type TerminalIO = {
  // A timeout returns null. EOF must throw, so a detached terminal cannot spin forever.
  read: (timeoutMs?: number) => string | null | Promise<string | null>;
  write: (text: string) => void;
  size: () => { columns: number; rows: number };
  close?: () => void;
  poll?: () => string | null;
};
const ESC = "\x1b";
const clean = (text: string) => text.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").replace(/[\x00-\x1f\x7f-\x9f]/g, " ");
const width = (char: string) => /\p{Mark}/u.test(char) ? 0 : /[\u1100-\u115f\u2329\u232a\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe10-\ufe19\ufe30-\ufe6f\uff01-\uff60\uffe0-\uffe6\u{1f300}-\u{1faff}]/u.test(char) ? 2 : 1;
function clip(text: string, columns: number): string {
  let used = 0, output = "";
  for (const char of clean(text)) { const count = width(char); if (used + count > columns) break; used += count; output += char; }
  return output;
}
const cellWidth = (text: string) => [...clean(text)].reduce((total, char) => total + width(char), 0);
const shorten = (text: string, columns: number) => cellWidth(text) <= columns ? clean(text) : columns > 1 ? clip(text, columns - 1) + "…" : clip("…", columns);
function qualifiedLine(row: SkillLine, columns: number): string {
  const prefix = clip(row.prefix, columns), available = Math.max(0, columns - cellWidth(prefix));
  const skill = shorten(row.skill, Math.max(0, available - 2));
  const sourceBudget = Math.max(0, available - cellWidth(skill) - 1);
  const source = sourceBudget ? shorten(row.source, sourceBudget) + "/" : "";
  // Data is sanitized and measured before these fixed, trusted styling sequences are added.
  return prefix + "\x1b[36m" + source + "\x1b[1;33m" + skill;
}
function wrap(text: string, columns: number): string[] {
  const result: string[] = [];
  for (const paragraph of text.split("\n")) {
    let remaining = clean(paragraph);
    while (remaining) { const line = clip(remaining, Math.max(1, columns)); result.push(line); remaining = remaining.slice(line.length); }
    if (!paragraph) result.push("");
  }
  return result;
}

function legendLines(legend: string, columns: number): string[] {
  const lines: string[] = [];
  for (const shortcut of legend.split("  ")) {
    const previous = lines.at(-1);
    if (previous && cellWidth(previous + "  " + shortcut) <= columns) lines[lines.length - 1] = previous + "  " + shortcut;
    else lines.push(...wrap(shortcut, columns));
  }
  return lines;
}

/** Owns one alternate screen. Selectors yield between synchronous reads so signals can restore it. */
export class TerminalUI {
  private pending = "";
  private closed = false;
  constructor(private io: TerminalIO) {
    try { io.write(`${ESC}[?1049h${ESC}[?25l`); }
    catch (error) { try { this.close(); } catch { /* Preserve the original write failure after releasing the tty. */ } throw error; }
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    try { this.io.write(`${ESC}[0m${ESC}[?25h${ESC}[?1049l`); } finally { this.io.close?.(); }
  }
  private async key(): Promise<string> {
    while (true) {
      if (this.closed) throw new TerminalCancelled();
      if (!this.pending) { const chunk = await this.io.read(); if (chunk === null) continue; this.pending += chunk; }
      if (this.pending[0] === ESC) {
        // Escape sequences may arrive across multiple reads; a bare Esc is Back.
        const deadline = Date.now() + 50;
        while ((this.pending === ESC || /^\x1b[\[O][0-9;]*$/.test(this.pending)) && Date.now() < deadline) {
          const chunk = await this.io.read(Math.max(1, deadline - Date.now()));
          if (chunk === null) break;
          this.pending += chunk;
        }
        const match = this.pending.match(/^\x1b(?:\[[0-9;]*[A-Za-z~]|O[A-Z])/);
        if (match) {
          this.pending = this.pending.slice(match[0].length);
          const sequence = match[0];
          if (sequence.endsWith("A")) return "up";
          if (sequence.endsWith("B")) return "down";
          if (sequence.endsWith("C")) return "right";
          if (sequence.endsWith("D")) return "left";
          if (sequence.endsWith("H") || sequence === `${ESC}[1~`) return "home";
          if (sequence.endsWith("F") || sequence === `${ESC}[4~`) return "end";
          if (sequence === `${ESC}[5~`) return "pageup";
          if (sequence === `${ESC}[6~`) return "pagedown";
          continue;
        }
        this.pending = this.pending.slice(1);
        throw new TerminalBack();
      }
      const key = [...this.pending][0]!;
      this.pending = this.pending.slice(key.length);
      if (key === "\x03" || key === "\x04") { this.close(); throw new TerminalCancelled(); }
      if (key === "\r" || key === "\n") return "enter";
      return key.toLowerCase();
    }
  }
  pollCancel(): void {
    const chunk = this.pending + (this.io.poll?.() || "");
    this.pending = "";
    if (chunk && /[\x03\x04]/.test(chunk)) { this.close(); throw new TerminalCancelled(); }
  }
  private dimensions() { const size = this.io.size(); return { columns: Math.max(1, size.columns || 80), rows: Math.max(1, size.rows || 24) }; }
  private bodyRows() { return Math.max(1, this.dimensions().rows - (this.dimensions().columns < 76 ? 11 : 9)); }
  private draw(title: string, body: (string | SkillLine)[], footer: string, status = "") {
    if (this.closed) throw new Error("Terminal UI is closed");
    const { columns, rows } = this.dimensions();
    const lineWidth = Math.max(1, columns - 2);
    const header = ["clzmj / skills", "─".repeat(lineWidth), title, ""];
    const foot = ["", status, "─".repeat(lineWidth), ...legendLines(footer, lineWidth)];
    const bodySpace = Math.max(0, rows - header.length - foot.length - 1);
    const lines = [...header, ...body.slice(0, bodySpace), ...Array(Math.max(0, bodySpace - body.length)).fill(""), ...foot].slice(0, Math.max(1, rows - 1));
    this.io.write(`${ESC}[H${ESC}[2J` + lines.map((line, index) => {
      const content = typeof line === "string" ? clip(line, lineWidth) : qualifiedLine(line, lineWidth);
      const marker = typeof line === "string" ? line : line.prefix;
      const color = index === 0 || index === 2 ? "\x1b[1;36m" : marker.startsWith("›") ? "\x1b[1;32m" : "";
      return ` ${color}${content}${ESC}[0m`;
    }).join("\r\n"));
  }
  private async choose(title: string, rows: Choice[], defaults: string[], multi: boolean, context = "", minSelected = 0): Promise<string[]> {
    const selected = new Set(defaults.filter(name => rows.some(row => row.name === name)));
    let cursor = Math.max(0, rows.findIndex(row => selected.has(row.name))), offset = 0, validation = "", query = "", filtering = false;
    while (true) {
      const visible = query ? rows.filter(row => `${row.name} ${row.description || ""}`.toLowerCase().includes(query.toLowerCase())) : rows;
      cursor = Math.max(0, Math.min(cursor, visible.length - 1));
      const capacity = Math.max(1, this.bodyRows() - 4);
      offset = Math.max(0, Math.min(offset, cursor));
      if (cursor >= offset + capacity) offset = cursor - capacity + 1;
      const body: (string | SkillLine)[] = visible.slice(offset, offset + capacity).map((row, index) => {
        const prefix = `${index + offset === cursor ? "›" : " "} ${multi ? selected.has(row.name) ? "[x]" : "[ ]" : index + offset === cursor ? "●" : "○"} `;
        return row.source !== undefined && row.skill !== undefined ? { prefix, source: row.source, skill: row.skill } : prefix + row.name;
      });
      if (!visible.length) body.push(query ? "No matching items. / Edit filter; Esc Clear." : "No items available.");
      const detail = visible[cursor]?.description || "";
      body.push("", ...wrap(detail, Math.max(1, this.dimensions().columns - 4)).slice(0, 2));
      const footer = filtering ? "Type to filter  Backspace Delete  Enter Choose  Esc Clear  Ctrl-C Cancel" : multi ? "↑↓ Move  Space Toggle  a All catalog  n None  / Filter  Enter Next  Esc Back  Ctrl-C Cancel" : "↑↓ Move  Enter Select  Esc Back  Ctrl-C Cancel";
      const counts = `${multi ? `${selected.size} selected · ` : ""}`;
      const status = filtering ? `${counts}${visible.length}/${rows.length} shown · Filter: ${query}▏` : `${counts}${visible.length ? `${cursor + 1}/${visible.length}` : "0"}${query ? ` of ${rows.length} · Filter: ${query}` : ""}${context ? ` · ${context}` : ""}`;
      this.draw(title, body, footer, validation || status);
      let key: string;
      try { key = await this.key(); }
      catch (error) {
        if (error instanceof TerminalBack && (filtering || query)) { filtering = false; query = ""; cursor = offset = 0; continue; }
        throw error;
      }
      if (key !== "enter") validation = "";
      if (filtering) {
        if (key === "enter") filtering = false;
        else if (key === "\x7f" || key === "\b") { query = [...query].slice(0, -1).join(""); cursor = offset = 0; }
        else if ([...key].length === 1 && key >= " " && !/[\x7f-\x9f]/.test(key)) { query += key; cursor = offset = 0; }
        continue;
      }
      if (multi && key === "/") { filtering = true; continue; }
      if (key === "up") cursor = Math.max(0, cursor - 1);
      if (key === "down") cursor = Math.min(visible.length - 1, cursor + 1);
      if (key === "home") cursor = 0;
      if (key === "end") cursor = Math.max(0, visible.length - 1);
      if (key === "pageup") cursor = Math.max(0, cursor - capacity);
      if (key === "pagedown") cursor = Math.min(visible.length - 1, cursor + capacity);
      // All and None always address the complete catalog, including filtered-out selections.
      if (multi && key === "a") for (const row of rows) selected.add(row.name);
      if (multi && key === "n") selected.clear();
      if (multi && key === " " && visible[cursor]) { const name = visible[cursor]!.name; if (selected.has(name)) selected.delete(name); else selected.add(name); }
      if (key === "enter") {
        if (multi && selected.size < minSelected) { validation = `Select at least ${minSelected} ${minSelected === 1 ? "item" : "items"} to continue. Space toggles; a selects all.`; continue; }
        return multi ? rows.filter(row => selected.has(row.name)).map(row => row.name) : visible[cursor] ? [visible[cursor]!.name] : [];
      }
    }
  }
  multiselect(title: string, rows: Choice[], defaults: string[] = [], context?: string, minSelected = 0): Promise<string[]> { return this.choose(title, rows, defaults, true, context, minSelected); }
  async select(title: string, rows: Choice[], defaultValue: string, context?: string): Promise<string> { return (await this.choose(title, rows, [defaultValue], false, context))[0] || ""; }
  async directory(title: string, start: string): Promise<string> {
    let current = realpathSync(start), message = "";
    while (true) {
      let children: Choice[] = [];
      try { children = readdirSync(current, { withFileTypes: true }).filter(entry => { try { return entry.isDirectory() || entry.isSymbolicLink() && statSync(resolve(current, entry.name)).isDirectory(); } catch { return false; } }).sort((a, b) => a.name.localeCompare(b.name)).map(entry => ({ name: entry.name + "/", description: resolve(current, entry.name) })); }
      catch { message = "Cannot read this folder; choose its parent or another folder."; }
      const rows = [{ name: "Use this folder", description: current }, { name: "../", description: "Parent folder" }, ...children];
      const value = await this.select(title, rows, "Use this folder", `${current}${message ? " · " + message : ""}`);
      if (value === "Use this folder") return current;
      current = realpathSync(value === "../" ? dirname(current) : resolve(current, value));
      message = "";
    }
  }
  confirm(title: string, defaultValue = false, details: string[] = []): Promise<boolean> {
    return this.confirmLines(title, details, defaultValue);
  }
  private async confirmLines(title: string, details: string[], defaultValue: boolean): Promise<boolean> {
    let offset = 0, selected = defaultValue;
    while (true) {
      const capacity = Math.max(1, this.bodyRows() - 2);
      const lines = details.flatMap(line => wrap(line, Math.max(1, this.dimensions().columns - 4)));
      this.draw(title, [...lines.slice(offset, offset + capacity), "", selected ? "› Yes     No" : "  Yes   › No"], "y Yes  n No  ←→ Choose  Enter Confirm  ↑↓ Scroll  Esc Back  Ctrl-C Cancel", lines.length > capacity ? `${offset + 1}–${Math.min(lines.length, offset + capacity)} of ${lines.length} lines` : "Review your choices before continuing");
      const key = await this.key();
      if (key === "y") return true;
      if (key === "n") return false;
      if (key === "enter") return selected;
      if (key === "up") offset = Math.max(0, offset - 1);
      if (key === "down") offset = Math.min(Math.max(0, lines.length - capacity), offset + 1);
      if (key === "pageup") offset = Math.max(0, offset - capacity);
      if (key === "pagedown") offset = Math.min(Math.max(0, lines.length - capacity), offset + capacity);
      if (key === "left" || key === "right") selected = !selected;
    }
  }
  review(title: string, lines: string[]): Promise<boolean> { return this.confirmLines(title, lines, false); }
  async page(title: string, lines: string[], closing = false): Promise<void> {
    let offset = 0;
    while (true) {
      const capacity = this.bodyRows(), wrapped = lines.flatMap(line => wrap(line, Math.max(1, this.dimensions().columns - 4)));
      this.draw(title, wrapped.slice(offset, offset + capacity), closing ? "↑↓ Scroll  Enter Finish  Esc Close  Ctrl-C Cancel" : "↑↓ Scroll  Enter Continue  Esc Back  Ctrl-C Cancel");
      let key: string;
      try { key = await this.key(); } catch (error) { if (closing && error instanceof TerminalBack) return; throw error; }
      if (key === "enter") return;
      if (key === "down") offset = Math.min(Math.max(0, wrapped.length - capacity), offset + 1);
      if (key === "up") offset = Math.max(0, offset - 1);
      if (key === "pagedown") offset = Math.min(Math.max(0, wrapped.length - capacity), offset + capacity);
      if (key === "pageup") offset = Math.max(0, offset - capacity);
    }
  }
  progress(state: Progress): void {
    const elapsed = Math.max(0, state.elapsedMs || 0), seconds = Math.floor(elapsed / 1000);
    const spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"][Math.floor(elapsed / 100) % 10];
    const done = Math.min(state.total, state.completed + state.failed), width = Math.max(1, Math.min(36, this.dimensions().columns - 12));
    const filled = state.total ? Math.floor(done / state.total * width) : 0;
    this.draw(state.title || (state.total ? "Installing your skills" : "Preparing your installation"), ["", `${spinner} ${state.label}`, "", state.total ? `[${"█".repeat(filled)}${"░".repeat(width - filled)}] ${done}/${state.total}` : "Working…", "", `${state.completed} completed   ${state.failed} failed   ${Math.floor(seconds / 60)}m ${seconds % 60}s elapsed`], "Ctrl-C Cancel", "Your selected installation");
  }
  finish(title: string, lines: string[] = []): Promise<void> { return this.page(title, lines, true); }
  error(message: string): Promise<void> { return this.page("Installation needs attention", [message], true); }
}

/** Blocking output has its own descriptor: nonblocking input must never truncate a screen. */
export function writeTerminalFrame(fd: number, text: string, write: (fd: number, buffer: Buffer, offset: number, length: number) => number = (fd, buffer, offset, length) => writeSync(fd, buffer, offset, length)): void {
  const buffer = Buffer.from(text);
  let offset = 0;
  while (offset < buffer.length) {
    try {
      const written = write(fd, buffer, offset, buffer.length - offset);
      if (written <= 0) throw new Error("Terminal output ended before the complete screen was written");
      offset += written;
    } catch (error: any) { if (error.code !== "EINTR") throw error; }
  }
}

export function openTerminalUI(options: { onSignal?: (signal: NodeJS.Signals) => void } = {}): TerminalUI | undefined {
  let fd: number, outputFd: number;
  try { fd = openSync("/dev/tty", constants.O_RDONLY | constants.O_NONBLOCK); } catch { return undefined; }
  try { outputFd = openSync("/dev/tty", constants.O_WRONLY); } catch { closeSync(fd); return undefined; }
  let stream: ReadStream;
  try { stream = new ReadStream(fd); stream.setRawMode(true); } catch { closeSync(fd); closeSync(outputFd); return undefined; }
  let ui: TerminalUI, terminalClosed = false;
  const onExit = () => ui.close();
  const onSignal = (signal: NodeJS.Signals) => {
    try { ui.close(); }
    finally {
      if (options.onSignal) options.onSignal(signal);
      else setImmediate(() => process.exit(signal === "SIGINT" ? 130 : signal === "SIGTERM" ? 143 : 129));
    }
  };
  const readAvailable = () => {
    if (terminalClosed) throw new TerminalCancelled();
    const buffer = Buffer.alloc(256);
    try { const length = readSync(fd, buffer, 0, buffer.length, null); if (!length) throw new TerminalCancelled(); return buffer.subarray(0, length).toString("utf8"); }
    catch (error: any) { if (error.code !== "EAGAIN" && error.code !== "EWOULDBLOCK" && error.code !== "EINTR") throw error; return null; }
  };
  ui = new TerminalUI({
    poll: readAvailable,
    size: () => ({ columns: stream.columns || process.stdout.columns || 80, rows: stream.rows || process.stdout.rows || 24 }),
    write: text => { writeTerminalFrame(outputFd, text); },
    async read(timeoutMs) {
      const deadline = timeoutMs === undefined ? Infinity : Date.now() + timeoutMs;
      do {
        const value = readAvailable();
        if (value !== null) return value;
        await new Promise(done => setTimeout(done, Math.min(8, Math.max(0, deadline - Date.now()))));
      } while (Date.now() < deadline);
      return null;
    },
    close() {
      terminalClosed = true;
      process.off("exit", onExit);
      for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"] as const) process.off(signal, onSignal);
      try { stream.setRawMode(false); } finally { try { stream.destroy(); } finally { closeSync(outputFd); } }
    },
  });
  process.once("exit", onExit);
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"] as const) process.once(signal, onSignal);
  return ui;
}
