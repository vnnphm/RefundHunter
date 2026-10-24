# RefundHunter

## Run it locally

1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv`).
2. `uv sync` — installs Python 3.13 if needed and the exact locked dependencies.
3. `cp .env.example .env` and set `AGENT_SEED` to your **own** long random phrase. Don't reuse a teammate's seed: the same seed gives the same agent address, and two running copies will conflict.
4. `uv run agent.py`, then follow "Run and connect" in `AGENTS.md`.
   That's it.
