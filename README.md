# RefundHunter

## Run it locally

1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`).
2. `uv sync` — installs Python 3.13 if needed and the exact locked dependencies.
3. `cp .env.example .env` and set `AGENT_SEED` and `PRICE_AGENT_SEED` to your **own** random values. Make one with `uv run python -c "import secrets; print(secrets.token_hex(32))"`. Don't reuse a teammate's seed: the same seed gives the same agent address, and two running copies will conflict. The agents won't start with the `change-me…` placeholders.
4. `uv run agent.py`, then follow "Run and connect" in `AGENTS.md`.
5. In a second terminal: `uv run price_agent.py`, connect its mailbox the same way, then put its address in `.env` as `PRICE_AGENT_ADDRESS` and restart `agent.py`. It also needs `BRIGHTDATA_API_TOKEN`. Without the price agent, the coordinator shows test prices.
6. To test Bright Data alone (1 credit): `uv run price_agent.py --check <url> amazon` (or `walmart`). To test the Amazon search (3 credits): `uv run price_agent.py --search "<item name>" [amazon|walmart]`.
7. Receipts: `uv run parser_agent.py` in a third terminal (needs `PARSER_AGENT_SEED` and `ASI_ONE_API_KEY`), connect its mailbox, put its address in `.env` as `PARSER_AGENT_ADDRESS` and restart `agent.py`. To test the parser alone: `uv run parser_agent.py --check <photo.jpg | image link | email.txt>`.
8. Email receipts (**Connect Gmail**): do the Google Cloud setup in "Email receipts" in `AGENTS.md`, set `PUBLIC_URL`, `OAUTH_STATE_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and run `uv run uvicorn server:app --port 8000` (or deploy it to Vercel). Pipedream forwarding is optional and not needed.
9. Refund requests: `uv run claim_agent.py` in another terminal (needs `CLAIM_AGENT_SEED`, `ASI_ONE_API_KEY`, and `COORDINATOR_ADDRESS` so only your coordinator can use it), connect its mailbox, put its address in `.env` as `CLAIM_AGENT_ADDRESS` and restart `agent.py`. To test it alone: `uv run claim_agent.py --check amazon "Ticonderoga pencils" 4.50 2.97`.
