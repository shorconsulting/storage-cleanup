---
description: Free disk space across every local git checkout and tidy the GitHub remotes (test mode reports only)
argument-hint: '[test | clean | schedule] [--home dgshor,aiuser1] [--local-only | --remote-only] [--no-caches] [--where]'
allowed-tools: Bash(python3 *), Artifact, mcp__scheduled-tasks__create_scheduled_task, mcp__scheduled-tasks__list_scheduled_tasks
---

Free disk space on this computer and tidy the GitHub remotes, or (test mode) report how much would be freed, and
show the result as the `Storage cleanup` artifact. Run unattended: never ask a question, never stop to confirm,
never delete anything yourself. The script decides what is safe; your job is to run it, publish its page, and report.

This plugin works on this computer's disk, so it always runs locally, never in a cloud environment. If this session
is a cloud session (Claude Code on the web, a cloud routine, a remote container), the script refuses with exit code 4:
reply in one line that the cleanup has to run on the Mac itself, from the Claude desktop app's Code tab or a local
scheduled task, publish nothing, and stop.

## Steps

0. Schedule (the arguments ask to schedule it, or `schedule`): create a **local** scheduled task on this computer
   with the `create_scheduled_task` tool (scheduled-tasks) - never a cloud routine (`/schedule`, `RemoteTrigger`), which
   runs in a remote container that cannot see this disk. Schedule: daily at a quiet hour (`17 3 * * *` unless the
   arguments give one); prompt: `/storage-cleanup clean` (or `/storage-cleanup test` when the arguments say test).
   Reply with the task's name and next run time, and stop.

1. Pick the mode from the arguments: `clean` frees the space; anything else (no arguments, `test`, `scan`,
   `clean --dry-run`) is a test run that deletes and moves nothing (it only refreshes remote-tracking refs with `git fetch --prune` and writes its own report files). Pass the remaining flags through, and always add
   `--all-accounts` (unless the arguments already name accounts with `--home`): the script then also runs as every
   other account in `homes` through `sudo -u <account>`, each changing only its own files, and merges everything
   into one summary and one page. Let `PAGE` be
   `<your scratchpad directory, or the current directory if you have none>/storage-cleanup.html`.

   Every run below can take several minutes (it sizes folders and fetches each checkout): give each command a
   timeout of at least 30 minutes. Exit code 1 means some actions failed (listed under `Failed:`); 2 means it was
   started as root (refused); 3 means another run holds the lock; 4 means this is a cloud session (refused, see
   above).

2. Find the artifact: call the Artifact tool with `action: "list"` and look for one titled `Storage cleanup`. If one
   exists, read it once (`action: "read"`) and publish to its `url` in every step below, so the link stays the same
   from run to run. If none exists, the first publish creates it with `icon: "disk"` and the description
   `What the scheduled storage cleanup freed on this computer and on GitHub`; later publishes in this session use the
   same `PAGE` path, which updates that artifact in place.

3. Test run: run the script, publish `PAGE`, and go to step 5.

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/storage_cleanup.py" test [flags] --html "$PAGE"
   ```

4. Clean: show the artifact first, then update it when the cleaning is done.
   1. Preview: run `storage_cleanup.py test --pending [flags] --html "$PAGE"` and publish `PAGE`. The page says
      "Cleaning now" with what is about to be freed. Do not wait for anything else before publishing.
   2. Clean: run `storage_cleanup.py clean [flags] --html "$PAGE"` and publish `PAGE` again to the same artifact.
      The page now shows what was freed, what failed and the recent runs.
      If the clean run fails before writing the page (exit 2 or 3, or a crash), still republish so the artifact does
      not stay on "Cleaning now": run the preview again without `--pending` and publish that.

5. Reply with the summary the last run printed (the free-space line, the table, the largest items, the remote
   lines, the `Needs another account` lines and the `Kept` lines) and the artifact link. Then add at most three
   lines:
   - in a test run, the single command that would free the space (`/storage-cleanup clean`, with the same flags);
   - when a `Note:` says `could not run as <account> through sudo`: sudo needs a password and this session has no
     terminal to type it in. Say once that the other accounts were not cleaned, and that running
     `python3 <plugin>/scripts/storage_cleanup.py clean --all-accounts` in Terminal (it asks for the password once)
     covers them. Never edit sudoers or sudo settings yourself;
   - any `Failed:` item, `Note:` about gh not being logged in, or exit code other than 0, in one line each.

## What it does (for answering questions about a run)

- **Worktrees**: removed when clean (no uncommitted or untracked files), idle (12 h of no git activity), not in use
  by a running process, and their work is merged (in the default branch, or the head of a merged PR, squash
  merges included). A worktree whose work is only pushed goes after 14 idle days and its branch is kept.
- **Stray worktrees**: Claude's temp folders (`/private/tmp/claude-<uid>`) are searched too. A worktree outside its
  checkout that has to stay (uncommitted or unmerged work) is moved into `<checkout>/.claude/worktrees/<name>` with
  `git worktree move` once idle and unused.
- **Orphaned worktree folders** (git no longer knows them; the checkout may be gone, moved or unplugged): listed
  under `Orphaned worktree folders`, and only their `node_modules` and similar build folders go, after 7 days with
  nothing changed. The folders themselves stay unless `orphanWorktreeDays` is set.
- **Claude temp folders**: a session's folder under `/private/tmp/claude-<uid>/<project>/<session id>/` (scratchpad,
  task output) after 7 days with nothing changed and its session transcript unchanged as long, unless a process runs
  in it or names the session, a live `*.pid` file (a test database) is in it, or it holds a repository or mount.
  Other folders in the temp root are only reported.
- **Crash reports**: files older than 7 days in apps' `Crashpad` / `Crash Reports` folders and macOS
  `DiagnosticReports`.
- **Test databases**: on the Postgres clusters listed under `postgres` in the config, matching databases with no
  connection and no write for `idleDays` are dropped (never `WITH (FORCE)`).
- **Local branches**: deleted only when merged by the same rule; protected names and the default branch never.
- **Build output**: git-ignored `node_modules`, `dist`, `coverage`, `.turbo`, `.next`, `playwright-report` and the
  like, in checkouts idle for 7 days. Never `.env`, never tracked files. `npm ci` brings them back.
- **Caches** (the running account only, each when at least 200 MB): npm, pnpm, yarn 1, pip, Homebrew, Playwright
  browser revisions beyond the newest two, docker build cache older than a week and dangling images, Gradle caches
  and Xcode DerivedData (not while Gradle, gradlew, Xcode or xcodebuild runs).
- **Reported, never touched**: the Trash (1 GB or more), iOS simulators (1 GB or more), and Time Machine local
  snapshots when the disk is over 85% full; snapshots keep deleted files, so space comes back only when macOS thins
  them or after `sudo tmutil deletelocalsnapshots /`.
- **Remote** (owners in `remoteOwners`, push access via `gh`): branches whose PR merged at least 1 day ago or closed
  unmerged at least 30 days ago, untouched since; never a branch an open PR uses as head or base, never protected
  branches, never a branch with no PR. Actions caches of closed PRs and deleted branches; artifacts older than
  14 days.
- Thresholds, homes and owners live in `~/.claude/storage-cleanup/config.json`
  (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/storage_cleanup.py" config init` writes it with every default).
