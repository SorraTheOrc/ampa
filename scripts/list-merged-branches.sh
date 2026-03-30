#!/usr/bin/env bash
# scripts/list-merged-branches.sh
# List local branches and conservative "merged into default" status.
# Output: NDJSON (one JSON object per line).
# Dependencies: git
set -euo pipefail

# Configuration
PROTECTED=("main" "master" "develop")
FETCH=true
DEFAULT_FALLBACK="main"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--no-fetch] [--default=<branch>] [--help]
  --no-fetch        Skip 'git fetch origin --prune'
  --default=BR      Use BR as default branch instead of auto-detect
  --help            Show this help
Output: NDJSON lines: {
  "branch": "...",
  "current": true|false,
  "protected": true|false,
  "has_remote": true|false,
  "merged_into_default": true|false,
  "last_commit_sha": "...",
  "last_commit_date": "...",
  "unpushed_commits": N,
  "upstream": "...",
  "work_item_token": "...",
  "work_item_id": "..."
}
EOF
}

# simple json escaper
escape_json() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e ':a;N;s/\n/\\n/g;ta'
}

# detect default branch
detect_default() {
  if [[ -n "${OPT_DEFAULT:-}" ]]; then
    echo "$OPT_DEFAULT"
    return
  fi

  if git remote show origin >/dev/null 2>&1; then
    headbranch=$(git remote show origin 2>/dev/null | awk -F': ' '/HEAD branch/ {print $2; exit}') || true
    if [[ -n "$headbranch" ]]; then
      echo "$headbranch"
      return
    fi
  fi

  if git symbolic-ref refs/remotes/origin/HEAD >/dev/null 2>&1; then
    git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@refs/remotes/origin/@@'
    return
  fi

  echo "$DEFAULT_FALLBACK"
}

# parse work item token like wl-123 or wl123
parse_work_item() {
  local bname="$1"
  # prefer prefix-number (letters then dash then digits)
  if [[ "$bname" =~ ([A-Za-z]+-[0-9]+) ]]; then
    token="${BASH_REMATCH[1]}"
    if [[ "$token" =~ -([0-9]+)$ ]]; then
      echo "$token" "${BASH_REMATCH[1]##*-}"
      return
    fi
    echo "$token" ""
    return
  fi
  # fallback: letters+digits (wl123)
  if [[ "$bname" =~ ([A-Za-z]+[0-9]+) ]]; then
    token="${BASH_REMATCH[1]}"
    if [[ "$token" =~ ([A-Za-z]+)([0-9]+)$ ]]; then
      echo "${BASH_REMATCH[1]}${BASH_REMATCH[2]}" "${BASH_REMATCH[2]}"
      return
    fi
    echo "$token" ""
    return
  fi
  echo "" ""
}

# parse args
OPT_DEFAULT=""
while [[ ${#@} -gt 0 ]]; do
  case "$1" in
    --no-fetch) FETCH=false; shift ;;
    --default=*) OPT_DEFAULT="${1#*=}"; shift ;;
    --help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

# ensure we're in a git repo
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Not a git repository" >&2
  exit 2
fi

# optional fetch
if $FETCH; then
  git fetch origin --prune --quiet || true
fi

DEFAULT_BRANCH=$(detect_default)
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

# ensure we have a ref to compare (try origin/<default>, fallback to local <default>)
if git show-ref --verify --quiet "refs/remotes/origin/${DEFAULT_BRANCH}"; then
  DEFAULT_REF="origin/${DEFAULT_BRANCH}"
elif git show-ref --verify --quiet "refs/heads/${DEFAULT_BRANCH}"; then
  DEFAULT_REF="${DEFAULT_BRANCH}"
else
  echo "Default branch '${DEFAULT_BRANCH}' not found locally or on origin" >&2
  DEFAULT_REF="${DEFAULT_BRANCH}"
fi

# iterate branches
git for-each-ref --format='%(refname:short)' refs/heads/ | while IFS= read -r branch; do
  if [[ -z "$branch" ]]; then
    continue
  fi

  protected=false
  for p in "${PROTECTED[@]}"; do
    if [[ "$branch" == "$p" ]]; then
      protected=true
      break
    fi
  done

  current=false
  if [[ "$branch" == "$CURRENT_BRANCH" ]]; then
    current=true
  fi

  if git show-ref --verify --quiet "refs/remotes/origin/${branch}"; then
    has_remote=true
  else
    has_remote=false
  fi

  last_sha=$(git rev-parse --short "$branch" 2>/dev/null || echo "")
  last_date=$(git log -1 --format='%ci' "$branch" 2>/dev/null || echo "")

  upstream=""
  if git rev-parse --abbrev-ref --symbolic-full-name "${branch}@{u}" >/dev/null 2>&1; then
    upstream=$(git rev-parse --abbrev-ref --symbolic-full-name "${branch}@{u}" 2>/dev/null || echo "")
  fi

  unpushed=0
  if $has_remote; then
    unpushed=$(git rev-list --count "refs/remotes/origin/${branch}..refs/heads/${branch}" 2>/dev/null || echo 0)
  else
    unpushed=$(git rev-list --count "${DEFAULT_REF}..${branch}" 2>/dev/null || echo 0)
  fi

  merged=false
  if git show-ref --verify --quiet "refs/remotes/origin/${DEFAULT_BRANCH}" || git show-ref --verify --quiet "refs/heads/${DEFAULT_BRANCH}"; then
    if git merge-base --is-ancestor "refs/heads/${branch}" "${DEFAULT_REF}" >/dev/null 2>&1; then
      merged=true
    fi
  fi

  work_item_token=""; work_item_id=""
  read -r work_item_token work_item_id < <(parse_work_item "$branch")

  b_escaped=$(escape_json "$branch")
  u_escaped=$(escape_json "$upstream")
  lt_escaped=$(escape_json "$last_sha")
  ld_escaped=$(escape_json "$last_date")
  bt_escaped=$(escape_json "$work_item_token")
  bid_escaped=$(escape_json "$work_item_id")

  printf '{'
  printf '"branch":"%s",' "$b_escaped"
  printf '"current":%s,' "$([[ "$current" == true ]] && echo true || echo false)"
  printf '"protected":%s,' "$([[ "$protected" == true ]] && echo true || echo false)"
  printf '"has_remote":%s,' "$([[ "$has_remote" == true ]] && echo true || echo false)"
  printf '"merged_into_default":%s,' "$([[ "$merged" == true ]] && echo true || echo false)"
  printf '"last_commit_sha":"%s",' "$lt_escaped"
  printf '"last_commit_date":"%s",' "$ld_escaped"
  printf '"unpushed_commits":%s,' "$unpushed"
  printf '"upstream":"%s",' "$u_escaped"
  printf '"work_item_token":"%s",' "$bt_escaped"
  printf '"work_item_id":"%s"' "$bid_escaped"
  printf '}\n'

done
