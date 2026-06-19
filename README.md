# CIG AI Subaccount

AI workspace for Bybit sub-account trading automation.

## Main features

- Per-user workspace with separate settings, prompt, API keys, RSA key, logs and trade tracking.
- Bybit HMAC or RSA signing.
- Spot + Futures command routing.
- Prompt strategy loop with schedule parsing such as `10 usdt/1h`, `mỗi 1 giờ`, `mỗi ngày`.
- Direct command execution and bot-control commands.
- Risk guard for symbol allowlist, leverage, order size, TP/SL and cooldown.
- Account balance panel refreshed silently every 30 seconds.
- Trade tracking table with PNL %, estimated USDT PNL, TP/SL and status.
- Bybit Skill local cache with manual/automatic refresh.
- Railway-ready deployment.

## Run locally

```bash
cp .env.example .env
pip install -r requirements.txt
python server.py
```

Open:

```text
http://localhost:8000
```

## Railway variables

```env
RUNTIME_DIR=/data
APP_SECRET=replace_with_a_long_fixed_secret
```

Use a Railway Volume mounted at `/data` to persist users, settings, prompts, logs and encrypted API keys. Keep `APP_SECRET` unchanged after users are created.

## Safety

Use a dedicated Bybit sub-account for the bot. The API key should only have Read + Trade permission. Do not grant Withdraw permission.
