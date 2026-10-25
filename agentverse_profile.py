"""Agentverse profile settings shared by every agent.

uagents re-publishes the profile (description, readme, avatar) to Agentverse each time an agent
connects, so it lives in code: anything left out is sent empty and wipes what was set in the dashboard.
"""
from pathlib import Path

AVATAR_URL = "https://storage.googleapis.com/agentverse-prod-assets/agent-avatars-pub/257797515.jpg"
PROFILES = Path(__file__).with_name("profiles")


def profile(name: str, description: str) -> dict:
    """Agent(...) keyword arguments: avatar, description (Agentverse caps it at 300 chars), profiles/<name>.md."""
    assert len(description) <= 300, f"{name} description is {len(description)} chars"
    return {"avatar_url": AVATAR_URL, "description": description, "readme_path": str(PROFILES / f"{name}.md")}
