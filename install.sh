#!/bin/sh
# Install umbriel-session locally (no root) or system-wide.
set -eu

MODE="user"
if [ "${1:-}" = "--system" ]; then
    MODE="system"
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

if [ "$MODE" = "system" ]; then
    SITE="/usr/lib/python${PYVER}/site-packages"
    BIN="/usr/bin"
    echo "Installing system-wide (requires root)..."
else
    SITE="$HOME/.local/lib/python${PYVER}/site-packages"
    BIN="$HOME/.local/bin"
    mkdir -p "$SITE" "$BIN"
    echo "Installing user-local (no root)..."
fi

# Copy package
rm -rf "$SITE/umbriel_session"
cp -r "$SRC_DIR/src/umbriel_session" "$SITE/umbriel_session"

# Create entry point
cat > "$BIN/umbriel-session" <<'ENTRY'
#!/bin/sh
exec /usr/bin/python3 -m umbriel_session.cli "$@"
ENTRY
chmod 755 "$BIN/umbriel-session"

echo "Installed umbriel-session to $BIN/umbriel-session"
echo "Python module: $SITE/umbriel_session"
