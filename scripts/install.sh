#!/usr/bin/env bash
# chiketi-appliance installer
# Usage: curl -sL https://raw.githubusercontent.com/rohanprakash12/chiketi-appliance/main/scripts/install.sh | bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
fail()  { echo -e "${RED}[x]${NC} $1"; exit 1; }
step()  { echo -e "${CYAN}[>]${NC} $1"; }

CONFIG_DIR="$HOME/.config/chiketi-appliance"
CONFIG_FILE="$CONFIG_DIR/config.yaml"

echo ""
echo "  chiketi-appliance installer"
echo "  Remote system monitoring dashboard"
echo ""

# ── Check OS ──
if [ -f /etc/os-release ]; then
    . /etc/os-release
    DISTRO="${ID:-unknown}"
else
    fail "Cannot detect OS. This installer supports Debian/Ubuntu."
fi

case "$DISTRO" in
    ubuntu|debian|raspbian|pop|linuxmint|elementary) ;;
    *) warn "Untested distro: $DISTRO. Proceeding anyway (apt required)." ;;
esac

# ── System packages ──
info "Updating package lists..."
sudo apt-get update -qq

PKGS=""
command -v python3 >/dev/null || PKGS="$PKGS python3"
command -v pip3 >/dev/null    || PKGS="$PKGS python3-pip"
command -v git >/dev/null     || PKGS="$PKGS git"
dpkg -s python3-venv >/dev/null 2>&1 || PKGS="$PKGS python3-venv"

# paramiko needs these to build its C extensions
dpkg -s python3-dev >/dev/null 2>&1  || PKGS="$PKGS python3-dev"
dpkg -s gcc >/dev/null 2>&1          || PKGS="$PKGS gcc"
dpkg -s libffi-dev >/dev/null 2>&1   || PKGS="$PKGS libffi-dev"
dpkg -s libssl-dev >/dev/null 2>&1   || PKGS="$PKGS libssl-dev"

# Chromium for kiosk dashboard display
command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1 || command -v google-chrome >/dev/null 2>&1 || PKGS="$PKGS chromium-browser"

if [ -n "$PKGS" ]; then
    info "Installing system packages:$PKGS"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $PKGS
else
    info "System packages already installed"
fi

# ── Check Python version ──
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    fail "Python 3.11+ required (found $PY_VER). Install a newer Python first."
fi
info "Python $PY_VER OK"

# ── Install pipx ──
if ! command -v pipx >/dev/null 2>&1; then
    info "Installing pipx..."
    python3 -m pip install --user pipx 2>/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq pipx
    python3 -m pipx ensurepath 2>/dev/null || true
    export PATH="$HOME/.local/bin:$PATH"
fi
info "pipx OK"

# ── Upgrade pip/setuptools ──
info "Ensuring pip and setuptools are up to date..."
python3 -m pip install --user --upgrade pip setuptools wheel 2>/dev/null || true

# ── Install chiketi-appliance ──
info "Installing chiketi-appliance from GitHub..."
pipx install "git+https://github.com/rohanprakash12/chiketi-appliance.git" --force --pip-args="--upgrade-strategy eager"

# ── Fix PATH ──
PIPX_BIN="$HOME/.local/bin"
if ! echo "$PATH" | grep -q "$PIPX_BIN"; then
    export PATH="$PIPX_BIN:$PATH"
    SHELL_RC=""
    if [ -f "$HOME/.bashrc" ]; then
        SHELL_RC="$HOME/.bashrc"
    elif [ -f "$HOME/.zshrc" ]; then
        SHELL_RC="$HOME/.zshrc"
    elif [ -f "$HOME/.profile" ]; then
        SHELL_RC="$HOME/.profile"
    fi
    if [ -n "$SHELL_RC" ]; then
        if ! grep -q '.local/bin' "$SHELL_RC" 2>/dev/null; then
            echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
            info "Added ~/.local/bin to PATH in $(basename "$SHELL_RC")"
        fi
    fi
fi

# ── Create config directory ──
mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_FILE" ]; then
    info "Config already exists at $CONFIG_FILE (not overwriting)"
else
    info "No config file — the setup wizard will guide you on first run."
fi

# ── Verify installation ──
if command -v chiketi-appliance >/dev/null 2>&1; then
    info "Installation complete!"
    echo ""
    echo "  Next steps:"
    echo ""
    step "1. Run the appliance:"
    echo "     chiketi-appliance"
    echo ""
    step "2. Open the setup wizard in your browser:"
    PI_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost')
    echo "     http://${PI_IP}:7777/"
    echo ""
    echo "  The wizard will walk you through adding servers, SSH keys, and themes."
    echo ""
else
    fail "Installation failed. Check errors above."
fi
