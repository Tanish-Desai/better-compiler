"""An account probe must not expose OAuth credentials or silently generate."""
import io
import json
from urllib.error import HTTPError

from scripts import probe_vertex as probe


def test_default_counts_tokens_without_generating_or_printing_token(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(probe, "access_token", lambda: "private-oauth-token")
    def request(url, payload, token):
        calls.append(url)
        assert token == "private-oauth-token"
        return {"totalTokens": 6}
    monkeypatch.setattr(probe, "request_json", request)
    assert probe.main(["--project", "research-project-a"]) == 0
    output = capsys.readouterr().out
    assert "private-oauth-token" not in output and "totalTokens" in output
    assert len(calls) == 1 and calls[0].endswith(":countTokens")


def test_generation_is_explicit_bounded_and_uses_current_model(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(probe, "access_token", lambda: "token")
    def request(url, payload, token):
        calls.append((url, json.loads(json.dumps(payload))))
        if url.endswith(":countTokens"):
            return {"totalTokens": 6}
        return {"modelVersion": probe.MODEL, "usageMetadata": {"totalTokenCount": 8},
                "candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}]}
    monkeypatch.setattr(probe, "request_json", request)
    assert probe.main(["--project", "research-project-a", "--generate"]) == 0
    assert len(calls) == 2
    assert "/locations/global/" in calls[1][0]
    assert calls[1][1]["generationConfig"] == {"maxOutputTokens": 128, "temperature": 0,
                                              "thinkingConfig": {"thinkingLevel": "MINIMAL"}}
    assert '"text": "OK"' in capsys.readouterr().out


def test_denied_access_never_attempts_generation(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(probe, "access_token", lambda: "token")
    def denied(url, *args):
        calls.append(url)
        raise HTTPError(url, 403, "Forbidden", {}, io.BytesIO(
            b'{"error":{"status":"PERMISSION_DENIED","message":"Enable the Vertex AI API"}}'))
    monkeypatch.setattr(probe, "request_json", denied)
    assert probe.main(["--project", "research-project-a", "--generate"]) == 1
    assert len(calls) == 1
    assert "Enable the Vertex AI API" in capsys.readouterr().err
