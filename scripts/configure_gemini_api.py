#!/usr/bin/env python3
"""Write an immutable, nonsecret Gemini profile; make no network requests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ce.protocol import load_protocol  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="nonsecret Google Cloud project ID; billing is verified separately")
    parser.add_argument("--out", default="run-provenance/gemini-api.env")
    parser.add_argument("--runtime-env", default=str(Path.home() / "better-compiler-runtime/env.sh"))
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", args.project):
        parser.error("Use the Google Cloud project ID, not a key, account number or display name")
    runtime = Path(args.runtime_env).resolve()
    if not runtime.is_file():
        parser.error("Native runtime env.sh is missing; complete CPU setup first")
    protocol, _ = load_protocol(str(ROOT / "data/repair_v3_gemini_api_protocol.json"))
    values = {"LAB_LLM_BACKEND": "openai", "LAB_LLM_URL": protocol["endpoint_url"], "LAB_LLM_MODEL": protocol["model"],
              "LAB_LLM_TEMP": protocol["temperature"], "LAB_LLM_MAX_TOKENS": protocol["max_completion_tokens"],
              "LAB_LLM_REQUEST_OPTIONS": json.dumps(protocol["request_options"], separators=(",", ":")),
              "LAB_LLM_API_LIMITS": json.dumps(protocol["api_limits"], separators=(",", ":")),
              "LAB_LLM_API_LEDGER": str((ROOT / "run-provenance/gemini-api-budget.json").resolve()),
              "LAB_LLM_SERVING_ID": f"gemini-developer-project:{args.project}"}
    content = ("# Nonsecret profile. Billing, model availability and credits require live verification.\n"
               f"source {shlex.quote(str(runtime))}\n"
               "unset LAB_LLM_TOKEN LAB_LLM_MODEL_REPOSITORY LAB_LLM_MODEL_REVISION LAB_VERTEX_PROJECT LAB_VERTEX_LOCATION\n")
    content += "".join(f"export {key}={shlex.quote(str(value))}\n" for key, value in values.items())
    path = Path(args.out)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            parser.error("Existing profile differs; preserve it and its budget ledger")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    print(f"Nonsecret profile: {path}. Source it, then enter your API key privately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
