"""Tests for storage_cleanup.py on throwaway git repositories in a temp folder (no network, fake GitHub)."""
import datetime as dt
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("STORAGE_CLEANUP_HOME", tempfile.mkdtemp(prefix="sc-test-home-"))
import storage_cleanup as sc  # noqa: E402

sc.QUIET = True

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def sh(cwd, *args):
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(cwd, name, text="x"):
    Path(cwd, name).write_text(text)
    sh(cwd, "add", name)
    sh(cwd, "commit", "-q", "-m", f"add {name}")
    return sh(cwd, "rev-parse", "HEAD")


def age(gitdir, seconds):
    """Make a checkout look idle for `seconds` by back-dating the files git rewrites on activity."""
    t = time.time() - seconds
    for name in ("index", "HEAD", "logs/HEAD", "ORIG_HEAD", "COMMIT_EDITMSG"):
        p = Path(gitdir, name)
        if p.exists():
            os.utime(p, (t, t))


def cfg(**over):
    c = sc.load_config()
    c.update(over)
    return c


class FakeGitHub:
    ok = True

    def __init__(self, pulls=None, branches=None, caches=None, artifacts=None, info=None):
        self._pulls, self._branches = pulls or [], branches or []
        self._caches, self._artifacts = caches or [], artifacts or []
        self._info = info or {"default_branch": "main", "push": True, "archived": False}
        self.deleted = []
        self.now_open = {}  # branch -> open PR numbers that appear after planning

    def open_using(self, slug, name):
        return self.now_open.get(name, [])

    def info(self, slug):
        return self._info

    def pulls(self, slug):
        return self._pulls

    def branches(self, slug):
        return self._branches

    def caches(self, slug):
        return self._caches

    def artifacts(self, slug):
        return self._artifacts

    def delete(self, path):
        self.deleted.append(path)
        return True, ""


def pr(n, ref, sha, state="closed", merged=True, closed_days=5, base="main"):
    closed = (NOW - dt.timedelta(days=closed_days)).strftime("%Y-%m-%dT%H:%M:%SZ") if state == "closed" else None
    return {"number": n, "state": state, "merged_at": closed if merged and closed else None, "closed_at": closed,
            "head_ref": ref, "head_sha": sha, "head_repo": "homeporthealth/app", "base_ref": base}


class Fixture(unittest.TestCase):
    """A bare origin, a clone of it with an initial commit on main, and helpers to add worktrees."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sc-test-"))
        self.origin = self.tmp / "origin.git"
        sh(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.repo = self.tmp / "home" / "claude" / "app"
        self.repo.parent.mkdir(parents=True)
        sh(self.tmp, "clone", "-q", str(self.origin), str(self.repo))
        sh(self.repo, "checkout", "-q", "-b", "main")
        Path(self.repo, ".gitignore").write_text("node_modules/\ndist/\n.env\n")
        sh(self.repo, "add", ".gitignore")
        commit(self.repo, "README.md")
        sh(self.repo, "push", "-q", "-u", "origin", "main")
        sh(self.repo, "remote", "set-head", "origin", "main")
        self.cfg = cfg(remoteOwners=["homeporthealth"])

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def worktree(self, name, branch):
        path = self.tmp / "home" / "claude" / "app" / ".claude" / "worktrees" / name
        sh(self.repo, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    def plan(self, gh=None, cwds=frozenset(), fetched=False):
        repo = sc.Repo(self.repo)
        repo.slug = "homeporthealth/app"
        repo.fetched = fetched
        plan = sc.Plan()
        sc.plan_repo(repo, self.cfg, gh, plan, set(cwds), time.time(), "me", fetch=False)
        return plan

    @staticmethod
    def targets(plan, kind):
        return [a["target"] for a in plan.actions if a["kind"] == kind]

    @staticmethod
    def kept(plan, kind):
        return {k["target"]: k["why"] for k in plan.kept if k["kind"] == kind}


class Worktrees(Fixture):
    def test_merged_idle_worktree_is_removed_with_its_branch(self):
        wt = self.worktree("done", "agent/done")
        commit(wt, "a.txt")
        (wt / "node_modules" / "pkg").mkdir(parents=True)  # ignored output does not block the removal
        (wt / "node_modules" / "pkg" / "index.js").write_text("x")
        sh(self.repo, "merge", "-q", "--ff-only", "agent/done")
        sh(self.repo, "push", "-q", "origin", "main")
        age(self.repo / ".git" / "worktrees" / "done", 13 * 3600)
        plan = self.plan()
        self.assertEqual(self.targets(plan, "worktree"), [str(wt)])
        self.assertIn(f"{self.repo} agent/done", self.targets(plan, "branch"))
        sc.execute(plan)
        self.assertFalse(wt.exists())
        self.assertEqual(sh(self.repo, "branch", "--list", "agent/done"), "")

    def test_a_commit_made_between_plan_and_execute_survives(self):
        wt = self.worktree("window", "agent/window")
        commit(wt, "w.txt")
        sh(self.repo, "merge", "-q", "--ff-only", "agent/window")
        sh(self.repo, "push", "-q", "origin", "main")
        age(self.repo / ".git" / "worktrees" / "window", 13 * 3600)
        plan = self.plan()
        self.assertEqual(self.targets(plan, "worktree"), [str(wt)])
        self.assertIn(f"{self.repo} agent/window", self.targets(plan, "branch"))
        late = commit(wt, "late.txt")  # someone resumes and commits while the run is still sizing checkouts
        sc.execute(plan)
        self.assertTrue((wt / "late.txt").exists())
        self.assertEqual(sh(self.repo, "rev-parse", "agent/window"), late)
        statuses = {a["kind"]: a["status"] for a in plan.actions if a["kind"] in ("worktree", "branch")}
        self.assertEqual(statuses, {"worktree": "kept", "branch": "kept"})

    def test_a_branch_that_moved_after_the_plan_is_not_deleted(self):
        sh(self.repo, "branch", "merged-then-moved")
        plan = self.plan()
        self.assertIn(f"{self.repo} merged-then-moved", self.targets(plan, "branch"))
        sh(self.repo, "checkout", "-q", "merged-then-moved")
        new = commit(self.repo, "new-work.txt")
        sh(self.repo, "checkout", "-q", "main")
        sc.execute(plan)
        self.assertEqual(sh(self.repo, "rev-parse", "merged-then-moved"), new)

    def test_merged_worktree_with_an_ignored_env_keeps_it_and_loses_only_build_output(self):
        wt = self.worktree("withenv", "agent/withenv")
        commit(wt, "e.txt")
        sh(self.repo, "merge", "-q", "--ff-only", "agent/withenv")
        sh(self.repo, "push", "-q", "origin", "main")
        Path(wt, ".env").write_text("SECRET=1")
        (wt / "node_modules" / "pkg").mkdir(parents=True)
        (wt / "node_modules" / "pkg" / "index.js").write_text("x")
        age(self.repo / ".git" / "worktrees" / "withenv", 13 * 3600)
        plan = self.plan()
        self.assertEqual(self.targets(plan, "worktree"), [])
        self.assertIn(".env", self.kept(plan, "worktree")[str(wt)])
        self.assertEqual(self.targets(plan, "build"), [str(wt / "node_modules")])
        sc.execute(plan)
        self.assertEqual((wt / ".env").read_text(), "SECRET=1")
        self.assertFalse((wt / "node_modules").exists())

    def test_an_ignored_file_under_a_folder_merely_named_target_is_not_build_output(self):
        wt = self.worktree("pem", "agent/pem")
        Path(wt, ".gitignore").write_text("node_modules/\ndist/\n.env\n*.pem\n")
        sh(wt, "add", ".gitignore")
        sh(wt, "commit", "-q", "-m", "ignore pem")
        sh(self.repo, "merge", "-q", "--ff-only", "agent/pem")
        sh(self.repo, "push", "-q", "origin", "main")
        (wt / "src" / "target").mkdir(parents=True)
        (wt / "src" / "target" / "dev.pem").write_text("key")
        age(self.repo / ".git" / "worktrees" / "pem", 13 * 3600)
        plan = self.plan()
        self.assertEqual(self.targets(plan, "worktree"), [])  # git lists the ignored `src/`; dev.pem stays
        self.assertTrue(self.kept(plan, "worktree")[str(wt)].startswith("merged, but holds ignored files"))
        sc.execute(plan)
        self.assertTrue((wt / "src" / "target" / "dev.pem").exists())

    def test_a_detached_head_is_never_removed_on_the_pushed_rule(self):
        wt = self.worktree("detached", "agent/detached")
        commit(wt, "d.txt")
        sh(wt, "push", "-q", "origin", "agent/detached")
        sh(wt, "checkout", "-q", "--detach")
        age(self.repo / ".git" / "worktrees" / "detached", 30 * 86400)
        plan = self.plan(fetched=True)
        self.assertEqual(self.targets(plan, "worktree"), [])

    def test_git_keeps_its_safe_directory_guard(self):
        seen = []
        real = sc.run
        sc.run = lambda cmd, **kw: (seen.append(cmd), (0, "", ""))[1]
        try:
            sc.git(self.repo, "status")
        finally:
            sc.run = real
        self.assertFalse(any("safe.directory" in part for part in seen[0]))

    def test_a_merged_worktree_holding_a_nested_repository_is_kept(self):
        wt = self.worktree("nested", "agent/nested")
        commit(wt, "n.txt")
        sh(self.repo, "merge", "-q", "--ff-only", "agent/nested")
        sh(self.repo, "push", "-q", "origin", "main")
        inner = wt / "dist" / "clone"
        inner.mkdir(parents=True)
        sh(inner, "init", "-q", "-b", "main")
        commit(inner, "only-here.txt")
        age(self.repo / ".git" / "worktrees" / "nested", 13 * 3600)
        plan = self.plan()
        self.assertEqual(self.targets(plan, "worktree"), [])
        self.assertIn("nested git repository", self.kept(plan, "worktree")[str(wt)])
        sc.execute(plan)
        self.assertTrue((inner / "only-here.txt").exists())

    def test_unpushed_work_dirty_tree_and_recent_activity_are_kept(self):
        unpushed = self.worktree("unpushed", "agent/unpushed")
        commit(unpushed, "b.txt")
        dirty = self.worktree("dirty", "agent/dirty")
        Path(dirty, "scratch.txt").write_text("wip")
        recent = self.worktree("recent", "agent/recent")
        for n in ("unpushed", "dirty"):
            age(self.repo / ".git" / "worktrees" / n, 30 * 86400)
        plan = self.plan(fetched=True)
        kept = self.kept(plan, "worktree")
        self.assertEqual(self.targets(plan, "worktree"), [])
        self.assertIn("not merged", kept[str(unpushed)])
        self.assertTrue(kept[str(dirty)].startswith("uncommitted or untracked changes"))
        self.assertIn("active", kept[str(recent)])
        self.assertNotIn(f"{self.repo} agent/unpushed", self.targets(plan, "branch"))

    def test_squash_merged_worktree_is_removed_through_the_pr_head_sha(self):
        wt = self.worktree("squash", "agent/squash")
        sha = commit(wt, "c.txt")
        age(self.repo / ".git" / "worktrees" / "squash", 13 * 3600)
        self.assertEqual(self.targets(self.plan(), "worktree"), [])  # no GitHub: not provably merged
        gh = FakeGitHub(pulls=[pr(7, "agent/squash", sha)])
        plan = self.plan(gh=gh)
        self.assertEqual(self.targets(plan, "worktree"), [str(wt)])
        self.assertIn("PR #7 merged", [a["why"] for a in plan.actions if a["kind"] == "worktree"][0])

    def test_commits_after_the_merged_pr_keep_the_worktree(self):
        wt = self.worktree("more", "agent/more")
        sha = commit(wt, "d.txt")
        commit(wt, "e.txt")
        age(self.repo / ".git" / "worktrees" / "more", 13 * 3600)
        plan = self.plan(gh=FakeGitHub(pulls=[pr(8, "agent/more", sha)]))
        self.assertEqual(self.targets(plan, "worktree"), [])

    def test_pushed_but_unmerged_worktree_goes_only_when_stale_and_keeps_its_branch(self):
        wt = self.worktree("pushed", "agent/pushed")
        commit(wt, "f.txt")
        sh(wt, "push", "-q", "origin", "agent/pushed")
        age(self.repo / ".git" / "worktrees" / "pushed", 3 * 86400)
        self.assertEqual(self.targets(self.plan(fetched=True), "worktree"), [])
        age(self.repo / ".git" / "worktrees" / "pushed", 15 * 86400)
        self.assertEqual(self.targets(self.plan(fetched=False), "worktree"), [])  # unverifiable without a fetch
        plan = self.plan(fetched=True)
        self.assertEqual(self.targets(plan, "worktree"), [str(wt)])
        self.assertNotIn(f"{self.repo} agent/pushed", self.targets(plan, "branch"))

    def test_a_worktree_a_process_is_using_is_kept(self):
        wt = self.worktree("busy", "agent/busy")
        age(self.repo / ".git" / "worktrees" / "busy", 13 * 3600)
        plan = self.plan(cwds={os.path.realpath(str(wt / "src"))})
        self.assertEqual(self.kept(plan, "worktree")[str(wt)], "a running process is using it")


class StrayWorktrees(Fixture):
    """Worktrees Claude left in scratch or temp folders, outside the checkout they belong to."""

    def stray(self, name, branch):
        path = self.tmp / "scratch" / "session-1" / "wt" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        sh(self.repo, "worktree", "add", "-q", "-b", branch, str(path))
        return path

    def test_kept_work_is_moved_into_the_checkout(self):
        unmerged = self.stray("L1040", "agent/l1040")
        commit(unmerged, "u.txt")
        dirty = self.stray("L1041", "agent/l1041")
        Path(dirty, "notes.txt").write_text("wip")
        old = time.time() - 13 * 3600
        os.utime(str(dirty / "notes.txt"), (old, old))  # edited before the idle window, not just now
        for n in ("L1040", "L1041"):
            age(self.repo / ".git" / "worktrees" / n, 13 * 3600)
        plan = self.plan()
        home = self.repo / ".claude" / "worktrees"
        self.assertEqual(sorted(self.targets(plan, "move")),
                         [f"{unmerged} -> {home / 'L1040'}", f"{dirty} -> {home / 'L1041'}"])
        self.assertEqual(self.targets(plan, "build"), [])
        sc.execute(plan)
        self.assertEqual([a["status"] for a in plan.actions if a["kind"] == "move"], ["done", "done"])
        self.assertFalse(unmerged.exists())
        self.assertTrue((home / "L1040" / "u.txt").exists())
        self.assertEqual((home / "L1041" / "notes.txt").read_text(), "wip")   # uncommitted work moved with it
        listed = sh(self.repo, "worktree", "list", "--porcelain")
        self.assertIn(f"worktree {home / 'L1040'}", listed)
        self.assertEqual(sh(self.repo, "status", "--porcelain"), "")         # excluded from the main checkout
        self.assertIn("/.claude/worktrees/", (self.repo / ".git" / "info" / "exclude").read_text())

    def test_uncommitted_files_edited_recently_do_not_move_even_when_git_is_idle(self):
        wt = self.stray("editing", "agent/editing")
        Path(wt, "draft.txt").write_text("saved by an editor a minute ago")
        age(self.repo / ".git" / "worktrees" / "editing", 30 * 86400)
        plan = self.plan()
        self.assertEqual(self.targets(plan, "move"), [])
        self.assertIn("edited 0h ago", self.kept(plan, "worktree")[str(wt)])

    def test_a_worktree_used_between_plan_and_move_is_not_moved(self):
        wt = self.stray("resumed", "agent/resumed")
        commit(wt, "r.txt")
        age(self.repo / ".git" / "worktrees" / "resumed", 13 * 3600)
        plan = self.plan()
        self.assertEqual(len(self.targets(plan, "move")), 1)
        commit(wt, "r2.txt")  # someone resumed work after the plan was made
        sc.execute(plan)
        move = [a for a in plan.actions if a["kind"] == "move"][0]
        self.assertEqual(move["status"], "kept")
        self.assertTrue(wt.exists())

    def test_active_merged_and_already_home_worktrees_do_not_move(self):
        active = self.stray("active", "agent/active")
        commit(active, "a.txt")
        merged = self.stray("merged", "agent/merged")
        commit(merged, "m.txt")
        sh(self.repo, "merge", "-q", "--ff-only", "agent/merged")
        sh(self.repo, "push", "-q", "origin", "main")
        age(self.repo / ".git" / "worktrees" / "merged", 13 * 3600)
        inside = self.worktree("inside", "agent/inside")
        commit(inside, "i.txt")
        age(self.repo / ".git" / "worktrees" / "inside", 13 * 3600)
        plan = self.plan()
        self.assertEqual(self.targets(plan, "move"), [])
        self.assertEqual(self.targets(plan, "worktree"), [str(merged)])        # merged work is removed instead

    def test_name_collisions_get_a_suffix(self):
        (self.repo / ".claude" / "worktrees" / "wt").mkdir(parents=True)
        a = self.tmp / "s1" / "wt"
        b = self.tmp / "s2" / "wt"
        for p, br in ((a, "agent/a"), (b, "agent/b")):
            p.parent.mkdir(parents=True)
            sh(self.repo, "worktree", "add", "-q", "-b", br, str(p))
            commit(p, "x.txt")
        for n in ("wt", "wt1"):
            age(self.repo / ".git" / "worktrees" / n, 13 * 3600)
        home = self.repo / ".claude" / "worktrees"
        dests = sorted(t.split(" -> ")[1] for t in self.targets(self.plan(), "move"))
        self.assertEqual(dests, [str(home / "wt-2"), str(home / "wt-3")])

    def test_a_temp_folder_leads_to_the_checkout_and_orphans_are_reported(self):
        temp = self.tmp / "claude-tmp"
        live = temp / "proj" / "session" / "scratchpad" / "wt" / "live"
        orphan = temp / "proj" / "session" / "scratchpad" / "wt" / "orphan"
        for p, br in ((live, "agent/live"), (orphan, "agent/orphan")):
            p.parent.mkdir(parents=True, exist_ok=True)
            sh(self.repo, "worktree", "add", "-q", "-b", br, str(p))
        subprocess.run(["rm", "-rf", str(self.repo / ".git" / "worktrees" / "orphan")], check=True)
        self.assertEqual(sc.main_repo_of(live)[0], self.repo)
        self.assertIsNone(sc.main_repo_of(orphan)[0])
        c = sc.load_config()
        c.update(homes=[], roots=[str(temp)], tempRoots=[])
        args = sc.argparse.Namespace(home=None, local_only=True, remote_only=False, no_caches=True)
        plan, _, repos = sc.build_plan(args, c, None)
        self.assertEqual([os.path.realpath(str(r.path)) for r in repos], [os.path.realpath(str(self.repo))])
        self.assertEqual([x["path"] for x in plan.loose], [str(orphan)])
        self.assertNotIn(str(orphan), " ".join(a["target"] for a in plan.actions))


class OtherAccounts(Fixture):
    def test_a_worktree_this_account_cannot_see_blocks_prune_and_is_never_touched(self):
        hidden = self.worktree("hidden", "agent/hidden")
        commit(hidden, "h.txt")
        sh(self.repo, "merge", "-q", "--ff-only", "agent/hidden")
        age(self.repo / ".git" / "worktrees" / "hidden", 30 * 86400)
        real = sc.path_state
        sc.path_state = lambda p: "unknown" if str(p).startswith(str(hidden)) else real(p)
        try:
            plan = self.plan()
        finally:
            sc.path_state = real
        self.assertEqual(self.targets(plan, "worktree"), [])
        self.assertNotIn(f"{self.repo} worktree prune", self.targets(plan, "git"))
        self.assertIn("skipped: a worktree is not visible", self.kept(plan, "git")[f"{self.repo} worktree prune"])
        self.assertTrue(any(str(hidden) in n for notes in plan.needs.values() for n in notes))

    def test_a_checkout_owned_by_another_account_is_left_to_that_account(self):
        Path(self.repo, "node_modules").mkdir()
        age(self.repo / ".git", 30 * 86400)
        real = sc.current_user
        sc.current_user = lambda: "aiuser1"
        try:
            plan = self.plan()
        finally:
            sc.current_user = real
        self.assertEqual(plan.actions, [])
        owner = sc.owner_of(self.repo)
        self.assertIn(f"checkout {self.repo} (owned by {owner})", plan.needs[owner])

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs root to chown a file to another user")
    def test_a_merged_worktree_holding_another_accounts_files_is_left_to_that_account(self):
        import pwd
        wt = self.worktree("mixed", "agent/mixed")
        commit(wt, "m.txt")
        sh(self.repo, "merge", "-q", "--ff-only", "agent/mixed")
        sh(self.repo, "push", "-q", "origin", "main")
        (wt / "node_modules" / "x").mkdir(parents=True)
        theirs = wt / "node_modules" / "x" / "built-by-agent.js"
        theirs.write_text("x")
        nobody = pwd.getpwnam("nobody")
        os.chown(str(theirs), nobody.pw_uid, nobody.pw_gid)
        age(self.repo / ".git" / "worktrees" / "mixed", 13 * 3600)
        plan = self.plan()
        self.assertEqual(self.targets(plan, "worktree"), [])
        self.assertTrue(any(str(theirs) in n for n in plan.needs.get("nobody", [])))

    def test_a_checkout_whose_git_files_another_account_wrote_is_left_to_it(self):
        fetch_head = self.repo / ".git" / "FETCH_HEAD"
        fetch_head.write_text("")
        real = sc.os.access
        sc.os.access = lambda p, mode: False if str(p) == str(fetch_head) else real(p, mode)
        try:
            plan = self.plan()
        finally:
            sc.os.access = real
        self.assertEqual(plan.actions, [])
        self.assertTrue(any("FETCH_HEAD" in n for notes in plan.needs.values() for n in notes))

    def test_path_state(self):
        self.assertEqual(sc.path_state(self.repo), "present")
        self.assertEqual(sc.path_state(self.repo / "nope"), "missing")
        self.assertEqual(sc.path_state(self.repo / "README.md" / "x"), "missing")


class Branches(Fixture):
    def test_merged_branches_go_and_protected_or_unmerged_stay(self):
        sh(self.repo, "branch", "merged-one")
        sh(self.repo, "checkout", "-q", "-b", "release/1.0")
        sh(self.repo, "checkout", "-q", "-b", "fix/local-only")
        commit(self.repo, "g.txt")
        sh(self.repo, "checkout", "-q", "main")
        plan = self.plan()
        branches = self.targets(plan, "branch")
        self.assertIn(f"{self.repo} merged-one", branches)
        self.assertNotIn(f"{self.repo} release/1.0", branches)
        self.assertNotIn(f"{self.repo} fix/local-only", branches)
        self.assertNotIn(f"{self.repo} main", branches)


class BuildOutput(Fixture):
    def test_ignored_build_dirs_in_an_idle_checkout(self):
        for d in ("node_modules", "packages/ui/node_modules", "dist", "build", "secrets"):
            Path(self.repo, d).mkdir(parents=True, exist_ok=True)
            Path(self.repo, d, "f").write_text("x" * 5000)
        commit(self.repo, "build/keep.txt")       # build/ is tracked: never removed
        Path(self.repo, ".env").write_text("A=1")  # ignored, but not a build dir name
        age(self.repo / ".git", 1 * 86400)
        self.assertEqual(self.targets(self.plan(), "build"), [])  # active within repoIdleDays
        age(self.repo / ".git", 8 * 86400)
        found = sorted(self.targets(self.plan(), "build"))
        self.assertEqual(found, sorted(str(self.repo / p) for p in ("dist", "node_modules",
                                                                     "packages/ui/node_modules")))
        sc.execute(self.plan())
        self.assertFalse((self.repo / "node_modules").exists())
        self.assertTrue((self.repo / "build" / "keep.txt").exists())
        self.assertTrue((self.repo / ".env").exists())
        self.assertTrue((self.repo / "secrets").exists())


class DeleteGuard(Fixture):
    def test_only_build_output_inside_a_checkout_or_a_cache_can_be_deleted(self):
        nm = self.repo / "packages" / "ui" / "node_modules"
        nm.mkdir(parents=True)
        self.assertIsNone(sc.refuse_delete(nm))
        loose = self.tmp / "not-a-repo" / "node_modules"
        loose.mkdir(parents=True)
        self.assertEqual(sc.refuse_delete(loose), "not inside a git checkout")
        src = self.repo / "src"
        src.mkdir()
        self.assertEqual(sc.refuse_delete(src), "not a build-output folder name")
        music = self.repo / "Music" / "node_modules"
        self.assertEqual(sc.refuse_delete(music), "inside a personal folder")
        home = Path.home()
        for p in (home, home / "Music", home / "Pictures", home / "work", home / "node_modules"):
            self.assertEqual(sc.refuse_delete(p), "a home folder or a folder directly in one")
        self.assertIsNone(sc.refuse_delete(sc.cache_dirs(home)["playwright"][0] / "chromium-1100"))
        self.assertIsNotNone(sc.refuse_delete(sc.cache_dirs(home)["playwright"][0]))   # the cache root itself

    def test_build_output_in_a_personal_folder_is_kept_at_plan_time(self):
        repo = self.tmp / "Downloads" / "proj"
        repo.mkdir(parents=True)
        sh(repo, "init", "-q", "-b", "main")
        Path(repo, ".gitignore").write_text("node_modules/\n")
        sh(repo, "add", ".gitignore")
        sh(repo, "commit", "-q", "-m", "i")
        (repo / "node_modules").mkdir()
        age(repo / ".git", 30 * 86400)
        plan = sc.Plan()
        sc.plan_repo(sc.Repo(repo), self.cfg, None, plan, set(), time.time(), "me", fetch=False)
        self.assertEqual([a for a in plan.actions if a["kind"] == "build"], [])
        self.assertEqual(self.kept(plan, "build")[str(repo / "node_modules")], "inside a personal folder")

    def test_rmtree_refuses_and_leaves_the_folder(self):
        keep = self.tmp / "Documents-copy" / "dist"
        keep.mkdir(parents=True)
        ok, msg = sc._rmtree(keep)
        self.assertFalse(ok)
        self.assertIn("refused", msg)
        self.assertTrue(keep.exists())


class NestedRepositories(Fixture):
    def test_an_unpushed_clone_inside_ignored_build_output_survives_clean(self):
        Path(self.repo, ".gitignore").write_text("node_modules/\ndist/\n.env\n.cache/\n")
        sh(self.repo, "add", ".gitignore")
        sh(self.repo, "commit", "-q", "-m", "ignore .cache")
        clone = self.repo / ".cache" / "tool"
        clone.mkdir(parents=True)
        sh(clone, "init", "-q", "-b", "main")
        sha = commit(clone, "unpushed.txt")
        (self.repo / "node_modules" / "p").mkdir(parents=True)  # plain build output beside it still goes
        age(self.repo / ".git", 8 * 86400)
        plan = self.plan()
        self.assertNotIn(str(self.repo / ".cache"), self.targets(plan, "build"))
        self.assertEqual(self.kept(plan, "build")[str(self.repo / ".cache")], "holds a nested git repository")
        self.assertIn(str(self.repo / "node_modules"), self.targets(plan, "build"))
        sc.execute(plan)
        self.assertEqual(sh(clone, "rev-parse", "HEAD"), sha)
        self.assertFalse((self.repo / "node_modules").exists())

    def test_the_nested_check_fails_closed_when_find_times_out(self):
        real_run, real_have = sc.run, sc.have
        sc.have = lambda tool: True if tool == "find" else real_have(tool)
        sc.run = lambda cmd, **kw: (124, "", "find: timed out after 1800s")
        try:
            self.assertTrue(sc.nested_git(self.repo))
        finally:
            sc.run, sc.have = real_run, real_have

    def test_build_output_of_a_checkout_used_after_the_plan_stays(self):
        (self.repo / "node_modules" / "p").mkdir(parents=True)
        age(self.repo / ".git", 8 * 86400)
        plan = self.plan()
        self.assertIn(str(self.repo / "node_modules"), self.targets(plan, "build"))
        os.utime(str(self.repo / ".git" / "index"))  # someone runs git in it while the run is still planning
        sc.execute(plan)
        self.assertTrue((self.repo / "node_modules").exists())

    def test_the_last_guard_refuses_a_folder_holding_a_git_repository(self):
        d = self.repo / "build" / "x"
        d.mkdir(parents=True)
        (d / ".git").write_text("gitdir: /somewhere")  # a worktree file counts too
        self.assertEqual(sc.refuse_delete(self.repo / "build"), "holds a nested git repository")
        ok, msg = sc._rmtree(self.repo / "build")
        self.assertFalse(ok)
        self.assertTrue((d / ".git").exists())


class Discovery(unittest.TestCase):
    def test_finds_repos_and_skips_heavy_and_hidden_folders(self):
        root = Path(tempfile.mkdtemp(prefix="sc-find-"))
        for p in ("a/repo1", "b/c/repo2", "Library/repo3", "x/node_modules/repo4", ".hidden/repo5", "a/repo1/sub/r6"):
            (root / p / ".git").mkdir(parents=True)
        repos, _, _ = sc.find_repos(root, sc.load_config())
        self.assertEqual([str(r.relative_to(root)) for r in repos], ["a/repo1", "b/c/repo2"])

    def test_parse_slug(self):
        self.assertEqual(sc.parse_slug("git@github.com:homeporthealth/app.git"), "homeporthealth/app")
        self.assertEqual(sc.parse_slug("https://github.com/Velocity-Org/tidyhaus"), "Velocity-Org/tidyhaus")
        self.assertIsNone(sc.parse_slug("/srv/git/origin.git"))

    def test_parse_worktrees(self):
        wts = sc.parse_worktrees("worktree /a\nHEAD 1\nbranch refs/heads/main\n\nworktree /b\nHEAD 2\ndetached\n"
                                 "locked\n\nworktree /c\nHEAD 3\nbranch refs/heads/agent/x\nprunable gone\n")
        self.assertEqual([(w["path"], w["branch"], w["main"], w["locked"], w["prunable"]) for w in wts],
                         [("/a", "main", True, False, False), ("/b", None, False, True, False),
                          ("/c", "agent/x", False, False, True)])


class Remote(unittest.TestCase):
    def plan(self, gh, **remote):
        c = sc.load_config()
        c["remote"].update(remote)
        plan = sc.Plan()
        sc.plan_remote("homeporthealth/app", c, gh, plan, NOW)
        return plan

    def test_branch_rules(self):
        gh = FakeGitHub(
            branches=[{"name": n, "sha": s, "protected": p} for n, s, p in [
                ("main", "m", True), ("agent/merged", "s1", False), ("agent/fresh", "s2", False),
                ("agent/closed-old", "s3", False), ("agent/closed-new", "s4", False), ("agent/pushed-after", "s5x", False),
                ("agent/stack-base", "s6", False), ("agent/open", "s7", False), ("agent/no-pr", "s8", False),
                ("release/2.0", "s9", False)]],
            pulls=[pr(1, "agent/merged", "s1", closed_days=3), pr(2, "agent/fresh", "s2", closed_days=0),
                   pr(3, "agent/closed-old", "s3", merged=False, closed_days=40),
                   pr(4, "agent/closed-new", "s4", merged=False, closed_days=10),
                   pr(5, "agent/pushed-after", "s5", closed_days=3),
                   pr(6, "agent/stack-base", "s6", closed_days=3),
                   pr(10, "agent/stacked", "z", state="open", merged=False, base="agent/stack-base"),
                   pr(7, "agent/open", "s7", state="open", merged=False),
                   pr(9, "release/2.0", "s9", closed_days=30)])
        plan = self.plan(gh)
        deleted = sorted(a["target"] for a in plan.actions if a["kind"] == "remote-branch")
        self.assertEqual(deleted, ["homeporthealth/app:agent/closed-old", "homeporthealth/app:agent/merged"])
        kept = {k["target"]: k["why"] for k in plan.kept}
        self.assertEqual(kept["homeporthealth/app:agent/stack-base"], "base of an open PR")
        self.assertIn("pushed to after PR #5", kept["homeporthealth/app:agent/pushed-after"])
        self.assertEqual(plan.remote[0]["noPr"], 1)
        sc.execute(plan)
        self.assertEqual(sorted(gh.deleted), ["repos/homeporthealth/app/git/refs/heads/agent/closed-old",
                                              "repos/homeporthealth/app/git/refs/heads/agent/merged"])

    def test_a_branch_that_becomes_a_base_before_the_delete_is_kept(self):
        gh = FakeGitHub(branches=[{"name": "agent/done", "sha": "s1", "protected": False}],
                        pulls=[pr(1, "agent/done", "s1", closed_days=3)])
        plan = self.plan(gh)
        self.assertEqual([a["target"] for a in plan.actions if a["kind"] == "remote-branch"],
                         ["homeporthealth/app:agent/done"])
        gh.now_open["agent/done"] = [99]  # a stacked PR opened while the checkouts were being sized
        sc.execute(plan)
        self.assertEqual(gh.deleted, [])
        self.assertEqual(plan.actions[0]["status"], "kept")
        self.assertIn("#99", plan.actions[0]["detail"])

    def test_caches_of_a_branch_whose_delete_was_kept_are_kept(self):
        gh = FakeGitHub(branches=[{"name": "main", "sha": "m", "protected": True},
                                  {"name": "agent/done", "sha": "s1", "protected": False}],
                        pulls=[pr(1, "agent/done", "s1", closed_days=3)],
                        caches=[{"id": 7, "ref": "refs/heads/agent/done", "size_in_bytes": 5}])
        plan = self.plan(gh)
        gh.now_open["agent/done"] = [42]
        sc.execute(plan)
        self.assertEqual(gh.deleted, [])
        self.assertEqual({a["kind"]: a["status"] for a in plan.actions},
                         {"remote-branch": "kept", "actions-cache": "kept"})

    def test_caches_and_artifacts(self):
        old = (NOW - dt.timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new = (NOW - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        gh = FakeGitHub(
            branches=[{"name": "main", "sha": "m", "protected": True}, {"name": "agent/live", "sha": "l",
                                                                         "protected": False}],
            pulls=[pr(11, "agent/x", "x"), pr(12, "agent/live", "l", state="open", merged=False)],
            caches=[{"id": 1, "ref": "refs/heads/main", "size_in_bytes": 10},
                    {"id": 2, "ref": "refs/pull/11/merge", "size_in_bytes": 20},
                    {"id": 3, "ref": "refs/pull/12/merge", "size_in_bytes": 30},
                    {"id": 4, "ref": "refs/heads/agent/gone", "size_in_bytes": 40},
                    {"id": 5, "ref": "refs/heads/agent/live", "size_in_bytes": 50}],
            artifacts=[{"id": 21, "name": "old", "size_in_bytes": 100, "created_at": old, "expired": False},
                       {"id": 22, "name": "new", "size_in_bytes": 100, "created_at": new, "expired": False},
                       {"id": 23, "name": "gone", "size_in_bytes": 100, "created_at": old, "expired": True}])
        plan = self.plan(gh)
        self.assertEqual(sorted(a["target"].split()[0] for a in plan.actions if a["kind"] == "actions-cache"),
                         ["homeporthealth/app:2", "homeporthealth/app:4"])
        self.assertEqual([a["target"] for a in plan.actions if a["kind"] == "artifact"], ["homeporthealth/app:21 old"])

    def test_no_push_access_or_archived_is_skipped(self):
        for info, why in (({"default_branch": "main", "push": False}, "no push access"),
                          ({"default_branch": "main", "push": True, "archived": True}, "archived")):
            plan = self.plan(FakeGitHub(info=info, branches=[{"name": "x", "sha": "1", "protected": False}],
                                        pulls=[pr(1, "x", "1", closed_days=40)]))
            self.assertEqual(plan.actions, [])
            self.assertEqual(plan.remote[0]["skipped"], why)


def sample_report(mode="scan", pending=False, failed=False):
    acts = [
        {"kind": "build", "target": "/Users/dgshor/claude/app/node_modules", "bytes": 3 * sc.GB, "why": "idle 9d",
         "status": "done" if mode == "clean" else "planned"},
        {"kind": "worktree", "target": "/Users/dgshor/claude/app/.claude/worktrees/<m05>", "bytes": sc.GB,
         "why": "PR #12 merged", "status": "failed" if failed else ("done" if mode == "clean" else "planned"),
         "detail": "contains modified files" if failed else ""},
        {"kind": "artifact", "target": "homeporthealth/app:9 sbom", "bytes": 200 * sc.MB, "why": "older than 14 days",
         "status": "planned"},
    ]
    return {"mode": mode, "pending": pending, "at": "2026-10-13 03:17", "user": "dgshor", "host": "studio",
            "homes": ["dgshor", "aiuser1"], "checkouts": 7, "freeBefore": 5 * sc.GB,
            "freeAfter": 9 * sc.GB if mode == "clean" else 5 * sc.GB, "total": 500 * sc.GB, "reportPath": "/r.json",
            "actions": acts, "kept": [{"kind": "worktree", "target": "/w", "why": "uncommitted or untracked changes"}],
            "needs": {"aiuser1": ["checkout /Users/aiuser1/claude/x (owned by aiuser1)"]}, "notes": [],
            "remote": [{"repo": "homeporthealth/app", "branches": 3, "caches": 2, "cacheBytes": 10, "artifacts": 1,
                        "artifactBytes": 200 * sc.MB, "noPr": 4, "skipped": ""}]}


class Report(unittest.TestCase):
    def test_test_run_page(self):
        page = sc.render_html(sample_report())
        self.assertIn("<title>Storage cleanup</title>", page)
        self.assertIn("Test run. Nothing was deleted or moved", page)
        self.assertIn("Would free <b>4.0 GB</b>", page)
        self.assertIn("&lt;m05&gt;", page)          # paths are escaped
        self.assertNotIn("<m05>", page)
        self.assertIn("Needs another account", page)
        self.assertIn("4 branches without a PR left alone", page)

    def test_pending_then_clean_pages(self):
        self.assertIn("Cleaning now", sc.render_html(sample_report(pending=True)))
        done = sc.render_html(sample_report("clean"), runs=[{"at": "x", "mode": "scan", "user": "dgshor",
                                                              "freeBefore": 1, "freeAfter": 1}])
        self.assertIn("Freed <b>4.0 GB</b>", done)
        self.assertIn("chip gain", done)
        self.assertIn("Recent runs", done)
        bad = sc.render_html(sample_report("clean", failed=True))
        self.assertIn("Cleaned, 1 failed", bad)
        self.assertIn("contains modified files", bad)

    def test_test_and_dry_run_never_clean(self):
        ns = sc.argparse.Namespace
        self.assertEqual(sc.run_mode(ns(cmd="test")), "scan")
        self.assertEqual(sc.run_mode(ns(cmd="scan")), "scan")
        self.assertEqual(sc.run_mode(ns(cmd="clean", dry_run=True)), "scan")
        self.assertEqual(sc.run_mode(ns(cmd="clean", dry_run=False)), "clean")


class AllAccounts(unittest.TestCase):
    def test_each_account_runs_as_itself_local_only(self):
        args = sc.argparse.Namespace(no_caches=True, where=False, pending=True, quiet=False)
        cmd = sc.account_command("aiuser2", "scan", args, "/Users/Shared/storage-cleanup-501/storage_cleanup.py")
        self.assertEqual(cmd[:5], ["sudo", "-n", "-u", "aiuser2", "-H"])
        self.assertFalse(any("preserve-env" in c or "GH_TOKEN" in c for c in cmd))  # no token crosses accounts
        self.assertNotIn("root", cmd)
        for flag in ("test", "--home", "--local-only", "--json", "--no-caches", "--pending"):
            self.assertIn(flag, cmd)
        self.assertNotIn("--all-accounts", cmd)  # an account's own part never fans out again
        self.assertEqual(cmd[cmd.index("--home") + 1], "aiuser2")
        clean = sc.account_command("dgshor", "clean", sc.argparse.Namespace(), "/s.py")
        self.assertIn("clean", clean)
        self.assertNotIn("test", clean)

    def test_reports_merge_and_accounts_that_ran_need_nothing(self):
        plan = sc.Plan()
        plan.needs = {"dgshor": ["checkout /Users/dgshor/x"], "aiuser2": ["npm cache 2 GB"]}
        rep = {"actions": [{"kind": "build", "target": "/Users/dgshor/claude/a/node_modules", "bytes": 5,
                            "why": "idle", "home": "dgshor", "status": "done"}],
               "kept": [], "loose": [], "notes": ["n"], "needs": {"aiuser2": ["worktree w"]}}
        sc.merge_accounts(plan, {"dgshor": rep}, ["could not run as aiuser2 through sudo"])
        self.assertEqual(len(plan.actions), 1)
        self.assertNotIn("dgshor", plan.needs)
        self.assertEqual(plan.needs["aiuser2"], ["npm cache 2 GB", "worktree w"])
        self.assertIn("dgshor: n", plan.notes)
        self.assertIn("could not run as aiuser2 through sudo", plan.notes)

    def test_a_shared_folder_owned_by_another_account_is_a_note_not_a_crash(self):
        real_uid, real_have = sc.os.getuid, sc.have
        sc.os.getuid = lambda: 4242  # the folder below is created by this test's real account, not by 4242
        sc.have = lambda tool: True if tool == "sudo" else real_have(tool)
        try:
            base = Path("/Users/Shared") if sys.platform == "darwin" else Path("/tmp")
            (base / "storage-cleanup-4242").mkdir(exist_ok=True)
            with self.assertRaises(RuntimeError):
                sc.shared_script()
            reports, notes = sc.run_accounts(sc.argparse.Namespace(), "scan", ["aiuser2"])
        finally:
            sc.os.getuid, sc.have = real_uid, real_have
            subprocess.run(["rm", "-rf", str(base / "storage-cleanup-4242")])
        self.assertEqual(reports, {})
        self.assertIn("not a folder owned by", notes[0])

    def test_own_shared_folder_and_no_token_passed_on(self):
        path = sc.shared_script()
        self.assertEqual(path.parent.name, f"storage-cleanup-{os.getuid()}")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o644")
        seen = {}
        real_run, real_have = sc.subprocess.run, sc.have
        os.environ["GH_TOKEN"] = "secret-test-token"

        def fake_run(cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw.get("env", {})
            return subprocess.CompletedProcess(cmd, 0, stdout="{}")

        sc.subprocess.run, sc.have = fake_run, (lambda tool: True if tool == "sudo" else real_have(tool))
        try:
            reports, _ = sc.run_accounts(sc.argparse.Namespace(), "scan", ["aiuser2"])
        finally:
            sc.subprocess.run, sc.have = real_run, real_have
            del os.environ["GH_TOKEN"]
        self.assertEqual(reports, {"aiuser2": {}})
        self.assertNotIn("GH_TOKEN", seen["env"])
        self.assertNotIn("secret-test-token", " ".join(seen["cmd"]))

    def test_sudo_is_renewed_before_each_account_in_a_terminal(self):
        calls = []
        real_call, real_run, real_have, real_tty = sc.subprocess.call, sc.subprocess.run, sc.have, sc.sys.stdin.isatty
        sc.subprocess.call = lambda cmd: calls.append(("call", cmd)) or 0
        sc.subprocess.run = lambda cmd, **kw: (calls.append(("run", cmd[3])),
                                                subprocess.CompletedProcess(cmd, 0, stdout="{}"))[1]
        sc.have = lambda tool: True if tool == "sudo" else real_have(tool)
        sc.sys.stdin.isatty = lambda: True
        try:
            sc.run_accounts(sc.argparse.Namespace(), "scan", ["aiuser1", "aiuser2"])
        finally:
            sc.subprocess.call, sc.subprocess.run, sc.have = real_call, real_run, real_have
            sc.sys.stdin.isatty = real_tty
        self.assertEqual(calls, [("call", ["sudo", "-v"]), ("run", "aiuser1"),
                                 ("call", ["sudo", "-v"]), ("run", "aiuser2")])

    def test_no_sudo_means_a_note_not_a_crash(self):
        real = sc.have
        sc.have = lambda tool: False if tool == "sudo" else real(tool)
        try:
            reports, notes = sc.run_accounts(sc.argparse.Namespace(), "scan", ["aiuser2"])
        finally:
            sc.have = real
        self.assertEqual(reports, {})
        self.assertIn("needs sudo", notes[0])


class Helpers(unittest.TestCase):
    def test_stale_playwright_keeps_newest_per_kind(self):
        dirs = [Path(n) for n in ("chromium-1100", "chromium-1140", "chromium-1120", "firefox-1400",
                                  "chromium_headless_shell-1140", "chromium_headless_shell-1100", ".links")]
        self.assertEqual([p.name for p in sc.stale_playwright(dirs, 2)], ["chromium-1100"])
        self.assertEqual([p.name for p in sc.stale_playwright(dirs, 1)],
                         ["chromium-1100", "chromium-1120", "chromium_headless_shell-1100"])

    def test_parse_reclaimed(self):
        self.assertEqual(sc.parse_reclaimed("Total reclaimed space: 1.5GB\nTotal reclaimed space: 500MB"),
                         int(1.5e9 + 500e6))
        self.assertEqual(sc.parse_reclaimed("==> This operation has freed approximately 2GB of disk space."),
                         int(2e9))

    def test_human(self):
        self.assertEqual(sc.human(512), "512 B")
        self.assertEqual(sc.human(3 * sc.GB), "3.0 GB")

    def test_homes_skip_accounts_that_do_not_exist(self):
        c = sc.load_config()
        c["homes"] = ["~", "~no-such-account-xyz"]
        homes, notes = sc.resolve_homes(c)
        self.assertEqual(len(homes), 1)
        self.assertIn("no-such-account-xyz", notes[0])


if __name__ == "__main__":
    unittest.main()
