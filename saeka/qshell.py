import argparse
import json
import os
import re
import subprocess
import sys

REPO_URL = "https://github.com/saekacutie/qqwerty.git"
BASE_DIR = os.path.expanduser("~/.saeka")
CLONE_DIR = os.path.join(BASE_DIR, "qqwerty")
MARKER = os.path.join(BASE_DIR, ".deployed")
PROJECT_FILE = os.path.join(BASE_DIR, "project")
LOG_FILE = os.path.join(BASE_DIR, "saeka.log")

# Words that identify a student/lab project (matched on project ID and name).
NAME_HINTS = ("student", "qwiklabs", "workspace")
HINT_RE = re.compile("|".join(NAME_HINTS), re.I)

# A deploy.sh without this marker is the old script that ignores the chosen project.
REQUIRED_SCRIPT_MARKER = "XGCPX_PROJECT"


def log(msg):
    line = f"[saeka] {msg}"
    print(line, file=sys.stderr)
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


def protected_projects():
    """Projects that must never be deployed into.

    Explicit ones (MAIN_PROJECT_ID, XGCPX_FORBIDDEN_PROJECT) always count. The active
    gcloud project and DEVSHELL_PROJECT_ID count only when they do NOT look like a
    student/lab project, because in a lab Cloud Shell the active project IS the lab project.
    """
    mains = []

    def add(value):
        value = (value or "").strip()
        if value and value != "(unset)" and value not in mains:
            mains.append(value)

    for var in ("MAIN_PROJECT_ID", "XGCPX_FORBIDDEN_PROJECT"):
        add(os.environ.get(var))

    implicit = [os.environ.get("DEVSHELL_PROJECT_ID")]
    try:
        implicit.append(sh(["gcloud", "config", "get-value", "project"]).stdout)
    except Exception:
        pass
    for value in implicit:
        value = (value or "").strip()
        if value and not HINT_RE.search(value):
            add(value)
    return mains


def explicit_protected():
    found = []
    for var in ("MAIN_PROJECT_ID", "XGCPX_FORBIDDEN_PROJECT"):
        value = os.environ.get(var, "").strip()
        if value and value not in found:
            found.append(value)
    return found


def detect_project(forced=None):
    """Return (project_id, reason, mains). project_id is None if no safe choice exists."""
    mains = protected_projects()
    forced = (forced or os.environ.get("STUDENT_PROJECT_ID", "")).strip()
    if forced:
        if forced in explicit_protected():
            return None, f"{forced} is listed as a protected main project", mains
        return forced, "explicit (--project / STUDENT_PROJECT_ID)", mains

    if os.path.isfile(PROJECT_FILE):
        with open(PROJECT_FILE) as f:
            saved = f.read().strip()
        if saved and saved not in mains:
            return saved, "saved from previous run", mains

    log(f"treating as MAIN (excluded): {mains or 'none found'}")
    try:
        out = sh([
            "gcloud", "projects", "list", "--format=json",
            "--filter=lifecycleState:ACTIVE",
        ]).stdout
        projects = json.loads(out or "[]")
    except Exception as e:
        return None, f"could not list projects ({e})", mains

    candidates = [
        p for p in projects
        if p.get("projectId") not in mains
        and not p.get("projectId", "").startswith("sys-")
    ]
    hinted = [
        p for p in candidates
        if HINT_RE.search(p.get("projectId", "")) or HINT_RE.search(p.get("name", ""))
    ]

    if len(hinted) == 1:
        return hinted[0]["projectId"], "single name-hint match", mains
    if len(hinted) > 1:
        return None, f"multiple matches {[p['projectId'] for p in hinted]}", mains
    if len(candidates) == 1:
        return candidates[0]["projectId"], "only non-main project available", mains
    return None, f"ambiguous candidates {[p['projectId'] for p in candidates]}", mains


def fetch_repo():
    """Clone or update the repo. Returns the directory, or None on failure."""
    try:
        if os.path.isdir(os.path.join(CLONE_DIR, ".git")):
            r = subprocess.run(["git", "-C", CLONE_DIR, "pull", "--ff-only"])
            if r.returncode != 0:
                log("WARNING: git pull failed; using the existing local copy, which may be stale")
        else:
            subprocess.run(["git", "clone", REPO_URL, CLONE_DIR], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log(f"STOP: could not fetch {REPO_URL}: {e}")
        return None
    return CLONE_DIR


def ensure_deployed(args):
    if args.redeploy and os.path.exists(MARKER):
        os.remove(MARKER)
    if os.path.exists(MARKER):
        return True

    os.makedirs(BASE_DIR, exist_ok=True)
    project, reason, mains = detect_project(args.project)
    if not project:
        log(f"STOP: no safe project detected: {reason}")
        log("Run once with:  saeka --project YOUR_STUDENT_PROJECT_ID")
        return False
    log(f"target project: {project} ({reason})")

    if args.local:
        deploy_dir = os.path.abspath(os.path.expanduser(args.local))
        log(f"using local directory {deploy_dir} instead of {REPO_URL}")
    else:
        deploy_dir = fetch_repo()
        if not deploy_dir:
            return False

    script = os.path.join(deploy_dir, "deploy.sh")
    if not os.path.isfile(script):
        log(f"STOP: {script} not found")
        return False
    with open(script, encoding="utf-8", errors="replace") as f:
        if REQUIRED_SCRIPT_MARKER not in f.read():
            log("STOP: this deploy.sh is the old version and ignores the chosen project.")
            log("Push the updated deploy.sh to the repo, or use:  saeka --local ~/qqwerty")
            return False

    env = os.environ.copy()
    for key in (
        "XGCPX_PROJECT", "STUDENT_PROJECT_ID", "CLOUDSDK_CORE_PROJECT",
        "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "PROJECT_ID",
    ):
        env[key] = project
    # Let deploy.sh's own guard refuse the main project(s).
    if mains:
        env["MAIN_PROJECT_ID"] = mains[0]
    if len(mains) > 1:
        env["XGCPX_FORBIDDEN_PROJECT"] = mains[1]

    cmd = ["bash", script, "--project", project]
    if args.yes:
        cmd.append("--yes")
    log("running deploy.sh ...")
    r = subprocess.run(cmd, cwd=deploy_dir, env=env)
    if r.returncode != 0:
        log(f"deploy.sh failed with exit code {r.returncode} (will retry next run)")
        return False

    with open(PROJECT_FILE, "w") as f:
        f.write(project + "\n")
    with open(MARKER, "w") as f:
        f.write("deployed\n")
    log("deploy.sh finished OK")
    return True


def main():
    parser = argparse.ArgumentParser(prog="saeka", add_help=True)
    parser.add_argument("--project", help="force the GCP project ID to deploy into")
    parser.add_argument("--local", metavar="DIR", help="run DIR/deploy.sh instead of cloning the GitHub repo")
    parser.add_argument("--yes", action="store_true", help="skip deploy.sh's confirmation prompt")
    parser.add_argument("--redeploy", action="store_true", help="run deploy.sh again")
    parser.add_argument("--reset", action="store_true", help="forget the saved project and deploy state")
    args, rest = parser.parse_known_args()

    if args.reset:
        for path in (MARKER, PROJECT_FILE):
            if os.path.exists(path):
                os.remove(path)
        log("state cleared")

    if not ensure_deployed(args):
        sys.exit(1)

    # Extra CLI arguments are reserved for the package's application logic.
    print("[saeka] ready.")


if __name__ == "__main__":
    main()
