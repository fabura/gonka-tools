#!/usr/bin/env python3
"""
Gonka.ai Telegram Bot v3 - Complete Node Monitoring
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

PUBLIC_API = "http://node2.gonka.ai:8000"

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
            timeout=15,
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
            return resp.json()
    except:
        return None


async def get_network_participant():
    """Get participant info from Gonka network"""
    url = "{}/chain-api/productscience/inference/inference/participant/{}".format(
        PUBLIC_API, WALLET_ADDRESS
    )
    data = await fetch_url(url)
    if data and "participant" in data:
        return data["participant"]
    return None


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


def get_full_status(name, config):
    """Get comprehensive node status"""
    status = {
        "name": name,
        "reachable": False,
        "cpu": 0, "memory": 0, "disk": 0, "uptime": "",
        "gpu_names": [], "gpu_temps": [], "gpu_util": [], "gpu_mem": [],
        "containers": 0, "containers_healthy": 0, "unhealthy": [],
        "sync_block": "0", "synced": False,
        "service_state": "UNKNOWN",
        "inference_running": False, "pow_running": False,
        "models": [],
    }
    
    ok, _ = ssh_exec(name, "echo ok", config)
    if not ok:
        return status
    status["reachable"] = True
    
    # System
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
    
    # GPU
    ok, out = ssh_exec(name, "nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null", config)
    if ok and out:
        for line in out.split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                status["gpu_names"].append(parts[0].replace("NVIDIA ", ""))
                try:
                    status["gpu_temps"].append(float(parts[1]))
                    status["gpu_util"].append(float(parts[2]))
                    status["gpu_mem"].append("{}/{}".format(int(float(parts[3])/1024), int(float(parts[4])/1024)))
                except: pass
    
    # Docker
    ok, out = ssh_exec(name, "docker ps --format '{{.Names}}:{{.Status}}' 2>/dev/null", config)
    if ok and out:
        for line in out.split("\n"):
            if line:
                status["containers"] += 1
                name_part = line.split(":")[0]
                if "unhealthy" in line.lower() or "restarting" in line.lower():
                    status["unhealthy"].append(name_part)
                else:
                    status["containers_healthy"] += 1
    
    # Sync
    ok, out = ssh_exec(name, "curl -s http://localhost:26657/status 2>/dev/null", config)
    if ok and out:
        try:
            data = json.loads(out)
            sync = data.get("result", {}).get("sync_info", {})
            status["synced"] = not sync.get("catching_up", True)
            status["sync_block"] = sync.get("latest_block_height", "0")
        except: pass
    
    # MLNode
    ok, out = ssh_exec(name, "curl -s http://localhost:8080/health 2>/dev/null", config)
    if ok and out:
        try:
            data = json.loads(out)
            status["service_state"] = data.get("service_state", "UNKNOWN")
            status["inference_running"] = data.get("managers", {}).get("inference", {}).get("running", False)
            status["pow_running"] = data.get("managers", {}).get("pow", {}).get("running", False)
        except: pass
    
    # Models
    ok, out = ssh_exec(name, "curl -s http://localhost:8080/api/v1/models/list 2>/dev/null", config)
    if ok and out:
        try:
            data = json.loads(out)
            for m in data.get("models", []):
                if m.get("status") == "DOWNLOADED":
                    status["models"].append(m.get("model", {}).get("hf_repo", "unknown"))
        except: pass
    
    return status


def get_pow_status(name, config):
    """Get detailed PoW/PoC status"""
    status = {
        "reachable": False,
        "service_state": "UNKNOWN",
        "pow_status": "UNKNOWN",
        "pow_running": False,
        "inference_running": False,
        "node_intended": "UNKNOWN",
        "node_current": "UNKNOWN",
        "poc_intended": "UNKNOWN",
        "poc_current": "UNKNOWN",
        "epoch_models": {},
    }
    
    ok, out = ssh_exec(name, "curl -s http://localhost:8080/health 2>/dev/null", config)
    if ok and out:
        try:
            data = json.loads(out)
            status["reachable"] = True
            status["service_state"] = data.get("service_state", "UNKNOWN")
            status["pow_running"] = data.get("managers", {}).get("pow", {}).get("running", False)
            status["inference_running"] = data.get("managers", {}).get("inference", {}).get("running", False)
        except:
            pass
    
    ok, out = ssh_exec(name, "curl -s http://localhost:8080/api/v1/pow/status 2>/dev/null", config)
    if ok and out:
        try:
            data = json.loads(out)
            status["pow_status"] = data.get("status", "UNKNOWN")
        except:
            pass
    
    ok, out = ssh_exec(name, "curl -s http://localhost:9200/admin/v1/nodes 2>/dev/null", config)
    if ok and out:
        try:
            data = json.loads(out)
            if data and len(data) > 0:
                state = data[0].get("state", {})
                status["node_intended"] = state.get("intended_status", "UNKNOWN")
                status["node_current"] = state.get("current_status", "UNKNOWN")
                status["poc_intended"] = state.get("poc_intended_status", "UNKNOWN")
                status["poc_current"] = state.get("poc_current_status", "UNKNOWN")
                status["epoch_models"] = state.get("epoch_models", {})
        except:
            pass
    
    return status


async def get_earnings_info():
    """Get earnings and epoch info from network"""
    info = {
        "status": "UNKNOWN",
        "url": "",
        "epochs_completed": 0,
        "epoch_inferences": 0,
        "epoch_earned": 0,
        "balance": 0,
    }
    
    participant = await get_network_participant()
    if participant:
        info["status"] = participant.get("status", "UNKNOWN")
        info["url"] = participant.get("inference_url", "")
        info["epochs_completed"] = participant.get("epochs_completed", 0)
        stats = participant.get("current_epoch_stats", {})
        info["epoch_inferences"] = int(stats.get("inference_count", 0))
        info["epoch_earned"] = int(stats.get("earned_coins", 0))
        info["balance"] = int(participant.get("coin_balance", 0))
    
    return info


def format_status_message(s, earnings):
    """Format comprehensive status message"""
    if not s["reachable"]:
        return "❌ <b>{}</b> - OFFLINE".format(s["name"])
    
    # Icons
    sync_icon = "✅" if s["synced"] else "🔄"
    state_icon = "🟢" if s["service_state"] == "INFERENCE" else "🟡" if s["service_state"] == "STOPPED" else "🔵"
    inf_icon = "✅" if s["inference_running"] else "⏸"
    
    # GPU info
    gpu_lines = []
    for i in range(len(s["gpu_names"])):
        temp = int(s["gpu_temps"][i]) if i < len(s["gpu_temps"]) else 0
        util = int(s["gpu_util"][i]) if i < len(s["gpu_util"]) else 0
        mem = s["gpu_mem"][i] if i < len(s["gpu_mem"]) else "0/0"
        gpu_lines.append("  {} {}°C {}% {}GB".format(s["gpu_names"][i], temp, util, mem))
    gpu_text = "\n".join(gpu_lines) if gpu_lines else "  No GPUs"
    
    # Models
    models_text = ", ".join(s["models"]) if s["models"] else "None"
    
    # Unhealthy
    unhealthy_text = ""
    if s["unhealthy"]:
        unhealthy_text = "\n⚠️ Issues: {}".format(", ".join(s["unhealthy"]))
    
    # Earnings
    balance_gnk = earnings["balance"] / 1000000000 if earnings["balance"] > 0 else 0
    epoch_earned_gnk = earnings["epoch_earned"] / 1000000000 if earnings["epoch_earned"] > 0 else 0
    
    return """<b>{name}</b>

<b>📊 System</b>
  CPU: {cpu:.0f}% | RAM: {mem:.0f}% | Disk: {disk:.0f}%
  {uptime}

<b>🖥 GPUs</b>
{gpu}

<b>⛓ Blockchain</b>
  {sync_icon} Block {block} ({sync_status})

<b>🤖 Gonka</b>
  {state_icon} State: {state}
  {inf_icon} Inference: {inf_status}
  📦 Models: {models}

<b>🌐 Network</b>
  Status: {net_status}
  Epochs: {epochs}
  
<b>💰 Earnings</b>
  Balance: {balance:.4f} GNK
  This epoch: {epoch_earned:.6f} GNK ({epoch_inf} inferences)

<b>🐳 Containers</b>
  {containers_ok}/{containers_total} healthy{unhealthy}""".format(
        name=s["name"],
        cpu=s["cpu"], mem=s["memory"], disk=s["disk"],
        uptime=s["uptime"],
        gpu=gpu_text,
        sync_icon=sync_icon,
        block=s["sync_block"],
        sync_status="synced" if s["synced"] else "syncing",
        state_icon=state_icon,
        state=s["service_state"],
        inf_icon=inf_icon,
        inf_status="Running" if s["inference_running"] else "Stopped",
        models=models_text,
        net_status=earnings["status"],
        epochs=earnings["epochs_completed"],
        balance=balance_gnk,
        epoch_earned=epoch_earned_gnk,
        epoch_inf=earnings["epoch_inferences"],
        containers_ok=s["containers_healthy"],
        containers_total=s["containers"],
        unhealthy=unhealthy_text
    )


async def send_report():
    """Send periodic status report"""
    earnings = await get_earnings_info()
    
    for name, config in NODES.items():
        status = get_full_status(name, config)
        msg = format_status_message(status, earnings)
        msg += "\n\n📅 {}".format(datetime.now().strftime("%Y-%m-%d %H:%M"))
        await send_message(ALLOWED_CHAT_ID, msg)


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
        await send_message(chat_id, """🤖 <b>Gonka Bot v3</b>

<b>Commands:</b>
/status - Full node status
/quick - Quick summary
/earnings - Balance & epoch stats  
/sync - Blockchain sync
/models - Downloaded models
/logs - Recent logs
/restart - Restart services
/pow - PoW/PoC status
/report - Send full report

<b>Auto:</b> Reports every 30 min""")
    
    elif cmd == "/status":
        await send_message(chat_id, "🔍 Getting status...")
        earnings = await get_earnings_info()
        for name, config in NODES.items():
            status = get_full_status(name, config)
            await send_message(chat_id, format_status_message(status, earnings))
    
    elif cmd == "/quick":
        earnings = await get_earnings_info()
        for name, config in NODES.items():
            s = get_full_status(name, config)
            if not s["reachable"]:
                await send_message(chat_id, "❌ {} offline".format(name))
            else:
                sync = "✅" if s["synced"] else "🔄"
                state = "🟢" if s["service_state"] == "INFERENCE" else "🟡"
                balance = earnings["balance"] / 1000000000
                await send_message(chat_id, "{} {} | {} Blk {} | 💰{:.4f} GNK".format(
                    state, name[:15], sync, s["sync_block"], balance))
    
    elif cmd == "/earnings" or cmd == "/balance":
        earnings = await get_earnings_info()
        balance = earnings["balance"] / 1000000000
        epoch_earned = earnings["epoch_earned"] / 1000000000
        await send_message(chat_id, """💰 <b>Earnings</b>

Balance: <b>{:.4f} GNK</b>
Status: {}
Epochs completed: {}

<b>Current Epoch:</b>
  Inferences: {}
  Earned: {:.6f} GNK""".format(
            balance, earnings["status"], earnings["epochs_completed"],
            earnings["epoch_inferences"], epoch_earned))
    
    elif cmd == "/sync":
        for name, config in NODES.items():
            s = get_full_status(name, config)
            icon = "✅" if s["synced"] else "🔄"
            await send_message(chat_id, "{} Block: {}\nSynced: {}".format(
                icon, s["sync_block"], "Yes" if s["synced"] else "No"))
    
    elif cmd == "/models":
        for name, config in NODES.items():
            s = get_full_status(name, config)
            if s["models"]:
                text = "📦 <b>Models</b>\n\n" + "\n".join(["✅ {}".format(m) for m in s["models"]])
            else:
                text = "📦 No models downloaded"
            await send_message(chat_id, text)
    
    elif cmd == "/logs":
        name = list(NODES.keys())[0]
        config = NODES[name]
        ok, logs = ssh_exec(name,
            "cd /opt/gonka/deploy/join && docker compose logs --tail 15 2>&1 | tail -25",
            config)
        if ok and logs:
            await send_message(chat_id, "<pre>{}</pre>".format(logs[-3500:]))
        else:
            await send_message(chat_id, "❌ Failed to get logs")
    
    elif cmd == "/restart":
        name = list(NODES.keys())[0]
        config = NODES[name]
        await send_message(chat_id, "🔄 Restarting...")
        ok, _ = ssh_exec(name,
            "cd /opt/gonka/deploy/join && docker compose -f docker-compose.yml -f docker-compose.mlnode.yml restart 2>&1",
            config)
        await send_message(chat_id, "✅ Restart initiated" if ok else "❌ Failed")
    
    elif cmd == "/pow":
        for name, config in NODES.items():
            p = get_pow_status(name, config)
            if not p["reachable"]:
                await send_message(chat_id, "❌ {} offline".format(name))
                continue
            
            state_icon = "🟢" if p["service_state"] == "INFERENCE" else "🟡" if p["service_state"] == "STOPPED" else "🔵"
            pow_icon = "✅" if p["pow_running"] else "⏸"
            inf_icon = "✅" if p["inference_running"] else "⏸"
            
            models = list(p["epoch_models"].keys()) if p["epoch_models"] else ["None assigned"]
            
            await send_message(chat_id, """⚡ <b>PoW/PoC Status</b>

<b>Service State:</b> {} {}
<b>PoW Status:</b> {}

<b>Managers:</b>
  {} PoW: {}
  {} Inference: {}

<b>Node Controller:</b>
  Intended: {}
  Current: {}
  PoC Intended: {}
  PoC Current: {}

<b>Epoch Models:</b>
  {}""".format(
                state_icon, p["service_state"],
                p["pow_status"],
                pow_icon, "Running" if p["pow_running"] else "Stopped",
                inf_icon, "Running" if p["inference_running"] else "Stopped",
                p["node_intended"],
                p["node_current"],
                p["poc_intended"],
                p["poc_current"],
                "\n  ".join(models)
            ))
    
    elif cmd == "/report":
        await send_report()


async def check_alerts():
    global _last_alerts
    now = time.time()
    
    for name, config in NODES.items():
        s = get_full_status(name, config)
        
        # Offline alert
        if not s["reachable"]:
            key = "{}:offline".format(name)
            if key not in _last_alerts or now - _last_alerts[key] > ALERT_COOLDOWN:
                await send_message(ALLOWED_CHAT_ID, "🚨 <b>{}</b> is OFFLINE!".format(name))
                _last_alerts[key] = now
            continue
        
        # Not synced
        if not s["synced"]:
            key = "{}:sync".format(name)
            if key not in _last_alerts or now - _last_alerts[key] > ALERT_COOLDOWN * 2:
                await send_message(ALLOWED_CHAT_ID, "⚠️ <b>{}</b> not synced (block {})".format(name, s["sync_block"]))
                _last_alerts[key] = now
        
        # Inference not running when it should be
        if s["service_state"] == "INFERENCE" and not s["inference_running"]:
            key = "{}:inference".format(name)
            if key not in _last_alerts or now - _last_alerts[key] > ALERT_COOLDOWN:
                await send_message(ALLOWED_CHAT_ID, "⚠️ <b>{}</b> inference stopped!".format(name))
                _last_alerts[key] = now
        
        # Container issues
        if len(s["unhealthy"]) > 1:  # Allow 1 unhealthy (bridge)
            key = "{}:containers".format(name)
            if key not in _last_alerts or now - _last_alerts[key] > ALERT_COOLDOWN:
                await send_message(ALLOWED_CHAT_ID, "⚠️ <b>{}</b> unhealthy: {}".format(name, ", ".join(s["unhealthy"])))
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
    _last_report = time.time()
    
    while True:
        await asyncio.sleep(60)
        now = time.time()
        
        # Check alerts every 5 min
        try:
            await check_alerts()
        except Exception as e:
            print("Alert error: {}".format(e))
        
        # Send report every 30 min
        if now - _last_report >= REPORT_INTERVAL:
            try:
                await send_report()
                _last_report = now
            except Exception as e:
                print("Report error: {}".format(e))


async def main():
    print("Gonka Bot v3 started at {}".format(datetime.now()))
    
    await send_message(ALLOWED_CHAT_ID, """🤖 <b>Gonka Bot v3 Online!</b>

<b>New features:</b>
• Epoch & earnings tracking
• Network registration status
• Inference state monitoring
• Smart alerts

Type /status for full report""")
    
    await asyncio.gather(poll_updates(), periodic_tasks())


if __name__ == "__main__":
    asyncio.run(main())
