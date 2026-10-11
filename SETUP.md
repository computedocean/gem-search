# Local setup

For the browser spider, follow the quick start in [README.md](README.md). No wallet, Pinata account or npm build is required for browser research.

The dashboard requires sign-in. `python3 app.py` prints a one-click sign-in link on an interactive terminal, or you can run `python3 app.py --login-url` at any time. To run it in Docker, see [docs/DOCKER.md](docs/DOCKER.md).

## Optional Grok

Copy `.env.example` to `.env`, then set GROK_ENABLED=1 and XAI_API_KEY locally. Restart Python. The default request limit is 12 per rolling day, shared by the four reviewers. Do not paste API keys into issues or chat messages.

## Optional token launching

This is independent of the browser extension and experimental. Keep LAUNCH_MODE=dry_run until you have reviewed [the launch module](docs/AUTOLAUNCH.md).

```sh
npm ci --ignore-scripts
npm run wallet:create
```

This creates a new dedicated keypair for **your own installation** in data/wallets/treasury.json and prints only its public address. Back up that file. Never send funds to an address copied from somebody else's documentation.

For a live attempt you need your own funded treasury, PINATA_JWT, and LAUNCH_MODE=live in `.env`. You fund the wallet yourself. At least 0.025 SOL is required for the first allocation. No key or default funding address is distributed in this repository.

Browser captures are research-only unless you additionally set SPIDER_ALLOW_LAUNCH=1. Enabling it with live mode means qualifying captures can cause automatic financial actions without per-token confirmation. Request limits for Grok and transaction limits for Solana are separate.

Pause queue stops new submissions but cannot cancel transactions already broadcast. Preserve SQLite, data/launch-jobs/ and all creator keys together for recovery. Pending outcomes block further allocations until reconciled. Do not remove history to force retries.

The unsigned PumpPortal zero-buy construction was probed successfully; live token creation, Pinata publication and funded execution have not been end-to-end validated by this repository's tests.


## Other launch platforms

The built-in instructions above apply to `LAUNCH_PROVIDER=pumpportal`. To connect a different Solana launch platform through your own HTTPS adapter, see [Launch providers](docs/LAUNCH_PROVIDERS.md). Bridge mode does not require local Pinata or wallet credentials; signing and spending enforcement belong to the bridge.
