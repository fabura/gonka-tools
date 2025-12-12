# 🚀 Gonka.ai Toolchain

A comprehensive toolkit for managing Gonka.ai compute nodes, monitoring their health, and analyzing mining performance.

## Features

- **🖥️ Remote Node Setup** - Automated deployment of Gonka compute nodes on remote servers
- **📊 Health Monitoring** - Continuous monitoring with Telegram notifications
- **💰 Earnings Analytics** - Track and analyze your mining performance
- **⚡ CLI Interface** - Easy-to-use command-line tools

## Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/gonka-tools.git
cd gonka-tools

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dependencies
pip install -e .
```

### Configuration

1. Copy the example configuration:
```bash
cp config/env.example .env
cp config/nodes.yaml.example config/nodes.yaml
```

2. Edit `.env` with your credentials:
```bash
# Telegram (for monitoring alerts)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Gonka wallet
GONKA_WALLET_ADDRESS=your_wallet_address
```

3. Edit `config/nodes.yaml` with your server details:
```yaml
nodes:
  - name: "my-gpu-server"
    host: "192.168.1.100"
    port: 22
    user: "root"
    ssh_key: "~/.ssh/id_rsa"
    node_type: "inference"
```

## Usage

### Node Setup

Set up a compute node on a remote server:

```bash
# Using configuration file
gonka setup node my-gpu-server

# Or specify connection directly
gonka setup node --host 192.168.1.100 --user root --key ~/.ssh/id_rsa

# Set up all configured nodes
gonka setup all

# Check status of all nodes
gonka setup status
```

### Monitoring

Monitor your nodes with Telegram alerts:

```bash
# Start continuous monitoring
gonka monitor start --interval 300

# Run a single check
gonka monitor once

# Send status report to Telegram
gonka monitor report

# Generate standalone monitoring script for local deployment
gonka monitor deploy-local --output my_monitor.py
```

### Analytics

Track and analyze your mining performance:

```bash
# Show earnings summary
gonka analytics summary
gonka analytics summary --days 30

# Generate full report
gonka analytics report

# Add manual earnings entry
gonka analytics add 1.5 --node gpu-server-1

# Export to CSV
gonka analytics export --output earnings.csv

# Show daily chart
gonka analytics chart --days 14

# Show earnings by node
gonka analytics nodes
```

### Quick Commands

```bash
# Quick status check
gonka status

# Quick earnings summary
gonka earnings --days 7
```

## Configuration Reference

### Environment Variables (.env)

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather | - |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID | - |
| `GONKA_WALLET_ADDRESS` | Your Gonka wallet address | - |
| `SSH_KEY_PATH` | Default SSH key path | `~/.ssh/id_rsa` |
| `MONITOR_INTERVAL_SECONDS` | Monitoring check interval | `300` |
| `CPU_ALERT_THRESHOLD` | CPU usage alert % | `90` |
| `MEMORY_ALERT_THRESHOLD` | Memory usage alert % | `85` |
| `DISK_ALERT_THRESHOLD` | Disk usage alert % | `90` |
| `GPU_TEMP_ALERT_THRESHOLD` | GPU temperature alert °C | `85` |

### Nodes Configuration (nodes.yaml)

```yaml
nodes:
  - name: "gpu-server-1"        # Unique identifier
    host: "192.168.1.100"       # Server IP or hostname
    port: 22                    # SSH port
    user: "root"                # SSH username
    ssh_key: "~/.ssh/id_rsa"    # SSH private key path
    node_type: "inference"      # "network" or "inference"
    gpus:
      - model: "H100"
        count: 4
    monitoring:
      enabled: true
      cpu_threshold: 90
      memory_threshold: 85
    labels:
      datacenter: "dc1"

global:
  inferenced_path: "/usr/local/bin/inferenced"
  config_dir: "/etc/gonka"
  data_dir: "/var/lib/gonka"
```

## Setting Up Telegram Notifications

1. Create a bot with [@BotFather](https://t.me/BotFather) on Telegram
2. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot)
3. Add the bot token and chat ID to your `.env` file

## Deploying Local Monitoring

For servers where you want local monitoring (e.g., to catch issues even when the main monitoring system is down):

```bash
# Generate the standalone script
gonka monitor deploy-local --output local_monitor.py

# Copy to your server
scp local_monitor.py root@your-server:/root/

# On the server, run it
python3 local_monitor.py --token YOUR_BOT_TOKEN --chat YOUR_CHAT_ID --interval 300

# Or set up as a systemd service
```

Example systemd service (`/etc/systemd/system/gonka-monitor.service`):

```ini
[Unit]
Description=Gonka Local Monitor
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /root/local_monitor.py --token YOUR_TOKEN --chat YOUR_CHAT
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Project Structure

```
gonka.ai/
├── gonka_tools/
│   ├── __init__.py      # Package initialization
│   ├── config.py        # Configuration management
│   ├── setup.py         # Remote node setup
│   ├── monitor.py       # Health monitoring
│   ├── analytics.py     # Earnings analytics
│   └── cli.py           # CLI interface
├── config/
│   ├── env.example      # Environment variables template
│   └── nodes.yaml.example  # Nodes configuration template
├── exports/             # Analytics exports directory
├── gonka               # CLI entry point script
├── pyproject.toml      # Python project configuration
├── requirements.txt    # Dependencies
└── README.md           # This file
```

## Contributing

Contributions are welcome! Please feel free to submit issues and pull requests.

## License

MIT License - see LICENSE file for details.

## Disclaimer

This is an unofficial toolchain for Gonka.ai. Please refer to the official Gonka documentation for the most up-to-date information about the platform.

