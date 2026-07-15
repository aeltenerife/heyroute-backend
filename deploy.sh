#!/bin/bash
# ============================================================
# HeyRoute Backend — One-Shot Server Deployment Script
# Server: HXIL-INT03-Stag (SSH port 14031, HTTP port 14030)
# Run as root: bash deploy.sh
# ============================================================
set -e

echo "=========================================="
echo "  HeyRoute Backend — Auto Deploy"
echo "=========================================="

# --- Step 1: System packages ---
echo ""
echo "[1/7] Installing system dependencies..."
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv python3-dev \
    ffmpeg nginx git build-essential gcc postgresql postgresql-contrib

# --- Step 2: Clone the repo ---
echo ""
BRANCH_NAME="task/integration-into-prod-server"
echo "[2/7] Cloning heyroute-backend repo ($BRANCH_NAME branch)..."
cd heyroute-amiel
if [ -d "heyroute-backend" ]; then
    echo "  → heyroute-backend directory already exists, pulling latest..."
    cd heyroute-backend
    git config remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*"
    git fetch --all
    git checkout $BRANCH_NAME
    git reset --hard origin/$BRANCH_NAME
    git pull origin $BRANCH_NAME
else
    git clone -b $BRANCH_NAME https://github.com/aeltenerife/heyroute-backend.git
    cd heyroute-backend
fi

# --- Step 3: Python virtual environment ---
echo ""
echo "[3/7] Setting up Python virtual environment..."
cd /root/heyroute-backend
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# --- Step 4: Create or update .env file ---
echo ""
echo "[4/7] Setting up .env file..."
ENV_FILE="/root/heyroute-backend/.env"
TEMPLATE_FILE="/root/heyroute-backend/template.env"

if [ ! -f "$ENV_FILE" ]; then
    echo "  → .env file not found on server, initializing from template..."
    if [ -f "$TEMPLATE_FILE" ]; then
        cp "$TEMPLATE_FILE" "$ENV_FILE"
    else
        touch "$ENV_FILE"
    fi
else
    echo "  → Existing .env file found. Preserving your manual configuration."
fi

# Ensure loopback QWEN_API_URL and configuration is present and updated
# We define a helper to set or update environment variables in the file
set_env_var() {
    local key=$1
    local val=$2
    if grep -q "^${key}=" "$ENV_FILE"; then
        # Replace existing key (using | as delimiter since values contain slashes)
        sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
    else
        # Append new key
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

set_env_var "QWEN_API_URL" "http://172.16.3.213:80/v1/chat/completions"
set_env_var "QWEN_MODEL_NAME" "Qwen/Qwen2.5-7B-Instruct-AWQ"
set_env_var "PORT" "8000"
set_env_var "HOST" "127.0.0.1"

echo "  → .env file verified and updated"

# --- Step 5: Create systemd service ---
echo ""
echo "[5/7] Creating systemd service..."
cat > /etc/systemd/system/heyroute.service << 'SERVICEEOF'
[Unit]
Description=HeyRoute FastAPI Backend
After=network.target

[Service]
User=root
WorkingDirectory=/root/heyroute-backend
Environment="PATH=/root/heyroute-backend/venv/bin"
ExecStart=/root/heyroute-backend/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable heyroute
systemctl restart heyroute
echo "  → heyroute.service restarted"

# --- Step 6: Configure Nginx ---
echo ""
echo "[6/7] Configuring Nginx reverse proxy..."

# Remove default site if it exists
rm -f /etc/nginx/sites-enabled/default

cat > /etc/nginx/sites-available/heyroute << 'NGINXEOF'
server {
    listen 80;
    server_name _;

    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
}
NGINXEOF

ln -sf /etc/nginx/sites-available/heyroute /etc/nginx/sites-enabled/
nginx -t
systemctl restart nginx || { echo "Nginx failed to start! Fetching actual error log..."; cat /var/log/nginx/error.log | tail -n 20; exit 1; }
echo "  → Nginx configured and restarted"

# --- Step 7: Verify ---
echo ""
echo "[7/7] Verifying..."
sleep 2
systemctl status heyroute --no-pager || true
echo ""
echo "Testing API endpoint..."
curl -s http://localhost:8000/health || echo "(Server may still be starting up...)"

echo ""
echo "=========================================="
echo "  ✅ DEPLOYMENT COMPLETE!"
echo "=========================================="
echo ""
echo "  Backend URL:  http://altdsidccf.dlsu.edu.ph:14030"
echo "  Service:      sudo systemctl status heyroute"
echo "  Logs:         sudo journalctl -u heyroute -f"
echo "  Restart:      sudo systemctl restart heyroute"
echo ""
