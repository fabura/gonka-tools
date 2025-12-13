#!/bin/bash
#
# Gonka.ai Quick Node Setup Script v3.0
# =====================================
# This script sets up a Gonka compute node using Docker Compose.
#
# Usage:
#   # With existing account (provide mnemonic when prompted):
#   ./quick_setup.sh
#
#   # With environment variables:
#   export GONKA_MNEMONIC="your 24 word mnemonic..."
#   export KEYRING_PASSWORD="yourpassword"
#   ./quick_setup.sh
#
# Optional environment variables:
#   GONKA_MNEMONIC     - 24-word mnemonic for existing account
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
# Step 6: Setup Keys (Account Key + ML Ops Key)
# ============================================================================
log_step 6 "Setting up keys..."

mkdir -p /root/.inference/keyring-file
mkdir -p /root/.inference/keyring-test

# Check if we have a mnemonic
if [ -z "$GONKA_MNEMONIC" ]; then
    echo ""
    log_info "Do you have an existing Gonka account with a mnemonic?"
    read -p "Enter 24-word mnemonic (or press Enter to create new account): " GONKA_MNEMONIC
fi

if [ -n "$GONKA_MNEMONIC" ]; then
    # Import existing account
    log_info "Importing account from mnemonic..."
    
    # Import to file backend with password using expect
    expect << EOF
spawn inferenced keys add gonka-account-key --recover --keyring-backend file
expect "mnemonic"
send "$GONKA_MNEMONIC\r"
expect "passphrase"
send "$KEYRING_PASSWORD\r"
expect "passphrase"
send "$KEYRING_PASSWORD\r"
expect eof
EOF
    
    ACCOUNT_ADDRESS=$(inferenced keys show gonka-account-key --keyring-backend file -a 2>/dev/null < <(echo "$KEYRING_PASSWORD") || echo "")
else
    # Create new account
    log_info "Creating new Gonka account..."
    
    expect << EOF
spawn inferenced keys add gonka-account-key --keyring-backend file
expect "passphrase"
send "$KEYRING_PASSWORD\r"
expect "passphrase"
send "$KEYRING_PASSWORD\r"
expect eof
EOF
    
    log_warn "SAVE YOUR MNEMONIC! It was displayed above."
fi

# Get account info
ACCOUNT_INFO=$(echo "$KEYRING_PASSWORD" | inferenced keys show gonka-account-key --keyring-backend file 2>/dev/null || echo "")
ACCOUNT_ADDRESS=$(echo "$ACCOUNT_INFO" | grep "address:" | awk '{print $2}')
ACCOUNT_PUBKEY=$(echo "$ACCOUNT_INFO" | grep "key:" | sed 's/.*key":"\([^"]*\)".*/\1/')

log_info "Account Address: $ACCOUNT_ADDRESS"

# Create ML Ops Key
log_info "Creating ML Ops key..."
expect << EOF
spawn inferenced keys add ml-ops-key --keyring-backend file
expect "passphrase"
send "$KEYRING_PASSWORD\r"
expect "passphrase"
send "$KEYRING_PASSWORD\r"
expect eof
EOF

ML_OPS_ADDRESS=$(echo "$KEYRING_PASSWORD" | inferenced keys show ml-ops-key --keyring-backend file -a 2>/dev/null || echo "")
log_info "ML Ops Address: $ML_OPS_ADDRESS"

log_success "Keys created"

# ============================================================================
# Step 7: Grant ML Ops Permissions
# ============================================================================
log_step 7 "Granting ML Ops permissions..."

if [ -n "$ACCOUNT_ADDRESS" ] && [ -n "$ML_OPS_ADDRESS" ]; then
    log_info "Submitting grant-ml-ops-permissions transaction..."
    
    echo "$KEYRING_PASSWORD" | inferenced tx inference grant-ml-ops-permissions \
        gonka-account-key \
        "$ML_OPS_ADDRESS" \
        --from gonka-account-key \
        --keyring-backend file \
        --node http://node2.gonka.ai:26657 \
        --chain-id gonka-mainnet \
        --gas 1000000 \
        -y 2>&1 | tail -5
    
    sleep 10
    log_success "ML Ops permissions granted"
else
    log_warn "Skipping grant - keys not properly created"
fi

# ============================================================================
# Step 8: Configure Gonka
# ============================================================================
log_step 8 "Configuring Gonka node..."

# Create shared directory for models
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
HF_HOME=/mnt/shared
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

# Register/update participant on network
if [ -n "$ACCOUNT_ADDRESS" ]; then
    log_info "Registering node on Gonka network..."
    
    echo "$KEYRING_PASSWORD" | inferenced tx inference submit-new-participant \
        "http://${SERVER_IP}:8000" \
        --keyring-backend file \
        --from gonka-account-key \
        --node http://node2.gonka.ai:26657 \
        --chain-id gonka-mainnet \
        --gas 1000000 \
        -y 2>&1 | tail -3
    
    sleep 10
    log_success "Node registered"
fi

# Download model
if [ "$SKIP_MODEL_DOWNLOAD" != "1" ]; then
    MODEL=$(cat node-config.json | jq -r '.[0].models | keys[0]' 2>/dev/null)
    
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
log_info "Account Information:"
echo "  Address:       $ACCOUNT_ADDRESS"
echo "  ML Ops:        $ML_OPS_ADDRESS"
echo "  Keyring Pass:  $KEYRING_PASSWORD"

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
log_info "Verify Registration:"
echo "  curl -s 'http://node2.gonka.ai:8000/chain-api/productscience/inference/inference/participant/$ACCOUNT_ADDRESS' | jq"

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

log_warn "IMPORTANT: Save your keyring password: $KEYRING_PASSWORD"
echo ""
log_success "Your Gonka node is ready! PoC validation happens every 24h. Happy earning! 💰"
