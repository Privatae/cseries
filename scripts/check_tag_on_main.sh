#!/usr/bin/env bash
# check_tag_on_main.sh — the shipped tag must be an ancestor of origin/main.
#
# THE GAP THIS CLOSES.
# The release tags BEFORE the build, so image content equals the tagged
# commit by construction. That is correct and should stay. What it silently
# assumes is that the tag is the LAST commit — which is false on any ship where
# smoke actually finds something.
#
# Every smoke fix is a commit, and `git tag -f` moves the tag without pushing the
# branch. On one release that left SIX commits inside the published image and absent
# from main:
#     workbench bundle repack, the factory-bundle half of it, the research-gate
#     correction, the tool-lane registration fix, and two factory-build fixture
#     fixes.
# It was caught only incidentally, when a deploy's `git pull --ff-only`
# landed on a commit that did not match the tag.
#
# Left alone: the shipped code is not on the branch, anything that follows
# main never sees it, and the NEXT ship builds
# from a tree missing this ship's fixes.
#
# Run it in the release checkout, after the last commit of the release and
# BEFORE anything deploys from the branch.
#
#   scripts/check_tag_on_main.sh v1.2.3
#   scripts/check_tag_on_main.sh v1.2.3 --fix   # push main if it is a fast-forward
#
# Exit 0 = the tag is on main. Exit 1 = it is not, and the commits are listed.
set -uo pipefail

VERSION="${1:-}"
MODE="${2:-}"
BRANCH="${SHIP_BRANCH:-main}"

if [[ -z "$VERSION" ]]; then
    echo "usage: $(basename "$0") <tag> [--fix]" >&2
    exit 2
fi

git rev-parse --git-dir >/dev/null 2>&1 || { echo "not a git repo: $PWD" >&2; exit 2; }

if ! git rev-parse -q --verify "refs/tags/$VERSION" >/dev/null; then
    echo "FAIL  no such tag: $VERSION" >&2
    exit 1
fi

git fetch origin --tags --quiet || echo "warn: git fetch failed; comparing against a possibly stale origin/$BRANCH" >&2

if ! git rev-parse -q --verify "refs/remotes/origin/$BRANCH" >/dev/null; then
    echo "FAIL  no origin/$BRANCH to compare against" >&2
    exit 1
fi

TAG_SHA="$(git rev-list -n1 "$VERSION")"

if git merge-base --is-ancestor "$TAG_SHA" "origin/$BRANCH"; then
    echo "OK    $VERSION ($(git rev-parse --short "$TAG_SHA")) is on origin/$BRANCH"
    exit 0
fi

echo "FAIL  TAG NOT ON BRANCH — $VERSION is not an ancestor of origin/$BRANCH"
echo
echo "      These commits are in the shipped image but NOT on origin/$BRANCH:"
git log --oneline "origin/$BRANCH..$VERSION" | sed 's/^/        /'
echo
echo "      The mirror and code index follow origin/$BRANCH, so they will never"
echo "      see this code, and the next ship builds from a tree without it."

if [[ "$MODE" == "--fix" ]]; then
    echo
    echo "      --fix: attempting a fast-forward push of $BRANCH…"
    # Only ever a fast-forward. If local $BRANCH is not a descendant of the
    # remote, that is a divergence a human has to look at, not something to
    # force past during a ship.
    if ! git merge-base --is-ancestor "origin/$BRANCH" "$BRANCH" 2>/dev/null; then
        echo "      REFUSING: local $BRANCH is not a fast-forward of origin/$BRANCH."
        echo "      Resolve the divergence by hand — never force-push during a ship."
        exit 1
    fi
    if git push origin "$BRANCH"; then
        echo "      pushed. re-checking…"
        git fetch origin --quiet
        if git merge-base --is-ancestor "$TAG_SHA" "origin/$BRANCH"; then
            echo "OK    $VERSION is now on origin/$BRANCH"
            exit 0
        fi
        echo "      still not an ancestor after the push — look at this by hand."
        exit 1
    fi
    echo "      push failed."
    exit 1
fi

echo
echo "      Fix: git push origin $BRANCH   (or re-run with --fix)"
exit 1
