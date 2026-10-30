#!/usr/bin/env bash
# One-command setup for infermatrix-copilot.
#
#   bash install.sh              install (idempotent)
#   bash install.sh --uninstall  remove ONLY what this installer created
#
# Managed installation is the default. Use --development for the editable
# checkout installer, or --uninstall to remove only its legacy local artifacts.
set -euo pipefail
cd "$(dirname "$0")"

MANIFEST=".install-manifest"
WRAPPER="./infermatrix-copilot"

if [[ "${1:-}" == "--uninstall" ]]; then
    # remove only installer-created artifacts, recorded in the manifest
    if [[ -f "$MANIFEST" ]]; then
        while IFS= read -r item; do
            [[ -e "$item" ]] && rm -rf "$item" && echo "removed $item"
        done < "$MANIFEST"
        rm -f "$MANIFEST"
    else
        echo "no $MANIFEST — nothing this installer created to remove"
    fi
    echo "note: .env, managed installations, and host registrations were NOT touched"
    exit 0
fi

# Managed CLI and MCP hosts share the same independent launcher. The legacy
# editable path remains available for contributors.
if [[ "${1:-}" != "--development" ]]; then
    exec ./install-mcp.sh --cli-only --local-cli-wrapper "$PWD/infermatrix-copilot" "$@"
fi
shift

created=()

# 1. venv: reuse an active one, else create ./.venv
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    VENV="$VIRTUAL_ENV"
    echo "using active virtualenv: $VENV"
elif [[ -d .venv && -x .venv/bin/pip ]]; then
    VENV="$PWD/.venv"
    echo "reusing existing ./.venv"
else
    # newest available interpreter; a failed creation (missing python3-venv)
    # is cleaned up and reported with the exact fix, not left half-made
    PY="${OMNI_PYTHON:-}"
    if [[ -z "$PY" ]]; then
        for cand in python3.12 python3.11 python3; do
            command -v "$cand" >/dev/null && PY="$cand" && break
        done
    fi
    if ! "$PY" -m venv .venv 2>/tmp/omni-venv-err || [[ ! -x .venv/bin/pip ]]; then
        rm -rf .venv
        echo "✗ could not create a virtualenv with $PY:"
        sed -n 1,4p /tmp/omni-venv-err 2>/dev/null || true
        echo "  fix: install the venv module (e.g. sudo apt install ${PY}-venv),"
        echo "  or activate an existing venv / set OMNI_PYTHON and re-run."
        exit 1
    fi
    VENV="$PWD/.venv"
    created+=(".venv")
    echo "created ./.venv with $PY"
fi

# 2. install the package (editable)
"$VENV/bin/pip" install -q -e . || {
    echo "✗ pip install failed — see output above; fix and re-run bash install.sh"
    exit 1
}
echo "installed infermatrix-copilot into the venv"

# 3. seed .env (never overwrite)
if [[ ! -f .env ]]; then
    cp .env.template .env
    echo "created .env from template — EDIT IT: set ANTHROPIC_API_KEY (and"
    echo "  REPO_PATHS if your checkouts are elsewhere). Values are never printed."
else
    echo ".env already present — left untouched"
fi

# 4. repo-local wrapper (invokable immediately; PATH is never modified)
cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/infermatrix-copilot" "\$@"
EOF
chmod +x "$WRAPPER"
created+=("$WRAPPER")
echo "wrote wrapper $WRAPPER"

# 5. record what we created, then diagnose
printf '%s\n' "${created[@]}" > "$MANIFEST"

echo
"$VENV/bin/infermatrix-copilot" doctor || true

cat <<'EOF'

next steps:
  ./infermatrix-copilot                          # conversational chat
  ./infermatrix-copilot -p "review pr 5134"      # one-shot
  optional: ln -s "$PWD/infermatrix-copilot" ~/.local/bin/infermatrix-copilot
EOF
