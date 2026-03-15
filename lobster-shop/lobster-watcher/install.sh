#!/bin/bash
#===============================================================================
# Lobster Watcher Skill Installer
#
# Installs the lobster-watcher observability dashboard as a Lobster skill.
# This sets up:
#   1. The lobster-watcher source (cloned/updated from GitHub)
#   2. The frontend (built with Vite, deployed to nginx)
#   3. The wire server (Python/Starlette, runs as a systemd service)
#   4. nginx configuration for /watcher/ and /watcher-wire/
#
# Idempotent — safe to re-run for updates.
#
# Usage: bash ~/lobster/lobster-shop/lobster-watcher/install.sh
#===============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
step()    { echo -e "\n${CYAN}${BOLD}--- $1${NC}"; }

# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------
LOBSTER_DIR="${LOBSTER_INSTALL_DIR:-$HOME/lobster}"
LOBSTER_WORKSPACE="${LOBSTER_WORKSPACE:-$HOME/lobster-workspace}"
LOBSTER_PROJECTS="${LOBSTER_PROJECTS:-$LOBSTER_WORKSPACE/projects}"

WATCHER_REPO="https://github.com/Bisque-Labs/lobster-watcher.git"
WATCHER_DIR="$LOBSTER_PROJECTS/lobster-watcher"
WIRE_PORT="${LOBSTER_WIRE_PORT:-8765}"
NGINX_STATIC_DIR="/var/www/html/watcher"
NGINX_SITES_AVAILABLE="/etc/nginx/sites-available/default"
DB_PATH="${LOBSTER_DB_PATH:-$HOME/messages/config/agent_sessions.db}"

echo ""
echo -e "${BOLD}Lobster Watcher Skill Installer${NC}"
echo "================================="
echo ""
echo "  Dashboard:  http://localhost/watcher/"
echo "  Wire API:   http://localhost/watcher-wire/"
echo "  Wire port:  $WIRE_PORT (internal, proxied via nginx)"
echo "  DB path:    $DB_PATH"
echo ""

#===============================================================================
# Step 1: Check prerequisites
#===============================================================================
step "Checking prerequisites"

# Node.js
if ! command -v node &>/dev/null; then
    error "Node.js is required. Install: curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs"
fi
NODE_VERSION=$(node --version | sed 's/v//' | cut -d. -f1)
if [ "$NODE_VERSION" -lt 18 ]; then
    error "Node.js >= 18 required. Found: $(node --version)"
fi
success "Node.js $(node --version)"

# npm
if ! command -v npm &>/dev/null; then
    error "npm is required (bundled with Node.js)"
fi
success "npm $(npm --version)"

# Python
PYTHON_BIN=""
LOBSTER_VENV="$LOBSTER_DIR/.venv"
if [ -f "$LOBSTER_VENV/bin/python" ]; then
    PYTHON_BIN="$LOBSTER_VENV/bin/python"
    success "Lobster Python venv: $PYTHON_BIN"
elif command -v python3 &>/dev/null; then
    PYTHON_BIN="$(command -v python3)"
    success "Python 3: $(python3 --version)"
else
    error "Python 3 is required."
fi

# pip / uv
PIP_BIN=""
if [ -f "$LOBSTER_VENV/bin/pip" ]; then
    PIP_BIN="$LOBSTER_VENV/bin/pip"
elif command -v uv &>/dev/null; then
    PIP_BIN="uv pip"
elif command -v pip3 &>/dev/null; then
    PIP_BIN="pip3"
else
    warn "No pip found — Python dependencies may need manual install"
fi

# sudo (required for nginx/systemd)
if ! command -v sudo &>/dev/null; then
    error "sudo is required for nginx and systemd operations."
fi

# nginx
if ! command -v nginx &>/dev/null; then
    warn "nginx not found. Attempting to install..."
    sudo apt-get update -qq && sudo apt-get install -y nginx || error "Could not install nginx"
fi
success "nginx found"

#===============================================================================
# Step 2: Clone or update lobster-watcher
#===============================================================================
step "Fetching lobster-watcher source"

mkdir -p "$LOBSTER_PROJECTS"

if [ -d "$WATCHER_DIR/.git" ]; then
    info "Repository already exists — pulling latest changes..."
    cd "$WATCHER_DIR"
    git fetch origin
    git reset --hard origin/$(git rev-parse --abbrev-ref HEAD)
    success "Updated to $(git rev-parse --short HEAD)"
else
    info "Cloning from $WATCHER_REPO..."
    rm -rf "$WATCHER_DIR"
    git clone "$WATCHER_REPO" "$WATCHER_DIR"
    success "Cloned to $WATCHER_DIR"
fi

cd "$WATCHER_DIR"

#===============================================================================
# Step 3: Install Python wire server dependencies
#===============================================================================
step "Installing Python dependencies for wire server"

if [ -n "$PIP_BIN" ]; then
    $PIP_BIN install --quiet starlette uvicorn 2>&1 | tail -3
    success "starlette + uvicorn installed"
else
    warn "Could not install Python dependencies automatically. Run: pip3 install starlette uvicorn"
fi

#===============================================================================
# Step 4: Build the frontend (with nginx-proxied URLs baked in)
#===============================================================================
step "Building frontend"

cd "$WATCHER_DIR"

info "Installing Node.js dependencies..."
npm install --silent 2>&1 | tail -3
success "Node.js dependencies installed"

info "Building with nginx proxy URLs..."
VITE_WIRE_URL=/watcher-wire/stream \
VITE_POLL_URL=/watcher-wire/api/sessions \
    npm run build 2>&1 | tail -10
success "Frontend built (dist/ ready)"

#===============================================================================
# Step 5: Deploy static files to nginx
#===============================================================================
step "Deploying static files to nginx"

sudo mkdir -p "$NGINX_STATIC_DIR"
sudo cp -r "$WATCHER_DIR/dist/." "$NGINX_STATIC_DIR/"
sudo chmod -R 755 "$NGINX_STATIC_DIR"
success "Static files deployed to $NGINX_STATIC_DIR"

#===============================================================================
# Step 6: Configure nginx (add /watcher/ and /watcher-wire/ blocks if missing)
#===============================================================================
step "Configuring nginx"

NGINX_WATCHER_MARKER="# Lobster Watcher — static dashboard"
NGINX_WIRE_MARKER="# Lobster Watcher — wire server proxy"

if grep -q "$NGINX_WATCHER_MARKER" "$NGINX_SITES_AVAILABLE" 2>/dev/null; then
    info "nginx /watcher/ block already present — skipping"
else
    info "Adding /watcher/ and /watcher-wire/ blocks to nginx config..."

    # Insert before the closing brace of the last server block
    # We use a temp file and sed to insert our blocks before the last `}`
    NGINX_SNIPPET=$(cat <<'NGINX_EOF'

    # Lobster Watcher — static dashboard
    location /watcher/ {
        alias /var/www/html/watcher/;
        try_files $uri $uri/ /watcher/index.html;
        add_header Cache-Control "no-cache";
    }

    # Lobster Watcher — wire server proxy (SSE + REST)
    location /watcher-wire/ {
        proxy_pass http://127.0.0.1:8765/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # Required for SSE — disable buffering so events reach the browser immediately
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600;
    }
NGINX_EOF
)

    # Write snippet to temp file for reliable insertion
    NGINX_SNIPPET_FILE=$(mktemp)
    echo "$NGINX_SNIPPET" > "$NGINX_SNIPPET_FILE"

    # Find the line number of the last `}` in the file and insert before it
    LAST_BRACE=$(grep -n "^}" "$NGINX_SITES_AVAILABLE" | tail -1 | cut -d: -f1)
    if [ -n "$LAST_BRACE" ]; then
        TEMP_CONF=$(mktemp)
        head -n $((LAST_BRACE - 1)) "$NGINX_SITES_AVAILABLE" > "$TEMP_CONF"
        cat "$NGINX_SNIPPET_FILE" >> "$TEMP_CONF"
        tail -n +"$LAST_BRACE" "$NGINX_SITES_AVAILABLE" >> "$TEMP_CONF"
        sudo cp "$TEMP_CONF" "$NGINX_SITES_AVAILABLE"
        rm -f "$TEMP_CONF"
        success "nginx config updated"
    else
        warn "Could not auto-patch nginx config. Add this to your nginx server block manually:"
        cat "$NGINX_SNIPPET_FILE"
    fi
    rm -f "$NGINX_SNIPPET_FILE"
fi

# Test and reload nginx
if sudo nginx -t 2>/dev/null; then
    sudo systemctl reload nginx 2>/dev/null || sudo nginx -s reload 2>/dev/null || warn "Could not reload nginx — restart it manually: sudo systemctl restart nginx"
    success "nginx reloaded"
else
    warn "nginx config test failed — check $NGINX_SITES_AVAILABLE and reload manually"
fi

#===============================================================================
# Step 7: Install and start the wire server as a systemd service
#===============================================================================
step "Setting up wire server systemd service"

WIRE_SERVER_PY="$WATCHER_DIR/wire-server/wire_server.py"
SERVICE_FILE="/etc/systemd/system/lobster-wire.service"

cat > /tmp/lobster-wire.service << EOF
[Unit]
Description=Lobster Wire Server (observability SSE for lobster-watcher)
After=network.target

[Service]
User=$(whoami)
WorkingDirectory=$WATCHER_DIR
Environment=LOBSTER_DB_PATH=$DB_PATH
Environment=LOBSTER_WIRE_PORT=$WIRE_PORT
Environment=LOBSTER_WIRE_CORS_ORIGINS=http://localhost
ExecStart=$PYTHON_BIN $WIRE_SERVER_PY
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo cp /tmp/lobster-wire.service "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable lobster-wire 2>/dev/null
success "systemd service installed: lobster-wire"

# Restart the service (start or restart idempotently)
if sudo systemctl is-active --quiet lobster-wire; then
    info "Wire server already running — restarting to pick up any changes..."
    sudo systemctl restart lobster-wire
else
    info "Starting wire server..."
    sudo systemctl start lobster-wire
fi

# Wait for health check
for i in $(seq 1 15); do
    if curl -s "http://localhost:$WIRE_PORT/health" >/dev/null 2>&1; then
        success "Wire server health check passed"
        break
    fi
    sleep 1
    if [ "$i" -eq 15 ]; then
        warn "Wire server not yet responding on port $WIRE_PORT — it may still be starting"
        echo "  Check status: sudo systemctl status lobster-wire"
        echo "  View logs:    sudo journalctl -u lobster-wire -f"
    fi
done

#===============================================================================
# Done
#===============================================================================
echo ""
echo -e "${GREEN}${BOLD}Lobster Watcher installed!${NC}"
echo ""
echo "  Dashboard:    http://localhost/watcher/"
echo "  Wire health:  curl http://localhost/watcher-wire/health"
echo "  Service:      sudo systemctl status lobster-wire"
echo "  Logs:         sudo journalctl -u lobster-wire -f"
echo ""
echo "  For remote access, SSH tunnel from your local machine:"
echo "    ssh -L 8080:localhost:80 $(whoami)@<this-host>"
echo "  Then open:   http://localhost:8080/watcher/"
echo ""
echo "  To update later, just re-run this script:"
echo "    bash ~/lobster/lobster-shop/lobster-watcher/install.sh"
echo ""
