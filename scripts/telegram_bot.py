#!/usr/bin/env python3
"""
Gonka.ai Interactive Telegram Bot with Full Node Status
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

CHECK_INTERVAL = 300
REPORT_INTERVAL = 1800
ALERT_COOLDOWN = 600

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


def get_full_node_status(name, config):
    """Get comprehensive node status including Gonka-specific checks"""
    status = {
        "name": name,
        "host": config["host"],
        "reachable": False,
        # System
        "cpu": 0, "memory": 0, "disk": 0, "uptime": "",
        # GPU
        "gpu_names": [], "gpu_temps": [], "gpu_util": [], "gpu_mem_used": [], "gpu_mem_total": [],
        # Docker
        "containers": 0, "containers_healthy": 0, "containers_unhealthy": [],
        # Gonka specific
        "sync_catching_up": None, "sync_block": "0",
        "service_state": "UNKNOWN", "pow_status": "UNKNOWN",
        "inference_running": False, "pow_running": False,
        "models": [], "models_downloading": [],
        "balance": None,
    }
    
    ok, _ = ssh_exec(name, "echo ok", config)
    if not ok:
        return status
    status["reachable"] = True
    
    # System metrics
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
    
    ok, out = ssh_exec(name, "uptime -p", config)
    if ok: status["uptime"] = out
    
    # GPU metrics
    ok, out = ssh_exec(name, "nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null", config)
    if ok and out:
        for line in out.split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                status["gpu_names"].append(parts[0])
                try:
                    status["gpu_temps"].append(float(parts[1]))
                    status["gpu_util"].append(float(parts[2]))
                    status["gpu_mem_used"].append(float(parts[3]))
                    status["gpu_mem_total"].append(float(parts[4]))
                except: pass
    
    # Docker containers
    ok, out = ssh_exec(name, "docker ps --format '{{.Names}}:{{.Status}}' 2>/dev/null", config)
    if ok and out:
        for line in out.split("\n"):
            if line:
                status["containers"] += 1
                if "healthy" in line.lower() or "up" in line.lower():
                    if "unhealthy" not in line.lower() and "restarting" not in line.lower():
                        status["containers_healthy"] += 1
                    else:
                        name_part = line.split(":")[0]
                        status["containers_unhealthy"].append(name_part)
                else:
                    name_part = line.split(":")[0]
                    status["containers_unhealthy"].append(name_part)
    
    # Node sync status
    ok, out = ssh_exec(name, "curl -s http://localhost:26657/status 2>/dev/null", config)
    if ok and out:
        try:
            data = json.loads(out)
            sync = data.get("result", {}).get("sync_info", {})
            status["sync_catching_up"] = sync.get("catching_up", True)
            status["sync_block"] = sync.get("latest_block_height", "0")
        except: pass
    
    # MLNode health
    ok, out = ssh_exec(name, "curl -s http://localhost:8080/health 2>/dev/null", config)
    if ok and out:
        try:
            data = json.loads(out)
            status["service_state"] = data.get("service_state", "UNKNOWN")
            managers = data.get("managers", {})
            status["pow_running"] = managers.get("pow", {}).get("running", False)
            status["inference_running"] = managers.get("inference", {}).get("running", False)
        except: pass
    
    # PoW status
    ok, out = ssh_exec(name, "curl -s http://localhost:8080/api/v1/pow/status 2>/dev/null", config)
    if ok and out:
        try:
            data = json.loads(out)
            status["pow_status"] = data.get("status", "UNKNOWN")
        except: pass
    
    # Models
    ok, out = ssh_exec(name, "curl -s http://localhost:8080/api/v1/models/list 2>/dev/null", config)
    if ok and out:
        try:
            data = json.loads(out)
            for m in data.get("models", []):
                model_name = m.get("model", {}).get("hf_repo", "unknown")
                model_status = m.get("status", "UNKNOWN")
                if model_status == "DOWNLOADED":
                    status["models"].append(model_name)
                elif model_status == "DOWNLOADING":
                    status["models_downloading"].append(model_name)
        except: pass
    
    # Balance (via docker network)
    ok, out = ssh_exec(name, "NODE_IP=\ && curl -s http://\:1317/cosmos/bank/v1beta1/balances/{} 2>/dev/null".format(WALLET_ADDRESS), config)
    if ok and out:
        try:
            data = json.loads(out)
            for b in data.get("balances", []):
                if b.get("denom") in ["ugnk", "gnk"]:
                    amount = float(b.get("amount", 0))
                    if b.get("denom") == "ugnk":
                        amount = amount / 1000000
                    status["balance"] = amount
        except: pass
    if status["balance"] is None:
        status["balance"] = 0.0
    
    return status


def format_full_status(s):
    """Format comprehensive status message"""
    if not s["reachable"]:
        return "❌ <b>{}</b> - UNREACHABLE".format(s["name"])
    
    # Status icons
    sync_icon = "✅" if s["sync_catching_up"] == False else "🔄" if s["sync_catching_up"] else "❓"
    service_icon = "🟢" if s["service_state"] in ["POW", "INFERENCE"] else "🟡" if s["service_state"] == "STOPPED" else "🔴"
    pow_icon = "✅" if s["pow_running"] else "⏸"
    inf_icon = "✅" if s["inference_running"] else "⏸"
    
    # GPU info
    gpu_lines = []
    for i in range(len(s["gpu_names"])):
        name = s["gpu_names"][i].replace("NVIDIA ", "")
        temp = int(s["gpu_temps"][i]) if i < len(s["gpu_temps"]) else 0
        util = int(s["gpu_util"][i]) if i < len(s["gpu_util"]) else 0
        mem_used = int(s["gpu_mem_used"][i]/1024) if i < len(s["gpu_mem_used"]) else 0
        mem_total = int(s["gpu_mem_total"][i]/1024) if i < len(s["gpu_mem_total"]) else 0
        gpu_lines.append("  GPU{}: {}°C {}% {}/{}GB".format(i, temp, util, mem_used, mem_total))
    gpu_text = "\n".join(gpu_lines) if gpu_lines else "  No GPUs"
    
    # Models
    models_text = ", ".join(s["models"]) if s["models"] else "None"
    if s["models_downloading"]:
        models_text += " (⏳ downloading: {})".format(", ".join(s["models_downloading"]))
    
    # Unhealthy containers
    unhealthy_text = ""
    if s["containers_unhealthy"]:
        unhealthy_text = "\n⚠️ Unhealthy: {}".format(", ".join(s["containers_unhealthy"]))
    
    return """<b>{name}</b>

<b>📊 System</b>
  CPU: {cpu:.0f}% | RAM: {mem:.0f}% | Disk: {disk:.0f}%
  Uptime: {uptime}

<b>🖥 GPUs</b>
{gpu}

<b>⛓ Blockchain</b>
  {sync_icon} Sync: Block {block} {sync_status}

<b>🤖 Gonka Services</b>
  {service_icon} State: {state}
  {pow_icon} PoW: {pow_status}
  {inf_icon} Inference: {inf_status}

<b>📦 Models</b>
  {models}

<b>🐳 Containers</b>
  {containers_ok}/{containers_total} healthy{unhealthy}

<b>💰 Balance</b>
  {balance:.4f} GNK""".format(
        name=s["name"],
        cpu=s["cpu"], mem=s["memory"], disk=s["disk"],
        uptime=s["uptime"],
        gpu=gpu_text,
        sync_icon=sync_icon,
        block=s["sync_block"],
        sync_status="(syncing)" if s["sync_catching_up"] else "(synced)",
        service_icon=service_icon,
        state=s["service_state"],
        pow_icon=pow_icon,
        pow_status=s["pow_status"],
        inf_icon=inf_icon,
        inf_status="Running" if s["inference_running"] else "Stopped",
        models=models_text,
        containers_ok=s["containers_healthy"],
        containers_total=s["containers"],
        unhealthy=unhealthy_text,
        balance=s["balance"]
    )


async def send_periodic_report():
    report_lines = ["📊 <b>Status Report</b> - {}".format(datetime.now().strftime("%H:%M")), ""]
    
    for name, config in NODES.items():
        s = get_full_node_status(name, config)
        
        if not s["reachable"]:
            report_lines.append("❌ {} - OFFLINE".format(name))
            continue
        
        sync_icon = "✅" if s["sync_catching_up"] == False else "🔄"
        service_icon = "🟢" if s["service_state"] in ["POW", "INFERENCE"] else "🟡"
        
        gpu_info = ""
        if s["gpu_temps"]:
            temps = ["{}°C".format(int(t)) for t in s["gpu_temps"]]
            utils = ["{}%".format(int(u)) for u in s["gpu_util"]]
            gpu_info = " | GPU: {} {}".format("/".join(temps), "/".join(utils))
        
        report_lines.append("{} <b>{}</b>".format(service_icon, name))
        report_lines.append("  CPU:{:.0f}% RAM:{:.0f}%{}".format(s["cpu"], s["memory"], gpu_info))
        report_lines.append("  {} Block {} | PoW: {}".format(sync_icon, s["sync_block"], s["pow_status"]))
        report_lines.append("  💰 {:.4f} GNK".format(s["balance"]))
        report_lines.append("")
    
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
        await send_message(chat_id, """🤖 <b>Gonka.ai Bot v2</b>

<b>Commands:</b>
/status - Full node status
/quick - Quick status summary
/sync - Blockchain sync
/balance - Wallet balance
/models - Downloaded models
/logs - Recent logs
/restart - Restart services
/report - Send full report

<b>Auto:</b> Reports every 30 min""")
    
    elif cmd == "/status":
        await send_message(chat_id, "🔍 Getting full status...")
        for name, config in NODES.items():
            status = get_full_node_status(name, config)
            await send_message(chat_id, format_full_status(status))
    
    elif cmd == "/quick":
        for name, config in NODES.items():
            s = get_full_node_status(name, config)
            if not s["reachable"]:
                await send_message(chat_id, "❌ {} offline".format(name))
            else:
                sync = "✅" if not s["sync_catching_up"] else "🔄"
                svc = "🟢" if s["service_state"] in ["POW","INFERENCE"] else "🟡"
                await send_message(chat_id, "{} {} | {} Block {} | 💰{:.2f}".format(
                    svc, name, sync, s["sync_block"], s["balance"]))
    
    elif cmd == "/sync":
        config = list(NODES.values())[0]
        s = get_full_node_status(list(NODES.keys())[0], config)
        icon = "✅" if s["sync_catching_up"] == False else "🔄"
        await send_message(chat_id, "{} Block: {}\nSyncing: {}".format(
            icon, s["sync_block"], "Yes" if s["sync_catching_up"] else "No"))
    
    elif cmd == "/balance":
        config = list(NODES.values())[0]
        s = get_full_node_status(list(NODES.keys())[0], config)
        await send_message(chat_id, "💰 Balance: {:.4f} GNK".format(s["balance"]))
    
    elif cmd == "/models":
        config = list(NODES.values())[0]
        s = get_full_node_status(list(NODES.keys())[0], config)
        text = "📦 <b>Models</b>\n\n"
        if s["models"]:
            for m in s["models"]:
                text += "✅ {}\n".format(m)
        else:
            text += "No models downloaded\n"
        if s["models_downloading"]:
            text += "\n⏳ Downloading:\n"
            for m in s["models_downloading"]:
                text += "  {}\n".format(m)
        await send_message(chat_id, text)
    
    elif cmd == "/logs":
        node_name = list(NODES.keys())[0]
        ok, logs = ssh_exec(node_name,
            "cd /opt/gonka/deploy/join && docker compose logs --tail 20 2>&1 | tail -30",
            NODES[node_name])
        if ok and logs:
            await send_message(chat_id, "<pre>{}</pre>".format(logs[-3500:]))
        else:
            await send_message(chat_id, "❌ Failed")
    
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
        s = get_full_node_status(name, config)
        now = time.time()
        
        if not s["reachable"]:
            key = name + ":unreachable"
            if key not in _last_alerts or now - _last_alerts[key] > ALERT_COOLDOWN:
                await send_message(ALLOWED_CHAT_ID, "🚨 {} unreachable!".format(name))
                _last_alerts[key] = now
        elif s["containers_healthy"] < s["containers"] - 1:
            key = name + ":unhealthy"
            if key not in _last_alerts or now - _last_alerts[key] > ALERT_COOLDOWN:
                await send_message(ALLOWED_CHAT_ID, "⚠️ {} has unhealthy containers: {}".format(
                    name, ", ".join(s["containers_unhealthy"])))
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
                for update in resp.json().get("result", []):
                    offset = update["update_id"] + 1
                    await handle_update(update)
            except Exception as e:
                print("Poll error: {}".format(e))
                await asyncio.sleep(5)


async def periodic_tasks():
    global _last_report
    while True:
        await asyncio.sleep(60)
        now = time.time()
        if now - _last_report >= REPORT_INTERVAL:
            try:
                await send_periodic_report()
                _last_report = now
            except: pass
        try:
            await check_alerts()
        except: pass


async def main():
    global _last_report
    _last_report = time.time()
    print("Gonka Bot v2 started")
    await send_message(ALLOWED_CHAT_ID, """🤖 <b>Gonka Bot v2 Online!</b>

New /status shows:
• Sync & block height
• Service state & PoW status
• Models downloaded
• Container health
• Balance

Type /help for commands""")
    await asyncio.gather(poll_updates(), periodic_tasks())

if __name__ == "__main__":
    asyncio.run(main())
