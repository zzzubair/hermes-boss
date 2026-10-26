"""GitHub evidence and read-only worktree inspection on the worker host."""
import json
import re
import shlex
import subprocess
from urllib.parse import urlsplit

from .common import BossError, refuse


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
        fields = "url,title,state,isDraft,headRefOid,headRefName,baseRefName,mergeStateStatus,reviewDecision,statusCheckRollup,mergeCommit"
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

    def branches(self, repository, prefix):
        """Origin branch names starting with prefix, in one call."""
        try:
            refs = json.loads(run(["gh", "api", f"repos/{repository}/git/matching-refs/heads/{prefix}"]))
            return {r["ref"].removeprefix("refs/heads/") for r in refs}
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise BossError("github", f"invalid branch list: {exc}") from None

    def branch_head(self, repository, branch):
        """Commit at the tip of an origin branch, or None when the branch is gone."""
        try:
            refs = json.loads(run(["gh", "api", f"repos/{repository}/git/matching-refs/heads/{branch}"]))
            return next((r["object"]["sha"] for r in refs if r["ref"] == "refs/heads/" + branch), None)
        except (KeyError, TypeError, ValueError) as exc:
            raise BossError("github", f"invalid branch ref: {exc}") from None

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
        print(json.dumps({"gone": True}))
        sys.exit(0)
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


def on_host(host, script, *args):
    argv = ["python3", "-c", script, *args]
    if host.get("ssh"):
        argv = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10", host["ssh"], shlex.join(argv)]
    return run(argv, timeout=100)


def inspect_worktree(host, path, branch, base):
    try:
        data = json.loads(on_host(host, GIT_PROBE, path, branch, base))
        if data == {"gone": True}:
            return data
        if not all(k in data for k in ("head", "branch", "origin", "dirty", "in_default", "pushed")):
            raise ValueError("incomplete git evidence")
        return data
    except ValueError as exc:
        raise BossError("git", f"invalid worktree evidence: {exc}") from None


# Removes a finished worker's worktree and local branch on its host. It re-checks
# right before removing, so work made after settle's inspection is never lost:
# uncommitted or unpushed work refuses. Work is safe when origin has the commit:
# in the default branch, on the pushed branch, or the head of a merged/closed PR.
WORKTREE_REMOVE = r'''
import json, pathlib, subprocess, sys
path, base, branches, safe_heads = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), json.loads(sys.argv[4])
def git(*args, cwd=path, allow_failure=False):
    p = subprocess.run(["git", "-C", cwd, *args], text=True, capture_output=True, timeout=60)
    if p.returncode and not allow_failure:
        raise RuntimeError("git " + args[0] + " failed: " + p.stderr.strip()[:300])
    return p
def done(**result):
    print(json.dumps(result))
    sys.exit(0)
try:
    if not pathlib.Path(path).is_dir():
        done(state="gone")
    top = git("rev-parse", "--show-toplevel").stdout.strip()
    listed = git("worktree", "list", "--porcelain").stdout.split("\n\n")
    primary = listed[0].splitlines()[0].removeprefix("worktree ")
    if pathlib.Path(top).resolve() != pathlib.Path(path).resolve():
        done(state="kept", reason="path is not a worktree root")
    if pathlib.Path(primary).resolve() == pathlib.Path(path).resolve():
        done(state="kept", reason="path is the primary checkout")
    head = git("rev-parse", "HEAD").stdout.strip()
    branch = git("branch", "--show-current").stdout.strip()
    if branch and branch not in branches:
        done(state="kept", reason="worktree is on another branch: " + branch)
    if git("status", "--porcelain", "--untracked-files=all").stdout:
        done(state="kept", reason="uncommitted work")
    git("fetch", "--no-tags", "origin", base)
    landed = git("merge-base", "--is-ancestor", head, "FETCH_HEAD", allow_failure=True).returncode == 0
    remote = git("ls-remote", "--heads", "origin", "refs/heads/" + branch).stdout.split() if branch else []
    if not (landed or head in safe_heads or (remote and remote[0] == head)):
        done(state="kept", reason="unpushed commits")
    git("worktree", "remove", path, cwd=primary)
    deleted = bool(branch) and not git("branch", "-D", branch, cwd=primary, allow_failure=True).returncode
    done(state="removed", branch=branch if deleted else None)
except Exception as e:
    print(json.dumps({"error": str(e)}))
    sys.exit(3)
'''


def remove_worktree(host, path, base, branches, safe_heads):
    """Remove a worktree only if nothing in it would be lost; returns state/reason."""
    try:
        data = json.loads(on_host(host, WORKTREE_REMOVE, path, base, json.dumps(sorted(branches)), json.dumps(sorted(safe_heads))))
        if data.get("state") not in ("removed", "kept", "gone"):
            raise ValueError("unknown worktree removal result")
        return data
    except ValueError as exc:
        raise BossError("git", f"invalid worktree removal result: {exc}") from None


def repository_from_origin(origin):
    """Accept GitHub origin URLs, never a lookalike hostname or extra path."""
    scp = re.fullmatch(r"(?:[^/@:]+@)?github\.com:([^\s]+)", origin)
    try:
        url = urlsplit(origin) if not scp else None
        if scp:
            path = scp[1]
        elif (url.scheme in ("https", "http", "ssh", "git") and url.hostname == "github.com"
              and not url.query and not url.fragment):
            path = url.path.lstrip("/")
        else:
            return None
    except ValueError:
        return None
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path) or any(p in (".", "..") for p in path.split("/")):
        return None
    return path


def resolve_repository(host, workspace_root):
    if not isinstance(workspace_root, str) or not workspace_root:
        refuse("T3 project has no workspaceRoot for origin lookup", "check the T3 project workspace")
    argv = ["git", "-C", workspace_root, "remote", "get-url", "origin"]
    if host.get("ssh"):
        argv = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10", host["ssh"], shlex.join(argv)]
    origin = run(argv).strip()
    repository = repository_from_origin(origin)
    if not repository:
        refuse("project origin must identify a GitHub owner/repo", "check git remote get-url origin on the project host")
    return repository


def github_repository(project):
    identity = project.get("repositoryIdentity") or {}
    if identity.get("owner") and identity.get("name"):
        return identity["owner"] + "/" + identity["name"]
    url = (identity.get("locator") or {}).get("remoteUrl", "")
    return repository_from_origin(url)
