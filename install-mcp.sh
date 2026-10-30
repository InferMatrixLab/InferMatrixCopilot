#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# A dry run must not install uv, create environments, or contact the network.
for option in "$@"; do
    if [ "$option" = "--dry-run" ]; then
        if command -v python3 >/dev/null 2>&1; then
            exec python3 "$project_root/scripts/install_mcp.py" "$@"
        fi
        echo "Dry run requires Python 3.11 or later; no changes were made." >&2
        exit 1
    fi
done

if ! command -v uv >/dev/null 2>&1; then
    echo "Installing the small uv runtime..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
    export PATH
fi

exec uv run --no-project "$project_root/scripts/install_mcp.py" "$@"
