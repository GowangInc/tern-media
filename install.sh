#!/bin/sh
set -eu

PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v tern >/dev/null 2>&1 || { echo "Tern CLI is required." >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || {
    echo "Python 3.10 or newer is required." >&2
    exit 1
}

EXTENSIONS="$HOME/.omp/agent/extensions"
TARGET="$EXTENSIONS/tern-media.ts"
mkdir -p "$EXTENSIONS"
if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    if [ ! -L "$TARGET" ] || [ "$(readlink "$TARGET")" != "$PLUGIN_DIR/omp-extension.ts" ]; then
        echo "Refusing an OMP extension from another installation: $TARGET" >&2
        exit 1
    fi
else
    ln -s "$PLUGIN_DIR/omp-extension.ts" "$TARGET"
fi

tern plugin link "$PLUGIN_DIR"
echo "Installed Session Media. Use Media: Open session gallery in Tern's command palette."
echo "Reload extensions or start a new OMP session to enable automatic media registration."
