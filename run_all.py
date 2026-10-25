"""Start every agent with one command: uv run run_all.py

Creates .env from .env.example if needed, fills in missing seeds and agent addresses,
then runs the price, policy, parser and coordinator agents together. Ctrl+C stops them all.
"""
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import dotenv_values, set_key
from uagents.crypto import Identity

ENV = Path(__file__).with_name(".env")
EXAMPLE = Path(__file__).with_name(".env.example")

# name, script, seed key, address key in .env (workers first: the coordinator reads their addresses at start)
AGENTS = [
    ("price", "price_agent.py", "PRICE_AGENT_SEED", "PRICE_AGENT_ADDRESS"),
    ("policy", "policy_agent.py", "POLICY_AGENT_SEED", "POLICY_AGENT_ADDRESS"),
    ("parser", "parser_agent.py", "PARSER_AGENT_SEED", "PARSER_AGENT_ADDRESS"),
    ("coordinator", "agent.py", "AGENT_SEED", "COORDINATOR_ADDRESS"),
]
KEYS = ["BRIGHTDATA_API_TOKEN", "ASI_ONE_API_KEY"]


def prepare_env() -> dict[str, str]:
    """Seeds and addresses in .env, so nobody has to copy them by hand. Never prints a seed."""
    if not ENV.exists():
        shutil.copy(EXAMPLE, ENV)
        print("Created .env from .env.example")
    env = dotenv_values(ENV)
    for name, _, seed_key, addr_key in AGENTS:
        seed = env.get(seed_key) or ""
        if not seed or seed.startswith("change-me"):
            seed = secrets.token_hex(32)
            set_key(ENV, seed_key, seed, quote_mode="never")
            print(f"Made a new {seed_key}")
        address = Identity.from_seed(seed, 0).address
        current = env.get(addr_key) or ""
        if not current:
            set_key(ENV, addr_key, address, quote_mode="never")
        elif current != address:
            print(f"Note: {addr_key} in .env doesn't match this {name} agent's seed, so messages go to that other "
                  f"address. To use the {name} agent started here, clear the value after {addr_key}= in .env, "
                  "save, and run this again.")
    return dotenv_values(ENV)


def relay(name: str, proc: subprocess.Popen):
    """Each agent's output, labelled with its name."""
    for line in proc.stdout:
        print(f"[{name:<11}] {line}", end="", flush=True)


def main():
    sys.stdout.reconfigure(errors="replace")  # an odd character in a log line never stops the relay
    env = prepare_env()
    missing = [k for k in KEYS if not (env.get(k) or os.getenv(k))]
    if missing:
        sys.exit(f"Paste your {' and '.join(missing)} into .env (open it in VS Code), save, then run this again.")

    print("\nAgent addresses (also saved in .env):")
    for name, _, seed_key, _ in AGENTS:
        print(f"  {name:<11} {Identity.from_seed(env[seed_key], 0).address}")
    print("\nFor each agent, open its 'Agent inspector available at' link below in Chrome or Brave,"
          "\nallow Local Network Access, then Connect -> Mailbox -> Finish. Ctrl+C stops everything.\n")

    procs = []
    try:
        for name, script, _, _ in AGENTS:
            if name == "coordinator":
                time.sleep(3)  # let the workers come up first
            proc = subprocess.Popen([sys.executable, "-u", script], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, cwd=ENV.parent,
                                    # UTF-8 both ways: Windows pipes default to the local code page
                                    encoding="utf-8", errors="replace",
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            procs.append((name, proc))
            threading.Thread(target=relay, args=(name, proc), daemon=True).start()
        while True:
            for name, proc in procs:
                if proc.poll() is not None:
                    print(f"[{name:<11}] stopped (exit code {proc.returncode}); see the lines above")
                    procs.remove((name, proc))
                    break
            if not procs:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping all agents…")
    finally:
        for _, proc in procs:
            proc.terminate()
        for _, proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    main()
