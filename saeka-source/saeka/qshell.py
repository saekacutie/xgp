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

# Words that identify a student/workspace project (matched on project ID and name)
NAME_HINTS = ("student", "qwiklabs", "workspace")


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


def detect_project(forced=None):
    """Return (project_id, reason). project_id is None if no safe choice exists."""
    forced = (forced or os.environ.get("STUDENT_PROJECT_ID", "")).strip()
    if forced:
        return forced, "explicit (--project / STUDENT_PROJECT_ID)"
    if os.path.isfile(PROJECT_FILE):
        saved = open(PROJECT_FILE).read().strip()
        if saved:
            return saved, "saved from previous run"

    mains = set()
    try:
        active = sh(["gcloud", "config", "get-value", "project"]).stdout.strip()
        if active and active != "(unset)":
            mains.add(active)
    except Exception:
        pass
    for var in ("DEVSHELL_PROJECT_ID", "MAIN_PROJECT_ID"):
        if os.environ.get(var):
            mains.add(os.environ[var])
    log(f"treating as MAIN (excluded): {sorted(mains) or 'none found'}")

    try:
        out = sh([
            "gcloud", "projects", "list", "--format=json",
            "--filter=lifecycleState:ACTIVE",
        ]).stdout
        projects = json.loads(out or "[]")
    except Exception as e:
        return None, f"could not list projects ({e})"

    candidates = [
        p for p in projects
        if p.get("projectId") not in mains
        and not p.get("projectId", "").startswith("sys-")
    ]
    pattern = re.compile("|".join(NAME_HINTS), re.I)
    hinted = [
        p for p in candidates
        if pattern.search(p.get("projectId", ""))
        or pattern.search(p.get("name", ""))
    ]

    if len(hinted) == 1:
        return hinted[0]["projectId"], "single name-hint match"
    if len(hinted) > 1:
        return None, f"multiple matches {[p['projectId'] for p in hinted]}"
    if len(candidates) == 1:
        return candidates[0]["projectId"], "only non-main project available"
    return None, f"ambiguous candidates {[p['projectId'] for p in candidates]}"


def ensure_deployed(forced_project=None, redeploy=False):
    if redeploy and os.path.exists(MARKER):
        os.remove(MARKER)
    if os.path.exists(MARKER):
        return True

    os.makedirs(BASE_DIR, exist_ok=True)
    project, reason = detect_project(forced_project)
    if not project:
        log(f"STOP: no safe project detected: {reason}")
        log("Run once with:  saeka --project YOUR_STUDENT_PROJECT_ID")
        return False
    log(f"target project: {project} ({reason})")

    try:
        if os.path.isdir(os.path.join(CLONE_DIR, ".git")):
            r = subprocess.run(["git", "-C", CLONE_DIR, "pull", "--ff-only"])
            if r.returncode != 0:
                log("git pull failed, using the existing local copy")
        else:
            subprocess.run(["git", "clone", REPO_URL, CLONE_DIR], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log(f"STOP: could not fetch {REPO_URL}: {e}")
        return False

    script = os.path.join(CLONE_DIR, "deploy.sh")
    if not os.path.isfile(script):
        log(f"STOP: {script} not found in the cloned repo")
        return False

    env = os.environ.copy()
    for key in (
        "STUDENT_PROJECT_ID", "CLOUDSDK_CORE_PROJECT", "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT", "PROJECT_ID",
    ):
        env[key] = project

    log("running deploy.sh ...")
    r = subprocess.run(["bash", script], cwd=CLONE_DIR, env=env)
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
    parser = argparse.ArgumentParser(prog="saeka", add_help=False)
    parser.add_argument("--project", help="force the GCP project ID to deploy into")
    parser.add_argument("--redeploy", action="store_true", help="pull and run deploy.sh again")
    parser.add_argument("--reset", action="store_true", help="forget the saved project and deploy state")
    args, rest = parser.parse_known_args()

    if args.reset:
        for path in (MARKER, PROJECT_FILE):
            if os.path.exists(path):
                os.remove(path)
        log("state cleared")

    if not ensure_deployed(args.project, args.redeploy):
        sys.exit(1)

    # Extra CLI arguments are reserved for the package's application logic.
    print("[saeka] ready.")


if __name__ == "__main__":
    main()
