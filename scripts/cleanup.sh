#!/bin/bash
# Gonka Node Disk Cleanup Script
# Runs periodically to prevent disk from filling up
# SAFE: Does NOT delete blockchain data (application.db, blockstore.db, state.db, tx_index.db)
#
# Install: copy to /opt/gonka/cleanup.sh and add to crontab:
#   0 */6 * * * /opt/gonka/cleanup.sh

set -e
LOG_FILE="/var/log/gonka-cleanup.log"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

log() {
    echo "[$TIMESTAMP] $1" >> "$LOG_FILE"
}

log "Starting cleanup..."

# 1. Clear xet cache (HuggingFace download cache) - SAFE to delete
if [ -d "/mnt/shared/xet" ]; then
    SIZE=$(du -sh /mnt/shared/xet 2>/dev/null | cut -f1 || echo "0")
    rm -rf /mnt/shared/xet
    log "Cleared xet cache: $SIZE"
fi

# 2. Docker prune (unused images, containers, networks) - SAFE
DOCKER_FREED=$(docker system prune -f 2>/dev/null | grep "Total reclaimed space" || echo "0B")
log "Docker prune: $DOCKER_FREED"

# 3. Clear journal logs older than 7 days - SAFE
journalctl --vacuum-time=7d 2>/dev/null || true
log "Cleared old journal logs"

# 4. Clear ONLY old snapshots (keep last 2) - SAFE, blockchain data is preserved
# NOTE: Only deletes .inference/data/snapshots/*, NOT the actual blockchain databases
SNAPSHOTS_DIR="/opt/gonka/deploy/join/.inference/data/snapshots"
if [ -d "$SNAPSHOTS_DIR" ]; then
    cd "$SNAPSHOTS_DIR"
    SNAP_COUNT=$(ls -1d */ 2>/dev/null | wc -l || echo "0")
    if [ "$SNAP_COUNT" -gt 2 ]; then
        ls -1dt */ 2>/dev/null | tail -n +3 | xargs rm -rf 2>/dev/null || true
        log "Cleaned old snapshots (kept 2 newest)"
    fi
fi

# 5. Clear apt cache - SAFE
apt-get clean 2>/dev/null || true

# 6. Clear old container logs (truncate, not delete) - SAFE
find /var/lib/docker/containers -name "*-json.log" -size +100M -exec truncate -s 10M {} \; 2>/dev/null || true
log "Truncated large container logs"

# Report disk usage
DISK_USAGE=$(df -h / | tail -1 | awk '{print $5 " used, " $4 " free"}')
log "Disk status: $DISK_USAGE"

log "Cleanup completed"

# NEVER DELETE (blockchain data):
# - /opt/gonka/deploy/join/.inference/data/application.db
# - /opt/gonka/deploy/join/.inference/data/blockstore.db
# - /opt/gonka/deploy/join/.inference/data/state.db
# - /opt/gonka/deploy/join/.inference/data/tx_index.db
# - /mnt/shared/hub (model cache - needed for inference)
