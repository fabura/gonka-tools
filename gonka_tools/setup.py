"""
Remote Server Setup Module for Gonka.ai Nodes.

This module provides functionality to set up and configure Gonka.ai compute nodes
on remote servers via SSH.
"""

import io
import sys
from pathlib import Path
from typing import Optional

import paramiko
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .config import NodeConfig, NodesConfig, get_settings, load_nodes_config

console = Console()


class RemoteExecutor:
    """Execute commands on remote servers via SSH."""

    def __init__(
        self,
        host: str,
        user: str = "root",
        port: int = 22,
        key_path: Optional[Path] = None,
        password: Optional[str] = None,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.password = password
        self.client: Optional[paramiko.SSHClient] = None

    def connect(self) -> bool:
        """Establish SSH connection."""
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs = {
                "hostname": self.host,
                "port": self.port,
                "username": self.user,
            }

            if self.key_path and self.key_path.exists():
                connect_kwargs["key_filename"] = str(self.key_path)
            elif self.password:
                connect_kwargs["password"] = self.password

            self.client.connect(**connect_kwargs)
            return True
        except Exception as e:
            console.print(f"[red]Failed to connect to {self.host}: {e}[/red]")
            return False

    def disconnect(self):
        """Close SSH connection."""
        if self.client:
            self.client.close()
            self.client = None

    def execute(self, command: str, sudo: bool = False) -> tuple[int, str, str]:
        """
        Execute command on remote server.

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        if not self.client:
            raise RuntimeError("Not connected to server")

        if sudo and self.user != "root":
            command = f"sudo {command}"

        stdin, stdout, stderr = self.client.exec_command(command)
        exit_code = stdout.channel.recv_exit_status()

        return exit_code, stdout.read().decode(), stderr.read().decode()

    def upload_file(self, local_path: Path, remote_path: str):
        """Upload file to remote server."""
        if not self.client:
            raise RuntimeError("Not connected to server")

        sftp = self.client.open_sftp()
        try:
            sftp.put(str(local_path), remote_path)
        finally:
            sftp.close()

    def upload_string(self, content: str, remote_path: str):
        """Upload string content as file to remote server."""
        if not self.client:
            raise RuntimeError("Not connected to server")

        sftp = self.client.open_sftp()
        try:
            with sftp.file(remote_path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()


class GonkaNodeSetup:
    """Setup and configure Gonka.ai nodes on remote servers."""

    # Installation script for Gonka node
    INSTALL_SCRIPT = '''#!/bin/bash
set -e

echo "=== Gonka.ai Node Installation Script ==="
echo "Starting installation at $(date)"

# Update system
echo "[1/7] Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq

# Install dependencies
echo "[2/7] Installing dependencies..."
apt-get install -y -qq curl wget git build-essential

# Install NVIDIA drivers and CUDA (if GPU present)
if lspci | grep -i nvidia > /dev/null 2>&1; then
    echo "[3/7] NVIDIA GPU detected, checking drivers..."
    if ! command -v nvidia-smi &> /dev/null; then
        echo "Installing NVIDIA drivers..."
        apt-get install -y -qq nvidia-driver-535 nvidia-cuda-toolkit
    else
        echo "NVIDIA drivers already installed"
        nvidia-smi --query-gpu=name,memory.total --format=csv
    fi
else
    echo "[3/7] No NVIDIA GPU detected, skipping driver installation"
fi

# Install Docker
echo "[4/7] Installing Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
else
    echo "Docker already installed"
fi

# Install NVIDIA Container Toolkit (for GPU support in Docker)
if lspci | grep -i nvidia > /dev/null 2>&1; then
    echo "[5/7] Installing NVIDIA Container Toolkit..."
    if ! dpkg -l | grep -q nvidia-container-toolkit; then
        distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
        curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
            sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
            tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
        apt-get update -qq
        apt-get install -y -qq nvidia-container-toolkit
        nvidia-ctk runtime configure --runtime=docker
        systemctl restart docker
    else
        echo "NVIDIA Container Toolkit already installed"
    fi
else
    echo "[5/7] Skipping NVIDIA Container Toolkit (no GPU)"
fi

# Download and install Gonka inferenced CLI
echo "[6/7] Installing Gonka inferenced CLI..."
GONKA_VERSION="${GONKA_VERSION:-latest}"
GONKA_INSTALL_DIR="/usr/local/bin"

# Download inferenced binary
if [ "$GONKA_VERSION" = "latest" ]; then
    curl -fsSL https://github.com/gonka-ai/gonka/releases/latest/download/inferenced-linux-amd64 -o $GONKA_INSTALL_DIR/inferenced
else
    curl -fsSL https://github.com/gonka-ai/gonka/releases/download/$GONKA_VERSION/inferenced-linux-amd64 -o $GONKA_INSTALL_DIR/inferenced
fi
chmod +x $GONKA_INSTALL_DIR/inferenced

# Create directories
echo "[7/7] Setting up directories and configuration..."
mkdir -p /etc/gonka
mkdir -p /var/lib/gonka
mkdir -p /var/log/gonka

# Verify installation
echo ""
echo "=== Installation Complete ==="
echo "Gonka inferenced version:"
inferenced --version || echo "Note: inferenced may need additional setup"
echo ""
echo "Next steps:"
echo "1. Generate your keys: inferenced keys generate"
echo "2. Configure your node: Edit /etc/gonka/config.yaml"
echo "3. Start the node: inferenced start"
'''

    # Systemd service file for Gonka node
    SYSTEMD_SERVICE = '''[Unit]
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
'''

    # Node configuration template
    NODE_CONFIG_TEMPLATE = '''# Gonka Node Configuration
# Generated by gonka-tools

network:
  # Network mode: mainnet, testnet
  mode: mainnet
  
  # Bootstrap nodes (leave empty for default)
  bootstrap_nodes: []

node:
  # Node type: network, inference
  type: {node_type}
  
  # Node name (for identification)
  name: "{node_name}"

identity:
  # Path to key file
  key_file: /etc/gonka/node.key

inference:
  # GPU devices to use (empty for all)
  devices: []
  
  # Maximum concurrent tasks
  max_concurrent: 4
  
  # Memory limit per task (in GB)
  memory_limit: 16

logging:
  level: info
  file: /var/log/gonka/inferenced.log
'''

    def __init__(self, nodes_config: Optional[NodesConfig] = None):
        self.nodes_config = nodes_config or load_nodes_config()
        self.settings = get_settings()

    def setup_node(
        self,
        node: NodeConfig,
        skip_install: bool = False,
        generate_keys: bool = True,
    ) -> bool:
        """
        Set up a single Gonka node on remote server.

        Args:
            node: Node configuration
            skip_install: Skip package installation
            generate_keys: Generate new keys for the node

        Returns:
            True if setup was successful
        """
        console.print(f"\n[bold cyan]Setting up node: {node.name}[/bold cyan]")
        console.print(f"  Host: {node.host}:{node.port}")
        console.print(f"  User: {node.user}")
        console.print(f"  Type: {node.node_type}")

        executor = RemoteExecutor(
            host=node.host,
            user=node.user,
            port=node.port,
            key_path=node.get_ssh_key_path(),
            password=node.password,
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            try:
                # Connect
                task = progress.add_task("Connecting to server...", total=None)
                if not executor.connect():
                    return False
                progress.update(task, description="[green]Connected!")

                if not skip_install:
                    # Upload and run installation script
                    task = progress.add_task("Installing dependencies...", total=None)
                    executor.upload_string(self.INSTALL_SCRIPT, "/tmp/gonka_install.sh")
                    exit_code, stdout, stderr = executor.execute("bash /tmp/gonka_install.sh", sudo=True)

                    if exit_code != 0:
                        console.print(f"[red]Installation failed:[/red]\n{stderr}")
                        return False
                    progress.update(task, description="[green]Dependencies installed!")

                # Upload systemd service
                task = progress.add_task("Configuring systemd service...", total=None)
                executor.upload_string(self.SYSTEMD_SERVICE, "/etc/systemd/system/gonka.service")
                executor.execute("systemctl daemon-reload", sudo=True)
                progress.update(task, description="[green]Systemd configured!")

                # Generate node configuration
                task = progress.add_task("Generating node configuration...", total=None)
                node_config = self.NODE_CONFIG_TEMPLATE.format(
                    node_type=node.node_type,
                    node_name=node.name,
                )
                executor.upload_string(node_config, "/etc/gonka/config.yaml")
                progress.update(task, description="[green]Configuration created!")

                # Generate keys if requested
                if generate_keys:
                    task = progress.add_task("Generating node keys...", total=None)
                    exit_code, stdout, stderr = executor.execute(
                        "inferenced keys generate --output /etc/gonka/node.key",
                        sudo=True,
                    )
                    if exit_code == 0:
                        progress.update(task, description="[green]Keys generated!")
                    else:
                        progress.update(task, description="[yellow]Key generation skipped (may already exist)")

                # Enable and start service
                task = progress.add_task("Starting Gonka service...", total=None)
                executor.execute("systemctl enable gonka", sudo=True)
                executor.execute("systemctl start gonka", sudo=True)
                progress.update(task, description="[green]Service started!")

                executor.disconnect()
                console.print(f"[bold green]✓ Node {node.name} setup complete![/bold green]")
                return True

            except Exception as e:
                console.print(f"[red]Setup failed: {e}[/red]")
                return False
            finally:
                executor.disconnect()

    def setup_all_nodes(self, skip_install: bool = False) -> dict[str, bool]:
        """
        Set up all configured nodes.

        Returns:
            Dictionary mapping node names to success status
        """
        results = {}
        for node in self.nodes_config.nodes:
            results[node.name] = self.setup_node(node, skip_install=skip_install)
        return results

    def check_node_status(self, node: NodeConfig) -> dict:
        """Check the status of a Gonka node."""
        executor = RemoteExecutor(
            host=node.host,
            user=node.user,
            port=node.port,
            key_path=node.get_ssh_key_path(),
            password=node.password,
        )

        status = {
            "name": node.name,
            "host": node.host,
            "reachable": False,
            "service_running": False,
            "gpu_available": False,
            "gpu_info": [],
        }

        try:
            if not executor.connect():
                return status

            status["reachable"] = True

            # Check service status
            exit_code, stdout, _ = executor.execute("systemctl is-active gonka")
            status["service_running"] = exit_code == 0 and "active" in stdout

            # Check GPU
            exit_code, stdout, _ = executor.execute("nvidia-smi --query-gpu=name,memory.total,temperature.gpu,utilization.gpu --format=csv,noheader")
            if exit_code == 0 and stdout.strip():
                status["gpu_available"] = True
                for line in stdout.strip().split("\n"):
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 4:
                        status["gpu_info"].append({
                            "name": parts[0],
                            "memory": parts[1],
                            "temperature": parts[2],
                            "utilization": parts[3],
                        })

        except Exception as e:
            status["error"] = str(e)
        finally:
            executor.disconnect()

        return status

    def print_status_table(self):
        """Print status of all nodes as a table."""
        table = Table(title="Gonka Nodes Status")
        table.add_column("Node", style="cyan")
        table.add_column("Host", style="dim")
        table.add_column("Reachable", justify="center")
        table.add_column("Service", justify="center")
        table.add_column("GPU", justify="center")
        table.add_column("GPU Info", style="dim")

        for node in self.nodes_config.nodes:
            status = self.check_node_status(node)

            reachable = "[green]✓[/green]" if status["reachable"] else "[red]✗[/red]"
            service = "[green]Running[/green]" if status["service_running"] else "[red]Stopped[/red]"
            gpu = "[green]✓[/green]" if status["gpu_available"] else "[dim]-[/dim]"

            gpu_info = ""
            if status["gpu_info"]:
                gpu_info = ", ".join([f"{g['name']} ({g['utilization']})" for g in status["gpu_info"]])

            table.add_row(node.name, f"{node.host}:{node.port}", reachable, service, gpu, gpu_info)

        console.print(table)


def setup_from_config(config_path: Optional[str] = None, node_name: Optional[str] = None):
    """
    Set up nodes from configuration file.

    Args:
        config_path: Path to nodes.yaml configuration
        node_name: Specific node to set up (None for all nodes)
    """
    nodes_config = load_nodes_config(config_path)
    setup = GonkaNodeSetup(nodes_config)

    if node_name:
        node = nodes_config.get_node(node_name)
        if not node:
            console.print(f"[red]Node '{node_name}' not found in configuration[/red]")
            return
        setup.setup_node(node)
    else:
        setup.setup_all_nodes()

