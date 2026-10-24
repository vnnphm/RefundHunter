# RefundHunter

## Run it locally

1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`).
2. `uv sync` — installs Python 3.13 if needed and the exact locked dependencies.
3. `cp .env.example .env` and set `AGENT_SEED` and `PRICE_AGENT_SEED` to your **own** random values. Make one with `uv run python -c "import secrets; print(secrets.token_hex(32))"`. Don't reuse a teammate's seed: the same seed gives the same agent address, and two running copies will conflict. The agents won't start with the `change-me…` placeholders.
4. `uv run agent.py`, then follow "Run and connect" in `AGENTS.md`.
5. In a second terminal: `uv run price_agent.py`, connect its mailbox the same way, then put its address in `.env` as `PRICE_AGENT_ADDRESS` and restart `agent.py`. It also needs `BRIGHTDATA_API_TOKEN`. Without the price agent, the coordinator shows test prices.
6. To test Bright Data alone (1 credit): `uv run price_agent.py --check <url> amazon` (or `walmart`).
