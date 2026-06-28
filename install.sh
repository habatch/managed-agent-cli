#!/usr/bin/env bash
# Managed Agent CLI installer.
#
# Generates a launcher (the wrapper) wired to your config and drops it on PATH.
# Everything is configurable via env vars; missing ones use sensible defaults.
#
#   CMD_NAME        launcher command name            (default: agent)
#   BIN_DIR         where to install the launcher     (default: ~/.local/bin)
#   AGENT_CLI_HOME  config directory                  (default: ~/.config/managed-agent-cli)
#   ENV_FILE        file that exports ANTHROPIC_API_KEY (default: $AGENT_CLI_HOME/anthropic.env)
#   PYTHON_RUNNER   how to run python                 (default: python3)
#   PASSPHRASE      optional launch passphrase        (default: empty = none)
#
# Example:
#   CMD_NAME=mybuddy PASSPHRASE="open sesame" ./install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CMD_NAME="${CMD_NAME:-agent}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
AGENT_CLI_HOME="${AGENT_CLI_HOME:-$HOME/.config/managed-agent-cli}"
ENV_FILE="${ENV_FILE:-$AGENT_CLI_HOME/anthropic.env}"
PYTHON_RUNNER="${PYTHON_RUNNER:-python3}"
PASSPHRASE="${PASSPHRASE:-}"

mkdir -p "$AGENT_CLI_HOME" "$BIN_DIR"
chmod 700 "$AGENT_CLI_HOME"

# Seed the env file if it does not exist (user must paste their key).
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<'EOF'
# Paste your Anthropic API key below (get one at console.anthropic.com).
export ANTHROPIC_API_KEY="sk-ant-REPLACE_ME"
EOF
    chmod 600 "$ENV_FILE"
    echo "→ created $ENV_FILE — edit it and paste your ANTHROPIC_API_KEY"
fi

# Generate the launcher from the template, escaping for safe inline substitution.
esc() { printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'; }
sed \
    -e "s/@@APP_DIR@@/$(esc "$APP_DIR")/g" \
    -e "s/@@ENV_FILE@@/$(esc "$ENV_FILE")/g" \
    -e "s/@@PYTHON_RUNNER@@/$(esc "$PYTHON_RUNNER")/g" \
    -e "s/@@PASSPHRASE@@/$(esc "$PASSPHRASE")/g" \
    -e "s/@@AGENT_CLI_HOME@@/$(esc "$AGENT_CLI_HOME")/g" \
    "$APP_DIR/bin/agent.template" > "$BIN_DIR/$CMD_NAME"
chmod +x "$BIN_DIR/$CMD_NAME"

echo "✅ installed launcher: $BIN_DIR/$CMD_NAME"
echo
echo "Next steps:"
echo "  1. pip install -r $APP_DIR/requirements.txt   (or use a venv/conda env)"
echo "  2. edit $ENV_FILE — paste your ANTHROPIC_API_KEY"
echo "  3. AGENT_NAME=\"My Buddy\" $PYTHON_RUNNER $APP_DIR/setup.py   # create agent"
echo "  4. $PYTHON_RUNNER $APP_DIR/setup_memory.py                   # optional: memory"
echo "  5. $CMD_NAME --doctor   then   $CMD_NAME 'hello'"
echo
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "⚠️  $BIN_DIR is not on PATH — add: export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
