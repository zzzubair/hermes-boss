"""GitHub evidence and read-only worktree inspection on the worker host."""
import json
import re
import shlex
import subprocess

from .common import BossError


PR_PATTERN = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)\Z")


def run(argv, timeout=45):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BossError("external", f"{argv[0]} failed: {exc}") from None
    if result.returncode:
        # External tools can echo credentials. Never truncate first and leak a
        # partial secret that the outer redactor can no longer recognize.
        raise BossError("external", f"{argv[0]} exited {result.returncode}",
                        "check gh auth status or worker-host Git/SSH access")
    return result.stdout


def ci_summary(checks):
    if not isinstance(checks, list) or any(not isinstance(c, dict) for c in checks):
        raise BossError("github", "invalid check-rollup data")
    counts = {"passed": 0, "failed": 0, "pending": 0, "skipped": 0}
    for check in checks:
        state = str(check.get("conclusion") or check.get("state") or check.get("status") or "UNKNOWN").upper()
        if state in ("SUCCESS",):
            counts["passed"] += 1
        elif state in ("SKIPPED", "NEUTRAL"):
            counts["skipped"] += 1
        elif state in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"):
            counts["failed"] += 1
        else:
            counts["pending"] += 1
    state = ("none" if not checks else "failed" if counts["failed"] else
             "pending" if counts["pending"] else "green" if counts["passed"] else "skipped")
    return {"state": state, **counts}


class GitHub:
    def view(self, url):
        if not PR_PATTERN.fullmatch(url):
            raise BossError("protocol", "expected a github.com pull request URL")
        fields = "url,state,isDraft,headRefOid,headRefName,baseRefName,mergeStateStatus,reviewDecision,statusCheckRollup,mergeCommit"
        try:
            data = json.loads(run(["gh", "pr", "view", url, "--json", fields]))
            for key in ("state", "headRefOid", "headRefName", "isDraft", "mergeStateStatus", "statusCheckRollup"):
                if key not in data:
                    raise ValueError(f"missing {key}")
            data["ci"] = ci_summary(data["statusCheckRollup"] or [])
            data["url"] = url
            return data
        except (ValueError, TypeError) as exc:
            raise BossError("github", f"invalid gh PR response: {exc}") from None

    def default_branch(self, repository):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise BossError("config", "repository must be owner/repo", exit_code=1)
        try:
            data = json.loads(run(["gh", "repo", "view", repository, "--json", "defaultBranchRef"]))
            branch = data["defaultBranchRef"]["name"]
            if not branch or branch.startswith("-"):
                raise ValueError("invalid default branch")
            return branch
        except (KeyError, TypeError, ValueError) as exc:
            raise BossError("github", f"cannot resolve origin default branch: {exc}") from None

    def merge(self, pr):
        run(["gh", "pr", "merge", pr["url"], "--squash", "--match-head-commit", pr["headRefOid"]])


# Runs on the selected host, never on a same-named local path for a remote worker.
# Fetch refreshes evidence only; it does not checkout, clean, merge or delete files.
GIT_PROBE = r'''
import json, pathlib, subprocess, sys
path, branch, base = sys.argv[1:]
def git(*args, allow_failure=False):
    p = subprocess.run(["git", "-C", path, *args], text=True, capture_output=True, timeout=30)
    if p.returncode and not allow_failure:
        raise RuntimeError("git " + args[0] + " failed: " + p.stderr.strip()[:300])
    return p
try:
    if not pathlib.Path(path).is_dir():
        raise RuntimeError("worktree does not exist")
    head = git("rev-parse", "HEAD").stdout.strip()
    actual_branch = git("branch", "--show-current").stdout.strip()
    origin = git("remote", "get-url", "origin").stdout.strip()
    dirty = bool(git("status", "--porcelain", "--untracked-files=all").stdout)
    git("fetch", "--no-tags", "origin", base)
    ancestor = git("merge-base", "--is-ancestor", head, "FETCH_HEAD", allow_failure=True)
    if ancestor.returncode not in (0, 1):
        raise RuntimeError("cannot check default-branch ancestry")
    remote = git("ls-remote", "--heads", "origin", "refs/heads/" + branch).stdout.split()
    print(json.dumps({"head": head, "branch": actual_branch, "origin": origin, "dirty": dirty,
                      "in_default": ancestor.returncode == 0,
                      "pushed": bool(remote and remote[0] == head)}))
except Exception as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(3)
'''


def inspect_worktree(host, path, branch, base):
    argv = ["python3", "-c", GIT_PROBE, path, branch, base]
    if host.get("ssh"):
        argv = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10", host["ssh"], shlex.join(argv)]
    try:
        data = json.loads(run(argv, timeout=100))
        if not all(k in data for k in ("head", "branch", "origin", "dirty", "in_default", "pushed")):
            raise ValueError("incomplete git evidence")
        return data
    except ValueError as exc:
        raise BossError("git", f"invalid worktree evidence: {exc}") from None


def github_repository(project):
    identity = project.get("repositoryIdentity") or {}
    if identity.get("owner") and identity.get("name"):
        return identity["owner"] + "/" + identity["name"]
    url = (identity.get("locator") or {}).get("remoteUrl", "")
    match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", url)
    return match[1] if match else None
