#!/usr/bin/env python3
"""Read-only publication inventory. Output metadata only, never matching content.

Run after fetching the remote refs that will be published. This is a focused
disclosure check, not a guarantee that arbitrary secrets are absent.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main():
    if sys.argv[1:] == ["--hosted"]:
        return hosted()
    refs = git("for-each-ref", "--format=%(refname) %(objectname)").decode().splitlines()
    commits = git("rev-list", "--all").decode().splitlines()
    objects = git("rev-list", "--objects", "--all").decode().splitlines()
    suspect_paths = set()
    metadata_paths = set()
    blob_count = 0
    size = 0
    sensitive_path = re.compile(r"(^|/)(?:\.env(?:\..*)?|\.dev\.vars.*|.*\.tfstate(?:\..*)?|.*\.tfplan|.*\.(?:pem|p12|pfx|key)|id_(?:rsa|ed25519)|credentials|\.terraform|node_modules|dist|\.aws)(?:/|$)", re.I)
    # Exact tree enumeration catches renamed/deleted paths independently of the
    # representative name chosen by rev-list --objects.
    for commit in commits:
        for path in git("ls-tree", "-rz", "--name-only", commit).decode().split("\0"):
            if sensitive_path.search(path):
                suspect_paths.add(path)
    for entry in objects:
        oid, _, path = entry.partition(" ")
        if git("cat-file", "-t", oid).strip() != b"blob":
            continue
        data = git("cat-file", "blob", oid)
        blob_count += 1
        size += len(data)
        if re.search(rb"/(?:Users|home)/[A-Za-z0-9_.-]+|[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", data):
            metadata_paths.add(path or oid)
    result = {"refs": refs, "commits": len(commits), "blobs": blob_count,
              "blob_bytes": size, "credential_state_artifact_paths": sorted(suspect_paths),
              "absolute_path_or_contact_metadata_paths": sorted(metadata_paths)}
    scanner = shutil.which("gitleaks")
    if scanner:
        with tempfile.TemporaryDirectory(prefix="skills-public-readiness-") as temp:
            report = Path(temp) / "redacted.json"
            # Ignore repository suppressions and inherited scanner overrides.
            env = {k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")}
            run = subprocess.run([scanner, "git", str(ROOT), "--log-opts=--all --full-history",
                                  "--redact=100", "--ignore-gitleaks-allow", "--gitleaks-ignore-path", temp,
                                  "--no-banner", "--no-color", "--report-format=json", "--report-path", str(report)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            findings = json.loads(report.read_text()) if report.exists() else []
            result["gitleaks"] = {"exit_code": run.returncode, "findings": [
                {k: f.get(k) for k in ("RuleID", "File", "Commit", "StartLine", "EndLine")}
                for f in findings], "version": subprocess.check_output([scanner, "version"]).decode().strip()}
            if run.returncode not in (0, 1):
                result["gitleaks"]["error"] = "Scanner failed; diagnostics withheld to avoid content disclosure"
            current = Path(temp) / "current"
            current.mkdir()
            current_paths = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
            for path in filter(None, current_paths):
                source = ROOT / path
                if not source.exists():
                    continue
                if not stat.S_ISREG(source.lstat().st_mode):
                    raise RuntimeError("Current tree contains a non-regular file: " + path)
                target = current / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
            run = subprocess.run([scanner, "dir", str(current), "--redact=100", "--ignore-gitleaks-allow",
                                  "--gitleaks-ignore-path", temp, "--no-banner", "--no-color",
                                  "--report-format=json", "--report-path", str(report)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            findings = json.loads(report.read_text()) if report.exists() else []
            result["current_tree"] = {"exit_code": run.returncode, "files": len(list(filter(None, current_paths))),
                                      "findings": [{"RuleID": f.get("RuleID"), "File": str(f.get("File", "")).replace(str(current) + "/", ""),
                                                    "StartLine": f.get("StartLine")} for f in findings]}
    else:
        result["gitleaks"] = {"error": "gitleaks unavailable; secret scan incomplete"}
    result["limitations"] = ["Only locally reachable refs, not unfetched/deleted remote refs or dangling objects",
                             "Does not inspect hosted Actions logs, artifacts, releases, issues, or external settings",
                             "Pattern-based scanner can miss unknown or encoded credentials; metadata matches need review"]
    print(json.dumps(result, indent=2))
    return 0 if result["gitleaks"].get("exit_code") == 0 and result.get("current_tree", {}).get("exit_code") == 0 and not suspect_paths else 1


def hosted():
    """Scan available hosted logs in memory; retain only redacted metadata."""
    scanner = shutil.which("gitleaks")
    if not scanner:
        raise RuntimeError("Gitleaks is required")

    def api(endpoint):
        return json.loads(subprocess.check_output(["gh", "api", "--paginate", "--slurp", endpoint], stderr=subprocess.PIPE))

    pages = api("repos/clzmj/skills/actions/runs?per_page=100")
    runs = [run for page in pages for run in page["workflow_runs"]]
    artifacts = [a for page in api("repos/clzmj/skills/actions/artifacts?per_page=100") for a in page["artifacts"]]
    releases = [release for page in api("repos/clzmj/skills/releases?per_page=100") for release in page]
    results = []
    with tempfile.TemporaryDirectory(prefix="skills-hosted-review-") as temp:
        report = Path(temp) / "redacted.json"
        for run in runs:
            log = subprocess.run(["gh", "run", "view", str(run["id"]), "--repo", "clzmj/skills", "--log"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            entry = {"run_id": run["id"], "head_sha": run["head_sha"], "status": run["status"]}
            if log.returncode:
                entry["error"] = "Run log unavailable; diagnostics withheld"
            else:
                report.unlink(missing_ok=True)
                scan = subprocess.run([scanner, "stdin", "--redact=100", "--ignore-gitleaks-allow",
                                       "--gitleaks-ignore-path", temp, "--no-banner", "--no-color",
                                       "--report-format=json", "--report-path", str(report)], input=log.stdout,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      env={k: v for k, v in os.environ.items() if not k.startswith("GITLEAKS_")})
                findings = json.loads(report.read_text()) if report.exists() else []
                entry.update({"log_bytes": len(log.stdout), "scanner_exit_code": scan.returncode,
                              "findings": [{k: f.get(k) for k in ("RuleID", "StartLine", "EndLine")} for f in findings]})
            results.append(entry)
    print(json.dumps({"runs": results, "artifacts": [{k: a.get(k) for k in ("id", "name", "expired", "size_in_bytes")} for a in artifacts],
                      "releases": [{"id": r["id"], "tag_name": r["tag_name"], "assets": [{"name": a["name"], "size": a["size"]} for a in r["assets"]]} for r in releases],
                      "limitations": ["Unavailable, expired, deleted, or future logs cannot be scanned", "Hosted artifact/release payloads are inventoried, not downloaded or scanned"]}, indent=2))
    return int(any(r.get("error") or r.get("scanner_exit_code") != 0 for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
