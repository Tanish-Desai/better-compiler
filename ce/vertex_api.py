"""Text-only Vertex REST transport with refreshable ADC and no generation retries.

The caller reserves each generation attempt. Authentication refresh can retry
token acquisition, but this transport never retries a generation HTTP request.
"""
from __future__ import annotations

from functools import lru_cache
import json
import os
import re
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .llm_api import backend, endpoint_url, request_options

MODEL = "gemini-3.1-flash-lite"
URL_TEMPLATE = "https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models"


def settings() -> dict:
    project = os.environ.get("LAB_VERTEX_PROJECT", "")
    location = os.environ.get("LAB_VERTEX_LOCATION", "global")
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("Set LAB_VERTEX_PROJECT to the credited research project ID")
    if location != "global" or endpoint_url() != URL_TEMPLATE.format(project=project):
        raise ValueError("Vertex profile requires its exact project URL and global location")
    if backend() != "vertex" or os.environ.get("LAB_LLM_MODEL") != MODEL:
        raise ValueError(f"Vertex transport is configured for {MODEL}")
    if request_options() not in ({"thinking_level": "MINIMAL"}, {"thinking_level": "LOW"}):
        raise ValueError("Vertex requires a frozen MINIMAL or LOW thinking profile")
    return {"project": project, "location": location}


@lru_cache(maxsize=1)
def _credentials(project: str):
    try:
        import google.auth
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"], quota_project_id=project)
    except Exception as error:
        raise RuntimeError("Vertex needs google-auth and Application Default Credentials; "
                           "run gcloud auth application-default login on this container") from error
    return credentials


def authorization(project: str) -> dict:
    credentials = _credentials(project)
    if not credentials.valid:
        try:
            from google.auth.transport.requests import Request as AuthRequest
            credentials.refresh(AuthRequest())
        except Exception as error:
            raise RuntimeError("Vertex ADC token refresh failed; check this container's Google login") from error
    if not isinstance(credentials.token, str) or not credentials.token:
        raise RuntimeError("Vertex ADC returned no access token")
    return {"Authorization": "Bearer " + credentials.token, "x-goog-user-project": project}


def native_messages(messages: list[dict]) -> dict:
    contents, systems = [], []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role not in ("system", "user", "assistant") or not isinstance(content, str):
            raise ValueError("Vertex transport supports text-only system/user/assistant messages")
        if role == "system":
            systems.append({"text": content})
        else:
            contents.append({"role": "model" if role == "assistant" else "user", "parts": [{"text": content}]})
    payload = {"contents": contents}
    if systems:
        payload["systemInstruction"] = {"parts": systems}
    return payload


class VertexHTTPError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"Vertex HTTP {status_code}: {detail}")


class VertexClient:
    def __init__(self):
        self.config = settings()
        self.options = request_options()
        self.base_url = endpoint_url() + "/" + MODEL

    def _post(self, action: str, payload: dict, timeout: int) -> dict:
        headers = {"Content-Type": "application/json", **authorization(self.config["project"])}
        request = Request(self.base_url + ":" + action, data=json.dumps(payload).encode(),
                          headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                detail = json.load(error).get("error", {}).get("message", "")[:1200]
            except (ValueError, OSError):
                detail = "request rejected"
            raise VertexHTTPError(error.code, detail) from error

    def count_prompt(self, messages: list[dict]) -> int:
        count = self._post("countTokens", native_messages(messages), 30).get("totalTokens")
        if type(count) is not int or count < 0:
            raise RuntimeError("Vertex countTokens returned no valid count")
        return count

    def check_access(self) -> None:
        self.count_prompt([{"role": "user", "content": "Reply with OK."}])

    def generate(self, *, model: str, messages: list[dict], temperature: float,
                 max_tokens: int, timeout: int = 300, **options):
        if model != MODEL or options != self.options:
            raise ValueError("Vertex generation differs from its fixed model/thinking profile")
        payload = native_messages(messages)
        payload["generationConfig"] = {"temperature": temperature, "maxOutputTokens": max_tokens,
                                       "thinkingConfig": {"thinkingLevel": options["thinking_level"]}}
        response = self._post("generateContent", payload, timeout)
        candidates = response.get("candidates") or []
        if len(candidates) != 1:
            raise RuntimeError("Vertex returned no unique candidate; preserve the failed request")
        candidate = candidates[0]
        text = "".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", [])
                       if not part.get("thought"))
        finish = candidate.get("finishReason")
        finish = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter"}.get(finish, finish)
        usage = response.get("usageMetadata") or {}
        visible, thoughts = usage.get("candidatesTokenCount"), usage.get("thoughtsTokenCount")
        # Gemini reports visible and thought tokens separately. Both are output
        # tokens for cost accounting. Missing fields remain unknown in metadata.
        completion = visible + (thoughts or 0) if type(visible) is int else None
        return SimpleNamespace(
            id=response.get("responseId"), model=response.get("modelVersion"), provider="google-vertex-ai",
            choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)],
            usage=SimpleNamespace(prompt_tokens=usage.get("promptTokenCount"), completion_tokens=completion,
                                  total_tokens=usage.get("totalTokenCount"),
                                  completion_tokens_details=SimpleNamespace(reasoning_tokens=thoughts),
                                  prompt_tokens_details=SimpleNamespace(cached_tokens=usage.get("cachedContentTokenCount"))))
