#!/usr/bin/env python3
"""Check Vertex access in Cloud Shell; optionally make one bounded generation.

This is an account/model probe, not a repair experiment or a billing receipt.
No API key, VM, GPU or third-party Python package is required. OAuth credentials
come from the already authorized gcloud session and are never printed or saved.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MODEL = "gemini-3.1-flash-lite"


def access_token() -> str:
    try:
        result = subprocess.run(["gcloud", "auth", "print-access-token", "--quiet"],
                                capture_output=True, text=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Authorize gcloud in Cloud Shell before running this probe") from error
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("gcloud returned no access token")
    return token


def request_json(url: str, payload: dict, token: str) -> dict:
    request = Request(url, data=json.dumps(payload).encode(), method="POST",
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="project ID linked to the credited Cloud billing account")
    parser.add_argument("--generate", action="store_true", help="also send one billable request with at most 128 output tokens")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", args.project):
        parser.error("Use a Google Cloud project ID, not an API key or billing-account number")
    base = (f"https://aiplatform.googleapis.com/v1/projects/{args.project}/locations/global/"
            f"publishers/google/models/{MODEL}")
    payload = {"contents": [{"role": "user", "parts": [{"text": "Reply with exactly OK."}]}]}
    try:
        token = access_token()
        count = request_json(base + ":countTokens", payload, token)
        if type(count.get("totalTokens")) is not int or count["totalTokens"] < 0:
            raise RuntimeError("Vertex returned no valid token count; do not start a repair sweep")
        print(json.dumps({"action": "countTokens", "model": MODEL,
                          "totalTokens": count.get("totalTokens")}, indent=2))
        if args.generate:
            payload["generationConfig"] = {"maxOutputTokens": 128, "temperature": 0,
                                           "thinkingConfig": {"thinkingLevel": "MINIMAL"}}
            response = request_json(base + ":generateContent", payload, token)
            candidates = response.get("candidates") or []
            text = "".join(part.get("text", "") for candidate in candidates
                           for part in candidate.get("content", {}).get("parts", [])
                           if not part.get("thought"))
            print(json.dumps({"action": "generateContent", "modelVersion": response.get("modelVersion"),
                              "usageMetadata": response.get("usageMetadata"), "text": text,
                              "finishReasons": [candidate.get("finishReason") for candidate in candidates]}, indent=2))
            if not text.strip():
                raise RuntimeError("Generation returned no visible text; do not start a repair sweep")
    except HTTPError as error:
        try:
            detail = json.load(error).get("error", {})
        except (ValueError, OSError):
            detail = {}
        print(f"Vertex HTTP {error.code}: {detail.get('status', '')}: {detail.get('message', '')[:1200]}",
              file=sys.stderr)
        return 1
    except (RuntimeError, URLError, TimeoutError, ValueError) as error:
        print(f"Vertex probe failed: {error}", file=sys.stderr)
        return 1
    print("Access probe only: verify credit application in Cloud Billing before a longer study.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
