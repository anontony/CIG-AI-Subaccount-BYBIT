---
name: bybit-trading
version: 1.4.3
author: Bybit
updated: 2026-06-17
---
# Bybit Trading Skill - Seed Cache

CIG AI Subaccount uses this local seed immediately, then auto-updates from Bybit manifest when network is available.

## Core safety rules
- Use a dedicated Bybit sub-account for AI trading with limited balance.
- API key should only have Read + Trade. Never enable Withdraw.
- Verify market time and wallet balance before trading.
- Never display full API keys or secrets.
- Use category=spot for spot orders and category=linear for USDT perpetual/futures.
- Spot has no short/leverage by default.
- Futures long/short must pass leverage, margin, notional, cooldown and TP/SL checks.
- CIG AI Subaccount Risk Guard is always the final authority before execution.

## Auto Update
At startup or when user clicks update, fetch https://api.bybit.com/skill/manifest, verify sha256 for SKILL.md and modules/*.md, then replace the local cache atomically.
