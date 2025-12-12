#!/bin/bash
#
# Gonka.ai Quick Node Setup Script
# ================================
# This script can be run directly on a server to set up a Gonka compute node.
#
# Usage:
#   curl -fsSL https://your-domain.com/setup.sh | bash
#   # or
#   ./quick_setup.sh
#
# Environment variables (optional):
#   GONKA_VERSION - Version to install (default: latest)
#   SKIP_NVIDIA   - Set to "1" to skip NVIDIA driver installation
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log_info() { echo -e "${CYAN}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo ""
echo "======================================"
echo "  Gonka.ai Node Setup Script"
echo "======================================"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    log_error "Please run as root (sudo)"
    exit 1
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

# Update system
log_info "Updating system packages..."
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

# Install basic dependencies
log_info "Installing dependencies..."
$INSTALL_CMD curl wget git build-essential jq
log_success "Dependencies installed"

# Check for NVIDIA GPU
HAS_GPU=false
if lspci 2>/dev/null | grep -i nvidia > /dev/null; then
    HAS_GPU=true
    log_info "NVIDIA GPU detected"
fi

# Install NVIDIA drivers if GPU present and not skipped
if [ "$HAS_GPU" = true ] && [ "$SKIP_NVIDIA" != "1" ]; then
    if ! command -v nvidia-smi &> /dev/null; then
        log_info "Installing NVIDIA drivers..."
        if [ "$OS" = "ubuntu" ]; then
            $INSTALL_CMD nvidia-driver-535 nvidia-cuda-toolkit
        elif [ "$OS" = "debian" ]; then
            # Add non-free repo
            sed -i 's/main/main contrib non-free/g' /etc/apt/sources.list
            apt-get update -qq
            $INSTALL_CMD nvidia-driver nvidia-cuda-toolkit
        else
            log_warn "Please install NVIDIA drivers manually for $OS"
        fi
        log_success "NVIDIA drivers installed"
    else
        log_success "NVIDIA drivers already installed"
    fi
    
    # Show GPU info
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true
fi

# Install Docker
if ! command -v docker &> /dev/null; then
    log_info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    log_success "Docker installed"
else
    log_success "Docker already installed"
fi

# Install NVIDIA Container Toolkit for GPU Docker support
if [ "$HAS_GPU" = true ]; then
    if ! dpkg -l 2>/dev/null | grep -q nvidia-container-toolkit; then
        log_info "Installing NVIDIA Container Toolkit..."
        
        if [ "$OS" = "ubuntu" ] || [ "$OS" = "debian" ]; then
            distribution="${OS}${VERSION_ID}"
            curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
                gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
            curl -s -L "https://nvidia.github.io/libnvidia-container/${distribution}/libnvidia-container.list" | \
                sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
                tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
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

# Download and install Gonka inferenced CLI
GONKA_VERSION="${GONKA_VERSION:-latest}"
GONKA_INSTALL_DIR="/usr/local/bin"

log_info "Installing Gonka inferenced CLI (${GONKA_VERSION})..."

# Try to download from GitHub releases (adjust URL as needed)
DOWNLOAD_URL="https://github.com/gonka-ai/gonka/releases"
if [ "$GONKA_VERSION" = "latest" ]; then
    DOWNLOAD_URL="${DOWNLOAD_URL}/latest/download/inferenced-linux-amd64"
else
    DOWNLOAD_URL="${DOWNLOAD_URL}/download/${GONKA_VERSION}/inferenced-linux-amd64"
fi

if curl -fsSL -o "$GONKA_INSTALL_DIR/inferenced" "$DOWNLOAD_URL" 2>/dev/null; then
    chmod +x "$GONKA_INSTALL_DIR/inferenced"
    log_success "Gonka CLI installed"
else
    log_warn "Could not download inferenced binary. Please install manually."
    log_info "Visit: https://gonka.ai for installation instructions"
fi

# Create directories
log_info "Creating Gonka directories..."
mkdir -p /etc/gonka
mkdir -p /var/lib/gonka
mkdir -p /var/log/gonka
log_success "Directories created"

# Create basic config if inferenced is available
if command -v inferenced &> /dev/null; then
    log_info "Generating initial configuration..."
    
    cat > /etc/gonka/config.yaml << 'EOF'
# Gonka Node Configuration
# Edit this file to customize your node settings

network:
  mode: mainnet
  bootstrap_nodes: []

node:
  type: inference
  name: "gonka-node"

identity:
  key_file: /etc/gonka/node.key

inference:
  devices: []
  max_concurrent: 4
  memory_limit: 16

logging:
  level: info
  file: /var/log/gonka/inferenced.log
EOF
    
    log_success "Configuration created at /etc/gonka/config.yaml"
fi

# Create systemd service
log_info "Creating systemd service..."
cat > /etc/systemd/system/gonka.service << 'EOF'
[Unit]
Description=Gonka.ai Inference Node
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/inferenced start --config /etc/gonka/config.yaml
ExecStop=/usr/local/bin/inferenced stop
Restart=always
RestartSec=10
StandardOutput=append:/var/log/gonka/inferenced.log
StandardError=append:/var/log/gonka/inferenced.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
log_success "Systemd service created"

# Summary
echo ""
echo "======================================"
echo "  Setup Complete!"
echo "======================================"
echo ""
log_info "Next steps:"
echo "  1. Generate keys:     inferenced keys generate --output /etc/gonka/node.key"
echo "  2. Edit config:       nano /etc/gonka/config.yaml"
echo "  3. Start service:     systemctl start gonka"
echo "  4. Enable on boot:    systemctl enable gonka"
echo "  5. Check status:      systemctl status gonka"
echo ""
if [ "$HAS_GPU" = true ]; then
    log_info "GPU Status:"
    nvidia-smi --query-gpu=name,memory.total,temperature.gpu --format=csv 2>/dev/null || echo "  Run 'nvidia-smi' to check GPU status"
fi
echo ""
log_success "Happy mining! 🚀"

