#!/usr/bin/env bash
# Link this repo's Hermes extensions into ~/.hermes/.
#
# Symlinks rather than copies: verified that gateway/hooks.py discovery uses
# Path.is_dir()/exists(), both of which follow symlinks. So edits in the repo
# are live after a gateway restart, with no reinstall step to forget.
#
# Idempotent. Safe to re-run after `hermes update`.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

link() {
    local src="$1" dst="$2"
    [ -e "$src" ] || { echo "  skip (absent): $src"; return; }
    mkdir -p "$(dirname "$dst")"
    ln -sfn "$src" "$dst"
    echo "  linked $(basename "$dst") -> $src"
}

echo "Installing Hermes extensions into $HERMES_HOME:"
for h in "$REPO"/hermes_ext/hooks/*/; do
    [ -d "$h" ] || continue
    link "${h%/}" "$HERMES_HOME/hooks/$(basename "${h%/}")"
done
for p in "$REPO"/hermes_ext/plugins/*/; do
    [ -d "$p" ] || continue
    link "${p%/}" "$HERMES_HOME/plugins/$(basename "${p%/}")"
done

echo
echo "Restart the gateway to load changes:"
echo "  systemctl --user restart hermes-gateway"
