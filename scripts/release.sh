#!/usr/bin/env bash
# ============================================================
#  release.sh - one-command version cut-off on devel
#
#  What it does (in order):
#    1. Sanity guards: must be on clean, synced devel
#    2. Bump the version (pyproject.toml + uv.lock via `uv version`)
#    3. Finalize CHANGELOG.md: rename [Unreleased] -> [X.Y.Z] - today,
#       prepend a fresh empty [Unreleased] section; refuses to release
#       when [Unreleased] has no content
#    4. Commit "chore(release): vX.Y.Z" and push devel
#    5. Open a devel -> master release PR (via gh CLI if available,
#       otherwise print the compare URL)
#
#  What it deliberately does NOT do:
#    - merge the PR (that stays with you + branch ruleset)
#    - tag / GitHub Release / devel sync-back: automated after the merge by
#      .github/workflows/release.yml (it fires on every push to master)
#
#  Usage:
#    scripts/release.sh 0.2.0            # explicit version
#    scripts/release.sh --bump minor     # auto-increment (patch/minor/major)
#    scripts/release.sh --bump minor --dry-run   # preview, no git changes
#
#  Full release flow (human part):
#    1. scripts/release.sh --bump minor
#    2. review & merge the PR on GitHub (checks must pass)
#    3. done - the release workflow tags, publishes and syncs devel
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

DRY_RUN=0
VER_ARG=""
LEVEL=""
usage() {
  echo "usage: scripts/release.sh <X.Y.Z|--bump patch|minor|major> [--dry-run]" >&2
}
args=("$@")
i=0
while [[ $i -lt ${#args[@]} ]]; do
  a="${args[$i]}"
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    --bump)    # "--bump minor" (two tokens)
      LEVEL="${args[$((i + 1))]:-}"
      [[ -n "$LEVEL" ]] || { usage; exit 1; }
      i=$((i + 1)) ;;
    --bump=*)  # "--bump=minor" (one token)
      LEVEL="${a#--bump=}" ;;
    *)
      [[ -z "$VER_ARG" ]] || { usage; exit 1; }
      VER_ARG="$a" ;;
  esac
  i=$((i + 1))
done
[[ -n "$VER_ARG" || -n "$LEVEL" ]] || { usage; exit 1; }

# ---- guards: right branch, synced with remote ----
# (clean-tree is only required for a real run: a dry run mutates nothing,
#  and the script file itself may still be untracked on first use)
[[ "$(git branch --show-current)" == "devel" ]] || {
  echo "[error] release cuts must be made on the devel branch" >&2; exit 1; }
command -v uv >/dev/null 2>&1 || {
  echo "[error] uv not found" >&2; exit 1; }
if timeout 60 git fetch origin -q 2>/dev/null; then
  [[ "$(git rev-parse devel)" == "$(git rev-parse origin/devel)" ]] || {
    echo "[error] devel is out of sync with origin, push/pull first" >&2
    exit 1; }
else
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "[warn] origin unreachable, skipping the sync check" >&2
  else
    echo "[error] origin unreachable, cannot verify sync state" >&2; exit 1
  fi
fi
if [[ "$DRY_RUN" != 1 ]]; then
  [[ -z "$(git status --porcelain)" ]] || {
    echo "[error] working tree is not clean" >&2; exit 1; }
fi

# ---- resolve the new version number ----
CUR_VER="$(uv version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
bump_level() {  # $1 = level, $2 = current X.Y.Z
  IFS='.' read -r MA MI PA <<< "$2"
  case "$1" in
    major) echo "$((MA + 1)).0.0" ;;
    minor) echo "$MA.$((MI + 1)).0" ;;
    patch) echo "$MA.$MI.$((PA + 1))" ;;
    *)     echo "[error] bump level must be patch|minor|major" >&2; return 1 ;;
  esac
}
if [[ -n "$LEVEL" ]]; then
  NEW_VER="$(bump_level "$LEVEL" "$CUR_VER")"
else
  [[ "$VER_ARG" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "[error] version must look like X.Y.Z" >&2; exit 1; }
  NEW_VER="$VER_ARG"
fi
[[ "$NEW_VER" != "$CUR_VER" ]] || {
  echo "[error] new version equals current version ($CUR_VER)" >&2; exit 1; }
echo "version: $CUR_VER -> $NEW_VER${DRY_RUN:+ (dry run)}"

# ---- finalize CHANGELOG.md ----
# Rename the [Unreleased] section to [NEW_VER] - today and prepend a
# fresh empty [Unreleased]. Refuse to release with empty notes.
TODAY="$(date +%F)"
finalize_changelog() {  # $1 = file, $2 = version, $3 = date
  python3 - "$1" "$2" "$3" <<'PYEOF'
import re, sys, pathlib
target, ver, today = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(target)
t = p.read_text(encoding="utf-8")
m = re.search(r"## \[Unreleased\]\n(.*?)(?=^## )", t, re.S | re.M)
if not m:
    sys.exit("[error] CHANGELOG.md has no [Unreleased] section")
if not m.group(1).strip():
    sys.exit("[error] [Unreleased] is empty, there is nothing to release")
t = t.replace("## [Unreleased]",
              f"## [Unreleased]\n\n## [{ver}] - {today}", 1)
p.write_text(t, encoding="utf-8")
print("[ok] changelog finalized")
PYEOF
}
if [[ "$DRY_RUN" == 1 ]]; then
  cp CHANGELOG.md /tmp/keyprism-changelog-preview.md
  finalize_changelog /tmp/keyprism-changelog-preview.md "$NEW_VER" "$TODAY"
  echo "--- preview of the new section head ---"
  grep -A 3 "^## \[$NEW_VER\]" /tmp/keyprism-changelog-preview.md | head -4
  echo "[dry-run] no files were modified, nothing committed"
  exit 0
fi

finalize_changelog CHANGELOG.md "$NEW_VER" "$TODAY"

# ---- write version, commit, push ----
uv version "$NEW_VER" >/dev/null
git add pyproject.toml uv.lock CHANGELOG.md
git commit -m "chore(release): v${NEW_VER}"
git push origin devel

# ---- open the release PR ----
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  gh pr create --base master --head devel \
    --title "release v${NEW_VER}" \
    --body "Version cut-off. Merge with **Create a merge commit** (NOT squash - squash breaks the devel lineage and re-creates doc conflicts). After merging, the release workflow tags v${NEW_VER}, publishes the GitHub Release and syncs master back into devel."
else
  echo "open the PR manually: https://github.com/Fillianore/keyprism/compare/master...devel"
fi
echo "done: v${NEW_VER} - merge the PR on GitHub (merge commit, not squash), automation finishes the rest"
