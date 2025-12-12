#!/usr/bin/env python3
"""
Gonka.ai Interactive Telegram Bot with Earnings

Deploy this to a monitoring server to get Telegram commands and alerts.

Configuration:
    Set these environment variables or edit the config section below:
    - TELEGRAM_BOT_TOKEN
    - TELEGRAM_CHAT_ID
    - GONKA_WALLET_ADDRESS
    - GONKA_NODE_HOST
    - GONKA_NODE_SSH_KEY
"""

import asyncio
import json
import os
import time
from datetime import datetime

import httpx
import paramiko

# Configuration - override with environment variables
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
ALLOWED_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
WALLET_ADDRESS = os.getenv("GONKA_WALLET_ADDRESS", "")

# Node configuration
NODES = {
    os.getenv("GONKA_NODE_NAME", "gonka-node"): {
        "host": os.getenv("GONKA_NODE_HOST", ""),
        "port": int(os.getenv("GONKA_NODE_PORT", "22")),
        "user": os.getenv("GONKA_NODE_USER", "root"),
        "key_path": os.getenv("GONKA_NODE_SSH_KEY", "/root/.ssh/gonka_key"),
    }
}

# Public Gonka endpoints (fallback)
PUBLIC_API = "http://node2.gonka.ai:8000"
PUBLIC_RPC = "http://node2.gonka.ai:26657"
PUBLIC_REST = "http://node2.gonka.ai:1317"

# Monitoring settings
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))  # 5 minutes
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "600"))  # 10 minutes

_last_alerts = {}


def ssh_exec(host, cmd, config):
    """Execute command via SSH"""
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=config["host"],
            port=config["port"],
            username=config["user"],
            key_filename=config["key_path"],
            timeout=10,
        )
        _, stdout, _ = client.exec_command(cmd, timeout=30)
        output = stdout.read().decode().strip()
        client.close()
        return True, output
    except Exception as e:
        return False, str(e)


async def fetch_url(url, timeout=10):
    """Fetch JSON from URL"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
    except:
        pass
    return None


async def get_wallet_balance():
    """Get wallet balance from blockchain"""
    if not WALLET_ADDRESS:
        return None
        
    # Try local node first
    node_config = list(NODES.values())[0]
    if node_config["host"]:
        ok, out = ssh_exec(
            "local",
            "curl -s http://localhost:1317/cosmos/bank/v1beta1/balances/{} 2>/dev/null".format(
                WALLET_ADDRESS
            ),
            node_config,
        )

        if ok and out and "balances" in out:
            try:
                data = json.loads(out)
                balances = data.get("balances", [])
                for b in balances:
                    if b.get("denom") in ["ugnk", "gnk"]:
                        amount = float(b.get("amount", 0))
                        if b.get("denom") == "ugnk":
                            amount = amount / 1000000
                        return amount
            except:
                pass

    # Try public node
    data = await fetch_url(
        "{}/cosmos/bank/v1beta1/balances/{}".format(PUBLIC_REST, WALLET_ADDRESS)
    )
    if data and "balances" in data:
        for b in data.get("balances", []):
            if b.get("denom") in ["ugnk", "gnk"]:
                amount = float(b.get("amount", 0))
                if b.get("denom") == "ugnk":
                    amount = amount / 1000000
                return amount

    return None


async def get_sync_status(node_config):
    """Get node sync status"""
    ok, out = ssh_exec(
        "node", "curl -s http://localhost:26657/status 2>/dev/null", node_config
    )

    if ok and out:
        try:
            data = json.loads(out)
            sync = data.get("result", {}).get("sync_info", {})
            return {
                "catching_up": sync.get("catching_up", True),
                "latest_block": sync.get("latest_block_height", "0"),
                "latest_time": sync.get("latest_block_time", ""),
            }
        except:
            pass
    return None


def get_node_status(name):
    """Get comprehensive node status"""
    if name not in NODES:
        return {"error": "Node {} not found".format(name)}

    config = NODES[name]
    status = {
        "name": name,
        "host": config["host"],
        "reachable": False,
        "cpu": 0,
        "memory": 0,
        "disk": 0,
        "gpu_temps": [],
        "gpu_names": [],
        "gpu_util": [],
        "containers": 0,
        "container_list": [],
        "uptime": "",
    }

    if not config["host"]:
        return status

    ok, _ = ssh_exec(name, "echo ok", config)
    if not ok:
        return status

    status["reachable"] = True

    # CPU
    ok, out = ssh_exec(name, "top -bn1 | grep 'Cpu(s)' | awk '{print $2}'", config)
    if ok and out:
        try:
            status["cpu"] = float(out.replace("%", ""))
        except:
            pass

    # Memory
    ok, out = ssh_exec(name, "free | grep Mem | awk '{print ($3/$2) * 100}'", config)
    if ok and out:
        try:
            status["memory"] = float(out)
        except:
            pass

    # Disk
    ok, out = ssh_exec(name, "df -h / | tail -1 | awk '{print $5}'", config)
    if ok and out:
        try:
            status["disk"] = float(out.replace("%", ""))
        except:
            pass

    # GPU with utilization
    ok, out = ssh_exec(
        name,
        "nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu --format=csv,noheader,nounits 2>/dev/null",
        config,
    )
    if ok and out:
        for line in out.split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                status["gpu_names"].append(parts[0])
                try:
                    status["gpu_temps"].append(float(parts[1]))
                    status["gpu_util"].append(float(parts[2]))
                except:
                    pass

    # Docker containers
    ok, out = ssh_exec(
        name, "docker ps --format '{{.Names}}:{{.Status}}' 2>/dev/null", config
    )
    if ok and out:
        for line in out.split("\n"):
            if line:
                status["container_list"].append(line)
                if "join" in line.lower() or "gonka" in line.lower():
                    status["containers"] += 1

    # Uptime
    ok, out = ssh_exec(name, "uptime -p", config)
    if ok:
        status["uptime"] = out

    return status


def format_status(status):
    """Format status as HTML message"""
    if "error" in status:
        return "❌ {}".format(status["error"])

    if not status["reachable"]:
        return """❌ <b>{}</b>

<b>Host:</b> {}
<b>Status:</b> UNREACHABLE""".format(
            status["name"], status["host"]
        )

    gonka_status = "🟢 Running" if status["containers"] > 0 else "🔴 Stopped"

    gpu_info = ""
    if status["gpu_names"]:
        for i in range(len(status["gpu_names"])):
            name = status["gpu_names"][i]
            temp = status["gpu_temps"][i] if i < len(status["gpu_temps"]) else 0
            util = status["gpu_util"][i] if i < len(status["gpu_util"]) else 0
            gpu_info += "\n  GPU {}: {} {}°C ({}%)".format(i, name, int(temp), int(util))
    else:
        gpu_info = "\n  No GPUs detected"

    return """✅ <b>{name}</b>

<b>Host:</b> {host}
<b>Gonka:</b> {gonka} ({containers} containers)
<b>Uptime:</b> {uptime}

<b>Resources:</b>
  CPU: {cpu:.1f}%
  Memory: {mem:.1f}%
  Disk: {disk:.1f}%

<b>GPUs:</b>{gpu}""".format(
        name=status["name"],
        host=status["host"],
        gonka=gonka_status,
        containers=status["containers"],
        uptime=status["uptime"],
        cpu=status["cpu"],
        mem=status["memory"],
        disk=status["disk"],
        gpu=gpu_info,
    )


async def send_message(chat_id, text):
    """Send message via Telegram API"""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://api.telegram.org/bot{}/sendMessage".format(BOT_TOKEN),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
    except Exception as e:
        print("Send error: {}".format(e))


async def handle_update(update):
    """Handle incoming Telegram update"""
    if "message" not in update:
        return

    msg = update["message"]
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")

    if chat_id != ALLOWED_CHAT_ID:
        await send_message(chat_id, "⛔ Unauthorized")
        return

    cmd = text.lower().strip()

    if cmd == "/start" or cmd == "/help":
        await send_message(
            chat_id,
            """🤖 <b>Gonka.ai Bot</b>

<b>Commands:</b>
/status - Node status (CPU, GPU, containers)
/sync - Node sync status
/balance - Wallet balance
/earnings - Earnings info
/nodes - List all nodes
/logs - Recent Gonka logs
/restart - Restart Gonka containers
/help - Show this message

<b>Wallet:</b>
<code>{}</code>""".format(
                WALLET_ADDRESS or "Not configured"
            ),
        )

    elif cmd == "/nodes":
        nodes_list = "\n".join(
            ["• <code>{}</code> ({})".format(n, c["host"]) for n, c in NODES.items() if c["host"]]
        )
        await send_message(
            chat_id,
            """📋 <b>Configured Nodes:</b>

{}""".format(
                nodes_list or "No nodes configured"
            ),
        )

    elif cmd == "/status":
        await send_message(chat_id, "🔍 Checking nodes...")
        for name in NODES:
            if NODES[name]["host"]:
                status = get_node_status(name)
                await send_message(chat_id, format_status(status))

    elif cmd == "/sync":
        await send_message(chat_id, "🔄 Checking sync status...")
        config = list(NODES.values())[0]
        if config["host"]:
            sync = await get_sync_status(config)

            if sync:
                sync_icon = "🔄" if sync["catching_up"] else "✅"
                await send_message(
                    chat_id,
                    """<b>Node Sync Status</b>

{} <b>Syncing:</b> {}
<b>Latest Block:</b> {}
<b>Block Time:</b> {}""".format(
                        sync_icon,
                        "Yes" if sync["catching_up"] else "No (Synced!)",
                        sync["latest_block"],
                        sync["latest_time"][:19] if sync["latest_time"] else "N/A",
                    ),
                )
            else:
                await send_message(chat_id, "❌ Could not get sync status")
        else:
            await send_message(chat_id, "❌ No node configured")

    elif cmd == "/balance" or cmd == "/earnings":
        await send_message(chat_id, "💰 Checking balance...")
        balance = await get_wallet_balance()

        if balance is not None:
            await send_message(
                chat_id,
                """💰 <b>Wallet Balance</b>

<b>Address:</b>
<code>{}</code>

<b>Balance:</b> {:.4f} GNK

<i>Note: Earnings accumulate as you process inference tasks.</i>""".format(
                    WALLET_ADDRESS, balance
                ),
            )
        else:
            await send_message(
                chat_id,
                """💰 <b>Wallet Balance</b>

<b>Address:</b>
<code>{}</code>

<b>Balance:</b> Unable to fetch (node may still be syncing)

<i>Check /sync to see node status.</i>""".format(
                    WALLET_ADDRESS or "Not configured"
                ),
            )

    elif cmd == "/logs":
        node_name = list(NODES.keys())[0]
        config = NODES[node_name]
        if config["host"]:
            await send_message(chat_id, "📜 Fetching logs...")
            ok, logs = ssh_exec(
                node_name,
                "cd /opt/gonka/deploy/join && docker compose -f docker-compose.yml -f docker-compose.mlnode.yml logs --tail 15 2>&1 | tail -40",
                config,
            )

            if ok and logs:
                if len(logs) > 3500:
                    logs = logs[-3500:]
                await send_message(chat_id, "<pre>{}</pre>".format(logs))
            else:
                await send_message(chat_id, "❌ Failed to get logs")
        else:
            await send_message(chat_id, "❌ No node configured")

    elif cmd == "/restart":
        node_name = list(NODES.keys())[0]
        config = NODES[node_name]
        if config["host"]:
            await send_message(chat_id, "🔄 Restarting Gonka...")
            ok, out = ssh_exec(
                node_name,
                "cd /opt/gonka/deploy/join && source config.env && docker compose -f docker-compose.yml -f docker-compose.mlnode.yml restart 2>&1",
                config,
            )

            if ok:
                await send_message(chat_id, "✅ Restart command sent!")
            else:
                await send_message(chat_id, "❌ Restart failed: {}".format(out))
        else:
            await send_message(chat_id, "❌ No node configured")


async def check_alerts():
    """Periodic health check with alerts"""
    global _last_alerts

    for name, config in NODES.items():
        if not config["host"]:
            continue
            
        status = get_node_status(name)
        now = time.time()

        if not status["reachable"]:
            key = name + ":unreachable"
            if key not in _last_alerts or now - _last_alerts[key] > ALERT_COOLDOWN:
                await send_message(
                    ALLOWED_CHAT_ID,
                    "🚨 <b>ALERT:</b> Node {} is unreachable!".format(name),
                )
                _last_alerts[key] = now
        elif status["containers"] == 0:
            key = name + ":stopped"
            if key not in _last_alerts or now - _last_alerts[key] > ALERT_COOLDOWN:
                await send_message(
                    ALLOWED_CHAT_ID,
                    "🚨 <b>ALERT:</b> Gonka stopped on {}!".format(name),
                )
                _last_alerts[key] = now


async def poll_updates():
    """Long-poll for Telegram updates"""
    offset = 0
    async with httpx.AsyncClient() as client:
        while True:
            try:
                resp = await client.get(
                    "https://api.telegram.org/bot{}/getUpdates".format(BOT_TOKEN),
                    params={"offset": offset, "timeout": 30},
                    timeout=35,
                )
                data = resp.json()

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    await handle_update(update)
            except Exception as e:
                print("Poll error: {}".format(e))
                await asyncio.sleep(5)


async def periodic_check():
    """Run periodic health checks"""
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            await check_alerts()
        except Exception as e:
            print("Alert check error: {}".format(e))


async def main():
    print("Gonka Bot started at {}".format(datetime.now()))
    print("Monitoring {} node(s)".format(len([n for n in NODES.values() if n["host"]])))

    await send_message(
        ALLOWED_CHAT_ID,
        """🤖 <b>Gonka Bot Online!</b>

Type /help for commands.
Monitoring active - alerts every {} min.""".format(CHECK_INTERVAL // 60),
    )

    await asyncio.gather(
        poll_updates(),
        periodic_check(),
    )


if __name__ == "__main__":
    asyncio.run(main())

