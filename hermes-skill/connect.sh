#!/usr/bin/env bash
# SwarmTrader + Hermes Agent — one-command launcher
#
# Usage:
#   ./hermes-skill/connect.sh                    # interactive mode (you type decisions)
#   ./hermes-skill/connect.sh --auto             # Hermes CLI makes decisions automatically
#   ./hermes-skill/connect.sh --register         # first-time registration
#   ./hermes-skill/connect.sh --live             # live trading (real money!)
#
# Prerequisites:
#   1. pip install aiohttp   (or: pip install websockets)
#   2. Kraken API keys in .env (for paper/live)
#   3. hermes installed (for --auto mode)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Load .env if it exists
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a; source "$PROJECT_DIR/.env"; set +a
fi

# Defaults
MODE="${SWARM_MODE:-paper}"
DURATION="${SWARM_DURATION:-3600}"
CAPITAL="${SWARM_CAPITAL:-500}"
MAX_SIZE="${SWARM_MAX_SIZE:-50}"
MAX_DD="${SWARM_MAX_DD:-100}"
PAIRS="${SWARM_PAIRS:-ETHUSD}"
WEB_PORT="${SWARM_WEB_PORT:-8080}"
GATEWAY_KEY="${SWARM_GATEWAY_KEY:-}"

# Parse flags
AUTO=""
REGISTER=""
for arg in "$@"; do
    case "$arg" in
        --auto)     AUTO="--hermes hermes" ;;
        --register) REGISTER="--register" ;;
        --live)     MODE="live" ;;
        --paper)    MODE="paper" ;;
        --mock)     MODE="mock" ;;
    esac
done

# Generate gateway key if not set
if [ -z "$GATEWAY_KEY" ]; then
    GATEWAY_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    export SWARM_GATEWAY_KEY="$GATEWAY_KEY"
fi

echo "============================================"
echo "  SwarmTrader + Hermes Agent"
echo "============================================"
echo "  Mode:     $MODE"
echo "  Capital:  \$$CAPITAL"
echo "  Max trade:\$$MAX_SIZE"
echo "  Max DD:   \$$MAX_DD"
echo "  Pairs:    $PAIRS"
echo "  Dashboard: http://localhost:$WEB_PORT"
echo "  Gateway:   ws://localhost:$((WEB_PORT + 1))"
echo "============================================"
echo ""

if [ "$MODE" = "live" ]; then
    echo "  *** LIVE TRADING — REAL MONEY ***"
    echo ""
    read -p "  Type 'yes' to confirm: " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Aborted."
        exit 0
    fi
    export SWARM_LIVE_CONFIRM="I_ACCEPT_RISK"
fi

# Step 1: Start SwarmTrader in background
echo "[1/2] Starting SwarmTrader ($MODE mode)..."
cd "$PROJECT_DIR"

python -m swarmtrader.main "$MODE" "$DURATION" \
    --hermes \
    --gateway \
    --gateway-key "$GATEWAY_KEY" \
    --web --web-port "$WEB_PORT" \
    --pairs $PAIRS \
    --capital "$CAPITAL" \
    --max-size "$MAX_SIZE" \
    --max-dd "$MAX_DD" \
    --dashboard &

SWARM_PID=$!
echo "  SwarmTrader PID: $SWARM_PID"

# Wait for gateway to be ready
echo "  Waiting for gateway..."
for i in $(seq 1 30); do
    if curl -s "http://localhost:$((WEB_PORT + 1))/api/gateway/agents" > /dev/null 2>&1; then
        echo "  Gateway ready!"
        break
    fi
    sleep 1
done

# Step 2: Connect Hermes Agent bridge
echo ""
echo "[2/2] Connecting Hermes Agent bridge..."
echo "  Gateway key: ${GATEWAY_KEY:0:8}..."
echo ""

python3 "$SCRIPT_DIR/scripts/swarm_bridge.py" \
    --gateway "ws://localhost:$((WEB_PORT + 1))" \
    --key "$GATEWAY_KEY" \
    --register \
    $AUTO

# Cleanup on exit
kill $SWARM_PID 2>/dev/null || true
echo "SwarmTrader stopped."
