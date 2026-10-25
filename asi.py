"""ASI:One (asi1) JSON calls shared by the parser and policy agents."""
import json
import os

from openai import APIError, AsyncOpenAI, BadRequestError

ASI_API = "https://api.asi1.ai/v1"
MODEL = "asi1"


class LLMError(Exception):
    """A short, user-safe reason the asi1 call failed."""


async def ask_json(content: list[dict] | str, schema: dict) -> dict:
    """One asi1 call (temperature 0, JSON schema output); returns the model's JSON object."""
    key = os.getenv("ASI_ONE_API_KEY", "")
    if not key:
        raise LLMError("ASI_ONE_API_KEY is not set")
    llm = AsyncOpenAI(api_key=key, base_url=ASI_API)
    try:
        r = await llm.chat.completions.create(
            model=MODEL, temperature=0,
            response_format={"type": "json_schema", "json_schema": schema},
            messages=[{"role": "user", "content": content}])
    except BadRequestError as e:
        if "unsupported_content_part" in str(e.body or e):
            raise LLMError("couldn't read that photo") from e
        raise LLMError("ASI:One rejected the request") from e
    except APIError as e:
        raise LLMError(f"ASI:One error: {type(e).__name__}") from e
    finally:
        await llm.close()
    text = (r.choices[0].message.content or "").strip()
    text = text.removeprefix("`" * 3 + "json").strip("`\n ")  # drop a markdown fence if present
    try:
        data = json.loads(text)
    except ValueError as e:
        raise LLMError("ASI:One didn't return JSON") from e
    if not isinstance(data, dict):
        raise LLMError("ASI:One didn't return JSON")
    return data
