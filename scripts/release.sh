#!/usr/bin/env bash
# Cut a passe-partout release.
#
# Usage: ./scripts/release.sh 0.4.3
#
# Steps:
#   1. Validate the version string (MAJOR.MINOR.PATCH).
#   2. Refuse if working tree dirty, not on master, or master diverges from origin.
#   3. Bump [project] version in pyproject.toml.
#   4. Regenerate uv.lock so it reflects the new version.
#   5. If the major.minor changed, update README's hardcoded Docker tag examples
#      (the `ghcr.io/jfim/passe-partout:M.N` references) to the new M.N. Patch
#      bumps don't touch the README — the M.N tag already auto-floats to track
#      patches via docker.yml's `{{major}}.{{minor}}` metadata pattern.
#   6. Run the full test suite (no smoke).
#   7. Commit `release: v<VERSION>` and tag `v<VERSION>`.
#   8. Push master + tags. The docker.yml workflow picks up the tag and
#      publishes ghcr.io/jfim/passe-partout:<VERSION> and :<MAJOR>.<MINOR>.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <version>  (e.g. $0 0.4.3)" >&2
    exit 2
fi

VERSION="$1"

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "error: version must be MAJOR.MINOR.PATCH (got '$VERSION')" >&2
    exit 2
fi

MAJOR_MINOR="${VERSION%.*}"

# Working tree must be clean — release commits modify pyproject.toml and uv.lock,
# we don't want to drag unrelated changes along.
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "error: working tree has uncommitted changes" >&2
    git status --short >&2
    exit 1
fi

# Must be on master so the tag points at a master commit, not some random branch.
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$BRANCH" != "master" ]]; then
    echo "error: must be on master to release (currently on '$BRANCH')" >&2
    exit 1
fi

# Master must match origin/master — otherwise we'd tag a commit nobody else can
# see, or miss commits that already landed upstream.
git fetch origin master --quiet
if [[ "$(git rev-parse master)" != "$(git rev-parse origin/master)" ]]; then
    echo "error: local master diverges from origin/master; pull or push first" >&2
    exit 1
fi

# Version must not collide with an existing tag.
if git rev-parse "v$VERSION" >/dev/null 2>&1; then
    echo "error: tag v$VERSION already exists" >&2
    exit 1
fi

# Capture the previous major.minor so we know whether to rewrite README.
PREV_VERSION=$(awk -F'"' '/^version = "/ { print $2; exit }' pyproject.toml)
PREV_MAJOR_MINOR="${PREV_VERSION%.*}"

echo "=> Releasing $PREV_VERSION -> $VERSION"

# 1. pyproject.toml version bump.
sed -i.bak -E "s/^(version = )\"[^\"]+\"/\1\"$VERSION\"/" pyproject.toml
rm pyproject.toml.bak

# 2. uv.lock refresh (the [package] entry for passe-partout carries the version
# string, and uv lock --no-sync only updates the lockfile, not the venv).
uv lock --quiet

# 3. README Docker tag rewrite — only when the major.minor floated. Patch
# releases don't touch the README because :M.N already tracks them.
if [[ "$MAJOR_MINOR" != "$PREV_MAJOR_MINOR" ]]; then
    echo "   major.minor changed ($PREV_MAJOR_MINOR -> $MAJOR_MINOR); rewriting README"
    # Match `:M.N` only when followed by end-of-line or a non-digit non-dot
    # character. The trailing capture is preserved so we don't eat the next
    # token. \b is not enough: `0.4\b` matches inside `0.4.2` because `.` is
    # non-word. Escape dots in the previous M.N so a literal `.` doesn't
    # become a regex wildcard.
    prev_escaped=${PREV_MAJOR_MINOR//./\\.}
    # `#` as the sed delimiter so the alternation pipe in the regex doesn't
    # collide. Pipes are sed's expression separator under `s|...|...|` form.
    sed -i.bak \
        -E "s#(ghcr\\.io/jfim/passe-partout:)$prev_escaped(\$|[^0-9.])#\\1$MAJOR_MINOR\\2#g" \
        README.md
    rm README.md.bak
fi

# 4. Tests must pass at the released commit. Slow but worth it — a broken
# release is worse than a slow `just release`.
echo "=> Running tests"
uv run pytest

# 5. Commit + tag + push.
git add pyproject.toml uv.lock README.md
git commit -m "release: v$VERSION"
git tag -a "v$VERSION" -m "Release v$VERSION"
echo "=> Pushing master and v$VERSION"
git push origin master "v$VERSION"

echo
echo "Released v$VERSION."
echo "Docker image will be published by .github/workflows/docker.yml as:"
echo "  ghcr.io/jfim/passe-partout:$VERSION"
echo "  ghcr.io/jfim/passe-partout:$MAJOR_MINOR"
echo "  ghcr.io/jfim/passe-partout:latest"
