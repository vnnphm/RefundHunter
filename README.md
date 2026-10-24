# RefundHunter

## Run it locally

1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`).
2. `uv sync` — installs Python 3.13 if needed and the exact locked dependencies.
3. `cp .env.example .env` and set `AGENT_SEED` and `PRICE_AGENT_SEED` to your **own** random values. Make one with `uv run python -c "import secrets; print(secrets.token_hex(32))"`. Don't reuse a teammate's seed: the same seed gives the same agent address, and two running copies will conflict. The agents won't start with the `change-me…` placeholders.
4. `uv run agent.py`, then follow "Run and connect" in `AGENTS.md`.
5. In a second terminal: `uv run price_agent.py`, connect its mailbox the same way, then put its address in `.env` as `PRICE_AGENT_ADDRESS` and restart `agent.py`. It also needs `BRIGHTDATA_API_TOKEN`. Without the price agent, the coordinator shows test prices.
6. To test Bright Data alone (1 credit): `uv run price_agent.py --check <url> amazon` (or `walmart`).
7. Receipts: `uv run parser_agent.py` in a third terminal (needs `PARSER_AGENT_SEED` and `ASI_ONE_API_KEY`), connect its mailbox, put its address in `.env` as `PARSER_AGENT_ADDRESS` and restart `agent.py`. To test the parser alone: `uv run parser_agent.py --check <photo.jpg | image link | email.txt>`.
8. Policy checks (Phase 3): run `uv run policy_agent.py` in another terminal (needs `POLICY_AGENT_SEED`), connect mailbox, put its address in `.env` as `POLICY_AGENT_ADDRESS`, then restart `agent.py`. Local rule check: `uv run policy_agent.py --check`.
9. Email receipts: run `uv run uvicorn server:app --port 8000` and follow "Email receipts" in `AGENTS.md` (Pipedream inbox, linking, keys).

## Policy check disclaimer (Phase 3 demo)

- `policy_agent.py` uses a conservative **demo heuristic** for Amazon/Walmart only.
- Its result is guidance for hackathon demos, **not** a legal or contractual merchant policy decision.
- Always verify final eligibility on the merchant's official policy pages shown in the policy result.
