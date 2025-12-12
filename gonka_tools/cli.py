"""
CLI Interface for Gonka.ai Toolchain.

Provides command-line access to all toolchain functionality.
"""

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from . import __version__
from .analytics import AnalyticsDashboard, EarningsTracker, export_earnings, print_full_report
from .config import load_nodes_config, get_settings
from .monitor import NodeMonitor, get_standalone_monitor_script, run_monitor, send_report
from .setup import GonkaNodeSetup, setup_from_config

console = Console()
app = typer.Typer(
    name="gonka",
    help="🚀 Gonka.ai Toolchain - Manage your compute nodes",
    add_completion=False,
)

# Sub-applications
setup_app = typer.Typer(help="Node setup and deployment commands")
monitor_app = typer.Typer(help="Monitoring and alerting commands")
analytics_app = typer.Typer(help="Earnings analytics commands")
config_app = typer.Typer(help="Configuration management")

app.add_typer(setup_app, name="setup")
app.add_typer(monitor_app, name="monitor")
app.add_typer(analytics_app, name="analytics")
app.add_typer(config_app, name="config")


def version_callback(value: bool):
    if value:
        console.print(f"[cyan]Gonka.ai Toolchain[/cyan] v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
):
    """
    🚀 Gonka.ai Toolchain
    
    A comprehensive toolkit for managing Gonka.ai compute nodes.
    """
    pass


# =============================================================================
# Setup Commands
# =============================================================================

@setup_app.command("node")
def setup_node(
    node_name: Optional[str] = typer.Argument(None, help="Name of node to set up (from config)"),
    host: Optional[str] = typer.Option(None, "--host", "-h", help="Server hostname or IP"),
    user: str = typer.Option("root", "--user", "-u", help="SSH username"),
    port: int = typer.Option(22, "--port", "-p", help="SSH port"),
    key: Optional[str] = typer.Option(None, "--key", "-k", help="Path to SSH private key"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Path to nodes.yaml config"),
    skip_install: bool = typer.Option(False, "--skip-install", help="Skip package installation"),
    no_keys: bool = typer.Option(False, "--no-keys", help="Don't generate new node keys"),
):
    """
    Set up a Gonka compute node on a remote server.
    
    Either specify a node name from your config file, or provide connection details directly.
    
    Examples:
    
        gonka setup node gpu-server-1
        
        gonka setup node --host 192.168.1.100 --user root --key ~/.ssh/id_rsa
    """
    nodes_config = load_nodes_config(config)
    setup = GonkaNodeSetup(nodes_config)

    if node_name:
        node = nodes_config.get_node(node_name)
        if not node:
            console.print(f"[red]Node '{node_name}' not found in configuration[/red]")
            raise typer.Exit(1)
    elif host:
        # Create ad-hoc node config
        from .config import NodeConfig
        node = NodeConfig({
            "name": host,
            "host": host,
            "port": port,
            "user": user,
            "ssh_key": key or "~/.ssh/id_rsa",
            "node_type": "inference",
        })
    else:
        console.print("[red]Please specify either a node name or --host[/red]")
        raise typer.Exit(1)

    success = setup.setup_node(node, skip_install=skip_install, generate_keys=not no_keys)
    raise typer.Exit(0 if success else 1)


@setup_app.command("all")
def setup_all(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Path to nodes.yaml config"),
    skip_install: bool = typer.Option(False, "--skip-install", help="Skip package installation"),
):
    """
    Set up all nodes defined in the configuration file.
    """
    nodes_config = load_nodes_config(config)
    setup = GonkaNodeSetup(nodes_config)
    
    if not nodes_config.nodes:
        console.print("[yellow]No nodes found in configuration[/yellow]")
        console.print("Create a config/nodes.yaml file with your node definitions.")
        raise typer.Exit(1)
    
    results = setup.setup_all_nodes(skip_install=skip_install)
    
    success_count = sum(1 for v in results.values() if v)
    console.print(f"\n[bold]Setup complete: {success_count}/{len(results)} nodes successful[/bold]")


@setup_app.command("status")
def setup_status(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Path to nodes.yaml config"),
):
    """
    Check status of all configured nodes.
    """
    nodes_config = load_nodes_config(config)
    setup = GonkaNodeSetup(nodes_config)
    
    if not nodes_config.nodes:
        console.print("[yellow]No nodes found in configuration[/yellow]")
        raise typer.Exit(1)
    
    setup.print_status_table()


# =============================================================================
# Monitor Commands
# =============================================================================

@monitor_app.command("start")
def monitor_start(
    interval: int = typer.Option(300, "--interval", "-i", help="Check interval in seconds"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Path to nodes.yaml config"),
):
    """
    Start continuous monitoring of all nodes.
    
    Monitors node health and sends Telegram alerts when issues are detected.
    Press Ctrl+C to stop.
    """
    asyncio.run(run_monitor(interval))


@monitor_app.command("once")
def monitor_once(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Path to nodes.yaml config"),
):
    """
    Run a single monitoring check on all nodes.
    """
    async def _run():
        monitor = NodeMonitor()
        metrics = await monitor.monitor_once()
        monitor._print_metrics_table(metrics)
    
    asyncio.run(_run())


@monitor_app.command("report")
def monitor_report():
    """
    Send a status report to Telegram.
    """
    asyncio.run(send_report())


@monitor_app.command("deploy-local")
def deploy_local_monitor(
    output: str = typer.Option("local_monitor.py", "--output", "-o", help="Output file path"),
):
    """
    Generate a standalone monitoring script for deployment on nodes.
    
    This script can be deployed directly on each node for local monitoring
    with Telegram notifications.
    """
    script = get_standalone_monitor_script()
    output_path = Path(output)
    output_path.write_text(script)
    console.print(f"[green]Standalone monitor script saved to: {output_path}[/green]")
    console.print("\nDeploy to your node and run:")
    console.print(f"  python3 {output} --token YOUR_BOT_TOKEN --chat YOUR_CHAT_ID")


# =============================================================================
# Analytics Commands
# =============================================================================

@analytics_app.command("summary")
def analytics_summary(
    days: Optional[int] = typer.Option(None, "--days", "-d", help="Number of days to analyze"),
):
    """
    Show earnings summary.
    """
    dashboard = AnalyticsDashboard()
    dashboard.print_summary(days=days)


@analytics_app.command("report")
def analytics_report(
    days: int = typer.Option(30, "--days", "-d", help="Number of days to include"),
):
    """
    Generate full analytics report.
    """
    print_full_report(days=days)


@analytics_app.command("add")
def analytics_add(
    amount: float = typer.Argument(..., help="Amount earned"),
    node: Optional[str] = typer.Option(None, "--node", "-n", help="Node name"),
):
    """
    Add a manual earnings entry.
    
    Example:
        gonka analytics add 1.5 --node gpu-server-1
    """
    tracker = EarningsTracker()
    tracker.add_manual_entry(amount=amount, node_name=node)


@analytics_app.command("export")
def analytics_export(
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output CSV file path"),
):
    """
    Export earnings data to CSV.
    """
    export_earnings(output)


@analytics_app.command("chart")
def analytics_chart(
    days: int = typer.Option(14, "--days", "-d", help="Number of days to show"),
):
    """
    Show daily earnings chart.
    """
    dashboard = AnalyticsDashboard()
    dashboard.print_daily_chart(days=days)


@analytics_app.command("nodes")
def analytics_nodes():
    """
    Show earnings breakdown by node.
    """
    dashboard = AnalyticsDashboard()
    dashboard.print_node_breakdown()


@analytics_app.command("sync")
def analytics_sync():
    """
    Sync earnings data from Gonka API.
    """
    async def _sync():
        dashboard = AnalyticsDashboard()
        await dashboard.sync_from_api()
    
    asyncio.run(_sync())


# =============================================================================
# Config Commands
# =============================================================================

@config_app.command("show")
def config_show():
    """
    Show current configuration.
    """
    settings = get_settings()
    nodes_config = load_nodes_config()

    console.print("\n[bold cyan]Configuration[/bold cyan]\n")
    
    console.print("[bold]Telegram:[/bold]")
    console.print(f"  Bot Token: {'✓ Configured' if settings.telegram_bot_token else '✗ Not set'}")
    console.print(f"  Chat ID: {'✓ Configured' if settings.telegram_chat_id else '✗ Not set'}")
    
    console.print("\n[bold]Wallet:[/bold]")
    console.print(f"  Address: {settings.gonka_wallet_address or 'Not configured'}")
    
    console.print("\n[bold]Monitoring Thresholds:[/bold]")
    console.print(f"  CPU: {settings.cpu_alert_threshold}%")
    console.print(f"  Memory: {settings.memory_alert_threshold}%")
    console.print(f"  Disk: {settings.disk_alert_threshold}%")
    console.print(f"  GPU Temp: {settings.gpu_temp_alert_threshold}°C")
    console.print(f"  Interval: {settings.monitor_interval_seconds}s")
    
    console.print(f"\n[bold]Nodes Configured:[/bold] {len(nodes_config.nodes)}")
    for node in nodes_config.nodes:
        monitoring = "✓" if node.monitoring.get("enabled", True) else "✗"
        console.print(f"  - {node.name} ({node.host}) [{node.node_type}] Monitoring: {monitoring}")


@config_app.command("init")
def config_init(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing config"),
):
    """
    Initialize configuration files with examples.
    """
    config_dir = Path("config")
    config_dir.mkdir(exist_ok=True)
    
    # Copy example configs
    nodes_yaml = config_dir / "nodes.yaml"
    if not nodes_yaml.exists() or force:
        example_path = config_dir / "nodes.yaml.example"
        if example_path.exists():
            nodes_yaml.write_text(example_path.read_text())
            console.print(f"[green]Created: {nodes_yaml}[/green]")
        else:
            console.print("[yellow]nodes.yaml.example not found[/yellow]")
    else:
        console.print(f"[yellow]Skipped: {nodes_yaml} (already exists)[/yellow]")
    
    # Create exports directory
    exports_dir = Path("exports")
    exports_dir.mkdir(exist_ok=True)
    console.print(f"[green]Created: {exports_dir}/[/green]")
    
    console.print("\n[bold]Next steps:[/bold]")
    console.print("1. Copy config/env.example to .env and fill in your values")
    console.print("2. Edit config/nodes.yaml to add your server details")
    console.print("3. Run 'gonka setup status' to verify configuration")


@config_app.command("validate")
def config_validate():
    """
    Validate configuration files.
    """
    errors = []
    warnings = []
    
    settings = get_settings()
    nodes_config = load_nodes_config()
    
    # Check required settings
    if not settings.telegram_bot_token:
        warnings.append("Telegram bot token not configured (monitoring alerts disabled)")
    if not settings.telegram_chat_id:
        warnings.append("Telegram chat ID not configured (monitoring alerts disabled)")
    if not settings.gonka_wallet_address:
        warnings.append("Wallet address not configured (analytics limited)")
    
    # Check nodes config
    if not nodes_config.nodes:
        warnings.append("No nodes configured in nodes.yaml")
    
    for node in nodes_config.nodes:
        if not node.host:
            errors.append(f"Node '{node.name}' missing host")
        key_path = node.get_ssh_key_path()
        if not key_path.exists() and not node.password:
            errors.append(f"Node '{node.name}': SSH key not found and no password set")
    
    # Print results
    if errors:
        console.print("[bold red]Errors:[/bold red]")
        for e in errors:
            console.print(f"  ✗ {e}")
    
    if warnings:
        console.print("[bold yellow]Warnings:[/bold yellow]")
        for w in warnings:
            console.print(f"  ⚠ {w}")
    
    if not errors and not warnings:
        console.print("[bold green]✓ Configuration is valid[/bold green]")
    
    raise typer.Exit(1 if errors else 0)


# =============================================================================
# Quick Commands (shortcuts)
# =============================================================================

@app.command("status")
def quick_status():
    """
    Quick check: Show status of all nodes.
    """
    setup_status()


@app.command("earnings")
def quick_earnings(
    days: Optional[int] = typer.Option(None, "--days", "-d", help="Number of days"),
):
    """
    Quick check: Show earnings summary.
    """
    analytics_summary(days)


def cli():
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    cli()

