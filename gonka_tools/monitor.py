"""
Monitoring Module for Gonka.ai Nodes.

Provides health monitoring with Telegram notifications for alerting.
Can be run as a standalone daemon or triggered periodically via cron.
"""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
import paramiko
from rich.console import Console
from rich.live import Live
from rich.table import Table

from .config import NodeConfig, NodesConfig, Settings, get_settings, load_nodes_config

console = Console()


class AlertLevel(Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class NodeMetrics:
    """Metrics collected from a node."""
    timestamp: datetime
    node_name: str
    host: str
    
    # Connectivity
    reachable: bool = False
    
    # System metrics
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    disk_percent: float = 0.0
    load_avg: tuple[float, float, float] = (0.0, 0.0, 0.0)
    
    # GPU metrics
    gpu_available: bool = False
    gpu_count: int = 0
    gpu_utilization: list[float] = None
    gpu_memory_used: list[float] = None
    gpu_temperature: list[float] = None
    
    # Gonka service
    service_running: bool = False
    gonka_version: str = ""
    
    # Errors
    error: Optional[str] = None

    def __post_init__(self):
        if self.gpu_utilization is None:
            self.gpu_utilization = []
        if self.gpu_memory_used is None:
            self.gpu_memory_used = []
        if self.gpu_temperature is None:
            self.gpu_temperature = []


class TelegramNotifier:
    """Send notifications via Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        
        # Rate limiting
        self._last_notification_time: dict[str, float] = {}
        self._cooldown_seconds = 300  # 5 minutes between same alerts

    async def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message to Telegram."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": message,
                        "parse_mode": parse_mode,
                    },
                    timeout=10,
                )
                return response.status_code == 200
        except Exception as e:
            console.print(f"[red]Failed to send Telegram message: {e}[/red]")
            return False

    async def send_alert(
        self,
        node_name: str,
        level: AlertLevel,
        title: str,
        details: str,
    ) -> bool:
        """
        Send an alert notification with rate limiting.
        """
        # Generate unique key for this alert type
        alert_key = f"{node_name}:{level.value}:{title}"
        
        # Check cooldown
        now = time.time()
        if alert_key in self._last_notification_time:
            elapsed = now - self._last_notification_time[alert_key]
            if elapsed < self._cooldown_seconds:
                return False  # Skip, still in cooldown
        
        # Format alert message
        level_emoji = {
            AlertLevel.INFO: "ℹ️",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.CRITICAL: "🚨",
        }
        
        message = f"""{level_emoji.get(level, "📢")} <b>Gonka Node Alert</b>

<b>Node:</b> {node_name}
<b>Level:</b> {level.value.upper()}
<b>Issue:</b> {title}

{details}

<i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"""

        success = await self.send_message(message)
        if success:
            self._last_notification_time[alert_key] = now
        return success

    async def send_status_report(self, metrics_list: list[NodeMetrics]) -> bool:
        """Send a status summary report."""
        healthy = sum(1 for m in metrics_list if m.reachable and m.service_running)
        total = len(metrics_list)
        
        lines = [
            "📊 <b>Gonka Nodes Status Report</b>",
            "",
            f"<b>Healthy:</b> {healthy}/{total} nodes",
            "",
        ]
        
        for metrics in metrics_list:
            status = "✅" if (metrics.reachable and metrics.service_running) else "❌"
            gpu_info = f"GPU: {metrics.gpu_count}" if metrics.gpu_available else "No GPU"
            lines.append(
                f"{status} <b>{metrics.node_name}</b> - "
                f"CPU: {metrics.cpu_percent:.1f}% | "
                f"MEM: {metrics.memory_percent:.1f}% | "
                f"{gpu_info}"
            )
        
        lines.extend(["", f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>"])
        
        return await self.send_message("\n".join(lines))


class NodeMonitor:
    """Monitor Gonka nodes and collect metrics."""

    def __init__(
        self,
        nodes_config: Optional[NodesConfig] = None,
        settings: Optional[Settings] = None,
    ):
        self.nodes_config = nodes_config or load_nodes_config()
        self.settings = settings or get_settings()
        
        # Initialize Telegram notifier if configured
        self.notifier: Optional[TelegramNotifier] = None
        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
            self.notifier = TelegramNotifier(
                self.settings.telegram_bot_token,
                self.settings.telegram_chat_id,
            )

    def collect_metrics(self, node: NodeConfig) -> NodeMetrics:
        """Collect metrics from a single node via SSH."""
        metrics = NodeMetrics(
            timestamp=datetime.now(),
            node_name=node.name,
            host=node.host,
        )

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            connect_kwargs = {
                "hostname": node.host,
                "port": node.port,
                "username": node.user,
                "timeout": 10,
            }
            
            key_path = node.get_ssh_key_path()
            if key_path.exists():
                connect_kwargs["key_filename"] = str(key_path)
            elif node.password:
                connect_kwargs["password"] = node.password

            client.connect(**connect_kwargs)
            metrics.reachable = True

            # Collect system metrics
            self._collect_system_metrics(client, metrics)
            
            # Collect GPU metrics
            self._collect_gpu_metrics(client, metrics)
            
            # Check Gonka service
            self._collect_gonka_metrics(client, metrics)

            client.close()

        except Exception as e:
            metrics.error = str(e)

        return metrics

    def _collect_system_metrics(self, client: paramiko.SSHClient, metrics: NodeMetrics):
        """Collect CPU, memory, disk metrics."""
        try:
            # CPU usage
            _, stdout, _ = client.exec_command(
                "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' | cut -d'%' -f1"
            )
            cpu_out = stdout.read().decode().strip()
            if cpu_out:
                metrics.cpu_percent = float(cpu_out)

            # Memory usage
            _, stdout, _ = client.exec_command(
                "free | grep Mem | awk '{print ($3/$2) * 100.0}'"
            )
            mem_out = stdout.read().decode().strip()
            if mem_out:
                metrics.memory_percent = float(mem_out)

            # Disk usage
            _, stdout, _ = client.exec_command(
                "df -h / | tail -1 | awk '{print $5}' | cut -d'%' -f1"
            )
            disk_out = stdout.read().decode().strip()
            if disk_out:
                metrics.disk_percent = float(disk_out)

            # Load average
            _, stdout, _ = client.exec_command("cat /proc/loadavg | awk '{print $1, $2, $3}'")
            load_out = stdout.read().decode().strip().split()
            if len(load_out) >= 3:
                metrics.load_avg = (float(load_out[0]), float(load_out[1]), float(load_out[2]))

        except Exception:
            pass

    def _collect_gpu_metrics(self, client: paramiko.SSHClient, metrics: NodeMetrics):
        """Collect NVIDIA GPU metrics."""
        try:
            _, stdout, _ = client.exec_command(
                "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu "
                "--format=csv,noheader,nounits 2>/dev/null"
            )
            gpu_out = stdout.read().decode().strip()
            
            if gpu_out:
                metrics.gpu_available = True
                for line in gpu_out.split("\n"):
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 4:
                        metrics.gpu_count += 1
                        metrics.gpu_utilization.append(float(parts[0]))
                        mem_used = float(parts[1])
                        mem_total = float(parts[2])
                        metrics.gpu_memory_used.append((mem_used / mem_total) * 100 if mem_total > 0 else 0)
                        metrics.gpu_temperature.append(float(parts[3]))

        except Exception:
            pass

    def _collect_gonka_metrics(self, client: paramiko.SSHClient, metrics: NodeMetrics):
        """Check Gonka service status."""
        try:
            # Check service status
            _, stdout, _ = client.exec_command("systemctl is-active gonka 2>/dev/null")
            status = stdout.read().decode().strip()
            metrics.service_running = status == "active"

            # Get version
            _, stdout, _ = client.exec_command("inferenced --version 2>/dev/null || echo ''")
            metrics.gonka_version = stdout.read().decode().strip()

        except Exception:
            pass

    async def check_and_alert(self, metrics: NodeMetrics) -> list[tuple[AlertLevel, str, str]]:
        """
        Check metrics against thresholds and send alerts.
        
        Returns list of (level, title, details) tuples for alerts sent.
        """
        alerts = []
        node_config = self.nodes_config.get_node(metrics.node_name)
        
        # Get thresholds from node config or defaults
        if node_config and node_config.monitoring:
            thresholds = node_config.monitoring
        else:
            thresholds = {}
        
        cpu_threshold = thresholds.get("cpu_threshold", self.settings.cpu_alert_threshold)
        memory_threshold = thresholds.get("memory_threshold", self.settings.memory_alert_threshold)
        disk_threshold = thresholds.get("disk_threshold", self.settings.disk_alert_threshold)
        gpu_temp_threshold = thresholds.get("gpu_temp_threshold", self.settings.gpu_temp_alert_threshold)

        # Check node reachability
        if not metrics.reachable:
            alerts.append((
                AlertLevel.CRITICAL,
                "Node Unreachable",
                f"Cannot connect to node.\nError: {metrics.error or 'Connection timeout'}",
            ))
        else:
            # Check service status
            if not metrics.service_running:
                alerts.append((
                    AlertLevel.CRITICAL,
                    "Gonka Service Stopped",
                    "The Gonka inferenced service is not running.",
                ))

            # Check CPU
            if metrics.cpu_percent >= cpu_threshold:
                alerts.append((
                    AlertLevel.WARNING,
                    "High CPU Usage",
                    f"CPU usage is at {metrics.cpu_percent:.1f}% (threshold: {cpu_threshold}%)",
                ))

            # Check memory
            if metrics.memory_percent >= memory_threshold:
                alerts.append((
                    AlertLevel.WARNING,
                    "High Memory Usage",
                    f"Memory usage is at {metrics.memory_percent:.1f}% (threshold: {memory_threshold}%)",
                ))

            # Check disk
            if metrics.disk_percent >= disk_threshold:
                alerts.append((
                    AlertLevel.WARNING,
                    "High Disk Usage",
                    f"Disk usage is at {metrics.disk_percent:.1f}% (threshold: {disk_threshold}%)",
                ))

            # Check GPU temperatures
            for i, temp in enumerate(metrics.gpu_temperature):
                if temp >= gpu_temp_threshold:
                    alerts.append((
                        AlertLevel.WARNING,
                        f"High GPU Temperature (GPU {i})",
                        f"GPU {i} temperature is {temp}°C (threshold: {gpu_temp_threshold}°C)",
                    ))

        # Send alerts via Telegram
        if self.notifier:
            for level, title, details in alerts:
                await self.notifier.send_alert(metrics.node_name, level, title, details)

        return alerts

    async def monitor_once(self) -> list[NodeMetrics]:
        """Run a single monitoring pass on all nodes."""
        all_metrics = []
        
        for node in self.nodes_config.get_enabled_for_monitoring():
            metrics = self.collect_metrics(node)
            all_metrics.append(metrics)
            await self.check_and_alert(metrics)
        
        return all_metrics

    async def monitor_loop(self, interval: Optional[int] = None):
        """
        Run continuous monitoring loop.
        
        Args:
            interval: Check interval in seconds. Uses settings default if None.
        """
        interval = interval or self.settings.monitor_interval_seconds
        console.print(f"[bold cyan]Starting Gonka node monitoring[/bold cyan]")
        console.print(f"Interval: {interval} seconds")
        console.print(f"Nodes: {len(self.nodes_config.nodes)}")
        console.print(f"Telegram: {'Enabled' if self.notifier else 'Disabled'}")
        console.print("-" * 40)

        while True:
            try:
                all_metrics = await self.monitor_once()
                self._print_metrics_table(all_metrics)
                await asyncio.sleep(interval)
            except KeyboardInterrupt:
                console.print("\n[yellow]Monitoring stopped[/yellow]")
                break
            except Exception as e:
                console.print(f"[red]Monitoring error: {e}[/red]")
                await asyncio.sleep(interval)

    def _print_metrics_table(self, metrics_list: list[NodeMetrics]):
        """Print metrics as a formatted table."""
        table = Table(title=f"Node Metrics - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        table.add_column("Node", style="cyan")
        table.add_column("Status", justify="center")
        table.add_column("CPU", justify="right")
        table.add_column("Memory", justify="right")
        table.add_column("Disk", justify="right")
        table.add_column("GPU", justify="center")
        table.add_column("GPU Temp", justify="right")
        table.add_column("Service", justify="center")

        for m in metrics_list:
            status = "[green]●[/green]" if m.reachable else "[red]●[/red]"
            cpu = f"{m.cpu_percent:.1f}%" if m.reachable else "-"
            mem = f"{m.memory_percent:.1f}%" if m.reachable else "-"
            disk = f"{m.disk_percent:.1f}%" if m.reachable else "-"
            
            gpu = f"{m.gpu_count}x" if m.gpu_available else "-"
            gpu_temp = (
                f"{max(m.gpu_temperature):.0f}°C" 
                if m.gpu_temperature 
                else "-"
            )
            
            service = (
                "[green]Running[/green]" 
                if m.service_running 
                else "[red]Stopped[/red]"
            ) if m.reachable else "-"

            table.add_row(m.node_name, status, cpu, mem, disk, gpu, gpu_temp, service)

        console.print(table)

    async def send_status_report(self):
        """Send a status report via Telegram."""
        if not self.notifier:
            console.print("[yellow]Telegram not configured, cannot send report[/yellow]")
            return

        all_metrics = await self.monitor_once()
        await self.notifier.send_status_report(all_metrics)
        console.print("[green]Status report sent to Telegram[/green]")


# Standalone monitoring script for deployment on remote servers
STANDALONE_MONITOR_SCRIPT = '''#!/usr/bin/env python3
"""
Standalone Gonka Node Monitor
Run this on the node itself for local monitoring with Telegram alerts.

Usage:
    python3 local_monitor.py --token YOUR_BOT_TOKEN --chat YOUR_CHAT_ID
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
                return  # Skip, in cooldown
        
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
                return [float(t.strip()) for t in output.split("\\n") if t.strip()]
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
                print("\\nStopped")
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
'''


def get_standalone_monitor_script() -> str:
    """Get the standalone monitoring script for deployment."""
    return STANDALONE_MONITOR_SCRIPT


async def run_monitor(interval: Optional[int] = None):
    """Run the monitoring loop."""
    monitor = NodeMonitor()
    await monitor.monitor_loop(interval)


async def send_report():
    """Send a status report via Telegram."""
    monitor = NodeMonitor()
    await monitor.send_status_report()

