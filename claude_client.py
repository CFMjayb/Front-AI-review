"""Anthropic SDK wrapper with prompt caching + cost estimation."""
import json
import logging
import re
from typing import Any, Optional

from anthropic import Anthropic

logger = logging.getLogger(__name__)

# Per-million token rates for Claude 4.x. Update if pricing changes.
RATES = {
    "claude-opus-4-7":   {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.5},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.3},
    "claude-haiku-4-5":  {"input": 1.0,  "output": 5.0,  "cache_write": 1.25,  "cache_read": 0.1},
}


def _rate(model: str) -> dict:
    return RATES.get(model, RATES["claude-haiku-4-5"])


def estimate_cost(model: str, usage: Any) -> float:
    if usage is None:
        return 0.0
    r = _rate(model)
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (inp * r["input"] + out * r["output"]
            + cw * r["cache_write"] + cr * r["cache_read"]) / 1_000_000


def parse_json_response(text: str) -> tuple[bool, Any, Optional[str]]:
    """(ok, value_or_None, error_message_or_None). Strips ```json fences, finds first balanced {...}."""
    if not text:
        return False, None, "empty response"
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"```\s*$", "", s)
    start_obj = s.find("{")
    start_arr = s.find("[")
    if start_obj == -1:
        start = start_arr
    elif start_arr == -1:
        start = start_obj
    else:
        start = min(start_obj, start_arr)
    if start == -1:
        return False, None, "no JSON found"
    open_ch = s[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return True, json.loads(s[start:i + 1]), None
                except json.JSONDecodeError as e:
                    return False, None, f"JSON parse error: {e}"
    return False, None, "unbalanced JSON"


class ClaudeClient:
    def __init__(self, api_key: str, default_model: str, fast_model: str):
        self.client = Anthropic(api_key=api_key)
        self.default_model = default_model
        self.fast_model = fast_model

    def call(self, *, system: str, user: str, model: Optional[str] = None,
             max_tokens: int = 1024, json_mode: bool = False,
             cached_system: bool = False) -> dict:
        m = model or self.default_model
        if cached_system:
            system_blocks = [{"type": "text", "text": system,
                              "cache_control": {"type": "ephemeral"}}]
        else:
            system_blocks = [{"type": "text", "text": system}]

        resp = self.client.messages.create(
            model=m,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        cost = estimate_cost(m, resp.usage)

        result = {"text": text, "json": None, "parse_error": None,
                  "cost_usd": cost, "model": m, "usage": resp.usage}
        if json_mode:
            ok, value, err = parse_json_response(text)
            if ok:
                result["json"] = value
            else:
                result["parse_error"] = err
        return result
