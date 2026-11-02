"""Explicit hosted-API options shared by generation, preflight and provenance.

Credentials are read separately by the SDK and must never enter the manifest.
No account/key rotation or model fallback is performed here.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

DEFAULT_URL = "https://api.deepseek.com"
RESERVED = {"model", "messages", "temperature", "max_tokens", "max_completion_tokens", "stream", "n"}
SECRETS = {"api_key", "api-key", "authorization", "access_token", "bearer", "password"}


def endpoint_url() -> str:
    return os.environ.get("LAB_LLM_URL", DEFAULT_URL).rstrip("/")


def backend() -> str:
    value = os.environ.get("LAB_LLM_BACKEND", "openai")
    if value not in ("openai", "vertex"):
        raise ValueError("LAB_LLM_BACKEND must be openai or vertex")
    return value


def request_options() -> dict:
    raw = os.environ.get("LAB_LLM_REQUEST_OPTIONS", "{}")
    try:
        options = json.loads(raw)
    except ValueError as error:
        raise ValueError("LAB_LLM_REQUEST_OPTIONS must be a JSON object") from error
    allowed = {"thinking_level"} if backend() == "vertex" else {"reasoning_effort", "extra_body"}
    if not isinstance(options, dict) or set(options) - allowed:
        raise ValueError("LAB_LLM_REQUEST_OPTIONS has unsupported fields for the selected backend")
    if "thinking_level" in options and options["thinking_level"] not in ("MINIMAL", "LOW", "MEDIUM", "HIGH"):
        raise ValueError("Unsupported thinking_level")
    effort = options.get("reasoning_effort")
    if "reasoning_effort" in options and effort not in ("none", "minimal", "low", "medium", "high"):
        raise ValueError("Unsupported reasoning_effort")
    body = options.get("extra_body", {})
    if not isinstance(body, dict) or RESERVED & set(body):
        raise ValueError("extra_body must be an object and cannot override the controlled model/prompt/budgets")

    def check_secrets(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in SECRETS:
                    raise ValueError("Put API credentials in LAB_LLM_TOKEN, not request options")
                check_secrets(child)
        elif isinstance(value, list):
            for child in value:
                check_secrets(child)
    check_secrets(options)
    if effort is not None and "thinking_config" in json.dumps(body):
        raise ValueError("Use reasoning_effort or thinking_config, not both")
    return options


def model_is_listed(model: str, served, url: str) -> bool:
    names = set(served)
    if urlparse(url).hostname == "generativelanguage.googleapis.com":
        # Some Gemini model listings retain their resource-name prefix.
        return model.removeprefix("models/") in {name.removeprefix("models/") for name in names}
    return model in names


def response_metadata(response) -> dict:
    usage = getattr(response, "usage", None)
    completion = getattr(usage, "completion_tokens_details", None)
    prompt = getattr(usage, "prompt_tokens_details", None)
    return {
        "response_id": getattr(response, "id", None),
        "system_fingerprint": getattr(response, "system_fingerprint", None),
        "provider": getattr(response, "provider", None),
        "reasoning_tokens": getattr(completion, "reasoning_tokens", None),
        "cached_prompt_tokens": getattr(prompt, "cached_tokens", None),
        "provider_reported_cost": getattr(usage, "cost", None),
    }
