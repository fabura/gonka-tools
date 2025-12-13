#!/bin/bash
#
# Gonka.ai Quick Node Setup Script
# =================================
# This script sets up a Gonka compute node using Docker Compose.
#
# Usage:
#   curl -fsSL https://your-domain.com/setup.sh | bash
#   # or
#   ./quick_setup.sh
#
# Required environment variables:
#   GONKA_ACCOUNT_PUBKEY - Your Gonka account public key
#
# Optional environment variables:
#   GONKA_VERSION     - Version to install (default: latest)
#   SKIP_NVIDIA       - Set to "1" to skip NVIDIA driver installation
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

TOTAL_STEPS=8

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   Gonka.ai Node Setup Script v2.0    ║"
echo "╚══════════════════════════════════════╝"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    log_error "Please run as root (sudo)"
    exit 1
fi

# Check for required environment variable
if [ -z "$GONKA_ACCOUNT_PUBKEY" ]; then
    log_warn "GONKA_ACCOUNT_PUBKEY not set"
    echo ""
    echo "To get your public key:"
    echo "  1. Download inferenced CLI from https://github.com/gonka-ai/gonka/releases"
    echo "  2. Run: inferenced keys add my-account --keyring-backend test"
    echo "  3. Copy the 'key' value from the output"
    echo ""
    read -p "Enter your Gonka account public key (or press Enter to skip): " GONKA_ACCOUNT_PUBKEY
fi

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

$INSTALL_CMD curl wget git jq unzip
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
        
        # Show GPU info
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
            distribution="${OS}${VERSION_ID}"
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
# Step 5: Clone Gonka repository
# ============================================================================
log_step 5 "Setting up Gonka..."

GONKA_DIR="/opt/gonka"
if [ -d "$GONKA_DIR" ]; then
    log_info "Updating existing Gonka installation..."
    cd "$GONKA_DIR" && git pull --quiet
else
    log_info "Cloning Gonka repository..."
    git clone https://github.com/gonka-ai/gonka.git -b main "$GONKA_DIR" --quiet
fi

cd "$GONKA_DIR/deploy/join"
log_success "Gonka repository ready"

# ============================================================================
# Step 6: Configure Gonka
# ============================================================================
log_step 6 "Configuring Gonka node..."

# Create shared directory for models
mkdir -p /mnt/shared

# Create config.env
cat > config.env << EOF
export KEY_NAME=gonka-node
export KEYRING_PASSWORD=
export KEYRING_BACKEND=test
export API_PORT=8000
export API_SSL_PORT=8443
export PUBLIC_URL=http://${SERVER_IP}:8000
export P2P_EXTERNAL_ADDRESS=tcp://${SERVER_IP}:5000
export ACCOUNT_PUBKEY=${GONKA_ACCOUNT_PUBKEY}
export NODE_CONFIG=./node-config.json
export HF_HOME=/mnt/shared
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

log_success "Configuration created"

# ============================================================================
# Step 7: Start Gonka services
# ============================================================================
log_step 7 "Starting Gonka services..."

source config.env

# Pull images
log_info "Pulling Docker images (this may take a while)..."
docker compose -f docker-compose.yml -f docker-compose.mlnode.yml pull --quiet 2>/dev/null || \
docker compose -f docker-compose.yml -f docker-compose.mlnode.yml pull 2>&1 | tail -5

# Start containers
docker compose -f docker-compose.yml -f docker-compose.mlnode.yml up -d 2>&1 | tail -10

log_success "Gonka services started"

# Wait for services to be ready
log_info "Waiting for services to initialize (60 seconds)..."
sleep 60

# ============================================================================
# Step 8: Download required model
# ============================================================================
log_step 8 "Downloading AI model..."

if [ "$SKIP_MODEL_DOWNLOAD" != "1" ]; then
    # Check which model is configured
    MODEL=$(cat node-config.json | jq -r '.[0].models | keys[0]' 2>/dev/null)
    
    if [ -n "$MODEL" ] && [ "$MODEL" != "null" ]; then
        log_info "Downloading model: $MODEL"
        
        # Trigger download via MLNode API
        curl -s -X POST http://localhost:8080/api/v1/models/download \
            -H 'Content-Type: application/json' \
            -d "{\"hf_repo\": \"$MODEL\"}" > /dev/null 2>&1
        
        # Wait for download
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
            log_warn "Model download still in progress. Check status with:"
            echo "  curl -s http://localhost:8080/api/v1/models/list | jq"
        fi
    else
        log_warn "No model configured in node-config.json"
    fi
else
    log_info "Skipping model download (SKIP_MODEL_DOWNLOAD=1)"
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
log_info "Useful Commands:"
echo "  # Check node sync status"
echo "  curl -s http://localhost:26657/status | jq '.result.sync_info'"
echo ""
echo "  # Check MLNode health"
echo "  curl -s http://localhost:8080/health | jq"
echo ""
echo "  # Check models"
echo "  curl -s http://localhost:8080/api/v1/models/list | jq"
echo ""
echo "  # View logs"
echo "  cd /opt/gonka/deploy/join"
echo "  docker compose -f docker-compose.yml -f docker-compose.mlnode.yml logs -f"
echo ""
echo "  # Restart services"
echo "  cd /opt/gonka/deploy/join && source config.env"
echo "  docker compose -f docker-compose.yml -f docker-compose.mlnode.yml restart"
echo ""

log_success "Your Gonka node is now running! Happy earning! 💰"
