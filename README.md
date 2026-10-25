# RefundHunter

## Run it locally

**Quickest way: one command for all agents.** Install uv (step 1 below), open this folder in VS Code, then Terminal → New Terminal and run:

```bash
uv run run_all.py
```

The first run creates `.env`, makes your own seeds, and saves every agent address in it. If `.env` already has a teammate's `PRICE_AGENT_ADDRESS` or `PARSER_AGENT_ADDRESS`, that agent isn't started here and the coordinator uses theirs. Paste your `BRIGHTDATA_API_TOKEN` and `ASI_ONE_API_KEY` into `.env` (click it in VS Code's file list), save, and run the command again. It starts the price, policy, parser and coordinator agents and prints their addresses. Open each "Agent inspector available at" link in Chrome or Brave, then Connect → Mailbox → Finish. Ctrl+C stops all of them. Then @mention the coordinator address in ASI:One.

**Step by step instead:**

1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`).
2. `uv sync` — installs Python 3.13 if needed and the exact locked dependencies.
3. `cp .env.example .env` and set `AGENT_SEED` and `PRICE_AGENT_SEED` to your **own** random values. Make one with `uv run python -c "import secrets; print(secrets.token_hex(32))"`. Don't reuse a teammate's seed: the same seed gives the same agent address, and two running copies will conflict. The agents won't start with the `change-me…` placeholders.
4. `uv run agent.py`, then follow "Run and connect" in `AGENTS.md`.
5. In a second terminal: `uv run price_agent.py`, connect its mailbox the same way, then put its address in `.env` as `PRICE_AGENT_ADDRESS` and restart `agent.py`. It also needs `BRIGHTDATA_API_TOKEN`. Without the price agent, the coordinator shows test prices.
6. To test Bright Data alone (1 credit): `uv run price_agent.py --check <url> amazon` (or `walmart`). To test the Amazon search (3 credits): `uv run price_agent.py --search "<item name>" [amazon|walmart]`.
7. Receipts: `uv run parser_agent.py` in a third terminal (needs `PARSER_AGENT_SEED` and `ASI_ONE_API_KEY`), connect its mailbox, put its address in `.env` as `PARSER_AGENT_ADDRESS` and restart `agent.py`. To test the parser alone: `uv run parser_agent.py --check <photo.jpg | image link | email.txt>`.
8. Email receipts (**Connect Gmail**): do the Google Cloud setup in "Email receipts" in `AGENTS.md`, set `PUBLIC_URL`, `OAUTH_STATE_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and run `uv run uvicorn server:app --port 8000` (or deploy it to Vercel). Pipedream forwarding is optional and not needed.
9. Refund requests: `uv run policy_agent.py` (needs only `POLICY_AGENT_SEED`; a rule table, no keys or credits), connect its mailbox and put its address in `.env` as `POLICY_AGENT_ADDRESS`. It answers `PolicyCheck` with the request to make and its deadline. To test it alone: `uv run policy_agent.py --check <amazon|walmart|target|bestbuy> <price paid> <price now> [YYYY-MM-DD purchase date]` prints the answer; `--send` with the same arguments sends it as a message to the policy agent running on this machine and prints its reply.
