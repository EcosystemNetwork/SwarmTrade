#!/usr/bin/env python3
"""SwarmTrader <-> Hermes Agent bridge.

Connects to SwarmTrader's Agent Gateway via WebSocket and pipes market briefs
to a running Hermes Agent instance via its RPC/stdin interface.  Hermes's
responses are sent back as trading decisions.

Usage:
    # Standalone — Hermes Agent calls this script as a tool
    python3 swarm_bridge.py --gateway ws://localhost:8081 --key MASTER_KEY

    # One-shot: register + connect + listen forever
    python3 swarm_bridge.py --gateway ws://localhost:8081 --key MASTER_KEY --register

    # Status check
    python3 swarm_bridge.py --gateway http://localhost:8081 --key MASTER_KEY --status

Environment variables (override CLI flags):
    SWARM_GATEWAY_URL   — Gateway WebSocket URL  (default: ws://localhost:8081)
    SWARM_GATEWAY_KEY   — Master key or agent API key
    SWARM_AGENT_KEY     — Agent-specific API key (from /api/gateway/connect)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no pip deps)
# ---------------------------------------------------------------------------

def _post(url: str, body: dict, headers: dict | None = None) -> dict:
    data = json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json", "User-Agent": "swarm-bridge/1.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        print(f"HTTP {e.code}: {body_text[:300]}", file=sys.stderr)
        sys.exit(1)


def _get(url: str, headers: dict | None = None) -> dict:
    hdrs = {"User-Agent": "swarm-bridge/1.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode() if e.fp else ""
        print(f"HTTP {e.code}: {body_text[:300]}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_agent(http_url: str, master_key: str) -> dict:
    """Register Hermes Agent with the gateway.  Returns agent credentials."""
    url = f"{http_url.rstrip('/')}/api/gateway/connect"
    result = _post(url, {
        "name": "hermes-agent",
        "protocol": "hermes",
        "agent_type": "brain",
        "capabilities": ["real-time-streaming", "multi-asset", "self-improving"],
        "description": "Nous Research Hermes Agent — autonomous trading brain",
        "master_key": master_key,
    })
    print(f"Registered as: {result.get('agent_id')}")
    print(f"Agent API key: {result.get('api_key')}")
    return result


def check_status(http_url: str, api_key: str):
    """Print gateway status and connected agents."""
    url = f"{http_url.rstrip('/')}/api/gateway/agents"
    result = _get(url, {"Authorization": f"Bearer {api_key}"})
    agents = result if isinstance(result, list) else result.get("agents", [])
    print(f"Connected agents: {len(agents)}")
    for a in agents:
        print(f"  {a.get('agent_id', '?'):30s} {a.get('status', '?'):10s} "
              f"signals={a.get('signal_count', 0)}")


# ---------------------------------------------------------------------------
# WebSocket bridge (uses aiohttp if available, falls back to websockets)
# ---------------------------------------------------------------------------

async def run_bridge(ws_url: str, api_key: str, hermes_cmd: str | None = None):
    """Main bridge loop: connect to gateway WS, pipe briefs to stdout,
    read decisions from stdin (or from a Hermes subprocess)."""

    # Try aiohttp first, fall back to websockets
    try:
        import aiohttp
        return await _run_aiohttp(ws_url, api_key, hermes_cmd)
    except ImportError:
        pass

    try:
        import websockets
        return await _run_websockets(ws_url, api_key, hermes_cmd)
    except ImportError:
        pass

    print("ERROR: Need either 'aiohttp' or 'websockets' package.", file=sys.stderr)
    print("  pip install aiohttp   # or: pip install websockets", file=sys.stderr)
    sys.exit(1)


async def _run_aiohttp(ws_url: str, api_key: str, hermes_cmd: str | None):
    import aiohttp

    connect_url = f"{ws_url.rstrip('/')}/ws/agent?api_key={api_key}"
    retry_delay = 2.0

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    connect_url, heartbeat=30.0, timeout=aiohttp.ClientTimeout(total=15)
                ) as ws:
                    # Read welcome
                    welcome = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
                    if welcome.get("type") == "error":
                        print(f"Gateway rejected: {welcome.get('message')}", file=sys.stderr)
                        return
                    agent_id = welcome.get("agent_id", "hermes")
                    prices = welcome.get("prices", {})
                    print(f"Connected as '{agent_id}' | prices: {prices}")
                    retry_delay = 2.0  # reset on success

                    # Message loop
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            msg_type = data.get("type", "")

                            if msg_type == "brain_brief":
                                await _handle_brief(ws, data, hermes_cmd,
                                                    send_fn=ws.send_json)
                            elif msg_type == "market":
                                _handle_market(data)
                            elif msg_type == "execution":
                                _handle_execution(data)
                            elif msg_type == "heartbeat_ack":
                                pass
                            else:
                                print(f"[{msg_type}] {msg.data[:200]}")

                        elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.ERROR):
                            print("WS closed, reconnecting...", file=sys.stderr)
                            break

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            print(f"Connection error: {e} — retrying in {retry_delay:.0f}s",
                  file=sys.stderr)

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 1.5, 30.0)


async def _run_websockets(ws_url: str, api_key: str, hermes_cmd: str | None):
    import websockets

    connect_url = f"{ws_url.rstrip('/')}/ws/agent?api_key={api_key}"
    retry_delay = 2.0

    while True:
        try:
            async with websockets.connect(
                connect_url, ping_interval=30, open_timeout=10
            ) as ws:
                welcome_raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                welcome = json.loads(welcome_raw)
                if welcome.get("type") == "error":
                    print(f"Gateway rejected: {welcome.get('message')}", file=sys.stderr)
                    return
                agent_id = welcome.get("agent_id", "hermes")
                print(f"Connected as '{agent_id}'")
                retry_delay = 2.0

                async for raw in ws:
                    data = json.loads(raw)
                    msg_type = data.get("type", "")

                    if msg_type == "brain_brief":
                        async def _send(obj):
                            await ws.send(json.dumps(obj))
                        await _handle_brief(ws, data, hermes_cmd, send_fn=_send)
                    elif msg_type == "market":
                        _handle_market(data)
                    elif msg_type == "execution":
                        _handle_execution(data)

        except (Exception,) as e:
            print(f"Connection error: {e} — retrying in {retry_delay:.0f}s",
                  file=sys.stderr)

        await asyncio.sleep(retry_delay)
        retry_delay = min(retry_delay * 1.5, 30.0)


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

_last_prices: dict[str, float] = {}
_brief_count = 0
_decision_count = 0


def _handle_market(data: dict):
    """Cache latest prices (don't spam stdout)."""
    global _last_prices
    _last_prices.update(data.get("prices", {}))


def _handle_execution(data: dict):
    """Print execution results so Hermes can learn from them."""
    status = data.get("status", "?")
    asset = data.get("asset", "?")
    side = data.get("side", "?")
    price = data.get("fill_price", 0)
    pnl = data.get("pnl", 0)
    print(f"\n  EXEC: {status} {side} {asset} @ ${price:.2f} | PnL: ${pnl:+.2f}")


async def _handle_brief(ws, data: dict, hermes_cmd: str | None, send_fn):
    """Receive a brain brief, get a decision, send it back."""
    global _brief_count, _decision_count
    _brief_count += 1

    brief = data.get("brief", "")
    system = data.get("system", "")

    # Print brief summary
    lines = brief.strip().split("\n")
    price_line = lines[0] if lines else ""
    consensus_lines = [l for l in lines if "CONSENSUS" in l]
    print(f"\n--- Brief #{_brief_count} | {price_line}")
    for cl in consensus_lines[:3]:
        print(f"  {cl.strip()}")

    # Get decision from Hermes (or stdin for interactive mode)
    decision_text = await _get_decision(brief, system, hermes_cmd)

    if decision_text:
        _decision_count += 1
        # Parse and send back
        try:
            parsed = json.loads(decision_text)
            # Wrap in decision envelope if it's a raw array
            if isinstance(parsed, list):
                await send_fn({"type": "decision", "data": parsed})
            elif isinstance(parsed, dict):
                await send_fn({"type": "decision", "data": parsed})
            else:
                await send_fn({"type": "decision", "data": decision_text})
            print(f"  -> Decision #{_decision_count} sent")
        except json.JSONDecodeError:
            # Send as raw text — gateway will try to parse it
            await send_fn({"type": "decision", "data": decision_text})
            print(f"  -> Raw decision sent")


async def _get_decision(brief: str, system: str, hermes_cmd: str | None) -> str:
    """Get a trading decision.

    In bridge mode (hermes_cmd set): pipes brief to Hermes subprocess.
    In interactive mode: prints to stdout, reads from stdin.
    In pipe mode (stdin is not a tty): reads from stdin pipe.
    """
    if hermes_cmd:
        return await _query_hermes_subprocess(brief, system, hermes_cmd)

    if not sys.stdin.isatty():
        # Piped mode — read one line from stdin
        loop = asyncio.get_event_loop()
        line = await loop.run_in_executor(None, sys.stdin.readline)
        return line.strip()

    # Interactive: print brief and prompt user
    print(f"\n  Waiting for decision (JSON or 'hold')...")
    loop = asyncio.get_event_loop()
    line = await loop.run_in_executor(None, sys.stdin.readline)
    text = line.strip()
    if text.lower() in ("hold", "wait", "skip", "pass", ""):
        return json.dumps([{"action": "hold", "reasoning": "manual hold"}])
    return text


async def _query_hermes_subprocess(brief: str, system: str, cmd: str) -> str:
    """Call Hermes Agent CLI to get a decision.

    Uses `hermes --no-tui -1` for single-turn mode if available,
    otherwise falls back to piping via stdin.
    """
    # Build the prompt that Hermes will see
    prompt = (
        f"You are in SwarmTrader trading mode. Analyze this market brief and "
        f"respond with ONLY a JSON array of trading decisions.\n\n"
        f"{brief}\n\n"
        f"Respond with JSON only: "
        f'[{{"action":"buy"|"sell"|"hold", "asset":"ETH", "size_usd":50, '
        f'"confidence":0.8, "reasoning":"..."}}]'
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd.split(),
            "--no-tui", "-1",  # single-turn, no terminal UI
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=120.0
        )
        result = stdout.decode().strip()
        if stderr:
            err = stderr.decode().strip()
            if err:
                print(f"  [hermes stderr] {err[:200]}", file=sys.stderr)

        # Extract JSON from Hermes response (may have prose around it)
        return _extract_json(result)

    except asyncio.TimeoutError:
        print("  Hermes subprocess timed out (120s)", file=sys.stderr)
        return json.dumps([{"action": "hold", "reasoning": "timeout"}])
    except FileNotFoundError:
        print(f"  Command not found: {cmd}", file=sys.stderr)
        return json.dumps([{"action": "hold", "reasoning": "hermes not found"}])


def _extract_json(text: str) -> str:
    """Extract JSON array from potentially mixed prose+JSON response."""
    # Try the whole thing first
    text = text.strip()
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # Find JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # Find JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # Give up — return as hold
    return json.dumps([{"action": "hold", "reasoning": f"unparseable: {text[:100]}"}])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="SwarmTrader <-> Hermes Agent bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Register and connect (first time):
  python3 swarm_bridge.py --gateway ws://localhost:8081 --key MASTER_KEY --register

  # Connect with existing agent key:
  python3 swarm_bridge.py --gateway ws://localhost:8081 --agent-key AGENT_KEY

  # Auto-pipe decisions from Hermes CLI:
  python3 swarm_bridge.py --gateway ws://localhost:8081 --agent-key KEY --hermes "hermes"

  # Check status:
  python3 swarm_bridge.py --gateway http://localhost:8081 --key MASTER_KEY --status
        """,
    )
    parser.add_argument("--gateway", default=os.getenv("SWARM_GATEWAY_URL", "ws://localhost:8081"),
                        help="Gateway URL (default: ws://localhost:8081)")
    parser.add_argument("--key", default=os.getenv("SWARM_GATEWAY_KEY", ""),
                        help="Gateway master key")
    parser.add_argument("--agent-key", default=os.getenv("SWARM_AGENT_KEY", ""),
                        help="Agent API key (from prior registration)")
    parser.add_argument("--register", action="store_true",
                        help="Register agent first, then connect")
    parser.add_argument("--status", action="store_true",
                        help="Show gateway status and exit")
    parser.add_argument("--hermes", default=None, metavar="CMD",
                        help="Hermes CLI command (e.g. 'hermes') for auto-decisions")
    args = parser.parse_args()

    # Normalize URL
    gw = args.gateway.rstrip("/")
    http_url = gw.replace("ws://", "http://").replace("wss://", "https://")
    ws_url = gw.replace("http://", "ws://").replace("https://", "wss://")

    if args.status:
        key = args.agent_key or args.key
        if not key:
            print("Need --key or --agent-key for status check", file=sys.stderr)
            sys.exit(1)
        check_status(http_url, key)
        return

    # Get agent key
    agent_key = args.agent_key
    if not agent_key:
        if args.register or not args.key:
            if not args.key:
                print("Need --key (master key) to register, or --agent-key to connect",
                      file=sys.stderr)
                sys.exit(1)
            result = register_agent(http_url, args.key)
            agent_key = result.get("api_key", "")
            if not agent_key:
                print("Registration failed — no api_key returned", file=sys.stderr)
                sys.exit(1)
            print(f"\nSave this for next time:  --agent-key {agent_key}\n")
        else:
            # Try master key as agent key (works if already registered)
            agent_key = args.key

    print(f"Connecting to {ws_url} ...")
    print(f"Mode: {'auto (hermes CLI)' if args.hermes else 'interactive (stdin)'}")
    print("Press Ctrl+C to disconnect.\n")

    # Handle clean shutdown
    loop = asyncio.new_event_loop()
    for sig_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig_name, lambda: loop.stop())

    try:
        loop.run_until_complete(run_bridge(ws_url, agent_key, args.hermes))
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\nDisconnected. Briefs received: {_brief_count}, Decisions sent: {_decision_count}")
        loop.close()


if __name__ == "__main__":
    main()