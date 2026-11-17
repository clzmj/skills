import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { releasePaths } from "../scripts/build-release";

const root = resolve(import.meta.dir, "..");

function assertReleaseInventory(
  paths: string[],
  readContents: (path: string) => string = (path) => readFileSync(resolve(root, path), "utf8"),
) {
  for (const path of paths) {
    if (
      path === "/home/box" ||
      path.startsWith("/home/box/") ||
      path.startsWith("grokbot/") ||
      path.startsWith("tests/") ||
      path.startsWith("docs/qe/")
    )
      throw new Error(`Forbidden release path: ${path}`);
    const contents = readContents(path);
    if (
      /\/home\/box|qe\.bots\/1|mac_machine_id|["']?agent_id["']?\s*[=:]\s*["']?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}["']?/i.test(
        contents,
      )
    )
      throw new Error(`Forbidden release content: ${path}`);
  }
}

test("release --list returns the filtered tracked inventory", () => {
  const result = Bun.spawnSync([process.execPath, "scripts/build-release.ts", "--list"], {
    cwd: root,
  });
  expect(result.exitCode).toBe(0);
  // Intentional release allowlist: packaging changes must review this snapshot.
  // Deriving it from releasePaths() or Git would let an accidental inclusion
  // change both expected and actual, defeating this independent inventory guard.
  expect(JSON.parse(result.stdout.toString())).toEqual([
    "LICENSE",
    "distribution/archive-source.ts",
    "distribution/archive.ts",
    "favorites.json",
    "scripts/init.ts",
    "scripts/self-update.ts",
    "skills/python-tooling/SKILL.md",
    "skills/python-tooling/scripts/install_hooks.py",
    "skills/python-tooling/scripts/python_tooling.py",
    "skills/rex/SKILL.md",
    "skills/rex/scripts/COORDINATION.md",
    "skills/rex/scripts/POLICY.md",
    "skills/rex/scripts/README.md",
    "skills/rex/scripts/agent_types.py",
    "skills/rex/scripts/agents/complex-worker.toml",
    "skills/rex/scripts/agents/focused-worker.toml",
    "skills/rex/scripts/agents/task-coordinator.toml",
    "skills/rex/scripts/claude_sessions.py",
    "skills/rex/scripts/codex_sessions.py",
    "skills/rex/scripts/demo/cleanup_demo.py",
    "skills/rex/scripts/grokbot_callback.py",
    "skills/rex/scripts/install_codex.py",
    "skills/rex/scripts/install_hooks.py",
    "skills/rex/scripts/question_broker.py",
    "skills/rex/scripts/question_guard.py",
    "skills/rex/scripts/question_guard.toml",
    "skills/rex/scripts/question_ledger.py",
    "skills/rex/scripts/question_model.py",
    "skills/rex/scripts/question_redact.py",
    "skills/rex/scripts/question_routes.py",
    "skills/rex/scripts/rex_visible.py",
  ]);
  expect(
    releasePaths(() => [
      "skills/rex/scripts/agent_id.py",
      "skills/rex/test_fixture.py",
      "skills/rex/.hidden",
    ]),
  ).toEqual([
    "LICENSE",
    "distribution/archive-source.ts",
    "distribution/archive.ts",
    "favorites.json",
    "scripts/init.ts",
    "scripts/self-update.ts",
    "skills/rex/scripts/agent_id.py",
  ]);
  expect(() => assertReleaseInventory(releasePaths())).not.toThrow();
});

test("release inventory rejects box-only paths and secret identifier values", () => {
  expect(() => assertReleaseInventory(["/home/box/config"], () => "")).toThrow(
    "Forbidden release path",
  );
  expect(() =>
    assertReleaseInventory(["scripts/hook.py"], () => 'const box = "/home/box"'),
  ).toThrow();
  expect(() =>
    assertReleaseInventory(
      ["scripts/hook.py"],
      () => '{"agent_id": "12345678-1234-1234-1234-123456789abc"}',
    ),
  ).toThrow();
  expect(() =>
    assertReleaseInventory(
      ["scripts/hook.py"],
      () => "agent_id = 12345678-1234-1234-1234-123456789abc",
    ),
  ).toThrow();
  expect(() =>
    assertReleaseInventory(
      ["scripts/hook.py"],
      () => "agent_id: 12345678-1234-1234-1234-123456789abc",
    ),
  ).toThrow();
});

test("release inventory allows agent_id as a code identifier", () => {
  expect(() =>
    assertReleaseInventory(["scripts/hook.py"], () =>
      readFileSync(resolve(root, "skills/rex/scripts/grokbot_callback.py"), "utf8"),
    ),
  ).not.toThrow();
});
