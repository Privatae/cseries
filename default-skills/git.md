---
name: git
description: The git rules every coding harness follows when it commits for a Host — commit in the run's own worktree, never push, never open a pull request; the Host lands the work.
allowed-tools: Bash, gitea
intent-match: false
---

# Git SOP

You are a coding harness working for a Host. Follow these rules for
every commit.

## Account

- Home: the self-hosted Gitea forge this Host is connected to.
- GitHub is a governed READ source only (importing or reading external
  repositories). Never push to GitHub.
- If a command needs auth that isn't loaded, stop and say so — don't
  try to log in.

## Standing rules

These are non-negotiable.

1. **No `Co-Authored-By:` lines.** Never append the model's identity
   to commit messages. Strip the default trailer everywhere.
2. **Never `--no-verify`, `--no-gpg-sign`, or anything that skips
   hooks.** If a pre-commit hook fails, fix the underlying issue and
   create a NEW commit. Never amend.
3. **Never `git commit --amend` on published commits.** If you must
   amend a local-only commit, ask first.
4. **Never push, and never force anything.** The Host lands your work.
5. **Don't use `-i` / interactive flags.** `git rebase -i`,
   `git add -i` — these require a TTY you don't have. Use
   non-interactive equivalents or break the operation into steps.
6. **No destructive ops without explicit authorization in the same
   turn.** `git reset --hard`, `git clean -f`, `git checkout --`,
   `git branch -D`, `rm -rf`. If you see these as the path forward,
   stop and surface why before doing it.
7. **Stage by name, not `git add -A` / `git add .`.** Those sweep up
   `.env`, credentials, large binaries. Be explicit about which paths
   are part of the commit.
8. **Pass commit messages via HEREDOC** so multi-line formatting and
   special chars survive. Example:
   ```bash
   git commit -m "$(cat <<'EOF'
   subject line under 70 chars

   Body paragraph explains the why, not the what.
   EOF
   )"
   ```

## Atomic operations the harness should expose

When the Host asks you to commit, expand to this sequence — one tool call
per step, never one big bash script:

- `git status --porcelain` — see what's actually changed
- `git diff` (+ `git diff --staged` if relevant) — read the diff before
  staging
- `git log --pretty=oneline -5` — match commit-message style of recent
  commits in this repo
- `git add <path>` — staging by name
- `git commit -m "..."` — using the HEREDOC pattern above

If any step fails, stop and report. Don't sweep failures into the next
step.

## Pull requests

You do not push and you do not open pull requests. Commit on your branch in
the run's worktree and report the commit hash and the changed paths. The
Host lands the work: for its own project through `review.adopt` and the
project's Code Approval, and for a repo it does not own (the platform
repo included) through `gitea` op='propose' with `from_project`, which opens the
pull request upstream. Never use `gh`: the forge is Gitea, not GitHub, and
merging is always the person's decision.

## Reporting back

After the work is done, return:
- the commit hash (`git rev-parse HEAD`)
- the changed paths
- a one-line summary of what changed

The host uses these to update its own context. Don't pad the report
with the full diff — the Host reads the commit itself.

## When to refuse

- Asked to bypass a failing hook.
- Asked to commit a file that looks like a secret (`.env`,
  `credentials.json`, anything matching `*key*.pem`, etc.).
- Asked to push, or to operate on a repo outside the run's worktree.

Surface the concern, name the rule, and let the host decide whether
to direct you forward anyway.
