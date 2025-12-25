#!/usr/bin/env python3
"""
Gonka.ai Telegram Bot - Node monitoring + installer

Security:
- Do NOT hardcode bot tokens, chat IDs, wallet addresses, or server IPs here.
- This script loads configuration from environment variables and a nodes.yaml file.
"""

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, List

import httpx
import paramiko
import yaml

# Configuration
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
WALLET_ADDRESS = os.environ.get("GONKA_WALLET_ADDRESS", "").strip()
PUBLIC_API = os.environ.get("GONKA_PUBLIC_API", "http://node2.gonka.ai:8000").strip()
NODES_YAML_PATH = os.environ.get("NODES_YAML_PATH", "./config/nodes.yaml").strip()
DEFAULT_MODEL_HF_REPO = os.environ.get("DEFAULT_MODEL_HF_REPO", "Qwen/Qwen3-32B-FP8").strip()
SINGLE_MODEL_MODE = os.environ.get("SINGLE_MODEL_MODE", "1").strip().lower() in ("1", "true", "yes", "y")
DEFAULT_ACCOUNT_PUBKEY = os.environ.get("DEFAULT_ACCOUNT_PUBKEY", "").strip()
DEFAULT_SSH_USER = os.environ.get("DEFAULT_SSH_USER", "ubuntu").strip()
DEFAULT_SSH_KEY_PATH = os.environ.get("DEFAULT_SSH_KEY_PATH", "/root/.ssh/gonka_key").strip()


def load_nodes() -> dict:
    """Load nodes from nodes.yaml (see config/nodes.yaml.example)."""
    try:
        with open(NODES_YAML_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}

    nodes = {}
    for n in data.get("nodes", []) or []:
        name = n.get("name")
        if not name:
            continue
        nodes[name] = {
            "host": n.get("host"),
            "port": int(n.get("port", 22)),
            "user": n.get("user", "root"),
            "key_path": os.path.expanduser(n.get("ssh_key") or "~/.ssh/gonka_key"),
        }
    return nodes


NODES = load_nodes()
_NODES_LOCK = asyncio.Lock()


def load_nodes_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_nodes_yaml(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)

CHECK_INTERVAL = 300
REPORT_INTERVAL = 1800
ALERT_COOLDOWN = 600

# Free disk space alert (GB). Percent-only alerts are not enough because a big disk at 90%
# can still have lots of space, while a smaller disk can be dangerously low.
DISK_FREE_GB_ALERT_THRESHOLD = float(os.environ.get("DISK_FREE_GB_ALERT_THRESHOLD", "15"))

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
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        output = out if out else err
        client.close()
        return True, output
    except Exception as e:
        return False, str(e)


def sh_quote(cmd: str) -> str:
    """Wrap a command so it executes safely under a POSIX shell on the remote host."""
    # Prefer bash for consistent behavior; fall back to sh.
    # We also must prevent the remote shell from expanding awk variables like `$3` or `$i`.
    safe = cmd.replace("$", "\\$")
    return "bash -lc " + json.dumps(safe)


def _to_float(s: str):
    try:
        return float(str(s).strip())
    except Exception:
        return None


def hf_cache_dir_for_repo(hf_repo: str) -> str:
    # HuggingFace hub cache naming: models--{org}--{name}
    return "/mnt/shared/hub/models--" + hf_repo.replace("/", "--")


def pick_inference_args(hf_repo: str, gpu_count: int) -> List[str]:
    """Pick known-good vLLM args for common Gonka models."""
    tp = max(1, int(gpu_count or 1))
    tp = 2 if tp >= 2 else 1

    if hf_repo == "Qwen/Qwen3-32B-FP8":
        return [
            "--tensor-parallel-size", str(tp),
            "--pipeline-parallel-size", "1",
            "--quantization", "fp8",
            "--kv-cache-dtype", "fp8",
            "--gpu-memory-utilization", "0.95",
            "--max-model-len", "32768",
        ]

    # Default: small/medium fp8 run (no special max-model-len needed)
    return [
        "--tensor-parallel-size", str(tp),
        "--pipeline-parallel-size", "1",
        "--quantization", "fp8",
        "--gpu-memory-utilization", "0.90",
    ]


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
        "cpu": None, "memory": None, "disk": None, "disk_free_gb": None, "uptime": "",
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
    
    # System (avoid awk $-field expansion issues by parsing with python)
    ok, out = ssh_exec(
        name,
        sh_quote(
            "python3 -c 'import re,subprocess; "
            "s=subprocess.check_output([\"top\",\"-bn1\"], text=True, stderr=subprocess.STDOUT); "
            "m=re.search(r\"%Cpu\\(s\\):.*?([0-9.]+)\\s*id\", s); "
            "print(100-float(m.group(1)) if m else \"\")'"
        ),
        config,
    )
    if ok and out:
        status["cpu"] = _to_float(out.replace("%", ""))

    ok, out = ssh_exec(
        name,
        sh_quote(
            "python3 -c 'import subprocess; "
            "s=subprocess.check_output([\"free\",\"-b\"], text=True, stderr=subprocess.STDOUT).splitlines(); "
            "mem=[l for l in s if l.startswith(\"Mem:\")][0].split(); "
            "total=int(mem[1]); used=int(mem[2]); "
            "print((used/total)*100.0 if total else \"\")'"
        ),
        config,
    )
    if ok and out:
        status["memory"] = _to_float(out)

    ok, out = ssh_exec(
        name,
        sh_quote(
            "python3 -c 'import subprocess; "
            "l=subprocess.check_output([\"df\",\"-P\",\"-B1\",\"/\"], text=True, stderr=subprocess.STDOUT).splitlines()[-1].split(); "
            "total=int(l[1]); used=int(l[2]); avail=int(l[3]); "
            "print(((used/total)*100.0 if total else \"\")); "
            "print(avail/1e9)'"
        ),
        config,
    )
    if ok and out:
        lines = [x for x in out.splitlines() if x.strip()]
        if len(lines) >= 1:
            status["disk"] = _to_float(lines[0])
        if len(lines) >= 2:
            status["disk_free_gb"] = _to_float(lines[1])
    
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
    
    cpu_txt = "n/a" if s.get("cpu") is None else f"{s['cpu']:.0f}%"
    mem_txt = "n/a" if s.get("memory") is None else f"{s['memory']:.0f}%"
    disk_pct_txt = "n/a" if s.get("disk") is None else f"{s['disk']:.0f}%"
    disk_free_txt = "n/a" if s.get("disk_free_gb") is None else f"{s['disk_free_gb']:.0f}GB free"

    return """<b>{name}</b>

<b>📊 System</b>
  CPU: {cpu_txt} | RAM: {mem_txt} | Disk: {disk_pct_txt} ({disk_free_txt})
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
        cpu_txt=cpu_txt,
        mem_txt=mem_txt,
        disk_pct_txt=disk_pct_txt,
        disk_free_txt=disk_free_txt,
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
    text = msg.get("text", "") or ""
    
    if chat_id != ALLOWED_CHAT_ID:
        await send_message(chat_id, "⛔ Unauthorized")
        return
    
    # Telegram group chats often send commands as /cmd@BotUserName.
    # Normalize by stripping the @suffix from the first token so commands work in groups too.
    raw_text = text.strip()
    parts = raw_text.split()
    if parts and parts[0].startswith("/"):
        parts[0] = parts[0].split("@", 1)[0]
    text = " ".join(parts) if parts else raw_text
    cmd = text.lower().strip()
    
    if cmd == "/start" or cmd == "/help":
        await send_message(chat_id, """🤖 <b>Gonka Bot v3</b>

<b>Commands:</b>
/status - Full node status
/quick - Quick summary
/earnings - Balance & epoch stats  
/sync - Blockchain sync
/models - Downloaded models
/epoch_model - Show controller epoch model
/align - Download + deploy controller epoch model (single-model mode)
/remove_node &lt;name&gt; - Remove node from servers list (nodes.yaml)
/logs - Recent logs
/restart - Restart services
/pow - PoW/PoC status
/report - Send full report

<b>Setup:</b>
/install &lt;ip&gt; [pubkey] - Install node on new server
/check &lt;ip&gt; - Check server before install (uses DEFAULT_SSH_USER)

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

    elif cmd.startswith("/remove_node"):
        parts = text.strip().split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            await send_message(chat_id, "Usage: /remove_node <name>\nExample: /remove_node neon-galaxy-fin-01")
            return

        node_name = parts[1].strip()
        async with _NODES_LOCK:
            try:
                data = load_nodes_yaml(NODES_YAML_PATH)
            except FileNotFoundError:
                await send_message(chat_id, f"❌ nodes.yaml not found at: {NODES_YAML_PATH}")
                return
            except Exception as e:
                await send_message(chat_id, f"❌ Failed to read nodes.yaml: {str(e)[:120]}")
                return

            nodes_list = data.get("nodes", []) or []
            before = len(nodes_list)
            nodes_list = [n for n in nodes_list if (n or {}).get("name") != node_name]
            after = len(nodes_list)
            if before == after:
                await send_message(chat_id, f"⚠️ Node not found in nodes.yaml: {node_name}")
                return

            data["nodes"] = nodes_list
            try:
                save_nodes_yaml(NODES_YAML_PATH, data)
            except Exception as e:
                await send_message(chat_id, f"❌ Failed to write nodes.yaml: {str(e)[:120]}")
                return

            # Reload in-memory nodes (in-place so we don't need `global NODES`)
            NODES.clear()
            NODES.update(load_nodes())

        await send_message(chat_id, f"✅ Removed node: <b>{node_name}</b>\nRemaining: {', '.join(NODES.keys()) if NODES else '(none)'}")

    elif cmd == "/epoch_model":
        for name, config in NODES.items():
            ok, out = ssh_exec(name, "curl -s http://localhost:9200/admin/v1/nodes 2>/dev/null", config)
            if not ok or not out:
                await send_message(chat_id, f"❌ {name}: cannot reach controller admin API on :9200 (is `api` container running?)")
                continue
            try:
                data = json.loads(out)
                state = (data[0] or {}).get("state", {}) if isinstance(data, list) and data else {}
                epoch_models = list((state.get("epoch_models") or {}).keys())
                await send_message(
                    chat_id,
                    "📌 <b>{}</b>\n"
                    "<b>Node:</b> {} → {}\n"
                    "<b>PoC:</b> {} → {}\n"
                    "<b>Epoch models:</b>\n  {}".format(
                        name,
                        state.get("intended_status", "UNKNOWN"),
                        state.get("current_status", "UNKNOWN"),
                        state.get("poc_intended_status", "UNKNOWN"),
                        state.get("poc_current_status", "UNKNOWN"),
                        "\n  ".join(epoch_models) if epoch_models else "None",
                    ),
                )
            except Exception as e:
                await send_message(chat_id, f"❌ {name}: failed to parse admin API: {str(e)[:120]}")

    elif cmd == "/align":
        # Align to controller epoch model: cleanup (single-model), download, deploy, verify.
        target_name = list(NODES.keys())[0]
        target = NODES[target_name]

        await send_message(chat_id, f"🧭 Aligning <b>{target_name}</b> to controller epoch model...")

        ok, out = ssh_exec(target_name, "curl -s http://localhost:9200/admin/v1/nodes 2>/dev/null", target)
        if not ok or not out:
            await send_message(chat_id, "❌ Controller admin API not reachable on :9200. Start it with:\n<pre>cd /opt/gonka/deploy/join && docker compose -f docker-compose.yml -f docker-compose.mlnode.yml up -d</pre>")
            return

        try:
            data = json.loads(out)
            state = (data[0] or {}).get("state", {}) if isinstance(data, list) and data else {}
            epoch_models = list((state.get("epoch_models") or {}).keys())
            epoch_model = epoch_models[0] if epoch_models else ""
        except Exception as e:
            await send_message(chat_id, f"❌ Failed to parse controller epoch_models: {str(e)[:120]}")
            return

        if not epoch_model:
            await send_message(chat_id, "❌ No epoch model assigned by controller.")
            return

        # GPU count for TP sizing
        ok, out = ssh_exec(target_name, "nvidia-smi -L 2>/dev/null | wc -l", target)
        gpu_count = 2
        if ok and out.strip().isdigit():
            gpu_count = int(out.strip())

        await send_message(chat_id, f"📌 Epoch model: <b>{epoch_model}</b> (GPUs: {gpu_count})")

        # Stop inference (required)
        ssh_exec(target_name, "curl -s -X POST http://localhost:8080/api/v1/inference/down -H 'Content-Type: application/json' >/dev/null 2>&1 || true", target)

        # Single-model mode cleanup on target host
        if SINGLE_MODEL_MODE:
            keep_dir = hf_cache_dir_for_repo(epoch_model)
            cleanup_cmd = f"""set -e
rm -rf /mnt/shared/xet/* 2>/dev/null || true
if [ -d /mnt/shared/hub ]; then
  for d in /mnt/shared/hub/models--*; do
    [ "$d" = "{keep_dir}" ] && continue
    rm -rf "$d" || true
  done
fi
df -h / | tail -1
"""
            ssh_exec(target_name, cleanup_cmd, target)

        # Download model
        await send_message(chat_id, "📥 Downloading model...")
        ssh_exec(
            target_name,
            f"curl -s -X POST http://localhost:8080/api/v1/models/download -H 'Content-Type: application/json' -d '{{\"hf_repo\":\"{epoch_model}\"}}' >/dev/null 2>&1 || true",
            target,
        )

        # Wait until downloaded (best-effort)
        for i in range(1, 61):
            ok, out = ssh_exec(target_name, "curl -s http://localhost:8080/api/v1/models/list 2>/dev/null", target)
            status = ""
            try:
                j = json.loads(out) if out else {}
                for m in j.get("models", []):
                    if (m.get("model") or {}).get("hf_repo") == epoch_model:
                        status = m.get("status", "")
                        break
            except Exception:
                status = ""

            if status == "DOWNLOADED":
                break

            if i in (1, 5, 10, 20, 30, 40, 50, 60):
                ok2, disk = ssh_exec(target_name, "df -h / | tail -1", target)
                await send_message(chat_id, f"⏳ Download status: {status or 'UNKNOWN'} (check {i}/60)\n<pre>{disk if ok2 else ''}</pre>")
            await asyncio.sleep(20)

        # Deploy model (inference/up)
        args = pick_inference_args(epoch_model, gpu_count)
        payload = {"model": epoch_model, "dtype": "float16", "additional_args": args}
        deploy_cmd = "curl -sS -X POST http://localhost:8080/api/v1/inference/up -H 'Content-Type: application/json' -d '{}'".format(
            json.dumps(payload).replace("'", "\\'")
        )
        ok, out = ssh_exec(target_name, deploy_cmd, target)
        if not ok:
            await send_message(chat_id, f"❌ Deploy failed: {out[:500]}")
            return

        await send_message(chat_id, f"🚀 Deploy triggered. Waiting for INFERENCE...")
        await asyncio.sleep(45)
        ok, state_out = ssh_exec(target_name, "curl -s http://localhost:8080/api/v1/state 2>/dev/null", target)
        await send_message(chat_id, f"✅ Align complete.\n<pre>{state_out[-2000:] if state_out else ''}</pre>")
    
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
        await send_message(chat_id, "🔍 Checking server {} (user: {})...".format(ip, DEFAULT_SSH_USER))
        
        config = {"host": ip, "port": 22, "user": DEFAULT_SSH_USER, "key_path": DEFAULT_SSH_KEY_PATH}
        
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
        if len(parts) < 2:
            await send_message(chat_id, "Usage: /install <ip> [pubkey]\n\nExample:\n/install 65.108.33.117\n/install 65.108.33.117 AxIZzsbyk90lA...")
            return
        
        ip = parts[1]
        pubkey = parts[2] if len(parts) >= 3 else DEFAULT_ACCOUNT_PUBKEY
        
        if not pubkey:
            await send_message(chat_id, "❌ No pubkey provided and DEFAULT_ACCOUNT_PUBKEY not set in environment.\n\nUsage: /install <ip> <pubkey>")
            return
        
        await send_message(chat_id, "🚀 Starting Gonka installation on {} (user: {})...\nThis will take 10-15 minutes.".format(ip, DEFAULT_SSH_USER))
        
        config = {"host": ip, "port": 22, "user": DEFAULT_SSH_USER, "key_path": DEFAULT_SSH_KEY_PATH}
        
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
        model_repo = os.environ.get("DEFAULT_MODEL_HF_REPO", "Qwen/Qwen3-32B-FP8")

        # Single-model mode: keep disk stable by removing other model caches.
        # We do this BEFORE starting a new download to avoid running out of space mid-download.
        if SINGLE_MODEL_MODE:
            await send_message(chat_id, f"🧹 Single-model mode: keeping only {model_repo} cache...")
            keep_dir = hf_cache_dir_for_repo(model_repo)
            cleanup_cmd = f"""set -e
rm -rf /mnt/shared/xet/* 2>/dev/null || true
if [ -d /mnt/shared/hub ]; then
  for d in /mnt/shared/hub/models--*; do
    [ \"$d\" = \"{keep_dir}\" ] && continue
    rm -rf \"$d\" || true
  done
fi
df -h / | tail -1
"""
            ssh_exec(ip, cleanup_cmd, config)
        await send_message(chat_id, f"📥 Downloading model ({model_repo})...")
        ok, _ = ssh_exec(ip, """
            curl -s -X POST http://localhost:8080/api/v1/models/download \
                -H 'Content-Type: application/json' \
                -d '{"hf_repo": "%s"}' > /dev/null 2>&1
        """ % model_repo, config)
        
        # Check model status after a bit
        await asyncio.sleep(30)
        ok, model_status = ssh_exec(ip, """
            curl -s -X POST http://localhost:8080/api/v1/models/status \
                -H 'Content-Type: application/json' \
                -d '{"hf_repo": "%s"}' 2>/dev/null | jq -r '.status' 2>/dev/null || echo 'UNKNOWN'
        """ % model_repo, config)
        
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
        
        # Final status check - wait for everything to stabilize
        await send_message(chat_id, "🔍 Running final status check...")
        await asyncio.sleep(30)
        
        # Check sync status
        ok, sync_out = ssh_exec(ip, "curl -s http://localhost:26657/status 2>/dev/null | jq -r '.result.sync_info | \"Block: \" + .latest_block_height + \" Syncing: \" + (.catching_up|tostring)'", config)
        sync_msg = sync_out if ok and sync_out else "Checking..."
        
        # Check MLNode/PoW status
        ok, health_out = ssh_exec(ip, """
            curl -s http://localhost:8080/health 2>/dev/null | jq -r '"State: " + .service_state + " | Inference: " + (.managers.inference.running|tostring)'
        """, config)
        mlnode_msg = health_out if ok and health_out else "Starting..."
        
        # Check PoW status
        ok, pow_out = ssh_exec(ip, "curl -s http://localhost:8080/api/v1/pow/status 2>/dev/null | jq -r '.status'", config)
        pow_msg = pow_out if ok and pow_out else "UNKNOWN"
        
        # Check admin node status
        ok, admin_out = ssh_exec(ip, """
            curl -s http://localhost:9200/admin/v1/nodes 2>/dev/null | jq -r '.[0].state | "Intended: " + .intended_status + " | Current: " + .current_status'
        """, config)
        admin_msg = admin_out if ok and admin_out else "Initializing..."
        
        # Check participant registration on network
        ok, participant_out = ssh_exec(ip, """
            curl -s 'http://node2.gonka.ai:8000/chain-api/productscience/inference/inference/participant/gonka1k82cpjqhfgt347x7grdudu7zfaew6strzjwhyl' 2>/dev/null | jq -r '"Status: " + .participant.status + " | URL: " + .participant.inference_url'
        """, config)
        participant_msg = participant_out if ok and "http" in str(participant_out) else "Pending..."
        
        # Recheck model status
        ok, model_final = ssh_exec(ip, """
            curl -s http://localhost:8080/api/v1/models/list 2>/dev/null | jq -r '.models[0] | .model.hf_repo + ": " + .status'
        """, config)
        model_final_msg = model_final if ok and model_final else model_msg

        # Attempt to deploy the model (Qwen3-32B-FP8 requires max-model-len tuning)
        await send_message(chat_id, "🚀 Deploying model (starting vLLM)...")
        deploy_cmd = f"""curl -sS -X POST http://localhost:8080/api/v1/inference/up \\
  -H 'Content-Type: application/json' \\
  -d '{{"model":"{model_repo}","dtype":"float16","additional_args":["--tensor-parallel-size","2","--pipeline-parallel-size","1","--quantization","fp8","--kv-cache-dtype","fp8","--gpu-memory-utilization","0.95","--max-model-len","32768"]}}'"""
        ok, deploy_out = ssh_exec(ip, deploy_cmd, config)
        deploy_msg = "✅ Deploy started" if ok and ("OK" in deploy_out or "status" in deploy_out) else f"⚠️ Deploy: {deploy_out[:120]}"
        
        await send_message(chat_id, """✅ <b>Installation Complete!</b>

<b>Server:</b> {}
<b>ML Ops:</b> <code>{}</code>
<b>Password:</b> <code>gonkapass</code>

<b>📋 Setup Status:</b>
  Grant: {}
  Register: {}

<b>⛓ Blockchain:</b>
  {}

<b>🤖 MLNode:</b>
  {}
  PoW: {}

<b>🎮 Controller:</b>
  {}

<b>🌐 Network:</b>
  {}

<b>📦 Model:</b>
  {}
  {}

<b>🐳 Containers:</b>
<pre>{}</pre>

{}""".format(
            server_ip, ml_ops_address,
            grant_msg, register_msg,
            sync_msg,
            mlnode_msg, pow_msg,
            admin_msg,
            participant_msg,
            model_final_msg,
            deploy_msg,
            status,
            "🎉 Ready! PoC runs every 24h." if "INFERENCE" in str(mlnode_msg) else "⏳ Starting up... Check /status in a few minutes."
        ))
        
        # Add to NODES for monitoring
        await send_message(chat_id, """📌 <b>Add to monitoring:</b>
To add this node to bot monitoring, update NODES in bot.py:

<pre>"{}": {{
    "host": "{}",
    "port": 22,
    "user": "root",
    "key_path": "/root/.ssh/gonka_key",
}}</pre>""".format(server_ip.replace(".", "-"), ip))
    
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

        # Low free disk space (GB)
        if s.get("disk_free_gb") is not None and s["disk_free_gb"] <= DISK_FREE_GB_ALERT_THRESHOLD:
            key = "{}:disk_free".format(name)
            if key not in _last_alerts or now - _last_alerts[key] > ALERT_COOLDOWN:
                await send_message(
                    ALLOWED_CHAT_ID,
                    "🚨 <b>{}</b> low disk space: <b>{:.1f} GB free</b> (threshold: {:.0f} GB)".format(
                        name, s["disk_free_gb"], DISK_FREE_GB_ALERT_THRESHOLD
                    ),
                )
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

    if not BOT_TOKEN or not ALLOWED_CHAT_ID:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in environment")
    # Allow starting even with an empty nodes.yaml so the bot can still be used for
    # /install, /check, and other non-monitoring commands.
    if not NODES:
        print(f"No nodes loaded. Monitoring disabled until NODES_YAML_PATH has nodes (currently: {NODES_YAML_PATH})")
    
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
