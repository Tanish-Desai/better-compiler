"""Conservative Gemini generation reservations; these are not billing receipts.

Count the prompt before generation, bound the study context, and reserve input
plus maximum output cost for every attempt. Do not refund on an ambiguous
timeout. Provider prices/taxes/credit eligibility must still be checked.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from urllib.request import Request, urlopen

from .llm_api import backend, endpoint_url, request_options


class APIBudgetError(RuntimeError):
    """Stop before another paid generation; preserve the budget ledger."""


def budget_limits() -> dict:
    raw = os.environ.get("LAB_LLM_API_LIMITS", "{}")
    try:
        limits = json.loads(raw)
    except ValueError as error:
        raise ValueError("LAB_LLM_API_LIMITS must be a JSON object") from error
    if limits == {}:
        return limits
    keys = {"max_prompt_tokens", "max_output_tokens", "max_usd",
            "input_usd_per_million", "output_usd_per_million"}
    if not isinstance(limits, dict) or set(limits) != keys:
        raise ValueError("LAB_LLM_API_LIMITS needs the five documented context/price/budget fields")
    for key in ("max_prompt_tokens", "max_output_tokens"):
        if type(limits[key]) is not int or not 1 <= limits[key] <= 24576:
            raise ValueError(f"Invalid {key} in LAB_LLM_API_LIMITS")
    for key in keys - {"max_prompt_tokens", "max_output_tokens"}:
        if (type(limits[key]) not in (int, float) or not math.isfinite(limits[key]) or limits[key] <= 0):
            raise ValueError(f"Invalid {key} in LAB_LLM_API_LIMITS")
    return limits


def count_gemini_prompt(messages: list[dict], model: str) -> int:
    systems, contents = [], []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if not isinstance(content, str) or role not in ("system", "user", "assistant"):
            raise APIBudgetError("Budget counter supports text-only system/user/assistant messages")
        if role == "system":
            systems.append({"text": content})
        else:
            contents.append({"role": "model" if role == "assistant" else "user", "parts": [{"text": content}]})
    native = {"model": "models/" + model, "contents": contents}
    if systems:
        native["systemInstruction"] = {"parts": systems}
    request = Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:countTokens",
        data=json.dumps({"generateContentRequest": native}).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["LAB_LLM_TOKEN"]},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        value = json.load(response).get("totalTokens")
    if type(value) is not int or value < 0:
        raise APIBudgetError("Gemini countTokens did not report a valid token count")
    return value


def reserve_generation(messages: list[dict], model: str, max_tokens: int) -> dict:
    limits = budget_limits()
    if not limits:
        if backend() == "vertex":
            raise APIBudgetError("Vertex generation requires the configured reservation limits and ledger")
        return {}
    if backend() == "vertex":
        from .vertex_api import VertexClient
        counter = VertexClient().count_prompt
    else:
        if (endpoint_url() != "https://generativelanguage.googleapis.com/v1beta/openai"
                or model != "gemini-2.5-flash" or request_options() != {"reasoning_effort": "none"}):
            raise APIBudgetError("This reservation profile requires Gemini 2.5 Flash with thinking disabled")
        counter = lambda messages: count_gemini_prompt(messages, model)
    if not 1 <= max_tokens <= limits["max_output_tokens"]:
        raise APIBudgetError("Completion allowance exceeds the API budget profile")
    path_value = os.environ.get("LAB_LLM_API_LEDGER")
    if not path_value:
        raise APIBudgetError("Set LAB_LLM_API_LEDGER to the persistent reservation file")
    if not os.environ.get("LAB_LLM_SERVING_ID", "").strip():
        raise APIBudgetError("Set LAB_LLM_SERVING_ID to identify the authorized research project")
    try:
        count = counter(messages)
    except APIBudgetError:
        raise
    except Exception as error:
        # An unavailable counter must not turn off accounting or send paid
        # requests anyway. Preserve partial cells and stop the worker.
        raise APIBudgetError(f"Prompt token counter failed ({type(error).__name__}: {error}); generation withheld") from error
    # Leave margin for provider-side prompt serialization.
    if count + 512 > limits["max_prompt_tokens"]:
        raise APIBudgetError(f"Prompt needs {count} tokens plus serialization margin; "
                             "study context exhausted before generation")
    charge = math.ceil(limits["max_prompt_tokens"] * limits["input_usd_per_million"]
                       + max_tokens * limits["output_usd_per_million"])
    ceiling = math.floor(limits["max_usd"] * 1_000_000)
    identity = {"model": model, "endpoint": endpoint_url(), "limits": limits,
                "serving_identity": os.environ.get("LAB_LLM_SERVING_ID"),
                "request_options": request_options()}
    signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise APIBudgetError("Budget ledger is locked; inspect the other worker or interrupted update") from error
    os.close(descriptor)
    try:
        if path.exists():
            try:
                ledger = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise APIBudgetError("Unreadable reservation ledger; preserve it") from error
            if not isinstance(ledger, dict):
                raise APIBudgetError("Malformed reservation ledger; preserve it")
            if ledger.get("identity_sha256") != signature:
                raise APIBudgetError("Budget ledger belongs to a different configuration; preserve it")
            used = ledger.get("reserved_micro_usd")
            attempts = ledger.get("generation_attempts")
            if type(used) is not int or used < 0 or type(attempts) is not int or attempts < 0:
                raise APIBudgetError("Malformed reservation ledger; preserve it")
        else:
            used, attempts = 0, 0
        if used + charge > ceiling:
            raise APIBudgetError("Generation reservation would exceed the configured dollar budget")
        ledger = {"identity_sha256": signature, "identity": identity,
                  "reserved_micro_usd": used + charge, "generation_attempts": attempts + 1,
                  "note": "Conservative reservations, not measured charges; no timeout refunds. Taxes and changed prices excluded."}
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        lock.unlink()
    return {"counted_prompt_tokens": count, "reserved_micro_usd": charge,
            "cumulative_reserved_micro_usd": used + charge}
