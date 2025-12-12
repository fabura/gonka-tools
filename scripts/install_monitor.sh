#!/bin/bash
#
# Install Local Monitoring Script
# ================================
# Installs the standalone monitoring script on a Gonka node
# and sets it up as a systemd service.
#
# Usage:
#   ./install_monitor.sh --token YOUR_BOT_TOKEN --chat YOUR_CHAT_ID
#

set -e

# Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${CYAN}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Parse arguments
BOT_TOKEN=""
CHAT_ID=""
INTERVAL=300
NODE_NAME=$(hostname)

while [[ $# -gt 0 ]]; do
    case $1 in
        --token)
            BOT_TOKEN="$2"
            shift 2
            ;;
        --chat)
            CHAT_ID="$2"
            shift 2
            ;;
        --interval)
            INTERVAL="$2"
            shift 2
            ;;
        --name)
            NODE_NAME="$2"
            shift 2
            ;;
        *)
            log_error "Unknown option: $1"
            echo "Usage: $0 --token BOT_TOKEN --chat CHAT_ID [--interval SECONDS] [--name NODE_NAME]"
            exit 1
            ;;
    esac
done

if [ -z "$BOT_TOKEN" ] || [ -z "$CHAT_ID" ]; then
    log_error "Bot token and chat ID are required"
    echo "Usage: $0 --token BOT_TOKEN --chat CHAT_ID [--interval SECONDS] [--name NODE_NAME]"
    exit 1
fi

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    log_error "Please run as root"
    exit 1
fi

log_info "Installing Gonka Monitor for node: $NODE_NAME"

# Install Python3 and pip if needed
if ! command -v python3 &> /dev/null; then
    log_info "Installing Python3..."
    apt-get update -qq
    apt-get install -y python3 python3-pip
fi

# Install httpx
log_info "Installing Python dependencies..."
pip3 install httpx -q

# Create monitor script
log_info "Creating monitor script..."
cat > /opt/gonka-monitor.py << 'MONITOR_EOF'
#!/usr/bin/env python3
"""
Standalone Gonka Node Monitor
"""

import argparse
import asyncio
import subprocess
import time
from datetime import datetime

try:
    import httpx
except ImportError:
    print("Installing httpx...")
    subprocess.run(["pip3", "install", "httpx"], check=True)
    import httpx


class LocalMonitor:
    def __init__(self, bot_token: str, chat_id: str, node_name: str = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.node_name = node_name or subprocess.getoutput("hostname")
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self._last_alerts = {}

    async def send_alert(self, level: str, title: str, details: str):
        """Send alert to Telegram with cooldown."""
        alert_key = f"{level}:{title}"
        now = time.time()
        
        if alert_key in self._last_alerts:
            if now - self._last_alerts[alert_key] < 300:
                return
        
        emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "📢")
        message = f"""{emoji} <b>Gonka Alert</b>

<b>Node:</b> {self.node_name}
<b>Level:</b> {level.upper()}
<b>Issue:</b> {title}

{details}

<i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"""

        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self.base_url}/sendMessage",
                    json={"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"},
                    timeout=10,
                )
            self._last_alerts[alert_key] = now
        except Exception as e:
            print(f"Failed to send alert: {e}")

    def get_cpu_usage(self) -> float:
        try:
            output = subprocess.getoutput("top -bn1 | grep 'Cpu(s)' | awk '{print $2}'")
            return float(output.replace("%", "").strip())
        except:
            return 0.0

    def get_memory_usage(self) -> float:
        try:
            output = subprocess.getoutput("free | grep Mem | awk '{print ($3/$2) * 100.0}'")
            return float(output.strip())
        except:
            return 0.0

    def get_disk_usage(self) -> float:
        try:
            output = subprocess.getoutput("df -h / | tail -1 | awk '{print $5}'")
            return float(output.replace("%", "").strip())
        except:
            return 0.0

    def get_gpu_temps(self) -> list:
        try:
            output = subprocess.getoutput(
                "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null"
            )
            if output:
                return [float(t.strip()) for t in output.split("\n") if t.strip()]
        except:
            pass
        return []

    def is_gonka_running(self) -> bool:
        try:
            output = subprocess.getoutput("systemctl is-active gonka 2>/dev/null")
            return output.strip() == "active"
        except:
            return False

    async def check_all(
        self,
        cpu_threshold: float = 90,
        mem_threshold: float = 85,
        disk_threshold: float = 90,
        gpu_temp_threshold: float = 85,
    ):
        """Check all metrics and send alerts if needed."""
        cpu = self.get_cpu_usage()
        mem = self.get_memory_usage()
        disk = self.get_disk_usage()
        gpu_temps = self.get_gpu_temps()
        gonka_running = self.is_gonka_running()

        if cpu >= cpu_threshold:
            await self.send_alert("warning", "High CPU", f"CPU: {cpu:.1f}%")

        if mem >= mem_threshold:
            await self.send_alert("warning", "High Memory", f"Memory: {mem:.1f}%")

        if disk >= disk_threshold:
            await self.send_alert("warning", "High Disk", f"Disk: {disk:.1f}%")

        for i, temp in enumerate(gpu_temps):
            if temp >= gpu_temp_threshold:
                await self.send_alert("warning", f"High GPU {i} Temp", f"Temperature: {temp}°C")

        if not gonka_running:
            await self.send_alert("critical", "Gonka Stopped", "Service is not running!")

        return {
            "cpu": cpu,
            "memory": mem,
            "disk": disk,
            "gpu_temps": gpu_temps,
            "gonka_running": gonka_running,
        }

    async def run(self, interval: int = 300):
        """Run monitoring loop."""
        print(f"Starting local monitor for {self.node_name}")
        print(f"Interval: {interval}s, Telegram: {self.chat_id}")
        
        while True:
            try:
                metrics = await self.check_all()
                print(f"[{datetime.now()}] CPU: {metrics['cpu']:.1f}%, "
                      f"MEM: {metrics['memory']:.1f}%, "
                      f"Gonka: {'Running' if metrics['gonka_running'] else 'STOPPED'}")
                await asyncio.sleep(interval)
            except KeyboardInterrupt:
                print("\nStopped")
                break
            except Exception as e:
                print(f"Error: {e}")
                await asyncio.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local Gonka node monitor")
    parser.add_argument("--token", required=True, help="Telegram bot token")
    parser.add_argument("--chat", required=True, help="Telegram chat ID")
    parser.add_argument("--name", help="Node name (default: hostname)")
    parser.add_argument("--interval", type=int, default=300, help="Check interval in seconds")
    args = parser.parse_args()

    monitor = LocalMonitor(args.token, args.chat, args.name)
    asyncio.run(monitor.run(args.interval))
MONITOR_EOF

chmod +x /opt/gonka-monitor.py
log_success "Monitor script created"

# Create systemd service
log_info "Creating systemd service..."
cat > /etc/systemd/system/gonka-monitor.service << EOF
[Unit]
Description=Gonka Local Monitor
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/gonka-monitor.py --token ${BOT_TOKEN} --chat ${CHAT_ID} --name ${NODE_NAME} --interval ${INTERVAL}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable gonka-monitor
systemctl start gonka-monitor

log_success "Monitor service installed and started"

# Show status
echo ""
systemctl status gonka-monitor --no-pager || true
echo ""
log_info "Commands:"
echo "  Status:   systemctl status gonka-monitor"
echo "  Logs:     journalctl -u gonka-monitor -f"
echo "  Stop:     systemctl stop gonka-monitor"
echo "  Restart:  systemctl restart gonka-monitor"

