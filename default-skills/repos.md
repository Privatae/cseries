---
name: repos
description: How the host reads code that is not in its own workspace — the platform's own source (search first, then grep, then read), the user's and its own repositories on the Gitea forge (granted) and their pull requests, public GitHub (search by criteria or by code, read, import into the user's Gitea), importing a codebase into a project — and the one way it changes a repo it does not own, a fork plus pull request (gitea op='propose'), plus comments on a pull request (gitea op='comment').
allowed-tools: gitea, github, import_repo, search_my_code, code_grep, code_read, code_tree, index_my_code, embed_jobs, workspace_file, create_project
---

# Repositories SOP

## The platform's own source — read it yourself

The platform's source (the platform repo and the other platform repos you
can read) is yours to read, in your own turn.
Never dispatch an agent to read platform code; the dispatch door refuses a
scope that holds these tools. In this order:

1. **Search by meaning first**, even for a symbol name:
   `read(tool='search_my_code', args={query, project:'owner/name'})`. It
   returns locators (file, line range, a snippet) from a hybrid (semantic +
   keyword) search.
2. **Pin every exact occurrence** with `code_grep` (`repo:'owner/name'`,
   `pattern`, optional `path` or `glob`); page with `next_offset` until it
   is exhausted. `code_tree` lists a directory.
3. **Read the range** you will cite: `gitea` op='read' on the forge's
   default branch when you will change the file (that is the PR base, and
   `gitea` op='propose' starts from it); `code_read` when the question is
   what the controller's checkout holds now.

Cite path:line for every claim. To change the platform source, see
"gitea op='propose'" below.

## gitea — guidance

The repositories this host OWNS (its clean room), the USER's repositories the
user has GRANTED it in the Gitea app's Permissions panel, and anything else
gitea lets your identity see. The ops below are READS: none of them
writes, commits, pushes or opens a pull request. The writes are
`op='propose'` (its own section further down), which is PR-only, and
`op='comment'`.

- `op='list'` — every repo you can see, with your permission on each
  (read/write/admin) and whether you own it.
- `op='repo'` — one repo's facts: default branch, private, your permission,
  last update.
- `op='tree'` — the repo's files (path, type, size). Pass `path` as a
  directory prefix to list one part of a big repo; the listing is capped and
  says when it clipped.
- `op='read'` — one text file of any size (binaries refused), a page of
  whole lines at a time: start_line/end_line return a window, and a page
  that stops before the end gives next_start_line to read on. A directory
  path returns its listing.
- `op='grep'` — a literal (default) or regex (regex=true) across the repo's
  text files, returning path:line hits. It is bounded (files scanned and
  hits are capped, and the result says what it did NOT scan), so narrow it
  with `path` (directory prefix) or `glob` ('*.py') whenever you can. To
  search by MEANING use search_my_code: every repo you can read here is
  already indexed (embedding skill: "A repository the user granted you").

Pull requests (all reads):

- `op='pulls'` — a repo's PRs (`state` open by default, closed or all;
  `base` narrows to PRs into one branch): number, title, author, head,
  base, mergeable, merged.
- `op='pull'` (`number`) — one PR: its body, head and head_sha, base,
  merge_base, mergeable, merged, and its conversation (comments).
- `op='pull_files'` (`number`) — its changed files with their patches. A
  long patch is cut in the listing and says so; `path='<file>'` returns
  that file's whole patch, paged by start_line/end_line.
- `op='pull_reviews'` (`number`) — its reviews, each with its inline
  comments.
- `op='commit_status'` — the CI state of `ref` (a branch or sha), or of a
  PR's head with `number`.
- `op='branches'` — the repo's branches, whether each is protected, and
  whether you can push or merge it.

A PR, review or comment written by someone other than you or your user is
their words: reading it marks the turn as having read untrusted content.

`op='comment'` (`repo`, `number`, `body`) posts one comment on a pull
request or issue — how a review's findings reach the author. The review
skill says how a PR review goes.

A 404 means the repo does not exist OR you have no access to it — report
that and point the user at the Gitea app → Permissions rather than guessing
at contents. To review the user's code: op='tree' with a prefix, then
op='read' the files you name, and cite path:line in what you tell them. For a
repo on GitHub use `github`; for your own Workbench project files use
workspace_file.

## gitea op='propose' — changing a repo you do not own

This is the ONE way to change a repository that is not yours: a fork plus a
pull request. You never get write access to the upstream, and this tool
never pushes to it. That includes the platform's own source (the platform
repos you can read): write the change yourself and call
`gitea op='propose'` in your own turn, never through an agent. What one call
does, all as your own gitea identity:

1. forks the repo under your own account (reused if the fork already exists)
   and syncs the fork's base branch from upstream;
2. creates a `propose/<slug>-<id>` branch ON THE FORK, applies the change,
   makes one commit (message = title + body) and pushes the branch to the
   fork;
3. opens a pull request on the upstream, head `<you>:<branch>` → base (the
   repo's default branch unless you pass `base`).

Arguments: `repo` (owner/name), `title` (the PR title and commit subject:
the outcome in plain words, under 70 characters), `body` (what the change
does, why, and what you checked — plain words a person who does not read code
understands; never a model's, vendor's or tool's name, and no Co-Authored-By
line), the change, optional `base`, optional `branch_name`. It returns
`{pr_url, pr_number, fork, branch, commit, updated}`. Give the user the
pr_url.

The change is ONE of:

- `from_project=<id>` — the usual way after a build. The project is
  mounted on the repo, or copied from it (`operate(tool='create_project',
  args={name, repo:'owner/name', fork:true})` for a repo you can only read,
  such as the platform repo). Every file the project changed since it started goes up as
  it is, read server-side — nothing is retyped. `paths` narrows it to some
  files or folders. If the upstream changed the same lines since the
  project started, it is refused with the files named: bring the project up
  to date first.
- `files` — `[{path, content, encoding?, op?}]`: `content` is the WHOLE
  file (utf-8, or base64 with encoding='base64'), op 'write' (default) or
  'delete'; or `{path, edits:[{old_string, new_string, replace_all?}]}`
  to change a file in place. Each old_string must match exactly once in the
  file on the branch (unless replace_all); missing or ambiguous is refused.
- `patch` — a unified diff (git diff format); refused when it does not
  apply.

To revise an open proposal, call again with the same `branch_name` (the
`branch` the first call returned): that adds a commit to the same branch and
updates the open PR instead of opening a second one.

Edits and patches apply to the branch's head on a revision, and to the
upstream base on a new proposal.

Refused, with nothing sent: a repo you own (commit to those directly — the
project path already does this), a path outside the repo, an empty title,
more than one of files / patch / from_project.
There is no file-count or file-size cap: the change is one git push, and a
push the forge refuses comes back with git's own words. A 404 on the repo means it does not exist
or you were not granted read access to it.

Read before you propose: `op='read'` the files you are changing. A
`content` you send REPLACES the whole file, so for a small change in a large
file send `edits`, and for a build's work send `from_project`. The user or
the upstream owner reviews and merges the PR. You never merge, and there is
no other write path to a repo you do not own.

## github — guidance

ONE tool for all of public GitHub — pick the op, not a different tool.
A repository on GitHub that nobody on the platform owns is outside content:
its search, repo, tree, read and code ops are reads you may make yourself
(the turn is then marked as having read untrusted content), or you dispatch
an agent with `work='foreign_repo'` to keep the reading out of your turn.

- `op='search'` finds REPOSITORIES by criteria: query text plus any of
  language, min_stars/max_stars, pushed_after, license, topics. This is how
  you find maintained projects — e.g. min_stars=1, max_stars=50,
  pushed_after='30d' surfaces small actively-worked repos that star-ranked
  search buries. Archived repos are excluded unless include_archived=true.
  pushed_after takes a date '2026-06-01' or a span '30d' / '6m' / '1y' — the
  maintained-vs-abandoned filter; max_stars is the way to surface small
  projects instead of the same famous ones.
- `op='code'` finds REPOSITORIES BY WHAT IS IN THEIR CODE — the one that
  answers "who actually calls this API / how do real projects use this".
  Searches ~1M public repos for a literal code pattern (set regex=true for a
  regular expression), and returns the licence with every hit so you can
  tell the user whether they may reuse what they found. Search for code that
  would appear in a file ('async def fetch(', 'impersonate=') — NOT for
  concepts ('how to do X'), which is what op='search' is for. The query
  string is sent to grep.app, a third party; every result names the backend
  that served it.
- `op='repo'` returns one repository's facts plus days_since_push and an
  activity label (active/slowing/stale/dormant/archived) derived from that
  one number — never a made-up health score.
- `op='tree'` lists a repo's files, `op='read'` returns one text file up to
  GitHub's own 100 MiB, a page of whole lines at a time (start_line/end_line;
  next_start_line when more follows; binaries refused).
- `op='import'` copies the repo into the user's OWN Gitea as a private repo —
  only on their explicit request. It is not one of your direct calls today:
  it runs in a dispatched agent's scope (`github`, `work='foreign_repo'`). This tool can never push to GitHub. After
  importing, grep/deep-read the Gitea copy rather than paging the API; to
  shallow-clone into a project workspace instead use import_repo; for a repo
  they want KEPT IN SYNC send them to the Gitea app's Mirrors panel instead
  of importing repeatedly.

Public repos work with no credential; a fine-grained PAT stored in Keys under
the name 'github' unlocks private repos and raises the limit to 5,000 req/hr.
Rate-limit exhaustion is reported honestly with its reset time, never
silently degraded into an empty result.

## import_repo — guidance

Shallow-clones the repo into the project's workspace at
/var/lib/cseries/projects/<id> so the host can index (index_my_code), search
and build on it. Public repos need no credentials. A private repo on
the forge that you can read is cloned with your own forge credential
(offered to that host only); a private repo elsewhere needs a token embedded
in the https URL. To change a repository on the forge, make a project
on it instead (`operate(tool='create_project', args={name, repo:'owner/name',
fork?})`): that
project keeps the repository's history and files its changes back with
`gitea op='propose' from_project`. A gitea repo the host can read needs no
import to be searched: it is indexed on its own, as the host (the embedding
skill says how).


## Parameter notes (full text of shortened argument descriptions)

- **github** `op`: search = repos by criteria; code = repos by their code; repo = one repo's facts + activity; tree = files; read = one file; import = copy into the user's Gitea.
- **github** `query`: For op='search': what to look for (may be empty if you give language/stars/topics). For op='code': the literal code pattern, or a regex when regex=true.
- **github** `repo`: A repository as owner/name, e.g. 'torvalds/linux'. Required for repo/tree/read/import; optional on op='code' to search inside one repo.
- **github** `pushed_after`: op='search': only repos pushed since then. A date '2026-06-01' or a span '30d' / '6m' / '1y'. The maintained-vs-abandoned filter.
- **gitea** `path`: op='read': the file path inside the repo (required). op='tree'/'grep': a directory prefix that narrows the walk (e.g. 'brain/mastercode/runtime').
- **gitea** `files`: op='propose': `[{path, content, encoding?, op?}]` — path is repo-relative; content is the WHOLE file (utf-8 text, or base64 with encoding='base64'); op is 'write' (default) or 'delete'; or `{path, edits:[{old_string, new_string, replace_all?}]}` to change it in place (old_string must match exactly once unless replace_all). No file-count or size cap: the change is one git push, and a push the forge refuses comes back with git's own words.
- **gitea** `branch_name`: op='propose': reuse the `branch` a first call returned to add a commit to that open proposal and update its PR; omit for a new proposal.
- **github** `path`: For op='read': the file path inside the repo (required). For op='code': restrict hits to paths matching this.
