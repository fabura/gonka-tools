"""
Analytics Module for Gonka.ai Mining Performance.

Provides tools for tracking earnings, analyzing mining performance,
and generating reports.
"""

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import Settings, get_settings

console = Console()


@dataclass
class EarningsRecord:
    """A single earnings record."""
    timestamp: datetime
    amount: float
    token: str = "GONKA"
    tx_hash: Optional[str] = None
    block_number: Optional[int] = None
    node_name: Optional[str] = None


@dataclass
class WalletBalance:
    """Current wallet balance."""
    address: str
    balance: float
    token: str = "GONKA"
    usd_value: Optional[float] = None
    last_updated: datetime = field(default_factory=datetime.now)


@dataclass
class MiningStats:
    """Aggregated mining statistics."""
    period_start: datetime
    period_end: datetime
    total_earned: float
    transaction_count: int
    avg_per_day: float
    avg_per_transaction: float
    best_day: Optional[tuple[datetime, float]] = None
    worst_day: Optional[tuple[datetime, float]] = None


class GonkaAPI:
    """
    Client for Gonka.ai API interactions.
    
    Note: This is a placeholder implementation. Update with actual API
    endpoints when Gonka releases their official API documentation.
    """

    BASE_URL = "https://api.gonka.ai"  # Placeholder
    EXPLORER_URL = "https://gonka.ai/wallet"

    def __init__(self, wallet_address: Optional[str] = None, api_key: Optional[str] = None):
        self.wallet_address = wallet_address
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=30)

    async def close(self):
        await self.client.aclose()

    async def get_balance(self, address: Optional[str] = None) -> Optional[WalletBalance]:
        """
        Get wallet balance.
        
        Note: Implementation may need adjustment based on actual Gonka API.
        """
        addr = address or self.wallet_address
        if not addr:
            console.print("[yellow]No wallet address configured[/yellow]")
            return None

        try:
            # Placeholder - replace with actual API call
            response = await self.client.get(
                f"{self.BASE_URL}/v1/wallet/{addr}/balance",
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            )
            
            if response.status_code == 200:
                data = response.json()
                return WalletBalance(
                    address=addr,
                    balance=data.get("balance", 0),
                    token=data.get("token", "GONKA"),
                    usd_value=data.get("usd_value"),
                )
        except httpx.RequestError as e:
            console.print(f"[yellow]API not available: {e}[/yellow]")
            console.print("[dim]Using local data if available...[/dim]")
        
        return None

    async def get_transactions(
        self,
        address: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EarningsRecord]:
        """
        Get transaction history for wallet.
        
        Note: Implementation may need adjustment based on actual Gonka API.
        """
        addr = address or self.wallet_address
        if not addr:
            return []

        try:
            response = await self.client.get(
                f"{self.BASE_URL}/v1/wallet/{addr}/transactions",
                params={"limit": limit, "offset": offset},
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            )
            
            if response.status_code == 200:
                data = response.json()
                return [
                    EarningsRecord(
                        timestamp=datetime.fromisoformat(tx["timestamp"]),
                        amount=tx["amount"],
                        token=tx.get("token", "GONKA"),
                        tx_hash=tx.get("hash"),
                        block_number=tx.get("block"),
                    )
                    for tx in data.get("transactions", [])
                ]
        except httpx.RequestError:
            pass
        
        return []

    async def get_network_stats(self) -> Optional[dict]:
        """Get overall network statistics."""
        try:
            response = await self.client.get(f"{self.BASE_URL}/v1/network/stats")
            if response.status_code == 200:
                return response.json()
        except httpx.RequestError:
            pass
        return None


class EarningsTracker:
    """Track and analyze mining earnings."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        data_file: Optional[Path] = None,
    ):
        self.settings = settings or get_settings()
        self.data_file = data_file or Path(self.settings.analytics_export_path) / "earnings.json"
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.records: list[EarningsRecord] = []
        self._load_data()

    def _load_data(self):
        """Load earnings data from file."""
        if self.data_file.exists():
            try:
                with open(self.data_file, "r") as f:
                    data = json.load(f)
                    self.records = [
                        EarningsRecord(
                            timestamp=datetime.fromisoformat(r["timestamp"]),
                            amount=r["amount"],
                            token=r.get("token", "GONKA"),
                            tx_hash=r.get("tx_hash"),
                            block_number=r.get("block_number"),
                            node_name=r.get("node_name"),
                        )
                        for r in data.get("records", [])
                    ]
            except (json.JSONDecodeError, KeyError):
                self.records = []

    def _save_data(self):
        """Save earnings data to file."""
        data = {
            "records": [
                {
                    "timestamp": r.timestamp.isoformat(),
                    "amount": r.amount,
                    "token": r.token,
                    "tx_hash": r.tx_hash,
                    "block_number": r.block_number,
                    "node_name": r.node_name,
                }
                for r in self.records
            ]
        }
        with open(self.data_file, "w") as f:
            json.dump(data, f, indent=2)

    def add_record(self, record: EarningsRecord):
        """Add a new earnings record."""
        # Avoid duplicates by tx_hash
        if record.tx_hash:
            for existing in self.records:
                if existing.tx_hash == record.tx_hash:
                    return  # Already exists
        
        self.records.append(record)
        self.records.sort(key=lambda r: r.timestamp)
        self._save_data()

    def add_manual_entry(
        self,
        amount: float,
        timestamp: Optional[datetime] = None,
        node_name: Optional[str] = None,
    ):
        """Add a manual earnings entry."""
        record = EarningsRecord(
            timestamp=timestamp or datetime.now(),
            amount=amount,
            node_name=node_name,
        )
        self.add_record(record)
        console.print(f"[green]Added earnings record: {amount} GONKA[/green]")

    def get_stats(
        self,
        days: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> MiningStats:
        """
        Calculate mining statistics for a period.
        
        Args:
            days: Number of days to look back (default: all time)
            start_date: Start of period
            end_date: End of period
        """
        end_date = end_date or datetime.now()
        
        if days:
            start_date = end_date - timedelta(days=days)
        elif not start_date:
            start_date = self.records[0].timestamp if self.records else end_date

        # Filter records
        filtered = [
            r for r in self.records
            if start_date <= r.timestamp <= end_date
        ]

        if not filtered:
            return MiningStats(
                period_start=start_date,
                period_end=end_date,
                total_earned=0,
                transaction_count=0,
                avg_per_day=0,
                avg_per_transaction=0,
            )

        total = sum(r.amount for r in filtered)
        count = len(filtered)
        days_in_period = max((end_date - start_date).days, 1)

        # Calculate daily totals for best/worst day
        daily_totals: dict[str, float] = {}
        for r in filtered:
            day_key = r.timestamp.strftime("%Y-%m-%d")
            daily_totals[day_key] = daily_totals.get(day_key, 0) + r.amount

        best_day = max(daily_totals.items(), key=lambda x: x[1]) if daily_totals else None
        worst_day = min(daily_totals.items(), key=lambda x: x[1]) if daily_totals else None

        return MiningStats(
            period_start=start_date,
            period_end=end_date,
            total_earned=total,
            transaction_count=count,
            avg_per_day=total / days_in_period,
            avg_per_transaction=total / count if count else 0,
            best_day=(datetime.strptime(best_day[0], "%Y-%m-%d"), best_day[1]) if best_day else None,
            worst_day=(datetime.strptime(worst_day[0], "%Y-%m-%d"), worst_day[1]) if worst_day else None,
        )

    def get_earnings_by_node(self) -> dict[str, float]:
        """Get total earnings grouped by node."""
        by_node: dict[str, float] = {}
        for r in self.records:
            node = r.node_name or "unknown"
            by_node[node] = by_node.get(node, 0) + r.amount
        return by_node

    def get_daily_earnings(self, days: int = 30) -> list[tuple[str, float]]:
        """Get daily earnings for the last N days."""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        # Initialize all days with 0
        daily: dict[str, float] = {}
        current = start_date
        while current <= end_date:
            daily[current.strftime("%Y-%m-%d")] = 0
            current += timedelta(days=1)
        
        # Sum up earnings
        for r in self.records:
            if start_date <= r.timestamp <= end_date:
                day_key = r.timestamp.strftime("%Y-%m-%d")
                daily[day_key] = daily.get(day_key, 0) + r.amount
        
        return sorted(daily.items())

    def export_csv(self, output_path: Optional[Path] = None) -> Path:
        """Export earnings data to CSV."""
        output_path = output_path or Path(self.settings.analytics_export_path) / "earnings_export.csv"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Amount", "Token", "TX Hash", "Block", "Node"])
            for r in self.records:
                writer.writerow([
                    r.timestamp.isoformat(),
                    r.amount,
                    r.token,
                    r.tx_hash or "",
                    r.block_number or "",
                    r.node_name or "",
                ])
        
        return output_path


class AnalyticsDashboard:
    """Display analytics and reports."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        tracker: Optional[EarningsTracker] = None,
    ):
        self.settings = settings or get_settings()
        self.tracker = tracker or EarningsTracker(settings=self.settings)
        self.api = GonkaAPI(
            wallet_address=self.settings.gonka_wallet_address,
        )

    def print_summary(self, days: Optional[int] = None):
        """Print earnings summary."""
        stats = self.tracker.get_stats(days=days)
        
        period_label = f"Last {days} days" if days else "All time"
        
        panel_content = Text()
        panel_content.append(f"Period: ", style="dim")
        panel_content.append(f"{stats.period_start.strftime('%Y-%m-%d')} → {stats.period_end.strftime('%Y-%m-%d')}\n")
        panel_content.append(f"\nTotal Earned: ", style="bold")
        panel_content.append(f"{stats.total_earned:.4f} GONKA\n", style="green bold")
        panel_content.append(f"Transactions: ", style="dim")
        panel_content.append(f"{stats.transaction_count}\n")
        panel_content.append(f"Avg/Day: ", style="dim")
        panel_content.append(f"{stats.avg_per_day:.4f} GONKA\n")
        panel_content.append(f"Avg/Transaction: ", style="dim")
        panel_content.append(f"{stats.avg_per_transaction:.4f} GONKA\n")
        
        if stats.best_day:
            panel_content.append(f"\nBest Day: ", style="dim")
            panel_content.append(f"{stats.best_day[0].strftime('%Y-%m-%d')} ({stats.best_day[1]:.4f})\n", style="green")
        if stats.worst_day:
            panel_content.append(f"Worst Day: ", style="dim")
            panel_content.append(f"{stats.worst_day[0].strftime('%Y-%m-%d')} ({stats.worst_day[1]:.4f})", style="red")

        console.print(Panel(panel_content, title=f"📊 Earnings Summary ({period_label})", border_style="cyan"))

    def print_daily_chart(self, days: int = 14):
        """Print ASCII chart of daily earnings."""
        daily = self.tracker.get_daily_earnings(days)
        if not daily:
            console.print("[yellow]No earnings data available[/yellow]")
            return

        max_value = max(v for _, v in daily) or 1
        chart_width = 40

        console.print(f"\n[bold cyan]Daily Earnings (Last {days} days)[/bold cyan]\n")
        
        for date_str, value in daily[-days:]:
            bar_len = int((value / max_value) * chart_width) if max_value > 0 else 0
            bar = "█" * bar_len
            date_short = date_str[5:]  # MM-DD
            console.print(f"{date_short} │ [green]{bar}[/green] {value:.4f}")

    def print_node_breakdown(self):
        """Print earnings by node."""
        by_node = self.tracker.get_earnings_by_node()
        
        if not by_node:
            console.print("[yellow]No earnings data by node[/yellow]")
            return

        table = Table(title="Earnings by Node")
        table.add_column("Node", style="cyan")
        table.add_column("Total Earned", justify="right", style="green")
        table.add_column("% of Total", justify="right")

        total = sum(by_node.values())
        for node, amount in sorted(by_node.items(), key=lambda x: -x[1]):
            pct = (amount / total * 100) if total else 0
            table.add_row(node, f"{amount:.4f}", f"{pct:.1f}%")

        console.print(table)

    def print_recent_transactions(self, limit: int = 10):
        """Print recent transactions."""
        records = self.tracker.records[-limit:][::-1]  # Last N, reversed
        
        if not records:
            console.print("[yellow]No transaction records[/yellow]")
            return

        table = Table(title=f"Recent Transactions (Last {limit})")
        table.add_column("Date", style="dim")
        table.add_column("Amount", justify="right", style="green")
        table.add_column("Node", style="cyan")
        table.add_column("TX Hash", style="dim")

        for r in records:
            tx_short = r.tx_hash[:16] + "..." if r.tx_hash else "-"
            table.add_row(
                r.timestamp.strftime("%Y-%m-%d %H:%M"),
                f"{r.amount:.4f}",
                r.node_name or "-",
                tx_short,
            )

        console.print(table)

    def full_report(self, days: Optional[int] = 30):
        """Print full analytics report."""
        console.print("\n" + "=" * 60)
        console.print("[bold cyan]   GONKA.AI MINING ANALYTICS REPORT[/bold cyan]")
        console.print("=" * 60 + "\n")

        self.print_summary(days=days)
        console.print()
        self.print_daily_chart(days=min(days or 14, 14))
        console.print()
        self.print_node_breakdown()
        console.print()
        self.print_recent_transactions()

    async def sync_from_api(self):
        """Sync earnings data from Gonka API."""
        console.print("[cyan]Syncing from Gonka API...[/cyan]")
        
        try:
            transactions = await self.api.get_transactions()
            added = 0
            for tx in transactions:
                before_count = len(self.tracker.records)
                self.tracker.add_record(tx)
                if len(self.tracker.records) > before_count:
                    added += 1
            
            console.print(f"[green]Synced {added} new transactions[/green]")
        except Exception as e:
            console.print(f"[yellow]Sync failed: {e}[/yellow]")
        finally:
            await self.api.close()


def print_earnings_summary(days: Optional[int] = None):
    """Print earnings summary."""
    dashboard = AnalyticsDashboard()
    dashboard.print_summary(days=days)


def print_full_report(days: int = 30):
    """Print full analytics report."""
    dashboard = AnalyticsDashboard()
    dashboard.full_report(days=days)


def add_earnings(amount: float, node: Optional[str] = None):
    """Add manual earnings entry."""
    tracker = EarningsTracker()
    tracker.add_manual_entry(amount=amount, node_name=node)


def export_earnings(output_path: Optional[str] = None):
    """Export earnings to CSV."""
    tracker = EarningsTracker()
    path = tracker.export_csv(Path(output_path) if output_path else None)
    console.print(f"[green]Exported to: {path}[/green]")

