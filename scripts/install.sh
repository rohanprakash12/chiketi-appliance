#!/usr/bin/env bash
# chiketi-appliance installer — sets up all prerequisites on Debian/Ubuntu (Raspberry Pi)
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
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
    sudo apt-get install -y -qq $PKGS
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
    python3 -m pip install --user pipx 2>/dev/null || sudo apt-get install -y -qq pipx
    python3 -m pipx ensurepath 2>/dev/null || true
    export PATH="$HOME/.local/bin:$PATH"
fi
info "pipx OK"

# ── Upgrade pip/setuptools ──
info "Ensuring pip and setuptools are up to date..."
python3 -m pip install --user --upgrade pip setuptools wheel 2>/dev/null || true

# ── Install chiketi-appliance ──
info "Installing chiketi-appliance..."
if [ -f "$PROJECT_DIR/pyproject.toml" ]; then
    # Installing from local source (development)
    pipx install "$PROJECT_DIR" --force --pip-args="--upgrade-strategy eager"
else
    # Installing from GitHub
    pipx install "git+https://github.com/rohanprakash12/chiketi-appliance.git" --force --pip-args="--upgrade-strategy eager"
fi

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

# ── Create config directory and default config ──
info "Setting up configuration..."
mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_FILE" ]; then
    info "Config already exists at $CONFIG_FILE (not overwriting)"
else
    # Find config.example.yaml
    EXAMPLE_CONFIG=""
    if [ -f "$PROJECT_DIR/config.example.yaml" ]; then
        EXAMPLE_CONFIG="$PROJECT_DIR/config.example.yaml"
    elif [ -f "/usr/share/chiketi-appliance/config.example.yaml" ]; then
        EXAMPLE_CONFIG="/usr/share/chiketi-appliance/config.example.yaml"
    fi

    if [ -n "$EXAMPLE_CONFIG" ]; then
        cp "$EXAMPLE_CONFIG" "$CONFIG_FILE"
        info "Copied example config to $CONFIG_FILE"
    else
        # Generate a minimal default config
        cat > "$CONFIG_FILE" << 'YAML'
# chiketi-appliance configuration
# Add your remote hosts below

hosts:
  - name: "my-server"
    host: 192.168.1.100
    user: your-username
    key: ~/.ssh/id_rsa
    # port: 22  (default)

display:
  theme: Panel/Gold
  rotate_interval: 10
  host_rotate: true
  host_rotate_interval: 30

server:
  port: 7777
  bind: 0.0.0.0
YAML
        info "Created default config at $CONFIG_FILE"
    fi
fi

# ── Verify installation ──
if command -v chiketi-appliance >/dev/null 2>&1; then
    info "Installation complete!"
    echo ""
    echo "  Next steps:"
    echo ""
    step "1. Edit your config file:"
    echo "     nano $CONFIG_FILE"
    echo ""
    step "2. Set up SSH keys to your remote hosts:"
    echo "     $SCRIPT_DIR/setup-ssh.sh user@hostname"
    echo ""
    step "3. Run the appliance:"
    echo "     chiketi-appliance --config $CONFIG_FILE"
    echo ""
    step "4. Open the control panel in a browser:"
    echo "     http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):7777"
    echo ""
    echo "  If 'chiketi-appliance' is not found, open a new terminal and try again."
    echo ""
else
    fail "Installation failed. Check errors above."
fi
