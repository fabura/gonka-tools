#!/bin/bash
#
# Gonka.ai Quick Node Setup Script v3.1
# =====================================
# This script sets up a Gonka compute node using Docker Compose.
#
# SECURITY: This script creates an ML Ops key (hot wallet) on the server.
# You should grant permissions from YOUR LOCAL MACHINE using your account key.
# Never share your account mnemonic with the server!
#
# Usage:
#   # Run on server:
#   ./quick_setup.sh
#
#   # After setup, run on YOUR LOCAL MACHINE to grant permissions:
#   inferenced tx inference grant-ml-ops-permissions \
#     your-account-key \
#     <ML_OPS_ADDRESS_FROM_SETUP> \
#     --from your-account-key \
#     --keyring-backend file \
#     --node http://node2.gonka.ai:26657 \
#     --chain-id gonka-mainnet \
#     --gas 1000000 -y
#
# Required:
#   ACCOUNT_PUBKEY     - Your account public key (get from: inferenced keys show your-key)
#
# Optional:
#   KEYRING_PASSWORD   - Password for keyring (min 8 chars, default: gonkapass)
#   SKIP_NVIDIA        - Set to "1" to skip NVIDIA driver installation
#   SKIP_MODEL_DOWNLOAD - Set to "1" to skip model pre-download
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info() { echo -e "${CYAN}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "\n${BOLD}[$1/$TOTAL_STEPS]${NC} $2"; }

TOTAL_STEPS=10

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   Gonka.ai Node Setup Script v3.0    ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    log_error "Please run as root (sudo)"
    exit 1
fi

# Set defaults
KEYRING_PASSWORD="${KEYRING_PASSWORD:-gonkapass}"
GONKA_DIR="/opt/gonka"

# Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
    VERSION=$VERSION_ID
else
    log_error "Cannot detect OS"
    exit 1
fi

log_info "Detected OS: $OS $VERSION"

# Get server IP
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
log_info "Server IP: $SERVER_IP"

# ============================================================================
# Step 1: Update system
# ============================================================================
log_step 1 "Updating system packages..."

if [ "$OS" = "ubuntu" ] || [ "$OS" = "debian" ]; then
    apt-get update -qq
    apt-get upgrade -y -qq
    INSTALL_CMD="apt-get install -y -qq"
elif [ "$OS" = "centos" ] || [ "$OS" = "rhel" ] || [ "$OS" = "rocky" ] || [ "$OS" = "almalinux" ]; then
    yum update -y -q
    INSTALL_CMD="yum install -y -q"
else
    log_error "Unsupported OS: $OS"
    exit 1
fi
log_success "System updated"

# ============================================================================
# Step 2: Install dependencies
# ============================================================================
log_step 2 "Installing dependencies..."

$INSTALL_CMD curl wget git jq unzip expect
log_success "Dependencies installed"

# ============================================================================
# Step 3: Check for NVIDIA GPU and install drivers
# ============================================================================
log_step 3 "Checking for NVIDIA GPU..."

HAS_GPU=false
if lspci 2>/dev/null | grep -i nvidia > /dev/null; then
    HAS_GPU=true
    log_success "NVIDIA GPU detected"
    
    if [ "$SKIP_NVIDIA" != "1" ]; then
        if ! command -v nvidia-smi &> /dev/null; then
            log_info "Installing NVIDIA drivers..."
            if [ "$OS" = "ubuntu" ]; then
                $INSTALL_CMD nvidia-driver-535 nvidia-cuda-toolkit
            elif [ "$OS" = "debian" ]; then
                sed -i 's/main/main contrib non-free/g' /etc/apt/sources.list
                apt-get update -qq
                $INSTALL_CMD nvidia-driver nvidia-cuda-toolkit
            else
                log_warn "Please install NVIDIA drivers manually for $OS"
            fi
        fi
        
        echo ""
        nvidia-smi --query-gpu=name,memory.total --format=csv 2>/dev/null || true
        echo ""
    fi
else
    log_warn "No NVIDIA GPU detected - node will have limited functionality"
fi

# ============================================================================
# Step 4: Install Docker
# ============================================================================
log_step 4 "Installing Docker..."

if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    log_success "Docker installed"
else
    log_success "Docker already installed"
fi

# Install NVIDIA Container Toolkit if GPU present
if [ "$HAS_GPU" = true ]; then
    if ! dpkg -l 2>/dev/null | grep -q nvidia-container-toolkit; then
        log_info "Installing NVIDIA Container Toolkit..."
        
        if [ "$OS" = "ubuntu" ] || [ "$OS" = "debian" ]; then
            curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
                gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg 2>/dev/null
            curl -s -L "https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list" | \
                sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
                tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null
            apt-get update -qq
            apt-get install -y -qq nvidia-container-toolkit
            nvidia-ctk runtime configure --runtime=docker
            systemctl restart docker
        fi
        log_success "NVIDIA Container Toolkit installed"
    else
        log_success "NVIDIA Container Toolkit already installed"
    fi
fi

# ============================================================================
# Step 5: Clone Gonka repository and install CLI
# ============================================================================
log_step 5 "Setting up Gonka..."

if [ -d "$GONKA_DIR" ]; then
    log_info "Updating existing Gonka installation..."
    cd "$GONKA_DIR" && git pull --quiet
else
    log_info "Cloning Gonka repository..."
    git clone https://github.com/gonka-ai/gonka.git -b main "$GONKA_DIR" --quiet
fi

# Install inferenced CLI
if ! command -v inferenced &> /dev/null; then
    log_info "Installing inferenced CLI..."
    ARCH=$(uname -m)
    if [ "$ARCH" = "x86_64" ]; then
        ARCH="amd64"
    elif [ "$ARCH" = "aarch64" ]; then
        ARCH="arm64"
    fi
    
    # Download from releases
    RELEASE_URL="https://github.com/gonka-ai/gonka/releases/latest/download/inferenced-linux-${ARCH}"
    if curl -fsSL -o /usr/local/bin/inferenced "$RELEASE_URL" 2>/dev/null; then
        chmod +x /usr/local/bin/inferenced
        log_success "inferenced CLI installed"
    else
        log_warn "Could not download inferenced CLI - will use docker version"
    fi
fi

cd "$GONKA_DIR/deploy/join"
log_success "Gonka repository ready"

# ============================================================================
# Step 6: Setup ML Ops Key (Hot Wallet - safe to keep on server)
# ============================================================================
log_step 6 "Setting up ML Ops key..."

mkdir -p /root/.inference/keyring-file
mkdir -p /root/.inference/keyring-test

# Get account public key (NOT the mnemonic - that stays on your local machine)
if [ -z "$ACCOUNT_PUBKEY" ]; then
    echo ""
    log_info "Your account public key is needed for node registration."
    echo "To get it, run this on YOUR LOCAL machine:"
    echo "  inferenced keys show your-account-name --keyring-backend file"
    echo "  (Copy the 'key' value from pubkey field)"
    echo ""
    read -p "Enter your account PUBLIC KEY (base64 string): " ACCOUNT_PUBKEY
fi

if [ -z "$ACCOUNT_PUBKEY" ]; then
    log_error "Account public key is required!"
    exit 1
fi

# Create ML Ops Key (this is the HOT wallet - safe to keep on server)
log_info "Creating ML Ops key (hot wallet for node operations)..."
expect << EOF
spawn inferenced keys add ml-ops-key --keyring-backend file
expect "passphrase"
send "$KEYRING_PASSWORD\r"
expect "passphrase"
send "$KEYRING_PASSWORD\r"
expect eof
EOF

# Get ML ops key info
sleep 2
ML_OPS_INFO=$(expect << EOF
spawn inferenced keys show ml-ops-key --keyring-backend file
expect "passphrase"
send "$KEYRING_PASSWORD\r"
expect eof
EOF
)

ML_OPS_ADDRESS=$(echo "$ML_OPS_INFO" | grep "address:" | awk '{print $2}' | tr -d '\r')
ML_OPS_PUBKEY=$(echo "$ML_OPS_INFO" | grep "key:" | sed 's/.*key":"\([^"]*\)".*/\1/' | tr -d '\r')

log_info "ML Ops Address: $ML_OPS_ADDRESS"
log_success "ML Ops key created"

# Save ML ops mnemonic to a file for backup
log_warn "The ML Ops key mnemonic was displayed above. Save it for backup!"

# ============================================================================
# Step 7: Instructions for Granting ML Ops Permissions
# ============================================================================
log_step 7 "ML Ops permissions setup..."

# Save the grant command for the user
GRANT_CMD="inferenced tx inference grant-ml-ops-permissions \\
    YOUR_ACCOUNT_KEY_NAME \\
    $ML_OPS_ADDRESS \\
    --from YOUR_ACCOUNT_KEY_NAME \\
    --keyring-backend file \\
    --node http://node2.gonka.ai:26657 \\
    --chain-id gonka-mainnet \\
    --gas 1000000 -y"

echo "$GRANT_CMD" > /opt/gonka/grant_permissions.sh
chmod +x /opt/gonka/grant_permissions.sh

log_warn "IMPORTANT: Run this command on YOUR LOCAL MACHINE (where your account key is):"
echo ""
echo "  $GRANT_CMD"
echo ""
log_info "This grants the ML Ops key permission to operate on behalf of your account."
log_info "Command saved to: /opt/gonka/grant_permissions.sh"

# Ask if they want to wait or continue
echo ""
read -p "Have you run the grant command on your local machine? (y/n): " GRANT_DONE
if [ "$GRANT_DONE" != "y" ] && [ "$GRANT_DONE" != "Y" ]; then
    log_warn "Remember to run the grant command before the node can earn rewards!"
fi

# ============================================================================
# Step 8: Configure Gonka
# ============================================================================
log_step 8 "Configuring Gonka node..."

# Create shared directory for model cache.
# NOTE: The MLNode container mounts host `/mnt/shared` to container `/root/.cache`.
mkdir -p /mnt/shared

# Create .env file (Docker Compose reads this automatically)
cat > .env << EOF
KEY_NAME=ml-ops-key
KEYRING_PASSWORD=$KEYRING_PASSWORD
KEYRING_BACKEND=file
API_PORT=8000
API_SSL_PORT=8443
PUBLIC_URL=http://${SERVER_IP}:8000
P2P_EXTERNAL_ADDRESS=tcp://${SERVER_IP}:5000
ACCOUNT_PUBKEY=${ACCOUNT_PUBKEY}
NODE_CONFIG=./node-config.json
# IMPORTANT:
# MLNode uses `/root/.cache/hub` as the HuggingFace cache location.
# In Gonka docker-compose, `/root/.cache` is bind-mounted to host `/mnt/shared`,
# so model downloads persist on the host.
HF_HOME=/root/.cache
SEED_API_URL=http://node2.gonka.ai:8000
SEED_NODE_RPC_URL=http://node2.gonka.ai:26657
SEED_NODE_P2P_URL=tcp://node2.gonka.ai:5000
DAPI_API__POC_CALLBACK_URL=http://api:9100
DAPI_CHAIN_NODE__URL=http://node:26657
DAPI_CHAIN_NODE__P2P_URL=http://node:26656
RPC_SERVER_URL_1=http://node1.gonka.ai:26657
RPC_SERVER_URL_2=http://node2.gonka.ai:26657
PORT=8080
INFERENCE_PORT=5050
EOF

# Also create config.env for shell sourcing
cat > config.env << EOF
export KEY_NAME=ml-ops-key
export KEYRING_PASSWORD=$KEYRING_PASSWORD
export KEYRING_BACKEND=file
export API_PORT=8000
export API_SSL_PORT=8443
export PUBLIC_URL=http://${SERVER_IP}:8000
export P2P_EXTERNAL_ADDRESS=tcp://${SERVER_IP}:5000
export ACCOUNT_PUBKEY=${ACCOUNT_PUBKEY}
export NODE_CONFIG=./node-config.json
export HF_HOME=/root/.cache
export SEED_API_URL=http://node2.gonka.ai:8000
export SEED_NODE_RPC_URL=http://node2.gonka.ai:26657
export SEED_NODE_P2P_URL=tcp://node2.gonka.ai:5000
export DAPI_API__POC_CALLBACK_URL=http://api:9100
export DAPI_CHAIN_NODE__URL=http://node:26657
export DAPI_CHAIN_NODE__P2P_URL=http://node:26656
export RPC_SERVER_URL_1=http://node1.gonka.ai:26657
export RPC_SERVER_URL_2=http://node2.gonka.ai:26657
export PORT=8080
export INFERENCE_PORT=5050
EOF

# Copy keyring to deploy directory for containers
mkdir -p .inference/keyring-file
cp -r /root/.inference/keyring-file/* .inference/keyring-file/ 2>/dev/null || true

log_success "Configuration created"

# ============================================================================
# Step 9: Start Gonka services
# ============================================================================
log_step 9 "Starting Gonka services..."

# Pull images
log_info "Pulling Docker images (this may take a while)..."
docker compose -f docker-compose.yml -f docker-compose.mlnode.yml pull 2>&1 | tail -5

# Start containers
docker compose -f docker-compose.yml -f docker-compose.mlnode.yml up -d 2>&1 | tail -10

log_success "Gonka services started"

# Wait for services to be ready
log_info "Waiting for services to initialize (90 seconds)..."
sleep 90

# ============================================================================
# Step 10: Register Node & Download Model
# ============================================================================
log_step 10 "Registering node and downloading model..."

# Save registration command for user
REGISTER_CMD="inferenced tx inference submit-new-participant \\
    http://${SERVER_IP}:8000 \\
    --from YOUR_ACCOUNT_KEY_NAME \\
    --keyring-backend file \\
    --node http://node2.gonka.ai:26657 \\
    --chain-id gonka-mainnet \\
    --gas 1000000 -y"

echo "$REGISTER_CMD" > /opt/gonka/register_node.sh
chmod +x /opt/gonka/register_node.sh

log_info "Node registration command saved to: /opt/gonka/register_node.sh"
log_info "Run this on your local machine if this is a new node or you changed the IP."

# Download model
if [ "$SKIP_MODEL_DOWNLOAD" != "1" ]; then
    # Prefer a well-known Gonka-recommended model, override via env if desired.
    MODEL="${DEFAULT_MODEL_HF_REPO:-}"
    if [ -z "$MODEL" ]; then
        MODEL=$(cat node-config.json | jq -r '.[0].models | keys[0]' 2>/dev/null)
    fi
    
    if [ -n "$MODEL" ] && [ "$MODEL" != "null" ]; then
        log_info "Downloading model: $MODEL"
        
        curl -s -X POST http://localhost:8080/api/v1/models/download \
            -H 'Content-Type: application/json' \
            -d "{\"hf_repo\": \"$MODEL\"}" > /dev/null 2>&1
        
        log_info "Model download started. Checking progress..."
        
        DOWNLOAD_COMPLETE=false
        for i in $(seq 1 60); do
            sleep 10
            STATUS=$(curl -s -X POST http://localhost:8080/api/v1/models/status \
                -H 'Content-Type: application/json' \
                -d "{\"hf_repo\": \"$MODEL\"}" 2>/dev/null | jq -r '.status' 2>/dev/null)
            
            if [ "$STATUS" = "DOWNLOADED" ]; then
                DOWNLOAD_COMPLETE=true
                break
            elif [ "$STATUS" = "DOWNLOADING" ]; then
                CACHE_SIZE=$(curl -s http://localhost:8080/api/v1/models/space 2>/dev/null | jq -r '.cache_size_gb' 2>/dev/null)
                echo -ne "\r  Downloaded: ${CACHE_SIZE:-0} GB..."
            fi
        done
        
        echo ""
        if [ "$DOWNLOAD_COMPLETE" = true ]; then
            log_success "Model downloaded successfully"
        else
            log_warn "Model download in progress. Check: curl -s http://localhost:8080/api/v1/models/list | jq"
        fi
    fi
fi

# ============================================================================
# Summary
# ============================================================================
echo ""
echo "╔══════════════════════════════════════╗"
echo "║         Setup Complete! 🚀           ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Show status
log_info "Container Status:"
docker ps --format 'table {{.Names}}\t{{.Status}}' | head -10

echo ""
log_info "Key Information:"
echo "  ML Ops Address:  $ML_OPS_ADDRESS"
echo "  ML Ops Pubkey:   $ML_OPS_PUBKEY"
echo "  Account Pubkey:  $ACCOUNT_PUBKEY"
echo "  Keyring Pass:    $KEYRING_PASSWORD"

echo ""
log_info "Node Information:"
echo "  Server IP:     $SERVER_IP"
echo "  API URL:       http://$SERVER_IP:8000"
echo "  P2P Address:   tcp://$SERVER_IP:5000"

if [ "$HAS_GPU" = true ]; then
    echo ""
    log_info "GPU Status:"
    nvidia-smi --query-gpu=name,memory.total,temperature.gpu --format=csv 2>/dev/null || echo "  Run 'nvidia-smi' to check"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ⚠️  IMPORTANT: Complete these steps on YOUR LOCAL MACHINE  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
log_warn "1. Grant ML Ops permissions (REQUIRED for earning):"
echo "   $GRANT_CMD"
echo ""
log_warn "2. Register node (if new or IP changed):"
echo "   $REGISTER_CMD"

echo ""
log_info "Useful Commands:"
echo "  # Check sync status"
echo "  curl -s http://localhost:26657/status | jq '.result.sync_info'"
echo ""
echo "  # Check MLNode health"
echo "  curl -s http://localhost:8080/health | jq"
echo ""
echo "  # Check PoW status"
echo "  curl -s http://localhost:9200/admin/v1/nodes | jq"
echo ""
echo "  # View logs"
echo "  cd /opt/gonka/deploy/join && docker compose logs -f"
echo ""
echo "  # Restart"
echo "  cd /opt/gonka/deploy/join && docker compose -f docker-compose.yml -f docker-compose.mlnode.yml restart"
echo ""

echo ""
log_warn "SAVE THIS INFORMATION:"
echo "  - Keyring password: $KEYRING_PASSWORD"
echo "  - ML Ops address: $ML_OPS_ADDRESS"
echo "  - Grant command: /opt/gonka/grant_permissions.sh"
echo "  - Register command: /opt/gonka/register_node.sh"
echo ""
log_success "Server setup complete! Run the grant command from your local machine to start earning! 💰"

echo ""
log_info "Recommended: Deploy the model (works for Qwen/Qwen3-32B-FP8 on 2 GPUs):"
echo "  curl -sS -X POST http://localhost:8080/api/v1/inference/up \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"model\":\"Qwen/Qwen3-32B-FP8\",\"dtype\":\"float16\",\"additional_args\":[\"--tensor-parallel-size\",\"2\",\"--pipeline-parallel-size\",\"1\",\"--quantization\",\"fp8\",\"--kv-cache-dtype\",\"fp8\",\"--gpu-memory-utilization\",\"0.95\",\"--max-model-len\",\"32768\"]}'"
