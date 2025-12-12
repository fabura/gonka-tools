#!/usr/bin/env python3
"""
Gonka.ai Interactive Telegram Bot with Earnings and Periodic Reports
"""

import asyncio
import json
import time
from datetime import datetime

import httpx
import paramiko

# Configuration
BOT_TOKEN = "8228176154:AAFEqKsG8lIBIW_3CztABKTsfRX5HMhOHTk"
ALLOWED_CHAT_ID = 98662716
WALLET_ADDRESS = "gonka1k82cpjqhfgt347x7grdudu7zfaew6strzjwhyl"

NODES = {
    "neon-galaxy-fin-01": {
        "host": "65.108.33.117",
        "port": 22,
        "user": "root",
        "key_path": "/root/.ssh/gonka_key",
    }
}

PUBLIC_REST = "http://node2.gonka.ai:1317"

# Timing settings
CHECK_INTERVAL = 300        # 5 minutes - for alerts
REPORT_INTERVAL = 1800      # 30 minutes - for status reports
ALERT_COOLDOWN = 600        # 10 minutes

_last_alerts = {}
_last_report = 0


def ssh_exec(host, cmd, config):
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
        _, stdout, stderr = client.exec_command(cmd, timeout=30)
        output = stdout.read().decode().strip()
        client.close()
        return True, output
    except Exception as e:
        return False, str(e)


async def fetch_url(url, timeout=10):
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
    except:
        pass
    return None


async def get_wallet_balance():
    node_config = list(NODES.values())[0]
    ok, out = ssh_exec("local", 
        "curl -s http://localhost:1317/cosmos/bank/v1beta1/balances/{} 2>/dev/null".format(WALLET_ADDRESS),
        node_config)
    
    if ok and out and "balances" in out:
        try:
            data = json.loads(out)
            for b in data.get("balances", []):
                if b.get("denom") in ["ugnk", "gnk"]:
                    amount = float(b.get("amount", 0))
                    if b.get("denom") == "ugnk":
                        amount = amount / 1000000
                    return amount
        except:
            pass
    
    data = await fetch_url("{}/cosmos/bank/v1beta1/balances/{}".format(PUBLIC_REST, WALLET_ADDRESS))
    if data and "balances" in data:
        for b in data.get("balances", []):
            if b.get("denom") in ["ugnk", "gnk"]:
                amount = float(b.get("amount", 0))
                if b.get("denom") == "ugnk":
                    amount = amount / 1000000
                return amount
    return None


async def get_sync_status(node_config):
    ok, out = ssh_exec("node", "curl -s http://localhost:26657/status 2>/dev/null", node_config)
    if ok and out:
        try:
            data = json.loads(out)
            sync = data.get("result", {}).get("sync_info", {})
            return {
                "catching_up": sync.get("catching_up", True),
                "latest_block": sync.get("latest_block_height", "0"),
            }
        except:
            pass
    return None


def get_node_status(name):
    if name not in NODES:
        return {"error": "Node not found"}
    
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
        "uptime": "",
    }
    
    ok, _ = ssh_exec(name, "echo ok", config)
    if not ok:
        return status
    
    status["reachable"] = True
    
    ok, out = ssh_exec(name, "top -bn1 | grep 'Cpu(s)' | awk '{print \}'", config)
    if ok and out:
        try: status["cpu"] = float(out.replace("%", ""))
        except: pass
    
    ok, out = ssh_exec(name, "free | grep Mem | awk '{print (\/\) * 100}'", config)
    if ok and out:
        try: status["memory"] = float(out)
        except: pass
    
    ok, out = ssh_exec(name, "df -h / | tail -1 | awk '{print \}'", config)
    if ok and out:
        try: status["disk"] = float(out.replace("%", ""))
        except: pass
    
    ok, out = ssh_exec(name, "nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu --format=csv,noheader,nounits 2>/dev/null", config)
    if ok and out:
        for line in out.split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                status["gpu_names"].append(parts[0])
                try:
                    status["gpu_temps"].append(float(parts[1]))
                    status["gpu_util"].append(float(parts[2]))
                except: pass
    
    ok, out = ssh_exec(name, "docker ps --format '{{.Names}}' 2>/dev/null | grep -c join || echo 0", config)
    if ok and out:
        try: status["containers"] = int(out)
        except: pass
    
    ok, out = ssh_exec(name, "uptime -p", config)
    if ok: status["uptime"] = out
    
    return status


def format_status(status):
    if "error" in status:
        return "❌ {}".format(status["error"])
    
    if not status["reachable"]:
        return "❌ <b>{}</b> - UNREACHABLE".format(status["name"])
    
    gonka = "🟢" if status["containers"] > 0 else "🔴"
    
    gpu_info = ""
    for i in range(len(status["gpu_names"])):
        temp = status["gpu_temps"][i] if i < len(status["gpu_temps"]) else 0
        util = status["gpu_util"][i] if i < len(status["gpu_util"]) else 0
        gpu_info += "\n  GPU{}: {}°C {}%".format(i, int(temp), int(util))
    
    return """<b>{name}</b> {gonka}
CPU: {cpu:.0f}% | MEM: {mem:.0f}% | Disk: {disk:.0f}%{gpu}
Containers: {containers} | {uptime}""".format(
        name=status["name"],
        gonka=gonka,
        cpu=status["cpu"],
        mem=status["memory"],
        disk=status["disk"],
        gpu=gpu_info,
        containers=status["containers"],
        uptime=status["uptime"]
    )


async def send_message(chat_id, text):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                "https://api.telegram.org/bot{}/sendMessage".format(BOT_TOKEN),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
    except Exception as e:
        print("Send error: {}".format(e))


async def send_periodic_report():
    """Send periodic status report"""
    report_lines = ["📊 <b>Status Report</b>", ""]
    
    for name in NODES:
        status = get_node_status(name)
        report_lines.append(format_status(status))
    
    # Add sync status
    config = list(NODES.values())[0]
    sync = await get_sync_status(config)
    if sync:
        sync_icon = "🔄" if sync["catching_up"] else "✅"
        report_lines.append("")
        report_lines.append("{} Sync: Block {}".format(sync_icon, sync["latest_block"]))
    
    # Add balance if available
    balance = await get_wallet_balance()
    if balance is not None:
        report_lines.append("💰 Balance: {:.4f} GNK".format(balance))
    
    report_lines.append("")
    report_lines.append("<i>{}</i>".format(datetime.now().strftime("%Y-%m-%d %H:%M")))
    
    await send_message(ALLOWED_CHAT_ID, "\n".join(report_lines))


async def handle_update(update):
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
        await send_message(chat_id, """🤖 <b>Gonka.ai Bot</b>

<b>Commands:</b>
/status - Node status
/sync - Sync status  
/balance - Wallet balance
/logs - Recent logs
/restart - Restart Gonka
/report - Send report now

<b>Auto Reports:</b> Every 30 min
<b>Wallet:</b> <code>{}</code>""".format(WALLET_ADDRESS))
    
    elif cmd == "/nodes":
        nodes_list = "\n".join(["• <code>{}</code> ({})".format(n, c["host"]) for n, c in NODES.items()])
        await send_message(chat_id, "📋 <b>Nodes:</b>\n\n{}".format(nodes_list))
    
    elif cmd == "/status":
        await send_message(chat_id, "🔍 Checking...")
        for name in NODES:
            status = get_node_status(name)
            await send_message(chat_id, format_status(status))
    
    elif cmd == "/sync":
        config = list(NODES.values())[0]
        sync = await get_sync_status(config)
        if sync:
            icon = "🔄" if sync["catching_up"] else "✅"
            await send_message(chat_id, "{} Syncing: {}\nBlock: {}".format(
                icon, "Yes" if sync["catching_up"] else "No", sync["latest_block"]))
        else:
            await send_message(chat_id, "❌ Could not get sync status")
    
    elif cmd == "/balance" or cmd == "/earnings":
        balance = await get_wallet_balance()
        if balance is not None:
            await send_message(chat_id, "💰 Balance: {:.4f} GNK".format(balance))
        else:
            await send_message(chat_id, "💰 Balance: Unable to fetch (node syncing?)")
    
    elif cmd == "/logs":
        node_name = list(NODES.keys())[0]
        ok, logs = ssh_exec(node_name, 
            "cd /opt/gonka/deploy/join && docker compose logs --tail 20 2>&1 | tail -30",
            NODES[node_name])
        if ok and logs:
            await send_message(chat_id, "<pre>{}</pre>".format(logs[-3500:]))
        else:
            await send_message(chat_id, "❌ Failed to get logs")
    
    elif cmd == "/restart":
        node_name = list(NODES.keys())[0]
        await send_message(chat_id, "🔄 Restarting...")
        ok, _ = ssh_exec(node_name,
            "cd /opt/gonka/deploy/join && source config.env && docker compose restart 2>&1",
            NODES[node_name])
        await send_message(chat_id, "✅ Done!" if ok else "❌ Failed")
    
    elif cmd == "/report":
        await send_periodic_report()


async def check_alerts():
    global _last_alerts
    
    for name, config in NODES.items():
        status = get_node_status(name)
        now = time.time()
        
        if not status["reachable"]:
            key = name + ":unreachable"
            if key not in _last_alerts or now - _last_alerts[key] > ALERT_COOLDOWN:
                await send_message(ALLOWED_CHAT_ID, "🚨 Node {} unreachable!".format(name))
                _last_alerts[key] = now
        elif status["containers"] == 0:
            key = name + ":stopped"
            if key not in _last_alerts or now - _last_alerts[key] > ALERT_COOLDOWN:
                await send_message(ALLOWED_CHAT_ID, "🚨 Gonka stopped on {}!".format(name))
                _last_alerts[key] = now


async def poll_updates():
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


async def periodic_tasks():
    global _last_report
    
    while True:
        await asyncio.sleep(60)  # Check every minute
        now = time.time()
        
        # Send report every 30 minutes
        if now - _last_report >= REPORT_INTERVAL:
            try:
                await send_periodic_report()
                _last_report = now
            except Exception as e:
                print("Report error: {}".format(e))
        
        # Check alerts every 5 minutes
        try:
            await check_alerts()
        except Exception as e:
            print("Alert error: {}".format(e))


async def main():
    global _last_report
    _last_report = time.time()  # Don't send report immediately
    
    print("Gonka Bot started at {}".format(datetime.now()))
    
    await send_message(ALLOWED_CHAT_ID, """🤖 <b>Gonka Bot Updated!</b>

📊 Status reports every 30 minutes
🚨 Alerts on issues
/report - Get report now
/help - All commands""")
    
    await asyncio.gather(
        poll_updates(),
        periodic_tasks(),
    )


if __name__ == "__main__":
    asyncio.run(main())
