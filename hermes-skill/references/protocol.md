# SwarmTrader Gateway Protocol

## Connection

### 1. Register agent

```bash
curl -X POST http://localhost:8081/api/gateway/connect \
  -H "Content-Type: application/json" \
  -d '{
    "name": "hermes-agent",
    "protocol": "hermes",
    "agent_type": "brain",
    "capabilities": ["real-time-streaming", "multi-asset"],
    "description": "Nous Research Hermes Agent — trading brain",
    "master_key": "YOUR_GATEWAY_MASTER_KEY"
  }'
```

Response:
```json
{
  "agent_id": "ext_hermes_agent",
  "api_key": "64_char_hex_agent_key",
  "protocol": "hermes",
  "endpoints": {
    "websocket": "WS /ws/agent"
  }
}
```

### 2. Open WebSocket

```
ws://localhost:8081/ws/agent?api_key=YOUR_AGENT_API_KEY
```

Welcome message:
```json
{
  "type": "welcome",
  "agent_id": "ext_hermes_agent",
  "prices": {"ETH": 3200.50},
  "portfolio": {"total_value": 500.0, "positions": {}}
}
```

## Messages: Gateway -> Agent

### brain_brief (every 15s)
```json
{
  "type": "brain_brief",
  "system": "You are an elite AI trading strategist...",
  "brief": "PRICES: ETH: $3200.50\n--- ETH SIGNALS ---\n...",
  "max_tokens": 1024,
  "ts": 1712937600.123
}
```

### market (every 0.2-5s)
```json
{
  "type": "market",
  "prices": {"ETH": 3200.50, "BTC": 68400.00},
  "gas_gwei": 12.5,
  "ts": 1712937600.123
}
```

### execution (after trade)
```json
{
  "type": "execution",
  "intent_id": "intent_xyz",
  "status": "filled",
  "fill_price": 3201.10,
  "pnl": 12.34,
  "asset": "ETH",
  "side": "buy",
  "ts": 1712937605.456
}
```

## Messages: Agent -> Gateway

### decision (response to brain_brief)
```json
{
  "type": "decision",
  "data": [
    {
      "action": "buy",
      "asset": "ETH",
      "size_usd": 50.0,
      "confidence": 0.8,
      "reasoning": "Strong consensus across agents",
      "order_type": "market",
      "urgency": "medium"
    }
  ]
}
```

### signal (proactive, anytime)
```json
{
  "type": "signal",
  "data": {
    "structured_output": {
      "signal": "bullish",
      "asset": "ETH",
      "confidence": 0.75,
      "target_price": 3500,
      "stop_loss": 3000
    }
  }
}
```

### heartbeat
```json
{"type": "heartbeat"}
```

## Timeouts

| Event | Timeout |
|-------|---------|
| WS connection | 10s |
| WS heartbeat | 30s |
| Decision response | 90s |
| Rate limit | 30 signals/min |
