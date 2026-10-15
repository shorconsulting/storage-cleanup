# storage-cleanup

A Claude Code plugin that frees local disk across every git checkout on the machine and tidies the GitHub remotes.
It scans the home folders of `dgshor`, `aiuser1` and `aiuser2` (plus the account running it) for repositories,
removes what is provably merged, pushed or regenerable, and reports everything it kept and why. A test mode shows
how much space would be recovered without changing anything.

Private plugin for David Shor's shared Mac. It started in `homeporthealth/app` (closed PR homeporthealth/app#1441, which
carries the review history) and moved here, beside the other `shorconsulting` plugins.

## Install (each account)

```
/plugin install storage-cleanup --marketplace shorconsulting/storage-cleanup
```

The repository is private, so each account needs git access to it first: `gh auth login`, then `gh auth setup-git`
(git then uses that `gh` login to fetch the marketplace).

Needs `python3` (3.9+, the macOS command line tools version) and `git`. Optional: `gh` logged in for remote cleanup
and squash-merge detection; `npm`, `pnpm`, `pip3`, `brew`, `docker` for their caches.

### Without installing

The script runs on its own, from any folder:

```bash
gh api -H "Accept: application/vnd.github.raw" \
  "repos/shorconsulting/storage-cleanup/contents/scripts/storage_cleanup.py" \
  > ~/storage_cleanup.py && test -s ~/storage_cleanup.py && python3 ~/storage_cleanup.py test
```

Then `python3 ~/storage_cleanup.py clean --all-accounts` from an administrator account.

## Commands

| Command                                       | What it does                                                                                   |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `/storage-cleanup` or `/storage-cleanup test` | Test run: shows what would be removed and how much space it frees. Deletes nothing (see below) |
| `/storage-cleanup clean`                      | Shows the plan at once ("Cleaning now"), frees the space, then updates the page                |
| `... --home dgshor,aiuser1`                   | Only these accounts' home folders                                                              |
| `... --local-only` / `--remote-only`          | Only the disk / only GitHub                                                                    |
| `... --no-caches`                             | Leave npm, pip, Playwright, Homebrew and docker caches alone                                   |
| `... --all-accounts`                          | Also run as each other account through `sudo -u`; one summary for all                          |
| `... --where`                                 | Also list the largest folders of each home (slow on big homes)                                 |

Nothing outside git checkouts and the named tool caches is ever deleted. The delete step itself refuses a home
folder, anything directly in one (`~/Music`, `~/Documents`, `~/work`), anything in a personal folder outside a known
cache, any folder that is not build output inside a git checkout, and any folder holding a nested git repository (a clone or worktree under an ignored folder, whose unpushed work the outer checkout cannot see). `--where` only measures folders, to show where
the space went. A test run deletes and moves nothing; it does refresh each checkout's remote-tracking refs
(`git fetch --prune`) and writes its own report files.

Planning takes minutes (every checkout is sized first), so each deletion re-checks just before it runs: a
worktree whose commit or git activity changed since the plan is kept, a local branch is deleted only at the commit
that was judged merged (git refuses the delete if the branch moved), and a branch's Actions caches go only when
the branch delete itself happened.

When much less space comes back than was removed, the run says so: on a Mac, Time Machine local snapshots keep
deleted files until they are thinned (`tmutil listlocalsnapshots /`), and files cloned or hard-linked from a package
store (pnpm, bun) free nothing while the store keeps them.

Every run is shown as the **Storage cleanup** artifact, at one link that stays the same from run to run: a disk bar
(used, freed by this run, free), the totals by category, the largest items, GitHub, the accounts that still need
their own run, what was kept and why, and the recent runs. During `clean` the artifact appears first with what is
about to be freed, and updates in place when the cleaning is done.

Without Claude: `python3 scripts/storage_cleanup.py test|clean [flags]` (`clean --dry-run` = `test`); the same page
is written to `~/.claude/storage-cleanup/out/storage-cleanup.html` to open in a browser.

## What it removes, and the rule for each

| What                                      | Removed when                                                                                                                                                                                                           | Never                                                                                                |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Worktrees (agent and `.claude/worktrees`) | clean tree, 12 h without git activity, no process running in it, and the work is merged: in `origin/<default>`, or the head of a merged PR (squash merges included). Pushed-only work: after 14 idle days, branch kept | locked, rebase/merge in progress, uncommitted or untracked files, unpushed commits                   |
| Local branches                            | merged by the same rule                                                                                                                                                                                                | default branch, `main`, `master`, `develop`, `release/*`, `hotfix/*`, `laravel-dev`, ...             |
| Build output                              | git-ignored `node_modules`, `dist`, `build`, `coverage`, `.turbo`, `.vite`, `.next`, `playwright-report`, `test-results`, `__pycache__`, `target`, ... in a checkout idle 7 days                                       | tracked files, `.env` or anything not in the list                                                    |
| Git upkeep                                | `git worktree prune`; `git gc` when loose objects exceed 100 MB                                                                                                                                                        |                                                                                                      |
| Caches (running account)                  | npm, pnpm, yarn 1, pip, Homebrew, Gradle caches, Xcode DerivedData, Puppeteer when at least 200 MB; Playwright revisions beyond the newest two per browser; simulators whose runtime is gone; docker build cache older than a week and dangling images | docker images in use, volumes; Gradle or Xcode caches while that tool runs                          |
| Remote branches                           | PR merged at least 1 day ago, or closed unmerged at least 30 days ago, and no push since it closed                                                                                                                     | an open PR's head or base (stacked PRs), protected or default branches, branches that never had a PR |
| Actions caches                            | of a closed PR or a deleted branch                                                                                                                                                                                     | the default branch's                                                                                 |
| Actions artifacts                         | older than 14 days                                                                                                                                                                                                     |                                                                                                      |
| Orphaned worktree folders                 | git no longer knows the folder: `node_modules` (beside a `package.json`), `.turbo`, `.next`, `coverage`, `__pycache__`, ... after 7 days with no file changed; the whole folder after 30 days (`orphanWorktreeDays`, 0 = never) | a file changed since the plan, a process with a file in it open, a nested repository, another account's files |
| Claude temp folders                       | `<tempRoot>/<project>/<session id>/` after 3 days with no file changed (`tempIdleDays`)                                                                                                                                | a process running in it, a live `*.pid` file in it (a test database), a git checkout in it, any other folder of the temp root |
| Crash reports                             | files older than 7 days (`crashDays`) in apps' `Crashpad` / `Crash Reports` folders and `~/Library/Logs/DiagnosticReports`                                                                                             | `settings.dat`, `metadata`                                                                           |
| Test databases                            | on the clusters under `postgres` in the config: a matching database with no connection and no write for `idleDays`                                                                                                     | `keep` patterns, a database connected or written since the plan; never `WITH (FORCE)`               |

## Stray worktrees

Claude sessions often create worktrees outside the checkout they belong to: in a session's scratchpad under
`/private/tmp/claude-<uid>/`, or in some other temp folder. Each run searches the running account's Claude temp
folders as well as the homes, and follows every worktree folder it finds back to its main checkout, wherever that
lives.

- A stray worktree whose work is merged is removed like any other (the rules above).
- One that has to stay (uncommitted files or unmerged commits) is moved into `<checkout>/.claude/worktrees/<name>`
  with `git worktree move`, which keeps git's records right. It moves only when it has been idle 12 h, no process
  runs in it, its uncommitted files were not edited in those 12 h either, it is not locked or mid-rebase, and the running account owns it. A name already taken gets `-2`, `-3`.
  `.claude/worktrees/` is added to the checkout's local `.git/info/exclude` when the checkout does not ignore it
  already, so the main checkout's `git status` stays clean. `worktreeHome: ""` in the config turns moving off.
- A folder that looks like a worktree but whose checkout no longer knows it (git's record of it was pruned, or the
  checkout itself was deleted) is an orphan. Its commits went with git's record, so only its files are left. It is
  listed under **Orphaned worktree folders**; its build output goes once no file in it has changed for 7 days, and
  the folder itself once no file has changed for 30 days (`orphanWorktreeDays`; 0 keeps them for good). Only build
  folder names that are never source go early (`orphanBuildDirs`: git can no longer say what is ignored), and
  `node_modules` only beside a `package.json`.
- Git-ignored folders a tool regenerates (`disposableIgnored`, by default husky's `.husky/_` hook shims) no longer
  keep a merged worktree the way an `.env` does.

## Other places AI sessions leave things

- **Claude temp folders.** Each session keeps a folder under `/private/tmp/claude-<uid>/<project>/<session id>/`
  (scratchpad, task output); agents put test databases, clones and downloads there. One goes when no file in it has
  changed for 3 days (`tempIdleDays`), nothing runs in it, no live `*.pid` file is in it and it holds no git
  checkout (worktrees there follow the worktree rules). Anything else in the temp root of 500 MB or more is reported
  only: a tool an agent installed there may still be run from it without anything being written.
- **Crash reports.** Electron and Chromium apps (Claude, Codex, Slack, Chrome, VS Code) write crash dumps into a
  `Crashpad` folder that is never emptied; one held 25 GB on 15 Oct 2026. Files older than 7 days go (`crashDays`).
- **Throwaway test databases.** Test scripts create a database per worktree and never drop it. List each cluster
  under `postgres` in the config (`psql`, `host`, `port`, `user`, `dataDir`, `match`, optional `keep`,
  `idleDays`); a matching database with no connection whose files in `dataDir/base/<oid>` were not written for
  `idleDays` is dropped, without `FORCE`, after both are checked again just before the drop.
- **Reported, never touched.** The Trash when it holds 1 GB or more. When the disk is over 85% full, the number of
  Time Machine local snapshots: they keep every deleted file, so deleting frees nothing until macOS thins them or
  `sudo tmutil deletelocalsnapshots /` is run in Terminal (it needs the password; backups on a Time Machine drive
  stay).

Remote cleanup runs only for repositories owned by `homeporthealth`, `Velocity-Org`, `Shor-Consulting-Inc` and
`shorconsulting`, where the `gh` login has push access; other remotes (for example the legacy repositories) get
local cleanup only. Before judging a checkout it runs `git fetch --prune`, so a branch deleted on GitHub is never
mistaken for pushed work.

## Several accounts

Each run changes only what the running account owns, even where it has write access to another account's
checkout: git run as one account leaves files in another's `.git` that the owner may then be unable to rewrite. The
rest is listed under **Needs another account**. A worktree inside a folder the running account cannot enter (another
account's private temp folder, for a Claude session's scratchpad worktree) is treated as unknown, never as missing:
git would report it as prunable, so `git worktree prune` is skipped for that checkout and `git gc` never prunes
worktrees. Accounts that share folders leave mixed ownership behind, so a worktree or build-output folder holding
any file another account owns, or a checkout whose `.git` files another account wrote, is left to that account
instead of being half deleted. A merged worktree holding git-ignored files that are not build output (an `.env`,
local settings) is kept, because `git worktree remove` would delete them; only its build output goes. It refuses to
run as root, because git run as root would leave root-owned files in the checkouts.

### All accounts from one place (`sudo -u`)

`--all-accounts`, which `/storage-cleanup` always passes, runs the same cleanup as each other account in `homes`
through `sudo -u <account>`, then this account's own part, and shows one summary and one page for all of them:

```
python3 storage_cleanup.py test --all-accounts     # what every account would free
python3 storage_cleanup.py clean --all-accounts
```

- Each account changes only its own files: sudo runs the script as that account, never as root.
- The other accounts do local cleanup only; GitHub is cleaned once, by the account that started the run, over the
  repositories every account found.
- Start it from an account that may use sudo (an administrator). sudo asks for that account's password once, in the
  terminal.
- The other accounts run a copy of the script at `/Users/Shared/storage-cleanup-<uid>/storage_cleanup.py` (one
  folder per starting account, which must be owned by it), because they cannot read the starting account's home.
- No GitHub token is passed between accounts: each account's part uses its own `gh` login, or none (it then keeps
  squash-merged work it cannot prove merged).
- Just before deleting a remote branch, the open pull requests are read again, and a branch an open PR has started
  to use since the plan was made is kept.
- With no terminal to type the password in (a run from a Claude session or a scheduled routine), sudo cannot ask,
  so those accounts are reported as `could not run as <account> through sudo` and left for the next run from
  Terminal, or for that account's own run. Letting it run unattended would take a password-free sudo rule for this
  command, a change to the Mac's security settings the script never makes itself.

## Schedule it

The routine has to run **on the Mac whose disk it cleans**. A cloud routine runs in a remote container and cannot
see this disk. From the Claude desktop app on that Mac, signed in as each account, create a routine or scheduled task
that runs on this computer:

- Schedule: daily at a quiet hour, for example `CRON_TZ=America/Toronto 17 3 * * *`.
- Prompt: `/storage-cleanup clean`, or `/storage-cleanup test` for a week to watch what it would do first.
- Without a password-free sudo rule, a scheduled run cleans the account it runs as (and GitHub); the other accounts
  are cleaned by running `clean --all-accounts` from Terminal, or by giving each account its own routine.

Without Claude, cron works too (each account, `crontab -e`, with the script downloaded as above):

```
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
17 3 * * * python3 ~/storage_cleanup.py clean >> ~/.claude/storage-cleanup/cron.log 2>&1
```

## Config and reports

`~/.claude/storage-cleanup/config.json` overrides any default (`storage_cleanup.py config init` writes the file
with every default; `config show` prints the effective one). Keys: `homes`, `roots`, `skipDirs`, `maxDepth`,
`worktreeIdleHours`, `worktreeHome`, `tempRoots`, `staleWorktreeDays`, `repoIdleDays`, `buildDirs`, `disposableIgnored`,
`orphanBuildDirs`, `orphanWorktreeDays`, `tempIdleDays`, `crashDays`, `postgres`, `protectBranches`, `caches`, `cacheMinMB`,
`playwrightKeep`, `remoteOwners`, `remote` (`branches`, `mergedGraceDays`, `closedGraceDays`, `actionsCaches`,
`artifacts`, `artifactDays`).

Each run writes `~/.claude/storage-cleanup/out/<time>-<scan|clean>.json` (the last 60 are kept) and the HTML page
`out/storage-cleanup.html` (`--html PATH` writes a copy elsewhere), and appends one line to `runs.jsonl` (free space
before and after), which feeds the page's recent runs. A lock file stops two runs from overlapping.

## Files

```
.claude-plugin/plugin.json        manifest
commands/storage-cleanup.md       /storage-cleanup
scripts/storage_cleanup.py        the cleaner (stdlib only)
scripts/test_storage_cleanup.py   tests on throwaway git repositories and a fake GitHub
```

Tests: `python3 -m unittest discover -s scripts -p 'test_*.py'` (54 cases).
