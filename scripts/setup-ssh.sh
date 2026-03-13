#!/usr/bin/env bash
# chiketi-appliance SSH key setup helper
# Usage: ./setup-ssh.sh user@host[:port] [--add-to-config NAME]
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
fail()  { echo -e "${RED}[x]${NC} $1"; exit 1; }

SSH_KEY="$HOME/.ssh/id_rsa"
CONFIG_FILE="$HOME/.config/chiketi-appliance/config.yaml"

usage() {
    echo "Usage: $(basename "$0") user@host[:port] [--add-to-config NAME]"
    echo ""
    echo "Sets up SSH key-based authentication to a remote host for"
    echo "chiketi-appliance monitoring."
    echo ""
    echo "Options:"
    echo "  --add-to-config NAME    Add the host to your config.yaml with"
    echo "                          the given display name"
    echo "  --key PATH              Use a specific SSH key (default: ~/.ssh/id_rsa)"
    echo "  --help                  Show this help message"
    echo ""
    echo "Examples:"
    echo "  $(basename "$0") rohan@192.168.1.50"
    echo "  $(basename "$0") deploy@webserver:2222"
    echo "  $(basename "$0") rohan@192.168.1.50 --add-to-config gpu-server"
    exit 0
}

# ── Parse arguments ──
if [ $# -lt 1 ]; then
    usage
fi

ADD_NAME=""
TARGET=""

while [ $# -gt 0 ]; do
    case "$1" in
        --help|-h)
            usage
            ;;
        --add-to-config)
            shift
            [ $# -eq 0 ] && fail "--add-to-config requires a name argument"
            ADD_NAME="$1"
            ;;
        --key)
            shift
            [ $# -eq 0 ] && fail "--key requires a path argument"
            SSH_KEY="$1"
            ;;
        *)
            if [ -z "$TARGET" ]; then
                TARGET="$1"
            else
                fail "Unexpected argument: $1"
            fi
            ;;
    esac
    shift
done

[ -z "$TARGET" ] && fail "No target specified. Usage: $(basename "$0") user@host[:port]"

# ── Parse user@host:port ──
if [[ "$TARGET" != *@* ]]; then
    fail "Target must be in user@host format (got: $TARGET)"
fi

SSH_USER="${TARGET%%@*}"
HOST_PORT="${TARGET#*@}"

if [[ "$HOST_PORT" == *:* ]]; then
    SSH_HOST="${HOST_PORT%%:*}"
    SSH_PORT="${HOST_PORT#*:}"
else
    SSH_HOST="$HOST_PORT"
    SSH_PORT="22"
fi

echo ""
echo "  chiketi-appliance SSH setup"
echo "  Target: ${SSH_USER}@${SSH_HOST}:${SSH_PORT}"
echo ""

# ── Generate SSH key if needed ──
if [ ! -f "$SSH_KEY" ]; then
    info "No SSH key found at $SSH_KEY, generating one..."
    ssh-keygen -t rsa -b 4096 -f "$SSH_KEY" -N "" -C "chiketi-appliance@$(hostname)"
    info "SSH key generated: $SSH_KEY"
else
    info "SSH key exists: $SSH_KEY"
fi

# ── Copy public key to remote host ──
PUB_KEY="${SSH_KEY}.pub"
if [ ! -f "$PUB_KEY" ]; then
    fail "Public key not found: $PUB_KEY"
fi

info "Copying public key to ${SSH_USER}@${SSH_HOST}..."
echo "  You may be prompted for the remote user's password."
echo ""

if command -v ssh-copy-id >/dev/null 2>&1; then
    ssh-copy-id -i "$PUB_KEY" -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}"
else
    # Fallback if ssh-copy-id is not available
    cat "$PUB_KEY" | ssh -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}" \
        'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
fi

echo ""

# ── Test passwordless connection ──
info "Testing passwordless SSH connection..."
if ssh -o BatchMode=yes -o ConnectTimeout=10 -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}" "echo OK" >/dev/null 2>&1; then
    info "Passwordless SSH connection successful!"
else
    fail "Passwordless SSH connection failed. Check that the key was copied correctly."
fi

# ── Test that we can read basic system info ──
info "Testing remote system access..."
REMOTE_HOSTNAME=$(ssh -o BatchMode=yes -o ConnectTimeout=10 -p "$SSH_PORT" "${SSH_USER}@${SSH_HOST}" "hostname" 2>/dev/null || echo "")
if [ -n "$REMOTE_HOSTNAME" ]; then
    info "Remote hostname: $REMOTE_HOSTNAME"
else
    warn "Could not read remote hostname (non-critical)"
fi

# ── Optionally add to config.yaml ──
if [ -n "$ADD_NAME" ]; then
    if [ ! -f "$CONFIG_FILE" ]; then
        warn "Config file not found at $CONFIG_FILE"
        warn "Run install.sh first or create the config manually."
    else
        info "Adding host '$ADD_NAME' to $CONFIG_FILE..."

        # Check if the host name already exists in config
        if grep -q "name: \"$ADD_NAME\"" "$CONFIG_FILE" 2>/dev/null; then
            warn "Host '$ADD_NAME' already exists in config, skipping."
        else
            # Build the host entry
            HOST_ENTRY="\n  - name: \"$ADD_NAME\"\n    host: $SSH_HOST\n    user: $SSH_USER\n    key: $SSH_KEY"
            if [ "$SSH_PORT" != "22" ]; then
                HOST_ENTRY="$HOST_ENTRY\n    port: $SSH_PORT"
            fi

            # Append after the last host entry (before display: section)
            if grep -q "^display:" "$CONFIG_FILE" 2>/dev/null; then
                sed -i "/^display:/i\\$HOST_ENTRY" "$CONFIG_FILE"
            else
                # Just append to end of file
                echo -e "$HOST_ENTRY" >> "$CONFIG_FILE"
            fi
            info "Host '$ADD_NAME' added to config."
        fi
    fi
fi

# ── Summary ──
echo ""
info "Setup complete for ${SSH_USER}@${SSH_HOST}:${SSH_PORT}"
echo ""
if [ -z "$ADD_NAME" ]; then
    echo "  To add this host to your config, either:"
    echo "    1. Re-run with: $(basename "$0") $TARGET --add-to-config my-server-name"
    echo "    2. Edit $CONFIG_FILE manually and add:"
    echo ""
    echo "      - name: \"my-server\""
    echo "        host: $SSH_HOST"
    echo "        user: $SSH_USER"
    echo "        key: $SSH_KEY"
    if [ "$SSH_PORT" != "22" ]; then
        echo "        port: $SSH_PORT"
    fi
    echo ""
fi
