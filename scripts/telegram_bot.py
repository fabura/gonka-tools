#!/usr/bin/env python3
"""
Gonka.ai Telegram Bot v3 - Complete Node Monitoring
"""

import asyncio
import json
import subprocess
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

<b>Setup:</b>
/install &lt;ip&gt; &lt;pubkey&gt; - Install node on new server
/check &lt;ip&gt; - Check server before install

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
    
    elif text.lower().startswith("/check "):
        # Check a server before installation
        parts = text.strip().split()
        if len(parts) < 2:
            await send_message(chat_id, "Usage: /check <ip>")
            return
        
        ip = parts[1]
        await send_message(chat_id, "🔍 Checking server {}...".format(ip))
        
        config = {"host": ip, "port": 22, "user": "root", "key_path": "/root/.ssh/gonka_key"}
        
        # Check connectivity
        ok, out = ssh_exec(ip, "echo ok", config)
        if not ok:
            await send_message(chat_id, "❌ Cannot connect: {}".format(out))
            return
        
        # Get system info
        ok, hostname = ssh_exec(ip, "hostname", config)
        ok, cpu_info = ssh_exec(ip, "nproc", config)
        ok, mem_info = ssh_exec(ip, "free -h | grep Mem | awk '{print $2}'", config)
        ok, disk_info = ssh_exec(ip, "df -h / | tail -1 | awk '{print $4}'", config)
        ok, gpu_info = ssh_exec(ip, "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo 'No GPU'", config)
        ok, docker_info = ssh_exec(ip, "docker --version 2>/dev/null || echo 'Not installed'", config)
        ok, gonka_info = ssh_exec(ip, "test -d /opt/gonka && echo 'Installed' || echo 'Not installed'", config)
        
        await send_message(chat_id, """✅ <b>Server Check: {}</b>

<b>System:</b>
  Hostname: {}
  CPUs: {}
  RAM: {}
  Disk Free: {}

<b>GPU:</b>
  {}

<b>Software:</b>
  Docker: {}
  Gonka: {}""".format(ip, hostname, cpu_info, mem_info, disk_info, gpu_info, docker_info, gonka_info))
    
    elif text.lower().startswith("/install "):
        # Install Gonka on a new server
        parts = text.strip().split()
        if len(parts) < 3:
            await send_message(chat_id, "Usage: /install <ip> <account_pubkey>\n\nExample:\n/install 65.108.33.117 AxIZzsbyk90lA...")
            return
        
        ip = parts[1]
        pubkey = parts[2]
        
        await send_message(chat_id, "🚀 Starting Gonka installation on {}...\nThis will take 10-15 minutes.".format(ip))
        
        config = {"host": ip, "port": 22, "user": "root", "key_path": "/root/.ssh/gonka_key"}
        
        # Check connectivity first
        ok, _ = ssh_exec(ip, "echo ok", config)
        if not ok:
            await send_message(chat_id, "❌ Cannot connect to {}. Check SSH key.".format(ip))
            return
        
        # Run installation
        await send_message(chat_id, "📦 Step 1/5: Installing dependencies...")
        ok, out = ssh_exec(ip, "apt-get update -qq && apt-get install -y -qq curl wget git jq unzip expect docker.io", config)
        if not ok:
            await send_message(chat_id, "❌ Failed to install dependencies")
            return
        
        await send_message(chat_id, "🐳 Step 2/5: Setting up Docker & NVIDIA...")
        ok, _ = ssh_exec(ip, """
            systemctl enable docker && systemctl start docker
            if lspci | grep -i nvidia > /dev/null; then
                curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg 2>/dev/null
                curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
                apt-get update -qq && apt-get install -y -qq nvidia-container-toolkit
                nvidia-ctk runtime configure --runtime=docker
                systemctl restart docker
            fi
        """, config)
        
        await send_message(chat_id, "📥 Step 3/5: Cloning Gonka repository...")
        ok, _ = ssh_exec(ip, """
            rm -rf /opt/gonka
            git clone https://github.com/gonka-ai/gonka.git -b main /opt/gonka --quiet
            mkdir -p /mnt/shared
            mkdir -p /root/.inference/keyring-file
        """, config)
        
        await send_message(chat_id, "🔐 Step 4/5: Creating ML Ops key...")
        # Create ML ops key
        ok, key_output = ssh_exec(ip, """
            cd /opt/gonka/deploy/join
            expect << 'EOF'
spawn inferenced keys add ml-ops-key --keyring-backend file
expect "passphrase"
send "gonkapass\\r"
expect "passphrase"
send "gonkapass\\r"
expect eof
EOF
            sleep 2
            echo "---KEY_INFO---"
            echo "gonkapass" | inferenced keys show ml-ops-key --keyring-backend file 2>/dev/null
        """, config)
        
        # Parse ML ops address
        ml_ops_address = ""
        if "---KEY_INFO---" in key_output:
            for line in key_output.split("\n"):
                if "address:" in line:
                    ml_ops_address = line.split()[-1].strip()
                    break
        
        await send_message(chat_id, "⚙️ Step 5/5: Configuring and starting services...")
        # Create config and start
        ok, _ = ssh_exec(ip, """
            cd /opt/gonka/deploy/join
            SERVER_IP=$(curl -s ifconfig.me)
            
            cat > .env << ENVEOF
KEY_NAME=ml-ops-key
KEYRING_PASSWORD=gonkapass
KEYRING_BACKEND=file
API_PORT=8000
API_SSL_PORT=8443
PUBLIC_URL=http://${{SERVER_IP}}:8000
P2P_EXTERNAL_ADDRESS=tcp://${{SERVER_IP}}:5000
ACCOUNT_PUBKEY={pubkey}
NODE_CONFIG=./node-config.json
HF_HOME=/mnt/shared
SEED_API_URL=http://node2.gonka.ai:8000
SEED_NODE_RPC_URL=http://node2.gonka.ai:26657
SEED_NODE_P2P_URL=tcp://node2.gonka.ai:5000
DAPI_API__POC_CALLBACK_URL=http://api:9100
DAPI_CHAIN_NODE__URL=http://node:26657
DAPI_CHAIN_NODE__P2P_URL=http://node:26656
RPC_SERVER_URL_1=http://node1.gonka.ai:26657
RPC_SERVER_URL_2=http://node2.gonka.ai:26657
PORT=8080
INFERENCE_PORT=5050
ENVEOF
            
            mkdir -p .inference/keyring-file
            cp -r /root/.inference/keyring-file/* .inference/keyring-file/ 2>/dev/null || true
            
            docker compose -f docker-compose.yml -f docker-compose.mlnode.yml pull 2>&1 | tail -3
            docker compose -f docker-compose.yml -f docker-compose.mlnode.yml up -d 2>&1 | tail -5
        """.format(pubkey=pubkey), config)
        
        # Wait a bit and check status
        await send_message(chat_id, "⏳ Waiting for services to start (90s)...")
        await asyncio.sleep(90)
        
        # Download model
        await send_message(chat_id, "📥 Downloading model (Qwen/Qwen2.5-7B-Instruct)...")
        ok, _ = ssh_exec(ip, """
            curl -s -X POST http://localhost:8080/api/v1/models/download \
                -H 'Content-Type: application/json' \
                -d '{"hf_repo": "Qwen/Qwen2.5-7B-Instruct"}' > /dev/null 2>&1
        """, config)
        
        # Check model status after a bit
        await asyncio.sleep(30)
        ok, model_status = ssh_exec(ip, """
            curl -s -X POST http://localhost:8080/api/v1/models/status \
                -H 'Content-Type: application/json' \
                -d '{"hf_repo": "Qwen/Qwen2.5-7B-Instruct"}' 2>/dev/null | jq -r '.status' 2>/dev/null || echo 'UNKNOWN'
        """, config)
        
        ok, status = ssh_exec(ip, "docker ps --format '{{.Names}}: {{.Status}}' | head -8", config)
        
        # Get server's public IP
        ok, server_ip = ssh_exec(ip, "curl -s ifconfig.me", config)
        
        model_msg = "✅ Downloaded" if "DOWNLOADED" in model_status else "⏳ Downloading (~15GB)"
        
        # Run grant-ml-ops-permissions from monitoring server
        await send_message(chat_id, "🔐 Granting ML Ops permissions...")
        try:
            grant_result = subprocess.run([
                "bash", "-c",
                "echo 'gonkapass' | inferenced tx inference grant-ml-ops-permissions "
                "gonka-account-key {} "
                "--from gonka-account-key "
                "--keyring-backend file "
                "--node http://node2.gonka.ai:26657 "
                "--chain-id gonka-mainnet "
                "--gas 1000000 -y 2>&1".format(ml_ops_address)
            ], capture_output=True, text=True, timeout=60)
            grant_ok = "confirmed" in grant_result.stdout.lower() or grant_result.returncode == 0
            grant_msg = "✅ Granted" if grant_ok else "⚠️ Check manually"
        except Exception as e:
            grant_msg = "⚠️ Error: {}".format(str(e)[:50])
        
        await asyncio.sleep(10)
        
        # Run submit-new-participant from monitoring server
        await send_message(chat_id, "📝 Registering node on network...")
        try:
            register_result = subprocess.run([
                "bash", "-c",
                "echo 'gonkapass' | inferenced tx inference submit-new-participant "
                "http://{}:8000 "
                "--from gonka-account-key "
                "--keyring-backend file "
                "--node http://node2.gonka.ai:26657 "
                "--chain-id gonka-mainnet "
                "--gas 1000000 -y 2>&1".format(server_ip)
            ], capture_output=True, text=True, timeout=60)
            register_ok = "confirmed" in register_result.stdout.lower() or register_result.returncode == 0
            register_msg = "✅ Registered" if register_ok else "⚠️ Check manually"
        except Exception as e:
            register_msg = "⚠️ Error: {}".format(str(e)[:50])
        
        await send_message(chat_id, """✅ <b>Installation Complete!</b>

<b>Server:</b> {} ({})
<b>ML Ops Address:</b> <code>{}</code>
<b>Keyring Password:</b> <code>gonkapass</code>

<b>Status:</b>
  Model: {}
  Grant: {}
  Register: {}

<b>Containers:</b>
<pre>{}</pre>

🎉 Node is ready! PoC validation runs every 24h.""".format(
            ip, server_ip, ml_ops_address, model_msg, grant_msg, register_msg, status))
    
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
