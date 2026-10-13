#!/usr/bin/env python3
"""storage-cleanup: free local disk across every git checkout on this machine, and tidy the GitHub remotes.

Standard library only (python3 >= 3.9, the version the macOS command line tools ship). `git` is required; every
other tool is optional and used when present: `gh` (remote cleanup, squash-merge detection), `du`, `lsof`, `npm`,
`pnpm`, `yarn`, `pip3`, `docker`, `brew`.

What it removes, and only when the rule says the data exists somewhere else or can be regenerated:
  local   worktrees whose work is merged (or pushed and long idle), local branches whose work is merged,
          git-ignored build output (node_modules, dist, coverage ...) in checkouts idle for days, oversized package
          and browser caches of the account running it, git garbage.
  remote  branches whose pull request closed (never one an open PR uses as head or base), Actions caches of
          closed PRs and deleted branches, Actions artifacts older than N days.

Several accounts' home folders can be scanned (`homes` in the config). A run changes only what the account running
it may write; everything else is reported as "needs <account>" so each account's own scheduled run picks it up.

Usage:
  storage_cleanup.py scan  [--home NAME,...] [--local-only | --remote-only] [--no-caches] [--where] [--json]
  storage_cleanup.py clean [same flags]
  storage_cleanup.py config show|path|init
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

try:  # POSIX only; the plugin targets macOS and Linux
    import fcntl
    import pwd
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore
    pwd = None  # type: ignore

HOME = Path(os.environ.get("STORAGE_CLEANUP_HOME") or (Path.home() / ".claude" / "storage-cleanup"))
CONFIG_PATH = HOME / "config.json"
UTC = dt.timezone.utc
GB = 1024 ** 3
MB = 1024 ** 2

DEFAULTS: dict = {
    # Home folders to scan: "~" is the account running the script, "~name" another account on this machine.
    "homes": ["~", "~dgshor", "~aiuser1", "~aiuser2"],
    # Extra folders to scan besides the homes (absolute or ~-relative).
    "roots": [],
    # Directory names never descended into while looking for repositories (hidden folders are skipped too).
    "skipDirs": ["Library", "Applications", "Pictures", "Music", "Movies", "node_modules", "vendor", "venv",
                 "Public", "snap", "go", "OneDrive", "Dropbox", "Google Drive"],
    "maxDepth": 6,
    # A worktree whose work is merged is removed once its git metadata has been idle this long.
    "worktreeIdleHours": 12,
    # Worktrees that must stay (uncommitted or unmerged work) but live outside their checkout (a Claude scratchpad,
    # a temp folder) are moved here, relative to the checkout, once idle. "" turns moving off.
    "worktreeHome": ".claude/worktrees",
    # Extra places Claude leaves worktrees, searched for the running account ({uid} is its numeric id).
    "tempRoots": ["/tmp/claude-{uid}", "/private/tmp/claude-{uid}"],
    # A worktree whose work is pushed (but not merged) is removed once idle this long; its branch is kept.
    "staleWorktreeDays": 14,
    # Git-ignored build output is removed from a checkout idle this long.
    "repoIdleDays": 7,
    "buildDirDepth": 5,
    "buildDirs": ["node_modules", "dist", "build", "coverage", ".turbo", ".vite", ".next", ".nuxt", ".svelte-kit",
                  ".parcel-cache", ".cache", "playwright-report", "test-results", "blob-report", "storybook-static",
                  "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "target", ".gradle"],
    # Branches never deleted, locally or remotely (fnmatch patterns; the default branch is always kept).
    "protectBranches": ["main", "master", "develop", "dev", "staging", "production", "release/*", "hotfix/*",
                        "laravel-dev"],
    # Package / browser caches of the running account, cleaned when at least cacheMinMB.
    "caches": {"npm": True, "pnpm": True, "yarn": True, "pip": True, "playwright": True, "brew": True,
               "docker": True},
    "cacheMinMB": 200,
    "playwrightKeep": 2,
    "dockerBuilderUntilHours": 168,
    # Remote cleanup runs only for repositories under these owners, with push access through gh.
    "remoteOwners": ["homeporthealth", "Velocity-Org", "Shor-Consulting-Inc", "shorconsulting"],
    "remote": {"branches": True, "mergedGraceDays": 1, "closedGraceDays": 30, "actionsCaches": True,
               "artifacts": True, "artifactDays": 14},
}


def log(msg: str) -> None:
    print(f"[storage-cleanup] {msg}", file=sys.stderr)


START = time.time()
QUIET = False


def step(msg: str) -> None:
    """Progress line on stderr with the elapsed time, so a long run is never silent (--quiet turns it off)."""
    if not QUIET:
        e = int(time.time() - START)
        print(f"[{e // 60:02d}:{e % 60:02d}] {msg}", file=sys.stderr, flush=True)


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ----------------------------------------------------------------------------- config

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def cmd_config(args: argparse.Namespace) -> int:
    if args.action == "path":
        print(CONFIG_PATH)
    elif args.action == "init":
        if CONFIG_PATH.exists():
            print(f"{CONFIG_PATH} exists; edit it directly")
        else:
            HOME.mkdir(parents=True, exist_ok=True)
            body = {"_comment": "Overrides for storage-cleanup; any key left out takes the built-in default "
                                "(storage_cleanup.py config show prints the effective config)."}
            body.update(DEFAULTS)
            CONFIG_PATH.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
            print(f"wrote {CONFIG_PATH}")
    else:
        print(json.dumps(load_config(), indent=2))
    return 0


# ----------------------------------------------------------------------------- process helpers

def run(cmd: list, cwd: str | None = None, timeout: int = 900) -> tuple:
    env = dict(os.environ)
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GH_PROMPT_DISABLED": "1"})
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out after {timeout}s"


def git(path, *args: str, timeout: int = 900) -> tuple:
    # GIT_OPTIONAL_LOCKS=0 (in run) keeps `git status` from rewriting the index, so reading a checkout never
    # makes it look active. git's own guard against another account's repository (safe.directory) stays on:
    # git refuses such a checkout, and it is left to that account's run (--all-accounts runs it as that account).
    return run(["git", "-C", str(path), *args], timeout=timeout)


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def du_bytes(path: Path) -> int:
    if not exists(path):
        return 0
    if have("du"):
        rc, out, _ = run(["du", "-sk", str(path)], timeout=1800)
        if out.strip():
            try:
                return int(out.split()[0]) * 1024
            except ValueError:
                pass
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_blocks * 512
            except OSError:
                pass
    return total


def path_state(path) -> str:
    """present | missing | unknown. A path inside a folder this account may not enter (another account's private
    temp or home) is unknown, never missing: python 3.13 raises on it, and git reports such a worktree as prunable
    even though it is someone's live checkout."""
    try:
        os.lstat(str(path))
        return "present"
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError:
        return "unknown"


def exists(path: Path) -> bool:
    return path_state(path) == "present"


def owner_near(path) -> str:
    """Owner of the path, or of its nearest ancestor this account can see."""
    p = Path(path)
    while True:
        if path_state(p) == "present":
            return owner_of(p)
        if p == p.parent:
            return "?"
        p = p.parent


def owner_of(path: Path) -> str:
    try:
        return pwd.getpwuid(path.stat().st_uid).pw_name if pwd else "?"
    except (KeyError, OSError):
        return "?"


def writable(path: Path) -> bool:
    return os.access(str(path), os.W_OK | os.X_OK)


def process_cwds() -> set:
    """Working directories of running processes, so a checkout something is using is never touched."""
    cwds = set()
    proc = Path("/proc")
    if proc.is_dir():
        for p in proc.iterdir():
            if p.name.isdigit():
                try:
                    cwds.add(os.path.realpath(os.readlink(str(p / "cwd"))))
                except OSError:
                    pass
    elif have("lsof"):
        rc, out, _ = run(["lsof", "-a", "-d", "cwd", "-F", "n"], timeout=120)
        for line in out.splitlines():
            if line.startswith("n/"):
                cwds.add(os.path.realpath(line[1:]))
    return cwds


def busy(path: Path, cwds: set) -> bool:
    p = os.path.realpath(str(path))
    return any(c == p or c.startswith(p + os.sep) for c in cwds)


def free_bytes(path: Path) -> tuple:
    try:
        u = shutil.disk_usage(str(path))
        return u.free, u.total
    except OSError:
        return 0, 0


# ----------------------------------------------------------------------------- homes and repository discovery

def resolve_homes(cfg: dict, only: list | None = None) -> tuple:
    """Returns (homes, notes): homes are {name, path}; notes name the accounts that do not exist here."""
    me = current_user()
    homes, notes, seen = [], [], set()
    for spec in cfg.get("homes", []):
        name = me if spec in ("~", "") else spec.lstrip("~")
        if only and name not in only:
            continue
        try:
            path = Path(pwd.getpwnam(name).pw_dir) if pwd else Path.home()
        except KeyError:
            notes.append(f"account {name} does not exist on this machine")
            continue
        real = os.path.realpath(str(path))
        if real in seen:
            continue
        seen.add(real)
        homes.append({"name": name, "path": Path(real)})
    return homes, notes


def current_user() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name if pwd else os.environ.get("USER", "?")
    except KeyError:
        return os.environ.get("USER", "?")


def _is_dir(e) -> bool:
    try:
        return e.is_dir(follow_symlinks=False)
    except OSError:
        return False


def find_repos(root: Path, cfg: dict) -> tuple:
    """Main checkouts (a `.git` directory) and worktree folders (a `.git` file) under root.
    Returns (repos, unreadable_dirs, worktree_dirs)."""
    skip = set(cfg.get("skipDirs", []))
    max_depth = int(cfg.get("maxDepth", 6))
    repos, unreadable, worktrees = [], [], []
    stack = [(root, 0)]
    seen = 0
    while stack:
        d, depth = stack.pop()
        seen += 1
        if seen % 5000 == 0:
            step(f"    {seen} folders looked at, {len(repos)} checkouts so far")
        try:
            entries = list(os.scandir(d))
        except PermissionError:
            unreadable.append(str(d))
            continue
        except OSError:
            continue
        dotgit = next((e for e in entries if e.name == ".git"), None)
        if dotgit is not None:
            # a checkout's own worktrees are found through git, not by walking into it
            (repos if _is_dir(dotgit) else worktrees).append(Path(d))
            continue
        if depth >= max_depth:
            continue
        for e in entries:
            if e.name.startswith(".") or e.name in skip:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((Path(e.path), depth + 1))
            except OSError:
                pass
    return sorted(repos), unreadable, sorted(worktrees)


def main_repo_of(worktree_dir: Path) -> tuple:
    """(main checkout, gitdir) of a worktree folder; main is None when git no longer knows the worktree (the
    folder is an orphan) or the gitdir is not a linked-worktree one."""
    g = gitdir_of(worktree_dir)
    if g is None or path_state(g) != "present":
        return None, g
    if g.parent.name == "worktrees" and g.parent.parent.name == ".git":
        main = g.parent.parent.parent
        if path_state(main / ".git") == "present":
            return main, g
    return None, g


# ----------------------------------------------------------------------------- git model

SLUG_RE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def parse_slug(url: str) -> str | None:
    m = SLUG_RE.search(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def parse_worktrees(porcelain: str) -> list:
    out, cur = [], None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[9:], "head": None, "branch": None, "locked": False, "prunable": False,
                   "bare": False}
            out.append(cur)
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "", 1)
        elif line.startswith("locked"):
            cur["locked"] = True
        elif line.startswith("prunable"):
            cur["prunable"] = True
        elif line == "bare":
            cur["bare"] = True
    for i, wt in enumerate(out):
        wt["main"] = i == 0
    return out


def gitdir_of(wt_path: Path) -> Path | None:
    dotgit = wt_path / ".git"
    if dotgit.is_dir():
        return dotgit
    try:
        text = dotgit.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text.startswith("gitdir:"):
        p = Path(text[7:].strip())
        return p if p.is_absolute() else (wt_path / p).resolve()
    return None


def last_activity(gitdir: Path | None) -> float:
    """Newest mtime of the files git rewrites when someone works in the checkout (FETCH_HEAD is left out: a
    background fetch is not work)."""
    if gitdir is None:
        return 0.0
    best = 0.0
    for name in ("index", "HEAD", "logs/HEAD", "ORIG_HEAD", "COMMIT_EDITMSG"):
        try:
            best = max(best, (gitdir / name).stat().st_mtime)
        except OSError:
            pass
    return best


IN_PROGRESS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.owner = owner_of(path)
        # Only the owning account changes a checkout, even when another account has write access to it (a shared
        # folder): git run as another account leaves files in .git that the owner may then be unable to rewrite.
        self.writable = self.owner == current_user() and writable(path) and writable(path / ".git")
        # Accounts share folders (agent accounts work in dgshor's checkouts): .git files another account wrote
        # make fetch and prune fail, so such a checkout is left to that account.
        self.blocker = None
        if self.writable:
            for name in ("config", "HEAD", "index", "FETCH_HEAD", "packed-refs"):
                f = path / ".git" / name
                if exists(f) and not os.access(str(f), os.R_OK | os.W_OK):
                    self.blocker = (str(f), owner_of(f))
                    self.writable = False
                    break
        rc, out, _ = git(path, "remote", "get-url", "origin")
        self.remote_url = out.strip() if rc == 0 else ""
        self.slug = parse_slug(self.remote_url) if self.remote_url else None
        self.fetched = False
        self.fetch_error = ""
        self.default_branch = None
        self.worktrees = []

    def load(self) -> None:
        self.default_branch = detect_default_branch(self.path)
        rc, out, _ = git(self.path, "worktree", "list", "--porcelain")
        self.worktrees = parse_worktrees(out) if rc == 0 else []

    @property
    def default_ref(self) -> str | None:
        return f"refs/remotes/origin/{self.default_branch}" if self.default_branch else None


def detect_default_branch(path: Path) -> str | None:
    rc, out, _ = git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.strip().startswith("origin/"):
        return out.strip()[7:]
    for b in ("main", "master"):
        if git(path, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{b}")[0] == 0:
            return b
    return None


def is_ancestor(path: Path, a: str, b: str) -> bool:
    return git(path, "merge-base", "--is-ancestor", a, b)[0] == 0


def has_object(path: Path, sha: str) -> bool:
    return git(path, "cat-file", "-e", f"{sha}^{{commit}}")[0] == 0


def finished(repo: Repo, sha: str, branch: str | None, prs: list) -> str | None:
    """Why the commit's work is safe elsewhere for good (merged), or None."""
    if repo.default_ref and is_ancestor(repo.path, sha, repo.default_ref):
        return f"in origin/{repo.default_branch}"
    for pr in prs:
        if not pr.get("merged_at"):
            continue
        if pr.get("head_sha") == sha:
            return f"PR #{pr['number']} merged"
        if branch and pr.get("head_ref") == branch and has_object(repo.path, pr["head_sha"]) \
                and is_ancestor(repo.path, sha, pr["head_sha"]):
            return f"PR #{pr['number']} merged"
    return None


def pushed(repo: Repo, sha: str) -> bool:
    """Every commit is on a remote branch. Only trusted after a successful `fetch --prune`, otherwise a stale
    remote-tracking ref of a branch deleted on GitHub would vouch for work that exists nowhere else."""
    if not repo.fetched:
        return False
    # origin only: it is the remote fetched with --prune; another remote's tracking refs may be stale.
    rc, out, _ = git(repo.path, "rev-list", "-n", "1", sha, "--not", "--remotes=origin")
    return rc == 0 and not out.strip()


def protected(name: str, cfg: dict, default: str | None) -> bool:
    return name == default or any(fnmatch.fnmatch(name, p) for p in cfg.get("protectBranches", []))


# ----------------------------------------------------------------------------- GitHub

class GitHub:
    """Thin `gh api` reader / deleter. Tests replace it with a fake that has the same methods."""

    def __init__(self):
        self.ok = have("gh") and run(["gh", "auth", "status"], timeout=60)[0] == 0
        self._cache: dict = {}

    def _lines(self, path: str, jq: str) -> list:
        rc, out, err = run(["gh", "api", "--paginate", path, "--jq", jq], timeout=1800)
        if rc != 0:
            raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else f"gh api {path} failed")
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def _memo(self, key: str, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def info(self, slug: str) -> dict:
        return self._memo("info:" + slug, lambda: self._lines(
            f"repos/{slug}", "{default_branch, push: .permissions.push, archived} | tojson")[0])

    def pulls(self, slug: str) -> list:
        jq = (".[] | {number, state, merged_at, closed_at, head_ref: .head.ref, head_sha: .head.sha, "
              "head_repo: .head.repo.full_name, base_ref: .base.ref} | tojson")
        return self._memo("pulls:" + slug, lambda: self._lines(f"repos/{slug}/pulls?state=all&per_page=100", jq))

    def branches(self, slug: str) -> list:
        return self._lines(f"repos/{slug}/branches?per_page=100", ".[] | {name, sha: .commit.sha, protected} | tojson")

    def caches(self, slug: str) -> list:
        return self._lines(f"repos/{slug}/actions/caches?per_page=100",
                           ".actions_caches[] | {id, ref, size_in_bytes, last_accessed_at} | tojson")

    def artifacts(self, slug: str) -> list:
        return self._lines(f"repos/{slug}/actions/artifacts?per_page=100",
                           ".artifacts[] | {id, name, size_in_bytes, created_at, expired} | tojson")

    def open_using(self, slug: str, name: str) -> list:
        """Open pull requests that use the branch as base or head, read fresh (no cache) just before a delete."""
        owner = slug.split("/")[0]
        q = urllib.parse.quote(name, safe="")
        found = []
        for param in (f"base={q}", f"head={owner}:{q}"):
            found += self._lines(f"repos/{slug}/pulls?state=open&per_page=5&{param}", ".[] | {number} | tojson")
        return [p["number"] for p in found]

    def delete(self, path: str) -> tuple:
        rc, _, err = run(["gh", "api", "-X", "DELETE", path], timeout=120)
        return rc == 0, err.strip()


# ----------------------------------------------------------------------------- plan

class Plan:
    def __init__(self):
        self.actions: list = []   # things to do: kind, target, bytes, why, run (callable, not serialized)
        self.kept: list = []      # things looked at and left alone: kind, target, why
        self.needs: dict = {}     # account -> list of notes for things this account may not change
        self.notes: list = []
        self.remote: list = []    # per-repository remote summary
        self.loose: list = []     # worktree folders git no longer knows (report only): path, bytes, modified
        self.slugs: list = []     # GitHub repositories of the checkouts found

    def add(self, kind: str, target: str, size: int, why: str, fn, home: str = "") -> None:
        self.actions.append({"kind": kind, "target": target, "bytes": int(size), "why": why, "home": home,
                             "status": "planned", "_run": fn})

    def keep(self, kind: str, target: str, why: str) -> None:
        self.kept.append({"kind": kind, "target": target, "why": why})

    def need(self, account: str, note: str) -> None:
        self.needs.setdefault(account, []).append(note)


def plan_repo(repo: Repo, cfg: dict, gh: GitHub | None, plan: Plan, cwds: set, now: float, home: str,
              fetch: bool = True) -> None:
    allowed = repo.slug and repo.slug.split("/")[0].lower() in {o.lower() for o in cfg["remoteOwners"]}
    if fetch and repo.writable and allowed:
        step(f"    git fetch --prune ({repo.slug})")
        rc, _, err = git(repo.path, "fetch", "--prune", "--quiet", "origin", timeout=300)
        repo.fetched = rc == 0
        repo.fetch_error = "" if rc == 0 else (err.strip().splitlines() or ["fetch failed"])[-1]
    repo.load()
    prs = []
    if gh and gh.ok and allowed:
        try:
            prs = [p for p in gh.pulls(repo.slug) if p.get("head_repo") in (None, repo.slug)]
        except RuntimeError as e:
            plan.notes.append(f"{repo.slug}: pull requests unreadable ({e}); merged-PR detection off")

    if not repo.writable:
        if repo.blocker:
            plan.need(repo.blocker[1], f"checkout {repo.path} ({repo.blocker[0]} belongs to {repo.blocker[1]})")
        else:
            plan.need(repo.owner, f"checkout {repo.path} (owned by {repo.owner})")
        return

    idle_wt_s = float(cfg["worktreeIdleHours"]) * 3600
    stale_wt_s = float(cfg["staleWorktreeDays"]) * 86400
    removing = set()
    checked_out = {wt["branch"] for wt in repo.worktrees if wt.get("branch")}
    me = current_user()
    blind = False  # a worktree this account cannot see: git would call it prunable, so never prune here
    moving: set = set()  # destinations planned in this checkout, so two worktrees never get the same one
    build_now: set = set()  # merged worktrees kept for their ignored files: build output goes without waiting

    for wt in repo.worktrees:
        if wt["main"] or wt["bare"]:
            continue
        path = Path(wt["path"])
        label = str(path)
        state = path_state(path)
        if state == "unknown":
            blind = True
            plan.need(owner_near(path), f"worktree {label} (not visible to {me})")
            continue
        if wt["prunable"] or state == "missing":
            continue  # `git worktree prune` below drops its metadata
        if wt["locked"]:
            plan.keep("worktree", label, "locked")
            continue
        if not writable(path) or owner_of(path) != me:
            plan.need(owner_of(path), f"worktree {label}")
            continue
        if busy(path, cwds):
            plan.keep("worktree", label, "a running process is using it")
            continue
        gitdir = gitdir_of(path)
        if gitdir and any(exists(gitdir / n) for n in IN_PROGRESS):
            plan.keep("worktree", label, "rebase / merge in progress")
            continue
        activity = last_activity(gitdir)
        idle = now - activity
        if idle < idle_wt_s:
            plan.keep("worktree", label, f"active {idle / 3600:.0f}h ago")
            continue
        rc, out, _ = git(path, "status", "--porcelain", "--untracked-files=normal")
        if rc != 0:
            plan.keep("worktree", label, "git status failed")
            continue
        sha = wt["head"]
        snap = (sha, activity)  # re-checked just before the worktree is moved or removed
        if out.strip():
            # An editor saves files without touching git: the changed files must be idle too before a move. All
            # untracked files are listed one by one, since an edit deep in a new folder leaves the folder's own
            # mtime unchanged.
            rc_all, every, _ = git(path, "status", "--porcelain", "--untracked-files=all")
            edited = now - newest_mtime(path, every if rc_all == 0 else out)
            if edited < idle_wt_s:
                plan.keep("worktree", label, f"uncommitted or untracked changes, edited {edited / 3600:.0f}h ago")
            else:
                plan.keep("worktree", label, "uncommitted or untracked changes")
                plan_move(repo, path, cfg, plan, home, moving, removing, snap)
            continue
        why = finished(repo, sha, wt["branch"], prs) if sha else None
        # Pushed-but-unmerged work only for a worktree on a branch, which is kept: a detached HEAD's commits would
        # rest on a remote branch alone, and this same run may delete that branch.
        if not why and sha and wt["branch"] and idle >= stale_wt_s and pushed(repo, sha):
            why = f"pushed, idle {idle / 86400:.0f}d (branch kept)"
        if not why:
            plan.keep("worktree", label, "has work that is not merged" +
                      ("" if repo.fetched else " (or not verifiable without a fetch)"))
            plan_move(repo, path, cfg, plan, home, moving, removing, snap)
            continue
        # `git worktree remove` also deletes git-ignored files: a worktree holding any that are not build output
        # (an .env, local settings) stays, and only its build output goes.
        extra = ignored_extras(path, cfg)
        if extra:
            plan.keep("worktree", label, f"merged, but holds ignored files that are not build output ({extra[0]}); "
                                         "its build output is removed instead")
            build_now.add(os.path.realpath(label))
            continue
        other = foreign_entry(path)
        if other:
            plan.need(owner_of(Path(other)), f"worktree {label} (holds {other}, owned by {owner_of(Path(other))})")
            continue
        removing.add(os.path.realpath(label))
        step(f"    sizing worktree {path.name}")
        plan.add("worktree", label, du_bytes(path), why,
                 lambda r=repo, p=path, s=snap: _remove_worktree(r, p, s), home)
        if wt["branch"] and "branch kept" not in why:
            checked_out.discard(wt["branch"])

    # Local branches whose work is merged (squash merges included, through the PR's head sha).
    rc, out, _ = git(repo.path, "for-each-ref", "refs/heads", "--format=%(refname:short)%00%(objectname)")
    for line in out.splitlines():
        if "\0" not in line:
            continue
        name, sha = line.split("\0", 1)
        if name in checked_out or protected(name, cfg, repo.default_branch):
            continue
        why = finished(repo, sha, name, prs)
        if why:
            plan.add("branch", f"{repo.path} {name}", 0, why,
                     lambda r=repo, n=name, s=sha: _delete_local_branch(r, n, s), home)

    # Git-ignored build output in checkouts nobody has touched for repoIdleDays.
    idle_repo_s = float(cfg["repoIdleDays"]) * 86400
    for wt in repo.worktrees:
        path = Path(wt["path"])
        if wt["bare"] or not exists(path) or os.path.realpath(str(path)) in removing:
            continue
        if not writable(path) or owner_of(path) != me or busy(path, cwds):
            continue
        gd = gitdir_of(path)
        act = last_activity(gd)
        idle = now - act
        if idle < idle_repo_s and os.path.realpath(str(path)) not in build_now:
            continue
        for d in build_dirs(path, cfg):
            refused = refuse_delete(d)
            if refused:
                plan.keep("build", str(d), refused)
                continue
            other = foreign_entry(d)
            if other:
                plan.need(owner_of(Path(other)), f"build output {d} (holds files owned by {owner_of(Path(other))})")
                continue
            step(f"    sizing {os.path.relpath(str(d), str(path))}")
            plan.add("build", str(d), du_bytes(d), f"git-ignored, checkout idle {idle / 86400:.0f}d",
                     lambda p=d, g=gd, a=act: _rmtree_if_idle(p, g, a), home)

    if blind:
        plan.keep("git", f"{repo.path} worktree prune", "skipped: a worktree is not visible to this account")
    else:
        plan.add("git", f"{repo.path} worktree prune", 0, "drop metadata of deleted worktrees",
                 lambda r=repo: _ok(git(r.path, "worktree", "prune")), home)
    loose = loose_bytes(repo.path)
    if loose >= 100 * MB:
        # gc would otherwise prune worktree metadata on its own; worktree cleanup stays with the rules above.
        plan.add("git", f"{repo.path} gc", loose, "loose objects",
                 lambda r=repo: _ok(git(r.path, "-c", "gc.worktreePruneExpire=never", "gc", "--quiet",
                                        timeout=3600)), home)


def nested_git(path: Path) -> bool:
    """A `.git` (folder or worktree file) anywhere under path, across mount points too: the delete crosses them."""
    if path_state(path) == "missing":
        return False
    if have("find"):
        rc, out, _ = run(["find", str(path), "-name", ".git", "-print", "-quit"], timeout=1800)
        if out.strip():
            return True
        return rc != 0  # a timeout or an unreadable folder proves nothing: fail closed, the folder stays
    for root, dirs, files in os.walk(str(path)):
        if ".git" in dirs or ".git" in files:
            return True
    return False


def foreign_entry(path: Path) -> str | None:
    """The first file or folder under path that belongs to another account, or None. Such a tree cannot be fully
    deleted by this account (git or rmtree would stop half way), so it is left to its owner."""
    uid = os.getuid()
    if have("find"):
        rc, out, _ = run(["find", str(path), "-xdev", "!", "-uid", str(uid), "-print", "-quit"], timeout=1800)
        return out.strip().splitlines()[0] if out.strip() else None
    for root, dirs, files in os.walk(str(path)):
        for name in dirs + files:
            p = os.path.join(root, name)
            try:
                if os.lstat(p).st_uid != uid:
                    return p
            except OSError:
                return p
    return None


HARMLESS_IGNORED = {".DS_Store", "Thumbs.db"}


def ignored_extras(worktree: Path, cfg: dict) -> list:
    """Git-ignored files in a worktree that are not build output, which `git worktree remove` would delete."""
    rc, out, _ = git(worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    if rc != 0:
        return ["(ignored files could not be listed)"]
    names = set(cfg.get("buildDirs", []))
    extras = []
    for entry in out.split("\0"):
        parts = [p for p in entry.strip("/").split("/") if p]
        if not parts:
            continue
        # Build output is a whole ignored folder with a build name (`--directory` collapses it to `name/`); an
        # ignored file that merely sits under a folder called `target` (an ignored `.pem`) is not.
        build = entry.endswith("/") and parts[-1] in names
        if not build and parts[-1] not in HARMLESS_IGNORED:
            extras.append(entry)
        elif build and nested_git(worktree / entry):
            extras.append(f"{entry} (a nested git repository)")
    return extras


def within(path, parent) -> bool:
    p, q = os.path.realpath(str(path)), os.path.realpath(str(parent))
    return p == q or p.startswith(q + os.sep)


def newest_mtime(worktree: Path, porcelain: str) -> float:
    """Newest modification time among the files `git status --porcelain` lists as changed or untracked."""
    best = 0.0
    for line in porcelain.splitlines():
        rel = line[3:].split(" -> ")[-1].strip().strip('"')
        try:
            best = max(best, os.lstat(str(worktree / rel)).st_mtime)
        except OSError:
            pass
    return best


def changed_since_plan(path: Path, snap: tuple) -> str | None:
    """Minutes pass between planning and acting (every checkout is sized first). Why the worktree is no longer
    the one that was judged (HEAD moved, git activity, gone), or None."""
    sha, activity = snap
    if path_state(path) != "present":
        return "the worktree is gone"
    rc, out, _ = git(path, "rev-parse", "HEAD")
    if rc != 0 or (sha and out.strip() != sha):
        return "its HEAD moved"
    if last_activity(gitdir_of(path)) != activity:
        return "it was used"
    return None


def _remove_worktree(repo: Repo, path: Path, snap: tuple) -> tuple:
    why = changed_since_plan(path, snap)
    if why:
        return False, f"kept: {why} since the plan was made"
    return _ok(git(repo.path, "worktree", "remove", str(path)))


def _delete_local_branch(repo: Repo, name: str, sha: str) -> tuple:
    """Delete only the commit that was judged merged: `update-ref -d <ref> <sha>` is refused by git when the branch
    moved since the plan, and a branch a worktree has checked out again is kept."""
    rc, out, _ = git(repo.path, "worktree", "list", "--porcelain")
    if f"branch refs/heads/{name}" in out.splitlines():
        return False, "kept: checked out in a worktree"
    rc, out, err = git(repo.path, "update-ref", "-d", f"refs/heads/{name}", sha)
    if rc != 0:
        return False, f"kept: the branch moved since the plan was made ({(err or out).strip()[:120]})"
    return True, ""


def plan_move(repo: Repo, path: Path, cfg: dict, plan: Plan, home: str, moving: set, removing: set,
              snap: tuple = ("", 0.0)) -> None:
    """A worktree that has to stay but lives outside its checkout moves to <checkout>/<worktreeHome>/<name>.
    Called only for a worktree already found idle, unlocked, not in use, owned by this account and kept."""
    rel = cfg.get("worktreeHome") or ""
    if not rel or within(path, repo.path):
        return
    dest_root = repo.path / rel
    dest = dest_root / path.name
    n = 2
    while path_state(dest) != "missing" or str(dest) in moving:
        dest = dest_root / f"{path.name}-{n}"
        n += 1
    moving.add(str(dest))
    removing.add(os.path.realpath(str(path)))  # no build-output cleanup on a path that is about to move
    plan.add("move", f"{path} -> {dest}", 0, "kept work, moved into its checkout",
             lambda r=repo, s=path, d=dest, x=rel, sn=snap: _move_worktree(r, s, d, x, sn), home)


def _move_worktree(repo: Repo, src: Path, dest: Path, rel: str, snap: tuple = ("", 0.0)) -> tuple:
    if snap[1]:
        why = changed_since_plan(src, snap)
        if why:
            return False, f"kept: {why} since the plan was made"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Keep the moved worktree out of the main checkout's `git status`: a local, uncommitted exclude line.
    probe = f"{rel.rstrip('/')}/{dest.name}"
    if git(repo.path, "check-ignore", "-q", probe + "/")[0] != 0:
        exclude = repo.path / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a", encoding="utf-8") as f:
            f.write(f"\n# storage-cleanup: worktrees moved into this checkout\n/{rel.strip('/')}/\n")
    return _ok(git(repo.path, "worktree", "move", str(src), str(dest)))


def loose_bytes(path: Path) -> int:
    rc, out, _ = git(path, "count-objects", "-v")
    vals = {}
    for line in out.splitlines():
        k, _, v = line.partition(":")
        try:
            vals[k.strip()] = int(v.strip())
        except ValueError:
            pass
    return (vals.get("size", 0) + vals.get("size-garbage", 0)) * 1024


def build_dirs(worktree: Path, cfg: dict) -> list:
    names = set(cfg["buildDirs"])
    max_depth = int(cfg["buildDirDepth"])
    found = []
    stack = [(worktree, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.name == ".git" or e.is_symlink():
                continue
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            p = Path(e.path)
            if e.name in names:
                if ignored_untracked(worktree, p):
                    found.append(p)
                continue  # never look inside a build directory, ignored or not
            if depth + 1 < max_depth and not exists(p / ".git"):
                stack.append((p, depth + 1))
    return sorted(found)


def ignored_untracked(worktree: Path, d: Path) -> bool:
    rel = os.path.relpath(str(d), str(worktree))
    if git(worktree, "check-ignore", "-q", rel + "/")[0] != 0:
        return False
    rc, out, _ = git(worktree, "ls-files", "--", rel)
    return rc == 0 and not out.strip()


def _ok(res: tuple) -> tuple:
    rc, out, err = res
    return rc == 0, (err or out).strip().splitlines()[-1] if (err or out).strip() else ""


PERSONAL_DIRS = {"Desktop", "Documents", "Downloads", "Music", "Pictures", "Movies", "Library", "Applications",
                 "Public", "Sites", "Mobile Documents", "CloudStorage", "Photos Library.photoslibrary"}


def all_homes() -> set:
    homes = {os.path.realpath(str(Path.home()))}
    if pwd:
        for u in pwd.getpwall():
            if u.pw_dir and u.pw_dir not in ("/", "/var/empty", "/nonexistent") and u.pw_dir.startswith(("/Users/",
                                                                                                          "/home/")):
                homes.add(os.path.realpath(u.pw_dir))
    return homes


def refuse_delete(p: Path) -> str | None:
    """The last guard before any folder is deleted, whatever rule chose it: None when it is build output inside a
    git checkout or part of a known tool cache of this account, else why not. Never a home folder, anything directly
    in one (~/Music, ~/Documents, ~/work), or anything in a personal folder outside a known cache."""
    real = Path(os.path.realpath(str(p)))
    homes = all_homes()
    if str(real) in homes or str(real.parent) in homes:
        return "a home folder or a folder directly in one"
    if nested_git(real):
        # A clone or worktree under an ignored folder is invisible to the outer checkout's git (ls-files lists
        # nothing in it), so its unpushed commits would go with the folder.
        return "holds a nested git repository"
    for roots in cache_dirs(Path.home()).values():
        for root in roots:
            if within(real, root) and real != Path(os.path.realpath(str(root))):
                return None
    if any(part in PERSONAL_DIRS for part in real.parts):
        return "inside a personal folder"
    if real.name not in set(load_config().get("buildDirs", [])):
        return "not a build-output folder name"
    for parent in real.parents:
        if str(parent) in homes or parent == parent.parent:
            break
        if path_state(parent / ".git") == "present":
            return None
    return "not inside a git checkout"


def _rmtree_if_idle(p: Path, gitdir: Path | None, activity: float) -> tuple:
    """Build output goes only if its checkout saw no git activity since the plan was made."""
    if last_activity(gitdir) != activity:
        return False, "kept: its checkout was used since the plan was made"
    return _rmtree(p)


def _rmtree(p: Path) -> tuple:
    if p.is_symlink() or not p.is_dir():
        return False, "not a directory"
    why = refuse_delete(p)
    if why:
        return False, f"refused: {why}"
    errors = []
    shutil.rmtree(str(p), onerror=lambda fn, path, exc: errors.append(path))
    return (not exists(p)), (f"{len(errors)} entries could not be removed" if errors else "")


# ----------------------------------------------------------------------------- caches (running account only)

def cache_dirs(home: Path) -> dict:
    mac = sys.platform == "darwin"
    lib = home / "Library" / "Caches"
    xdg = home / ".cache"
    return {
        "npm": [home / ".npm" / "_cacache"],
        "pnpm": [home / "Library" / "pnpm" / "store", home / ".local" / "share" / "pnpm" / "store"],
        "yarn": [lib / "Yarn"] if mac else [xdg / "yarn"],
        "pip": [lib / "pip"] if mac else [xdg / "pip"],
        "playwright": [lib / "ms-playwright"] if mac else [xdg / "ms-playwright"],
        "brew": [lib / "Homebrew"] if mac else [xdg / "Homebrew"],
    }


REV_RE = re.compile(r"^(.+)-(\d+)$")


def stale_playwright(dirs: list, keep: int) -> list:
    """Browser revisions beyond the newest `keep` of each kind (chromium-1140, firefox-1466, ...)."""
    kinds: dict = {}
    for d in dirs:
        m = REV_RE.match(d.name)
        if m:
            kinds.setdefault(m.group(1), []).append((int(m.group(2)), d))
    stale = []
    for revs in kinds.values():
        revs.sort(reverse=True)
        stale.extend(d for _, d in revs[keep:])
    return sorted(stale)


RECLAIMED_RE = re.compile(r"(?:reclaimed space|freed approximately)[:\s]+([\d.]+)\s*([KMGT]?i?B)", re.I)


def parse_reclaimed(text: str) -> int:
    total = 0.0
    scale = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "KIB": 1024, "MIB": MB, "GIB": GB, "TIB": GB * 1024}
    for num, unit in RECLAIMED_RE.findall(text):
        total += float(num) * scale.get(unit.upper(), 1)
    return int(total)


def plan_caches(cfg: dict, home: dict, me: str, plan: Plan) -> None:
    minimum = int(cfg["cacheMinMB"]) * MB
    on = cfg["caches"]
    dirs = cache_dirs(home["path"])
    mine = home["name"] == me
    for kind, paths in dirs.items():
        if not on.get(kind):
            continue
        size = sum(du_bytes(p) for p in paths if exists(p))
        if kind == "playwright":
            stale = []
            for p in paths:
                try:
                    stale += stale_playwright([x for x in p.iterdir() if x.is_dir()], int(cfg["playwrightKeep"]))
                except OSError:
                    pass
            size = sum(du_bytes(d) for d in stale)
            if size and mine:
                for d in stale:
                    plan.add("cache", str(d), du_bytes(d), "older Playwright browser revision",
                             lambda p=d: _rmtree(p), home["name"])
            elif size:
                plan.need(home["name"], f"old Playwright browsers {human(size)}")
            continue
        if size < minimum:
            continue
        if not mine:
            plan.need(home["name"], f"{kind} cache {human(size)}")
            continue
        cmd = cache_command(kind)
        if cmd:
            plan.add("cache", f"{kind} cache", size, f"at least {cfg['cacheMinMB']} MB",
                     lambda c=cmd: _ok(run(c, timeout=1800)), home["name"])
    if on.get("docker") and mine and have("docker") and run(["docker", "info"], timeout=60)[0] == 0:
        until = f"until={int(cfg['dockerBuilderUntilHours'])}h"
        plan.add("cache", "docker build cache and dangling images", 0, f"builder cache {until}, dangling images",
                 lambda u=until: _docker(u), home["name"])


def cache_command(kind: str) -> list | None:
    if kind == "npm" and have("npm"):
        return ["npm", "cache", "clean", "--force"]
    if kind == "pnpm" and have("pnpm"):
        return ["pnpm", "store", "prune"]
    if kind == "yarn" and have("yarn"):
        rc, out, _ = run(["yarn", "--version"], timeout=60)
        return ["yarn", "cache", "clean"] if out.startswith("1.") else None
    if kind == "pip":
        for pip in ("pip3", "pip"):
            if have(pip):
                return [pip, "cache", "purge"]
    if kind == "brew" and have("brew"):
        return ["brew", "cleanup", "--prune=30"]
    return None


def _docker(until: str) -> tuple:
    rc1, out1, err1 = run(["docker", "builder", "prune", "-f", "--filter", until], timeout=1800)
    rc2, out2, err2 = run(["docker", "image", "prune", "-f"], timeout=1800)
    freed = parse_reclaimed(out1 + out2)
    return rc1 == 0 and rc2 == 0, f"reclaimed {human(freed)}" if freed else (err1 or err2).strip()


# ----------------------------------------------------------------------------- remote

def _delete_remote_branch(gh: GitHub, slug: str, name: str, ref: str, kept: set) -> tuple:
    """Minutes pass between planning and deleting (every checkout is sized first): re-read the open pull requests
    and keep the branch if one now uses it. Deleting a stacked PR's base closes that PR on GitHub."""
    try:
        using = gh.open_using(slug, name)
    except RuntimeError as e:
        kept.add(name)
        return False, f"kept: open pull requests could not be re-checked ({e})"
    if using:
        kept.add(name)
        return False, f"kept: now used by open PR #{using[0]}"
    ok, msg = gh.delete(f"repos/{slug}/git/refs/heads/{ref}")
    if not ok:
        kept.add(name)
    return ok, msg


def _delete_branch_cache(gh: GitHub, slug: str, cache_id, branch: str | None, kept: set) -> tuple:
    """A cache planned because its branch is being deleted goes only when that delete happened (they run first)."""
    if branch and branch in kept:
        return False, "kept: its branch was kept"
    return gh.delete(f"repos/{slug}/actions/caches/{cache_id}")


def plan_remote(slug: str, cfg: dict, gh: GitHub, plan: Plan, now: dt.datetime) -> None:
    rc = cfg["remote"]
    summary = {"repo": slug, "branches": 0, "caches": 0, "cacheBytes": 0, "artifacts": 0, "artifactBytes": 0,
               "noPr": 0, "skipped": ""}
    plan.remote.append(summary)
    try:
        info = gh.info(slug)
    except RuntimeError as e:
        summary["skipped"] = f"unreadable ({e})"
        return
    if info.get("archived"):
        summary["skipped"] = "archived"
        return
    if not info.get("push"):
        summary["skipped"] = "no push access"
        return
    default = info.get("default_branch")
    try:
        prs = [p for p in gh.pulls(slug) if p.get("head_repo") in (None, slug)]
        all_prs = gh.pulls(slug)
        branches = gh.branches(slug)
    except RuntimeError as e:
        summary["skipped"] = f"unreadable ({e})"
        return
    open_heads = {p["head_ref"] for p in prs if p["state"] == "open"}
    open_bases = {p["base_ref"] for p in all_prs if p["state"] == "open"}
    latest: dict = {}
    for p in prs:
        if p["head_ref"] not in latest or p["number"] > latest[p["head_ref"]]["number"]:
            latest[p["head_ref"]] = p
    remaining = {b["name"] for b in branches}
    planned_deletes = set()   # branches this plan deletes: their caches go with them
    kept_branches: set = set()  # filled at execute time by a branch delete that ended up kept

    if rc.get("branches"):
        for b in branches:
            name = b["name"]
            if b.get("protected") or protected(name, cfg, default):
                continue
            if name in open_bases:
                plan.keep("remote-branch", f"{slug}:{name}", "base of an open PR")
                continue
            if name in open_heads:
                continue
            pr = latest.get(name)
            if not pr:
                summary["noPr"] += 1
                continue
            closed = parse_ts(pr.get("closed_at"))
            grace = rc["mergedGraceDays"] if pr.get("merged_at") else rc["closedGraceDays"]
            if not closed or (now - closed).days < int(grace):
                continue
            if b["sha"] != pr["head_sha"]:
                plan.keep("remote-branch", f"{slug}:{name}", f"pushed to after PR #{pr['number']} closed")
                continue
            state = "merged" if pr.get("merged_at") else "closed unmerged"
            ref = urllib.parse.quote(name, safe="/")
            summary["branches"] += 1
            remaining.discard(name)
            planned_deletes.add(name)
            plan.add("remote-branch", f"{slug}:{name}", 0, f"PR #{pr['number']} {state} {closed:%Y-%m-%d}",
                     lambda s=slug, n=name, r=ref: _delete_remote_branch(gh, s, n, r, kept_branches))

    if rc.get("actionsCaches"):
        state_of = {p["number"]: p["state"] for p in all_prs}
        try:
            caches = gh.caches(slug)
        except RuntimeError as e:
            caches = []
            plan.notes.append(f"{slug}: Actions caches unreadable ({e})")
        for c in caches:
            ref = c.get("ref") or ""
            m = re.match(r"refs/pull/(\d+)/", ref)
            why = None
            if m and state_of.get(int(m.group(1))) == "closed":
                why = f"PR #{m.group(1)} closed"
            elif ref.startswith("refs/heads/") and ref[11:] not in remaining:
                why = "branch deleted"
            if why:
                summary["caches"] += 1
                summary["cacheBytes"] += int(c.get("size_in_bytes") or 0)
                branch = ref[11:] if ref.startswith("refs/heads/") and ref[11:] in planned_deletes else None
                plan.add("actions-cache", f"{slug}:{c['id']} {ref}", int(c.get("size_in_bytes") or 0), why,
                         lambda s=slug, i=c["id"], b=branch: _delete_branch_cache(gh, s, i, b, kept_branches))

    if rc.get("artifacts"):
        try:
            artifacts = gh.artifacts(slug)
        except RuntimeError as e:
            artifacts = []
            plan.notes.append(f"{slug}: Actions artifacts unreadable ({e})")
        for a in artifacts:
            created = parse_ts(a.get("created_at"))
            if a.get("expired") or not created or (now - created).days < int(rc["artifactDays"]):
                continue
            summary["artifacts"] += 1
            summary["artifactBytes"] += int(a.get("size_in_bytes") or 0)
            plan.add("artifact", f"{slug}:{a['id']} {a.get('name', '')}", int(a.get("size_in_bytes") or 0),
                     f"older than {rc['artifactDays']} days",
                     lambda s=slug, i=a["id"]: gh.delete(f"repos/{s}/actions/artifacts/{i}"))


# ----------------------------------------------------------------------------- run

def where(home: Path, top: int = 12) -> list:
    if not have("du") or not home.is_dir():
        return []
    try:
        entries = sorted(home.iterdir())
    except OSError:
        return []
    rows = []
    for i, p in enumerate(entries, 1):
        if p.is_symlink():
            continue
        step(f"largest folders of {home}: [{i}/{len(entries)}] sizing {p.name}")
        rows.append((du_bytes(p), str(p)))
    return sorted(rows, reverse=True)[:top]


def execute(plan: Plan) -> None:
    for i, a in enumerate(plan.actions, 1):
        step(f"[{i}/{len(plan.actions)}] {KIND_TITLES.get(a['kind'], a['kind']).lower()}: {a['target']}")
        try:
            ok, msg = a["_run"]()
        except Exception as e:  # one failure never stops the rest
            ok, msg = False, f"{type(e).__name__}: {e}"
        a["status"] = "done" if ok else ("kept" if msg.startswith("kept:") else "failed")
        if msg:
            a["detail"] = msg


def build_plan(args: argparse.Namespace, cfg: dict, gh: GitHub | None, extra_slugs: tuple = ()) -> tuple:
    plan = Plan()
    me = current_user()
    only = [h.strip() for h in args.home.split(",")] if args.home else None
    homes, notes = resolve_homes(cfg, only)
    plan.notes.extend(notes)
    cwds = process_cwds()
    now = time.time()
    repos, slugs, seen_repos = [], [], set()

    def add_repo(p: Path, home: str) -> None:
        key = os.path.realpath(str(p))
        if key not in seen_repos:
            seen_repos.add(key)
            repos.append((Repo(p), home or owner_of(p)))

    roots = [(h["name"], h["path"]) for h in homes]
    roots += [("", Path(os.path.expanduser(r))) for r in cfg.get("roots", [])]
    if not only or me in only:  # Claude's temp folders of the running account: where stray worktrees end up
        uid = os.getuid() if hasattr(os, "getuid") else 0
        roots += [(me, Path(r.replace("{uid}", str(uid)))) for r in cfg.get("tempRoots", [])]
    walked, worktree_dirs = set(), []
    for name, root in roots:
        if not exists(root) or not root.is_dir() or os.path.realpath(str(root)) in walked:
            continue
        walked.add(os.path.realpath(str(root)))
        if not os.access(str(root), os.R_OK | os.X_OK):
            plan.need(name or owner_of(root), f"{root} is not readable by {me}")
            step(f"{root}: not readable by {me}, left for that account")
            continue
        step(f"looking for git checkouts in {root}")
        found, unreadable, wts = find_repos(root, cfg)
        step(f"{root}: {len(found)} checkouts, {len(wts)} worktree folders")
        if unreadable:
            plan.notes.append(f"{root}: {len(unreadable)} folders not readable by {me} were skipped")
        for p in found:
            add_repo(p, name)
        worktree_dirs += wts
    # A worktree folder leads to its main checkout wherever that lives; one git no longer knows is an orphan.
    for w in worktree_dirs:
        main, g = main_repo_of(w)
        if main is not None:
            add_repo(main, owner_of(main))
        elif g is not None and path_state(g) == "missing":
            try:
                modified = dt.datetime.fromtimestamp(os.lstat(str(w)).st_mtime).strftime("%Y-%m-%d")
            except OSError:
                modified = ""
            plan.loose.append({"path": str(w), "bytes": du_bytes(w), "modified": modified, "gitdir": str(g),
                               "owner": owner_of(w)})
        elif g is not None and path_state(g) == "unknown":
            plan.need(owner_near(g), f"worktree {w} (its checkout is not visible to {me})")
    owners = {o.lower() for o in cfg["remoteOwners"]}
    for r, _ in repos:  # GitHub names are case-insensitive: one pass per repository however a remote spells it
        if r.slug and r.slug.split("/")[0].lower() in owners and r.slug.lower() not in {s.lower() for s in slugs}:
            slugs.append(r.slug)
    for slug in extra_slugs:  # repositories the other accounts' runs found (--all-accounts)
        if slug.split("/")[0].lower() in owners and slug.lower() not in {s.lower() for s in slugs}:
            slugs.append(slug)
    plan.slugs = slugs

    if not args.local_only:
        if gh and gh.ok:
            for i, slug in enumerate(slugs, 1):
                step(f"GitHub [{i}/{len(slugs)}] {slug}: branches, pull requests, Actions caches and artifacts")
                plan_remote(slug, cfg, gh, plan, now_utc())
        else:
            plan.notes.append("remote cleanup skipped: gh is not installed or not logged in (gh auth login)")
    if not args.remote_only:
        if gh is not None and not gh.ok:
            plan.notes.append("merged-PR detection off (no gh): squash-merged branches are kept")
        for i, (r, home) in enumerate(repos, 1):
            step(f"checkout [{i}/{len(repos)}] {r.path}")
            planned = len(plan.actions)
            try:
                plan_repo(r, cfg, gh, plan, cwds, now, home)
            except Exception as e:  # one unreadable checkout never stops the run; nothing of it is planned
                del plan.actions[planned:]
                plan.keep("checkout", str(r.path), f"skipped after an error: {type(e).__name__}: {e}")
                continue
            if r.fetch_error:
                plan.notes.append(f"{r.path}: fetch failed ({r.fetch_error}); pushed-only worktrees kept")
        if not args.no_caches:
            for h in homes:
                if os.access(str(h["path"]), os.R_OK | os.X_OK):
                    step(f"sizing the caches of {h['name']}")
                    plan_caches(cfg, h, me, plan)
    return plan, homes, [r for r, _ in repos]


def summarize(plan: Plan, mode: str, homes: list, repos: list, before: tuple, after: tuple, args) -> str:
    me = current_user()
    host = socket.gethostname().split(".")[0]
    title = "test run, nothing changed" if mode == "scan" else "clean"
    lines = [f"## Storage cleanup {dt.date.today().isoformat()} on {host} as {me} ({title})", ""]
    free0, total = before
    local = sum(a["bytes"] for a in plan.actions if a["kind"] in ("worktree", "build", "git", "cache"))
    remote = sum(a["bytes"] for a in plan.actions if a["kind"] in ("actions-cache", "artifact"))
    if mode == "clean":
        lines.append(f"Free disk: {human(free0)} -> {human(after[0])} of {human(total)} "
                     f"(freed {human(max(0, after[0] - free0))}).")
    else:
        lines.append(f"**Would free {human(local)} on this disk** (free now {human(free0)} of {human(total)}, "
                     f"{human(free0 + local)} after), and {human(remote)} of GitHub Actions storage. "
                     "Run `clean` to do it.")
    lines.append(f"Homes: {', '.join(h['name'] for h in homes) or 'none'}; {len(repos)} checkouts.")
    lines.append("")
    kinds = list(KIND_TITLES)
    titles = KIND_TITLES
    rows = []
    for k in kinds:
        items = [a for a in plan.actions if a["kind"] == k and not (k == "git" and a["bytes"] == 0)]
        if not items:
            continue
        size = sum(a["bytes"] for a in items)
        row = f"| {titles[k]} | {len(items)} | {human(size) if size else '-'} |"
        if mode == "clean":
            row += f" {sum(1 for a in items if a['status'] == 'failed')} |"
        rows.append(row)
    if rows:
        lines += ["| What | Items | Size |" + (" Failed |" if mode == "clean" else ""),
                  "|---|---:|---:|" + ("---:|" if mode == "clean" else "")] + rows
    else:
        lines.append("Nothing to remove: every checkout is active, unmerged, another account's, or already clean.")
    lines.append("")
    if mode == "scan":
        lines += ["Caches cleaned by their own tool (npm, pip, brew, docker) are sized from their folders; docker "
                  "reports its saving only when it runs.", ""]
    big = sorted((a for a in plan.actions if a["kind"] in ("worktree", "build", "cache")),
                 key=lambda a: -a["bytes"])[:15]
    if big:
        lines.append("Largest items:")
        lines += [f"- {human(a['bytes'])}  {a['target']}  ({a['why']})" + (
            f" -- {a['status']}" if mode == "clean" else "") for a in big]
        lines.append("")
    moves = [a for a in plan.actions if a["kind"] == "move"]
    if moves:
        lines.append("Worktrees with kept work moved into their checkout:")
        lines += [f"- {a['target']}" + (f" -- {a['status']}" if mode == "clean" else "") for a in moves[:20]]
        lines.append("")
    if plan.loose:
        lines.append("Loose worktree folders git no longer knows (left alone; check them and delete by hand):")
        lines += [f"- {human(x['bytes'])}  {x['path']}  (changed {x['modified']})" for x in plan.loose[:20]]
        lines.append("")
    failed = [a for a in plan.actions if a["status"] == "failed"]
    if failed:
        lines.append("Failed:")
        lines += [f"- {a['kind']} {a['target']}: {a.get('detail', '')}" for a in failed[:20]]
        lines.append("")
    if plan.remote:
        lines.append("Remote:")
        for r in plan.remote:
            if r["skipped"]:
                lines.append(f"- {r['repo']}: skipped, {r['skipped']}")
            else:
                lines.append(f"- {r['repo']}: {r['branches']} branches, {r['caches']} caches "
                             f"({human(r['cacheBytes'])}), {r['artifacts']} artifacts ({human(r['artifactBytes'])})"
                             + (f"; {r['noPr']} branches without a PR left alone" if r["noPr"] else ""))
        lines.append("")
    if plan.needs:
        lines.append("Needs another account (run this on that account):")
        for acct, items in sorted(plan.needs.items()):
            lines.append(f"- {acct}: " + "; ".join(items[:6]) + (f"; and {len(items) - 6} more" if len(items) > 6
                                                                   else ""))
        lines.append("")
    if plan.kept:
        reasons: dict = {}
        for k in plan.kept:
            reasons.setdefault(f"{k['kind']}: {k['why']}", []).append(k["target"])
        lines.append("Kept:")
        for why, targets in sorted(reasons.items()):
            lines.append(f"- {len(targets)} x {why}" + (f" ({targets[0]})" if len(targets) == 1 else ""))
        lines.append("")
    for n in plan.notes:
        lines.append(f"Note: {n}")
    if getattr(args, "where_rows", None):
        lines += ["", "Largest folders per home:"]
        for name, rows in args.where_rows:
            lines.append(f"- {name}: " + ", ".join(f"{os.path.basename(p)} {human(b)}" for b, p in rows))
    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------------- HTML report (published as an artifact)

HTML_HEAD = """<title>Storage cleanup</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;600;750&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* Layout: one column; a disk bar first (used | freed by this run | free), then the detail sections. */
:root{--bg:oklch(0.975 0.006 195);--surface:oklch(1 0 0);--fg:oklch(0.24 0.02 220);--muted:oklch(0.5 0.02 215);
--line:oklch(0.9 0.01 205);--used:oklch(0.62 0.025 220);--gain:oklch(0.58 0.12 165);--gain-soft:oklch(0.93 0.05 165);
--free:oklch(0.9 0.012 200);--warn:oklch(0.62 0.13 70);--warn-soft:oklch(0.95 0.05 80);--bad:oklch(0.55 0.17 25);
--sans:"Public Sans",system-ui,-apple-system,"Segoe UI",sans-serif;--mono:"JetBrains Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:oklch(0.19 0.012 220);--surface:oklch(0.235 0.014 220);
--fg:oklch(0.93 0.01 200);--muted:oklch(0.7 0.015 210);--line:oklch(0.32 0.015 220);--used:oklch(0.5 0.02 220);
--gain:oklch(0.74 0.13 165);--gain-soft:oklch(0.32 0.06 165);--free:oklch(0.34 0.012 210);--warn:oklch(0.8 0.12 80);
--warn-soft:oklch(0.33 0.05 75);--bad:oklch(0.72 0.14 25);color-scheme:dark}}
:root[data-theme="dark"]{--bg:oklch(0.19 0.012 220);--surface:oklch(0.235 0.014 220);--fg:oklch(0.93 0.01 200);
--muted:oklch(0.7 0.015 210);--line:oklch(0.32 0.015 220);--used:oklch(0.5 0.02 220);--gain:oklch(0.74 0.13 165);
--gain-soft:oklch(0.32 0.06 165);--free:oklch(0.34 0.012 210);--warn:oklch(0.8 0.12 80);--warn-soft:oklch(0.33 0.05 75);
--bad:oklch(0.72 0.14 25);color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 var(--sans);padding-inline:16px;padding-block:28px 56px}
main{max-width:1040px;margin:0 auto;display:grid;grid-template-columns:minmax(0,1fr);gap:28px}
main>*{min-width:0}
header{display:grid;gap:6px}
h1{font-size:28px;font-weight:750;letter-spacing:-.01em;margin:0;text-wrap:balance}
h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:0 0 10px}
.meta{color:var(--muted);margin:0}
.chip{justify-self:start;display:inline-flex;align-items:center;gap:8px;padding:3px 12px;border-radius:999px;
font-size:13px;font-weight:600;background:var(--free);color:var(--fg)}
.chip.gain{background:var(--gain-soft);color:var(--gain)}.chip.warn{background:var(--warn-soft);color:var(--warn)}
.chip i{width:8px;height:8px;border-radius:50%;background:currentColor}
.chip.live i{animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{50%{opacity:.25}}
@media (prefers-reduced-motion: reduce){.chip.live i{animation:none}}
.disk{display:grid;gap:12px}
.headline{font-size:22px;font-weight:600;margin:0;text-wrap:balance}
.headline b{color:var(--gain);font-weight:750}
.bar{display:flex;height:22px;border-radius:6px;overflow:hidden;background:var(--free)}
.bar span{display:block;height:100%}.bar .u{background:var(--used)}.bar .g{background:var(--gain)}
.legend{display:flex;flex-wrap:wrap;gap:8px 22px;font-size:14px;color:var(--muted);margin:0;padding:0;list-style:none}
.legend li{display:flex;align-items:center;gap:8px}.legend b{color:var(--fg);font-variant-numeric:tabular-nums;font-weight:600}
.sw{width:12px;height:12px;border-radius:3px;display:inline-block}.sw.u{background:var(--used)}.sw.g{background:var(--gain)}
.sw.f{background:var(--free);outline:1px solid var(--line)}
.scroll{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:9px 14px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
th{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
code,.path{font-family:var(--mono);font-size:13px}
.path{overflow-wrap:anywhere;min-width:0}
.why{color:var(--muted);font-size:13px}
.st{font-size:12px;font-weight:600;white-space:nowrap}.st.done{color:var(--gain)}.st.failed{color:var(--bad)}
.st.planned{color:var(--muted)}
.callout{background:var(--warn-soft);border-radius:8px;padding:14px 16px;display:grid;gap:6px}
.callout h2{color:var(--warn);margin:0}
ul.plain{margin:0;padding-left:18px;display:grid;gap:4px}
.empty{color:var(--muted);margin:0}
footer{color:var(--muted);font-size:13px;display:grid;gap:4px}
@media (max-width:520px){h1{font-size:24px}.headline{font-size:19px}th,td{padding:8px 10px}}
</style>
"""

KIND_TITLES = {"worktree": "Worktrees", "move": "Worktrees moved into their checkout", "branch": "Local branches",
               "build": "Build output", "git": "Git upkeep", "cache": "Caches", "remote-branch": "Remote branches",
               "actions-cache": "Actions caches", "artifact": "Actions artifacts"}
LOCAL_KINDS = ("worktree", "build", "git", "cache")


def _e(s) -> str:
    import html as _html
    return _html.escape(str(s), quote=True)


def render_html(report: dict, runs: list | None = None) -> str:
    mode, pending = report["mode"], report.get("pending", False)
    acts = report["actions"]
    total = report.get("total") or 0
    before, after = report.get("freeBefore") or 0, report.get("freeAfter") or 0
    local = sum(a["bytes"] for a in acts if a["kind"] in LOCAL_KINDS)
    remote = sum(a["bytes"] for a in acts if a["kind"] in ("actions-cache", "artifact"))
    failed = [a for a in acts if a["status"] == "failed"]
    if mode == "clean":
        gained = max(0, after - before)
        chip = ('<span class="chip warn"><i></i>Cleaned, {} failed</span>'.format(len(failed)) if failed
                else '<span class="chip gain"><i></i>Cleaned</span>')
        headline = f"Freed <b>{human(gained)}</b> on this disk; {human(after)} free now."
        free_end = after
    else:
        gained = local
        chip = ('<span class="chip live"><i></i>Cleaning now. This page updates when it finishes</span>' if pending
                else '<span class="chip"><i></i>Test run. Nothing was deleted or moved</span>')
        headline = (f"{'About to free' if pending else 'Would free'} <b>{human(local)}</b> on this disk; "
                    f"{human(before + local)} free after.")
        free_end = before + local
    pct = (lambda b: f"{max(0.0, min(100.0, 100.0 * b / total)):.2f}%") if total else (lambda b: "0%")
    used_end = max(0, total - free_end)
    p = [HTML_HEAD, "<main><header>",
         f"<h1>Storage cleanup</h1>{chip}",
         f"<p class=\"meta\">{_e(report.get('host', ''))} as {_e(report.get('user', ''))} · {_e(report['at'])} · "
         f"{report.get('checkouts', 0)} checkouts in {_e(', '.join(report.get('homes', [])) or 'no home folders')}</p>",
         "</header>",
         "<section class=\"disk\" aria-label=\"Disk\">",
         f"<p class=\"headline\">{headline}</p>",
         f"<div class=\"bar\" role=\"img\" aria-label=\"{_e(human(used_end))} used, {_e(human(gained))} "
         f"{'freed' if mode == 'clean' else 'reclaimable'}, {_e(human(min(before, after) if mode == 'clean' else before))} free\">"
         f"<span class=\"u\" style=\"width:{pct(used_end)}\"></span><span class=\"g\" style=\"width:{pct(gained)}\"></span></div>",
         "<ul class=\"legend\">",
         f"<li><span class=\"sw u\"></span>Used <b>{human(used_end)}</b></li>",
         f"<li><span class=\"sw g\"></span>{'Freed' if mode == 'clean' else 'Reclaimable'} <b>{human(gained)}</b></li>",
         f"<li><span class=\"sw f\"></span>Free before <b>{human(before)}</b></li>",
         f"<li>Disk <b>{human(total)}</b></li>",
         f"<li>GitHub Actions storage {'removed' if mode == 'clean' else 'reclaimable'} <b>{human(remote)}</b></li>",
         "</ul></section>"]

    rows = []
    for k, title in KIND_TITLES.items():
        items = [a for a in acts if a["kind"] == k and not (k == "git" and a["bytes"] == 0)]
        if items:
            size = sum(a["bytes"] for a in items)
            bad = sum(1 for a in items if a["status"] == "failed")
            rows.append(f"<tr><td>{title}</td><td class=\"n\">{len(items)}</td>"
                        f"<td class=\"n\">{human(size) if size else '-'}</td>"
                        + (f"<td class=\"n\">{bad or ''}</td>" if mode == "clean" else "") + "</tr>")
    p.append("<section><h2>By category</h2>")
    if rows:
        p.append("<div class=\"scroll\"><table><thead><tr><th>What</th><th class=\"n\">Items</th><th class=\"n\">Size</th>"
                 + ("<th class=\"n\">Failed</th>" if mode == "clean" else "") + "</tr></thead><tbody>"
                 + "".join(rows) + "</tbody></table></div>")
    else:
        p.append("<p class=\"empty\">Nothing to remove: every checkout is active, unmerged or already clean.</p>")
    p.append("</section>")

    big = sorted((a for a in acts if a["kind"] in ("worktree", "build", "cache") and a["bytes"]),
                 key=lambda a: -a["bytes"])[:25]
    if big:
        p.append("<section><h2>Largest items</h2><div class=\"scroll\"><table><thead><tr><th class=\"n\">Size</th>"
                 "<th>Item</th><th>Why</th><th>State</th></tr></thead><tbody>")
        for a in big:
            state = a["status"] if mode == "clean" else ("next" if pending else "would go")
            p.append(f"<tr><td class=\"n\">{human(a['bytes'])}</td><td class=\"path\">{_e(a['target'])}</td>"
                     f"<td class=\"why\">{_e(KIND_TITLES[a['kind']])}: {_e(a['why'])}</td>"
                     f"<td><span class=\"st {_e(a['status'])}\">{_e(state)}</span></td></tr>")
        p.append("</tbody></table></div></section>")

    if failed:
        p.append("<section><h2>Failed</h2><div class=\"scroll\"><table><tbody>")
        for a in failed[:40]:
            p.append(f"<tr><td class=\"path\">{_e(a['target'])}</td><td class=\"why\">{_e(a.get('detail', ''))}</td></tr>")
        p.append("</tbody></table></div></section>")

    moves = [a for a in acts if a["kind"] == "move"]
    if moves:
        p.append("<section><h2>Moved into their checkout</h2><p class=\"meta\">Worktrees with work that has to stay "
                 "(uncommitted or unmerged), moved out of temp and scratch folders with <code>git worktree move</code>."
                 "</p><div class=\"scroll\"><table><thead><tr><th>From</th><th>To</th><th>State</th></tr></thead><tbody>")
        for a in moves:
            src, _, dest = a["target"].partition(" -> ")
            state = a["status"] if mode == "clean" else ("next" if pending else "would move")
            p.append(f"<tr><td class=\"path\">{_e(src)}</td><td class=\"path\">{_e(dest)}</td>"
                     f"<td><span class=\"st {_e(a['status'])}\">{_e(state)}</span></td></tr>")
        p.append("</tbody></table></div></section>")

    if report.get("loose"):
        p.append("<section class=\"callout\"><h2>Loose worktree folders</h2><p class=\"meta\">Git no longer knows "
                 "these folders, so the script cannot tell whether they hold unsaved work. They are left alone: check "
                 "each one and delete it by hand when it is no longer needed.</p><div class=\"scroll\"><table><thead>"
                 "<tr><th class=\"n\">Size</th><th>Folder</th><th>Changed</th></tr></thead><tbody>")
        for x in report["loose"]:
            p.append(f"<tr><td class=\"n\">{human(x['bytes'])}</td><td class=\"path\">{_e(x['path'])}</td>"
                     f"<td>{_e(x['modified'])}</td></tr>")
        p.append("</tbody></table></div></section>")

    if report.get("remote"):
        p.append("<section><h2>GitHub</h2><div class=\"scroll\"><table><thead><tr><th>Repository</th>"
                 "<th class=\"n\">Branches</th><th class=\"n\">Actions caches</th><th class=\"n\">Artifacts</th>"
                 "<th>Note</th></tr></thead><tbody>")
        for r in report["remote"]:
            if r["skipped"]:
                p.append(f"<tr><td><code>{_e(r['repo'])}</code></td><td class=\"n\">-</td><td class=\"n\">-</td>"
                         f"<td class=\"n\">-</td><td class=\"why\">Skipped: {_e(r['skipped'])}</td></tr>")
            else:
                note = f"{r['noPr']} branches without a PR left alone" if r["noPr"] else ""
                p.append(f"<tr><td><code>{_e(r['repo'])}</code></td><td class=\"n\">{r['branches']}</td>"
                         f"<td class=\"n\">{r['caches']} · {human(r['cacheBytes'])}</td>"
                         f"<td class=\"n\">{r['artifacts']} · {human(r['artifactBytes'])}</td>"
                         f"<td class=\"why\">{_e(note)}</td></tr>")
        p.append("</tbody></table></div></section>")

    if report.get("needs"):
        p.append("<section class=\"callout\"><h2>Needs another account</h2><p class=\"meta\">This account cannot "
                 "change these. The same run, signed in as that account, cleans them.</p><ul class=\"plain\">")
        for acct, items in sorted(report["needs"].items()):
            more = f"; and {len(items) - 8} more" if len(items) > 8 else ""
            p.append(f"<li><b>{_e(acct)}</b>: {_e('; '.join(items[:8]) + more)}</li>")
        p.append("</ul></section>")

    if report.get("kept"):
        reasons: dict = {}
        for k in report["kept"]:
            reasons.setdefault((k["kind"], k["why"]), []).append(k["target"])
        p.append("<section><h2>Kept, and why</h2><div class=\"scroll\"><table><thead><tr><th class=\"n\">Count</th>"
                 "<th>What</th><th>Reason</th><th>Example</th></tr></thead><tbody>")
        for (kind, why), targets in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            p.append(f"<tr><td class=\"n\">{len(targets)}</td><td>{_e(KIND_TITLES.get(kind, kind))}</td>"
                     f"<td>{_e(why)}</td><td class=\"path\">{_e(targets[0])}</td></tr>")
        p.append("</tbody></table></div></section>")

    if report.get("notes"):
        p.append("<section><h2>Notes</h2><ul class=\"plain\">"
                 + "".join(f"<li>{_e(n)}</li>" for n in report["notes"]) + "</ul></section>")

    if runs:
        p.append("<section><h2>Recent runs</h2><div class=\"scroll\"><table><thead><tr><th>When</th><th>Mode</th>"
                 "<th>Account</th><th class=\"n\">Free before</th><th class=\"n\">Free after</th>"
                 "<th class=\"n\">Failed</th></tr></thead><tbody>")
        for r in reversed(runs[-10:]):
            p.append(f"<tr><td>{_e(r.get('at', ''))}</td><td>{'test' if r.get('mode') == 'scan' else _e(r.get('mode'))}</td>"
                     f"<td>{_e(r.get('user', ''))}</td><td class=\"n\">{human(r.get('freeBefore') or 0)}</td>"
                     f"<td class=\"n\">{human(r.get('freeAfter') or 0)}</td><td class=\"n\">{r.get('failed') or ''}</td></tr>")
        p.append("</tbody></table></div></section>")

    p.append(f"<footer><span>Rules: merged or provably saved work only; uncommitted files, unpushed commits, open "
             f"pull requests and anything in use are never touched.</span><span>Full report: "
             f"<code>{_e(report.get('reportPath', ''))}</code></span></footer></main>")
    return "".join(p)


def read_runs() -> list:
    try:
        with open(HOME / "runs.jsonl", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []


def run_mode(args: argparse.Namespace) -> str:
    """`test`, `scan` and `clean --dry-run` all plan without executing."""
    return "clean" if args.cmd == "clean" and not getattr(args, "dry_run", False) else "scan"


# ----------------------------------------------------------------------------- --all-accounts (sudo -u)

def account_command(acct: str, mode: str, args: argparse.Namespace, script: str) -> list:
    """The same run as `acct` through `sudo -u`: that account changes its own files (never root), local cleanup
    only (GitHub is cleaned once, by the account that started the run); the report comes back as JSON on stdout.
    `-n`: never wait on a password prompt here; the one prompt happens up front, in the terminal."""
    cmd = ["sudo", "-n", "-u", acct, "-H", sys.executable, script,
           "clean" if mode == "clean" else "test", "--home", acct, "--local-only", "--json"]
    for flag in ("no_caches", "where", "pending", "quiet"):
        if getattr(args, flag, False):
            cmd.append("--" + flag.replace("_", "-"))
    return cmd


def run_accounts(args: argparse.Namespace, mode: str, accounts: list) -> tuple:
    """Run every other account's part through `sudo -u`. Returns ({account: report}, notes)."""
    reports, notes = {}, []
    if not accounts:
        return reports, notes
    if not have("sudo"):
        return reports, ["--all-accounts needs sudo; the other accounts were not cleaned"]
    if sys.stdin.isatty():
        step(f"sudo asks for your password, to run as {', '.join(accounts)} (again only if it runs out between them)")
    try:
        script = str(shared_script())
    except (OSError, RuntimeError) as e:
        return reports, [f"the other accounts were not cleaned: {e}"]
    # This account's GitHub token is never passed on: each account's run uses its own gh login, or none (then it
    # keeps squash-merged work it cannot prove merged). The other accounts are agent accounts with their own hooks
    # and credential helpers, and this token can delete branches.
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    for acct in accounts:
        if sys.stdin.isatty():
            # sudo's password lasts about 5 minutes (macOS timestamp_timeout) and one account's part takes longer:
            # renew it before each account (silent while still valid, a prompt again once it has run out).
            subprocess.call(["sudo", "-v"])
        step(f"running as {acct} (sudo -u {acct})")
        report = None
        try:
            p = subprocess.run(account_command(acct, mode, args, script), stdout=subprocess.PIPE, text=True,
                               env=env, timeout=4 * 3600)
            report = json.loads(p.stdout)
        except (ValueError, subprocess.TimeoutExpired, OSError):
            report = None
        if report is None:
            notes.append(f"could not run as {acct} through sudo (no password given, or this account may not use "
                         f"sudo); run it signed in as {acct} (README: All accounts from one place)")
        else:
            reports[acct] = report
    return reports, notes


def shared_script() -> Path:
    """A copy of this script the other accounts can read: they cannot read this account's home. One folder per
    account (any account may start --all-accounts), which must be a real folder this account owns: one another
    account created, or a symlink, is refused (RuntimeError, reported as a note) rather than written to."""
    base = Path("/Users/Shared") if sys.platform == "darwin" else Path("/tmp")
    d = base / f"storage-cleanup-{os.getuid()}"
    d.mkdir(parents=True, exist_ok=True)
    st = os.lstat(str(d))
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise RuntimeError(f"{d} is not a folder owned by {current_user()}; remove it and run again")
    os.chmod(str(d), 0o755)
    dest = d / "storage_cleanup.py"
    tmp = d / f".storage_cleanup.{os.getpid()}.py"
    tmp.write_text(Path(__file__).read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(str(tmp), 0o644)
    os.replace(str(tmp), str(dest))
    return dest


def merge_accounts(plan: Plan, reports: dict, notes: list) -> None:
    """Fold the other accounts' reports into this run's plan, for one summary and one page."""
    for acct, rep in reports.items():
        plan.actions.extend(rep.get("actions", []))
        plan.kept.extend(rep.get("kept", []))
        plan.loose.extend(rep.get("loose", []))
        plan.notes.extend(f"{acct}: {n}" for n in rep.get("notes", []))
        for who, items in rep.get("needs", {}).items():
            plan.needs.setdefault(who, []).extend(items)
    for acct in reports:  # an account that ran has nothing left waiting for it
        plan.needs.pop(acct, None)
    plan.needs.pop(current_user(), None)
    plan.notes.extend(notes)


def cmd_run(args: argparse.Namespace, mode: str) -> int:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        log("refusing to run as root: git would leave root-owned files in the checkouts; run it as each account")
        return 2
    cfg = load_config()
    HOME.mkdir(parents=True, exist_ok=True)
    lock = open(HOME / "lock", "w")
    if fcntl:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("another storage-cleanup run is in progress")
            return 3
    probe = Path.home()
    before = free_bytes(probe)
    others, accounts_notes = {}, []
    if getattr(args, "all_accounts", False):
        me = current_user()
        names = [h["name"] for h in resolve_homes(cfg)[0] if h["name"] != me]
        others, accounts_notes = run_accounts(args, mode, names)
        args.home = me  # this account's own part; the others reported theirs
    gh = None if args.local_only and args.no_gh else GitHub()
    extra = tuple(s for rep in others.values() for s in rep.get("slugs", []))
    plan, homes, repos = build_plan(args, cfg, gh, extra)
    if mode == "clean":
        execute(plan)
    if others or accounts_notes:
        merge_accounts(plan, others, accounts_notes)
    after = free_bytes(probe)
    if mode == "clean":
        targeted = sum(a["bytes"] for a in plan.actions if a["kind"] in LOCAL_KINDS and a["status"] == "done")
        gained = after[0] - before[0]
        if targeted >= 2 * GB and gained < targeted / 2:
            plan.notes.append(
                f"removed {human(targeted)} but the disk shows {human(max(0, gained))} more free. "
                + ("Time Machine local snapshots keep deleted files until they are thinned (`tmutil "
                   "listlocalsnapshots /`); " if sys.platform == "darwin" else "")
                + "files cloned or hard-linked from a package store (pnpm, bun) free nothing while the store keeps "
                  "them; other processes may have written in the meantime.")
    args.where_rows = [(h["name"], where(h["path"])) for h in homes] if args.where else None
    if args.where_rows is not None:
        args.where_rows += [(a, [tuple(r) for r in rep.get("where", [])]) for a, rep in others.items()]
    homes = homes + [{"name": a, "path": Path("/")} for a in others]
    repos = repos + [None] * sum(int(rep.get("checkouts", 0)) for rep in others.values())
    text = summarize(plan, mode, homes, repos, before, after, args)
    stamp = dt.datetime.now().strftime("%Y-%m-%dT%H%M%S")
    out_dir = HOME / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stamp}-{mode}.json"
    report = {"mode": mode, "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "user": current_user(),
              "host": socket.gethostname().split(".")[0], "homes": [h["name"] for h in homes],
              "checkouts": len(repos), "pending": bool(getattr(args, "pending", False)) and mode == "scan",
              "freeBefore": before[0], "freeAfter": after[0], "total": before[1], "reportPath": str(path),
              "actions": [{k: v for k, v in a.items() if k != "_run"} for a in plan.actions],
              "kept": plan.kept, "needs": plan.needs, "notes": plan.notes, "remote": plan.remote,
              "loose": plan.loose, "slugs": plan.slugs,
              "where": [list(r) for _, rows in (args.where_rows or [])[:1] for r in rows]}
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not report["pending"]:  # the preview before a clean is not a run of its own
        with open(HOME / "runs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": report["at"], "mode": mode, "user": report["user"], "freeBefore": before[0],
                                "freeAfter": after[0], "actions": len(plan.actions),
                                "failed": sum(1 for a in plan.actions if a["status"] == "failed")}) + "\n")
    for old in sorted(out_dir.glob("*.json"))[:-60]:
        old.unlink()
    page = render_html(report, read_runs())
    html_paths = [out_dir / "storage-cleanup.html"] + ([Path(args.html)] if getattr(args, "html", None) else [])
    for hp in html_paths:
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(page, encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(text)
        print(f"Report: {path}")
        print(f"HTML: {html_paths[-1]}")
    return 1 if any(a["status"] == "failed" for a in plan.actions) else 0


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="storage_cleanup.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "test", "clean"):
        p = sub.add_parser(name, help="delete and move nothing; report what clean would free" if name != "clean"
                           else "free the space")
        if name == "clean":
            p.add_argument("--dry-run", action="store_true", help="same as scan: report only, delete and move nothing")
        p.add_argument("--home", help="only these accounts' homes, comma separated (e.g. dgshor,aiuser1)")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--local-only", action="store_true", help="skip GitHub branch / cache / artifact cleanup")
        g.add_argument("--remote-only", action="store_true", help="only GitHub cleanup")
        p.add_argument("--no-caches", action="store_true", help="leave npm / pip / Playwright / docker caches")
        p.add_argument("--no-gh", action="store_true", help="with --local-only: do not use gh at all")
        p.add_argument("--where", action="store_true", help="also list the largest folders of each home")
        p.add_argument("--json", action="store_true", help="print the full JSON report instead of markdown")
        p.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
        p.add_argument("--all-accounts", action="store_true",
                       help="also run as every other account in `homes` through `sudo -u` (each changes only its "
                            "own files, never as root); one summary and one page for all of them")
        p.add_argument("--html", help="also write the HTML report here (it is always written to "
                                      "~/.claude/storage-cleanup/out/storage-cleanup.html)")
        if name != "clean":
            p.add_argument("--pending", action="store_true",
                           help="the preview shown while a clean runs: the page says it is cleaning now")
    c = sub.add_parser("config")
    c.add_argument("action", nargs="?", default="show", choices=["show", "path", "init"])
    args = ap.parse_args(argv)
    if args.cmd == "config":
        return cmd_config(args)
    global QUIET
    QUIET = args.quiet
    mode = run_mode(args)
    step(f"storage-cleanup {'clean' if mode == 'clean' else 'test run (nothing will be deleted or moved)'} as "
         f"{current_user()}; progress below, summary at the end")
    return cmd_run(args, mode)


if __name__ == "__main__":
    sys.exit(main())
