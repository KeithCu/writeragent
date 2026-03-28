#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 https://github.com/<owner>/<repo>/pull/<number>" >&2
  exit 1
fi

PR_URL="$1"

# Extract owner, repo, and PR number from the URL
if [[ "$PR_URL" =~ github\.com/([^/]+)/([^/]+)/pull/([0-9]+) ]]; then
  OWNER="${BASH_REMATCH[1]}"
  REPO="${BASH_REMATCH[2]}"
  PR_NUMBER="${BASH_REMATCH[3]}"
else
  echo "Error: URL must look like https://github.com/<owner>/<repo>/pull/<number>" >&2
  exit 1
fi

echo "Applying PR #${PR_NUMBER} from ${OWNER}/${REPO} onto current branch (squashed diff, no commit)..."

# Ensure we are in a git repository
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: This script must be run inside a git repository." >&2
  exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "Current branch: ${CURRENT_BRANCH}"

# One-off fetch from the PR's repository (does not modify git config); -f overwrites stale pr-N-temp
TMP_BRANCH="pr-${PR_NUMBER}-temp"
FETCH_REF="pull/${PR_NUMBER}/head:${TMP_BRANCH}"

echo "Fetching PR head into local branch ${TMP_BRANCH} (force-updating ref)..."
git fetch -f "https://github.com/${OWNER}/${REPO}.git" "${FETCH_REF}"

BASE="$(git merge-base HEAD "${TMP_BRANCH}")"
TMP_REV="$(git rev-parse "${TMP_BRANCH}")"
COMMIT_COUNT="$(git rev-list --count "${BASE}".."${TMP_BRANCH}")"

echo "Merge-base: ${BASE}"
echo "Commits on ${TMP_BRANCH} since merge-base: ${COMMIT_COUNT}"
echo "Diff vs PR head (stat):"
git diff --stat HEAD "${TMP_BRANCH}"
echo

# PR tip already contained in current branch: merging would be a no-op; do not report false success
if git merge-base --is-ancestor "${TMP_REV}" HEAD; then
  echo "PR head (${TMP_REV:0:7}) is already contained in the current branch; nothing to apply." >&2
  exit 1
fi

echo "Squash-applying ${TMP_BRANCH} onto ${CURRENT_BRANCH} (--squash --no-commit; PR commits are not recorded)..."
set +e
git merge --squash --no-commit "${TMP_BRANCH}"
MERGE_STATUS=$?
set -e

if [[ ${MERGE_STATUS} -eq 0 ]]; then
  echo "Successfully applied PR #${PR_NUMBER} as a single squashed diff (staged, not committed)."
  echo "Changes are staged; use 'git reset HEAD' to unstage while keeping edits. Test, then commit once or discard."
  exit 0
fi

if [[ ${MERGE_STATUS} -eq 1 ]]; then
  echo "Squash apply stopped due to conflicts." >&2
  echo "Resolve conflicted files, then: git add <paths>" >&2
  echo "To finish without recording PR commits: git commit (one squashed commit)." >&2
  echo "Or abort: git merge --abort" >&2
  exit 1
fi

echo "Squash apply failed (exit ${MERGE_STATUS}). See 'git status'." >&2
exit "${MERGE_STATUS}"
