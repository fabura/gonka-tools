"""
Configuration management for Gonka.ai Toolchain.
"""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram Configuration
    telegram_bot_token: Optional[str] = Field(default=None, description="Telegram bot token")
    telegram_chat_id: Optional[str] = Field(default=None, description="Telegram chat ID for notifications")

    # Gonka Wallet
    gonka_wallet_address: Optional[str] = Field(default=None, description="Gonka wallet address")
    gonka_private_key: Optional[str] = Field(default=None, description="Gonka wallet private key")

    # Remote Servers
    remote_servers: Optional[str] = Field(default=None, description="Comma-separated list of remote servers")
    ssh_key_path: str = Field(default="~/.ssh/id_rsa", description="Path to SSH private key")

    # Monitoring Thresholds
    monitor_interval_seconds: int = Field(default=300, description="Monitoring interval in seconds")
    cpu_alert_threshold: int = Field(default=90, ge=0, le=100, description="CPU usage alert threshold")
    memory_alert_threshold: int = Field(default=85, ge=0, le=100, description="Memory usage alert threshold")
    disk_alert_threshold: int = Field(default=90, ge=0, le=100, description="Disk usage alert threshold")
    gpu_temp_alert_threshold: int = Field(default=85, description="GPU temperature alert threshold in Celsius")

    # Analytics
    analytics_export_path: str = Field(default="./exports", description="Path for analytics exports")

    def get_ssh_key_path(self) -> Path:
        """Get expanded SSH key path."""
        return Path(self.ssh_key_path).expanduser()

    def get_remote_servers_list(self) -> list[dict]:
        """Parse remote servers string into list of server configs."""
        if not self.remote_servers:
            return []

        servers = []
        for server in self.remote_servers.split(","):
            server = server.strip()
            if not server:
                continue

            # Parse user@host:port format
            if "@" in server:
                user, host_port = server.split("@", 1)
            else:
                user = "root"
                host_port = server

            if ":" in host_port:
                host, port = host_port.rsplit(":", 1)
                port = int(port)
            else:
                host = host_port
                port = 22

            servers.append({"user": user, "host": host, "port": port})

        return servers


class NodeConfig:
    """Configuration for a single Gonka node."""

    def __init__(self, data: dict):
        self.name = data.get("name", "unnamed")
        self.host = data.get("host")
        self.port = data.get("port", 22)
        self.user = data.get("user", "root")
        self.ssh_key = data.get("ssh_key", "~/.ssh/id_rsa")
        self.password = data.get("password")
        self.node_type = data.get("node_type", "inference")
        self.gpus = data.get("gpus", [])
        self.monitoring = data.get("monitoring", {"enabled": True})
        self.labels = data.get("labels", {})

    def get_ssh_key_path(self) -> Path:
        """Get expanded SSH key path."""
        return Path(self.ssh_key).expanduser()

    def __repr__(self) -> str:
        return f"NodeConfig(name={self.name}, host={self.host}, type={self.node_type})"


class NodesConfig:
    """Configuration for all Gonka nodes."""

    def __init__(self, data: dict):
        self.nodes = [NodeConfig(n) for n in data.get("nodes", [])]
        self.global_settings = data.get("global", {})

    @property
    def inferenced_path(self) -> str:
        return self.global_settings.get("inferenced_path", "/usr/local/bin/inferenced")

    @property
    def config_dir(self) -> str:
        return self.global_settings.get("config_dir", "/etc/gonka")

    @property
    def data_dir(self) -> str:
        return self.global_settings.get("data_dir", "/var/lib/gonka")

    def get_node(self, name: str) -> Optional[NodeConfig]:
        """Get node by name."""
        for node in self.nodes:
            if node.name == name:
                return node
        return None

    def get_enabled_for_monitoring(self) -> list[NodeConfig]:
        """Get nodes that have monitoring enabled."""
        return [n for n in self.nodes if n.monitoring.get("enabled", True)]


def load_nodes_config(config_path: Optional[str] = None) -> NodesConfig:
    """
    Load nodes configuration from YAML file.

    Args:
        config_path: Path to configuration file. If None, searches default locations.

    Returns:
        NodesConfig object with all node configurations.
    """
    if config_path:
        path = Path(config_path)
    else:
        # Search default locations
        search_paths = [
            Path("config/nodes.yaml"),
            Path("nodes.yaml"),
            Path.home() / ".config" / "gonka" / "nodes.yaml",
        ]
        path = None
        for p in search_paths:
            if p.exists():
                path = p
                break

        if path is None:
            # Return empty config if no file found
            return NodesConfig({"nodes": [], "global": {}})

    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}

    return NodesConfig(data)


# Singleton settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

