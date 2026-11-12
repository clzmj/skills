#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Persistent Rex command routing, layout, and completion retention. Stdlib only."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("REX_VISIBLE_STATE_DIR", str(Path.home() / ".local/state/rex-visible"))).expanduser()
KINDS = ("server", "logs", "infra", "deploy", "test", "build", "install", "job")
POLICY = ROOT / "POLICY.md"


def uv_executable():
    """Use a persistent launcher rather than uv's temporary script interpreter."""
    executable = shutil.which("uv")
    if not executable:
        raise RuntimeError("uv is required; install uv and ensure it is on PATH")
    return str(Path(executable).absolute())


def classify(command):
    """Recognize common direct commands; agent policy covers custom scripts."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        segments, current = [], []
        for token in lexer:
            if token and all(c in ";&|()\n" for c in token):
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(token)
        if current:
            segments.append(current)
    except ValueError:
        return None
    for words in segments:
        while words and (re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", words[0]) or words[0] in ("env", "command", "exec", "sudo", "nohup")):
            words = words[1:]
        if not words:
            continue
        exe, args = Path(words[0]).name, words[1:]
        if exe in ("rex", "ssh", "mosh") or (str(ROOT / "rex_visible.py") in words and "run" in words):
            continue
        if exe in ("sh", "bash", "zsh") and any(x in ("-c", "-lc") for x in args):
            index = next(i for i, x in enumerate(args) if x in ("-c", "-lc"))
            if len(args) > index + 1:
                nested = classify(args[index + 1])
                if nested:
                    return nested
        if exe in ("terraform", "tofu", "terragrunt") and any(x in args for x in ("plan", "apply", "destroy", "import", "refresh")):
            return "infra"
        if exe in ("tail",) and any(x in ("-f", "-F", "--follow") or x.startswith("--follow=") for x in args):
            return "logs"
        if exe in ("docker", "kubectl", "railway", "vercel") and "logs" in args:
            return "logs"
        if exe == "kubectl" and any(x in args for x in ("apply", "delete", "rollout")):
            return "infra"
        if exe == "docker" and "compose" in args and "up" in args:
            return "server"
        if exe in ("wrangler", "vercel", "fly", "flyctl", "railway") and any(x in args for x in ("deploy", "up", "publish")):
            return "deploy"
        if exe in ("npm", "pnpm", "yarn", "bun", "cargo", "go", "uv", "poetry", "pip", "pip3"):
            if any(x in args for x in ("dev", "start", "serve", "preview")):
                return "server"
            if any(x in args for x in ("test", "pytest", "vitest", "jest", "playwright", "nextest")):
                return "test"
            if any(x in args for x in ("build", "check", "typecheck", "lint")):
                return "build"
            if any(x in args for x in ("install", "add", "sync", "ci")):
                return "install"
        if exe in ("pytest", "vitest", "jest", "playwright"):
            return "test"
        if exe in ("vite", "next", "uvicorn", "gunicorn"):
            return "server"
        if exe in ("make", "just") and args:
            return "job"
        if exe in ("prisma", "drizzle-kit", "sqlx", "alembic") and any("migrat" in x or x == "push" for x in args):
            return "infra"
        if exe in ("python", "python3", "uv") and "http.server" in args:
            return "server"
    return None


def hook():
    data = json.load(sys.stdin)
    event = data.get("hook_event_name")
    if event in ("SessionStart", "UserPromptSubmit"):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext":
            f"For important terminal work, follow {POLICY}. Reuse the main agent Rex session for job tabs and agent views; close completed owned panes under its retention rules. Read the policy before the first such command. Respect an explicitly requested other machine."}}))
        return
    if event != "PreToolUse" or data.get("tool_name") not in ("Bash", "exec_command", "shell", "shell_command"):
        return
    inputs = data.get("tool_input") or {}
    command = inputs.get("command", inputs.get("cmd", ""))
    if isinstance(command, list):
        command = shlex.join(command)
    kind = classify(command) if isinstance(command, str) else None
    if kind:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason":
            f"Run this {kind} command visibly in local Rex. Read {POLICY}, then use uv run --script {ROOT / 'rex_visible.py'} run --task TASK --kind {kind} --command ORIGINAL_COMMAND. Use --detach for servers/log followers. This hook has not executed the command. Preserve normal approvals; use the user's explicit machine if specified."}}))


def atomic_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(path)


def state_dir():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    return STATE


def rex_cli():
    executable = shutil.which("rex")
    if not executable:
        raise RuntimeError("Rex CLI is unavailable")
    # Discover the platform's canonical local endpoint without inheriting a remote.
    server = os.environ.get("REX_SERVER", "")
    if not server.startswith("unix://"):
        environment = dict(os.environ)
        environment.pop("REX_SERVER", None)
        result = subprocess.run(
            [executable, "--timeout", "5s", "--autostart=false", "whoami", "--json"],
            capture_output=True, text=True, env=environment,
        )
        if result.returncode:
            raise RuntimeError("Cannot discover the local Rex server with rex whoami")
        try:
            server = json.loads(result.stdout).get("endpoint", "")
        except (ValueError, AttributeError):
            raise RuntimeError("Rex returned invalid local server metadata") from None
        if not isinstance(server, str) or not server.startswith("unix://"):
            raise RuntimeError("The local Rex runner requires a Unix socket; use a configured Rex connection directly for remote work")
    return [executable, "-S", server, "--timeout", "5s"]


def rex_json(arguments):
    result = subprocess.run(rex_cli() + arguments, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout)


def blocks(layout):
    return [b for w in layout.get("windows", []) for layer in w.get("layers", []) for b in layer.get("blocks", [])]


def main_session(thread=None):
    """Resolve an existing local host, failing closed rather than creating one."""
    state_dir()
    thread = thread or os.environ.get("REX_COORD_THREAD") or os.environ.get("CODEX_THREAD_ID", "")
    key = hashlib.sha256(thread.encode()).hexdigest()[:16]
    binding = STATE / ("main-" + key + ".json")
    sessions = rex_json(["ls", "--json"])["sessions"]
    live = {s["session_id"] for s in sessions}
    saved = json.loads(binding.read_text()) if binding.exists() else {}
    # The root's binding takes precedence over a native child's inherited location.
    for candidate in (saved.get("session_id"), os.environ.get("REX_SESSION")):
        if candidate in live:
            atomic_json(binding, {"session_id": candidate})
            return candidate
    candidates = []
    for session in sessions:
        sid = session["session_id"]
        for block in blocks(rex_json(["session", "inspect", sid, "--json"])):
            if block.get("creator_name") != "com.superlogical.terminal":
                continue
            process = rex_json(["block", "call", "-s", sid, "-b", block["block_id"], "process"])
            if (process.get("foreground") or {}).get("name") in ("codex", "claude"):
                candidates.append(sid)
                break
    if len(candidates) != 1:
        raise RuntimeError("Cannot identify the main agent Rex session unambiguously. Supply its existing REX_SESSION; no session was created.")
    atomic_json(binding, {"session_id": candidates[0]})
    return candidates[0]


def close_pane(session, block):
    """Close a known owned pane, including Rex's retained failed terminals."""
    sessions = {s["session_id"] for s in rex_json(["ls", "--json"])["sessions"]}
    if session in sessions and block in {b["block_id"] for b in blocks(rex_json(["session", "inspect", session, "--json"]))}:
        result = subprocess.run(rex_cli() + ["block", "close", "-s", session, block], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def retire_empty_session(session, main):
    """Retire a legacy tool session only after all placed/detached blocks are gone."""
    if session == main:
        return
    live = {s["session_id"] for s in rex_json(["ls", "--json"])["sessions"]}
    if session in live and not rex_json(["block", "ls", "-s", session, "--json"])["blocks"]:
        result = subprocess.run(rex_cli() + ["kill", session], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def open_job(args, job_id, status):
    state_dir()
    session = main_session()
    task_key = hashlib.sha256(session.encode()).hexdigest()[:16]
    task_file = STATE / (task_key + ".json")
    label = re.sub(r"[^a-z0-9]+", "-", args.task.lower()).strip("-")[:48] or "agent-work"
    child = [uv_executable(), "run", "--script", str(Path(__file__).resolve()), "job", "--status", str(status),
             "--kind", args.kind, "--close-after", str(args.close_after), "--command", args.command,
             "--shell", args.shell]
    group = "Services" if args.kind in ("server", "logs") else ("Checks" if args.kind in ("test", "build", "install") else "Operations")
    with (STATE / (task_key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(task_file.read_text()) if task_file.exists() else {}
        layout = rex_json(["session", "inspect", session, "--json"])
        live_blocks = {b["block_id"] for b in blocks(layout)}
        state["owned_blocks"] = [b for b in state.get("owned_blocks", []) if b in live_blocks]
        candidate = None
        for window in layout.get("windows", []):
            window_blocks = [b for layer in window.get("layers", []) for b in layer.get("blocks", [])]
            if window.get("label") == group and len(window_blocks) == 1 and window_blocks[0]["block_id"] in state["owned_blocks"]:
                candidate = window_blocks[0]["block_id"]
        if candidate:
            created = rex_json(["split", "-s", session, "-b", candidate, "--split=below", "--ratio", "40",
                                "--label", label, "--focus=false", "-c", args.cwd, "--shell=none", "--json", "--"] + child)
        else:
            created = rex_json(["window", "new", "-s", session, group, "--focus=false", "-c", args.cwd,
                                "--shell=none", "--json", "--"] + child)
        block = created["block_ids"][0]
        state["session_id"] = session
        state["owned_blocks"] = (state.get("owned_blocks", []) + [block])[-100:]
        atomic_json(task_file, state)
    opened = {"job_id": job_id, "session_id": session, "block_id": block, "status_file": str(status)}
    atomic_json(status.with_suffix(".launch.json"), opened)
    return opened


def run(args):
    state_dir()
    job_id = uuid.uuid4().hex
    status = STATE / (job_id + ".status.json")
    opened = open_job(args, job_id, status)
    print(json.dumps(opened), flush=True)
    if args.detach:
        return 0
    # Quiet mode keeps full output in Rex and returns only receipts to the agent.
    last = ""
    while True:
        result = subprocess.run(rex_cli() + ["capture", "-s", opened["session_id"], "-b", opened["block_id"], "--trim", "--unwrap"], capture_output=True, text=True)
        if result.returncode == 0:
            content = result.stdout
            if not args.quiet and content.startswith(last):
                print(content[len(last):], end="", flush=True)
            elif not args.quiet and content != last:
                print(content, end="", flush=True)
            last = content
        if status.exists():
            completed = json.loads(status.read_text())
            # A job can finish between the capture and the status check.
            final = subprocess.run(rex_cli() + ["capture", "-s", opened["session_id"], "-b", opened["block_id"], "--trim", "--unwrap"], capture_output=True, text=True)
            if not args.quiet and final.returncode == 0 and final.stdout != last:
                print(final.stdout[len(last):] if final.stdout.startswith(last) else final.stdout, end="", flush=True)
            print("\n" + json.dumps(completed), flush=True)
            return completed["exit_code"]
        if result.returncode:
            raise RuntimeError("Rex capture failed; command may still be running. Inspect returned IDs before retrying: " + result.stderr.strip())
        time.sleep(0.5)


def job(args):
    print(f"[{args.kind}] {os.getcwd()}\n$ {args.command}\n", flush=True)
    started = time.time()
    try:
        # Login environment matches ordinary Rex command startup. Terminal I/O is inherited.
        result = subprocess.run([args.shell, "-lc", args.command])
        code = result.returncode if result.returncode >= 0 else 128 - result.returncode
    except KeyboardInterrupt:
        code = 130
    except OSError as error:
        print(str(error), file=sys.stderr, flush=True)
        code = 127
    print(f"\n[exit {code}; {time.time() - started:.1f}s]", flush=True)
    atomic_json(Path(args.status), {"exit_code": code, "kind": args.kind, "finished_at": time.time()})
    delay = args.close_after
    if delay is None:
        delay = 120 if code != 0 or args.kind in ("infra", "deploy") else 30
    if delay < 0:
        print("Pinned for review. Close this pane when finished.", flush=True)
        while True:
            time.sleep(30)
    else:
        print(f"Closing this completed pane in {delay:g}s.", flush=True)
        time.sleep(delay)
    launch = Path(args.status).with_suffix(".launch.json")
    # The parent publishes ownership after Rex returns the newly allocated IDs.
    for _ in range(50):
        if launch.exists():
            owned = json.loads(launch.read_text())
            close_pane(owned["session_id"], owned["block_id"])
            break
        time.sleep(0.1)
    return 0  # The real command result is in the receipt; avoid Rex error retention.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("hook")
    runner = sub.add_parser("run")
    runner.add_argument("--task", required=True, help="Task label inside the main Codex Rex session")
    runner.add_argument("--kind", choices=KINDS, default="job")
    runner.add_argument("--cwd", default=os.getcwd())
    runner.add_argument("--command", required=True)
    runner.add_argument("--detach", action="store_true")
    runner.add_argument("--quiet", action="store_true", help="Return job receipts only; full command output remains visible in Rex")
    runner.add_argument("--close-after", type=float, default=None, help="Completed pane retention: default 30s success, 120s failure/infra/deploy; -1 pins")
    runner.add_argument("--shell", default=os.environ.get("SHELL", "/bin/zsh"))
    child = sub.add_parser("job")
    child.add_argument("--status", required=True)
    child.add_argument("--kind", choices=KINDS, required=True)
    child.add_argument("--command", required=True)
    child.add_argument("--shell", required=True)
    child.add_argument("--close-after", type=lambda value: None if value == "None" else float(value), required=True)
    args = parser.parse_args()
    try:
        return {"hook": hook, "run": lambda: run(args), "job": lambda: job(args)}[args.action]() or 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"rex-visible: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
