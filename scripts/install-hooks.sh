#!/bin/sh
# Install this repo's git hooks. Run once after cloning:
#
#     sh scripts/install-hooks.sh
#
# Hooks are not versioned by git, which is exactly why this exists: nothing
# installs them for you, and the scan that keeps private data out of a public
# repo has to be armed on every clone.
#
# It copies rather than symlinks, so a hook keeps working across a checkout,
# and it refuses to clobber a hook you wrote yourself unless you pass --force.
set -e

force=0
case "$1" in
    --force) force=1 ;;
    '')      ;;
    *)       echo "usage: sh scripts/install-hooks.sh [--force]" >&2; exit 2 ;;
esac

cd "$(git rev-parse --show-toplevel)"
hooks_dir=$(git rev-parse --git-path hooks)
mkdir -p "$hooks_dir"

installed=0
for name in pre-commit commit-msg pre-push; do
    src="scripts/hooks/$name"
    dst="$hooks_dir/$name"
    if [ ! -f "$src" ]; then
        echo "install-hooks: missing $src" >&2
        exit 1
    fi
    if [ -e "$dst" ] && [ "$force" = 0 ] && ! cmp -s "$src" "$dst"; then
        echo "install-hooks: $name differs from the shipped one — leaving it."
        echo "               Re-run with --force to overwrite, or merge by hand:"
        echo "                 diff $dst $src"
        continue
    fi
    cp "$src" "$dst"
    chmod +x "$dst"
    installed=$((installed + 1))
done

echo "install-hooks: $installed hook(s) installed into $hooks_dir"
echo ""
echo "Verify with:  python3 scripts/scrub_check.py --self-test"
