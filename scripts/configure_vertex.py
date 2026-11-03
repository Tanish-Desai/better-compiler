#!/usr/bin/env python3
"""Write a nonsecret Vertex study profile; no login, network request or generation."""
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
    parser.add_argument("--project", required=True)
    parser.add_argument("--stage", choices=("pilot", "main"), default="pilot")
    parser.add_argument("--protocol", default=str(ROOT / "data/repair_v3_vertex_protocol.json"))
    parser.add_argument("--out")
    parser.add_argument("--runtime-env", default=str(Path.home() / "better-compiler-runtime/env.sh"))
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", args.project):
        parser.error("Use the research Google Cloud project ID")
    runtime = Path(args.runtime_env).resolve()
    if not runtime.is_file():
        parser.error("Existing native runtime env.sh is required")
    protocol, _ = load_protocol(args.protocol)
    if protocol.get("llm_backend") != "vertex":
        parser.error("Select a native Vertex protocol")
    prefix = protocol.get("profile_name", "vertex")
    if not isinstance(prefix, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", prefix):
        parser.error("Protocol profile_name must be a simple filename prefix")
    limits = protocol["api_limits"] | ({"max_usd": protocol.get("pilot_max_usd", 1.0)} if args.stage == "pilot" else {})
    values = {"LAB_LLM_BACKEND": "vertex", "LAB_VERTEX_PROJECT": args.project, "LAB_VERTEX_LOCATION": "global",
              "LAB_LLM_URL": protocol["endpoint_url_template"].format(project=args.project),
              "LAB_LLM_MODEL": protocol["model"], "LAB_LLM_TEMP": protocol["temperature"],
              "LAB_LLM_MAX_TOKENS": protocol["max_completion_tokens"],
              "LAB_LLM_REQUEST_OPTIONS": json.dumps(protocol["request_options"], separators=(",", ":")),
              "LAB_LLM_API_LIMITS": json.dumps(limits, separators=(",", ":")),
              "LAB_LLM_API_LEDGER": str((ROOT / f"run-provenance/{prefix}-{args.stage}-budget.json").resolve()),
              "LAB_LLM_SERVING_ID": f"vertex-project:{args.project}:global:native-rest",
              "CLOUDSDK_CONFIG": str(Path.home() / ".config/gcloud-better-compiler")}
    content = ("# Nonsecret Vertex profile; credentials stay in the dedicated Google config directory.\n"
               f"source {shlex.quote(str(runtime))}\n"
               "unset LAB_LLM_TOKEN LAB_LLM_MODEL_REPOSITORY LAB_LLM_MODEL_REVISION GOOGLE_APPLICATION_CREDENTIALS\n")
    content += "".join(f"export {key}={shlex.quote(str(value))}\n" for key, value in values.items())
    path = Path(args.out or f"run-provenance/{prefix}-{args.stage}.env")
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            parser.error("Existing Vertex profile differs; preserve it and its reservation ledger")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    print(f"Source {path}, then authenticate ADC on this container. No API key is required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
