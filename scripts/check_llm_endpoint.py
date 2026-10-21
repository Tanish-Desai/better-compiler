#!/usr/bin/env python3
"""Preflight: does the model endpoint work, before you start a multi-day sweep?

    python3 scripts/check_llm_endpoint.py
    python3 scripts/check_llm_endpoint.py --repair-format --repeat 3

WHY
---
The sweep is 792 runs and each one is an LLVM rebuild
(``docs/ANALYSIS_PLAN.md`` section 5). ``repair_experiment.py`` is deliberately
tolerant -- a provider error ends one run, not the sweep -- so a misconfigured
endpoint does not stop anything. It just quietly produces nothing, for hours.

This checks the same environment the runner reads, in a few seconds, and
reports wrong URLs, unavailable models, empty/truncated output and, with
``--repair-format``, whether the endpoint can produce an applicable v3 edit.
The tiny prompt does not validate a real repair or the experiment's full
context length, and identical toy answers do not diagnose broken sampling.

It never touches LLVM, the dataset, or ``results/``.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROMPT = (
    "Reply with exactly this C++ line and nothing else:\n"
    "```cpp\nreturn nullptr;\n```"
)

REPAIR_SOURCE = "int adjust(int value) {\n  return value - 1;\n}\n"
REPAIR_EXPECTED = "int adjust(int value) {\n  return value + 1;\n}\n"


def repair_messages() -> list[dict]:
    """Use the sweep's actual system/format instructions without importing LLVM."""
    from examples.repair_experiment import SYSTEM_PROMPT

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            "This is a patch-format preflight, not a benchmark repair.\n"
            "In this ORIGINAL source excerpt, change `return value - 1;` to "
            "`return value + 1;` and preserve every other line. Return only "
            "the SEARCH/REPLACE edit required by the system prompt.\n"
            f"```cpp\n{REPAIR_SOURCE}```"
        )},
    ]


def validate_repair_reply(reply: str) -> None:
    """Reject a malformed, inapplicable, nonediting or incorrect toy edit."""
    from ce.patching import PatchFormatError, apply_edits

    patched = apply_edits(REPAIR_SOURCE, reply)
    compact = lambda text: re.sub(r"\s+", "", text)
    if compact(patched) == compact(REPAIR_SOURCE):
        raise PatchFormatError("The edit leaves the toy source unchanged.")
    if compact(patched) != compact(REPAIR_EXPECTED):
        raise PatchFormatError("The edit did not make the specified toy change while preserving the other lines.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeat", type=int, default=1,
                        help="send the same prompt N times and report how many "
                             "distinct replies came back (descriptive only)")
    parser.add_argument("--repair-format", action="store_true",
                        help="require an applicable v3 SEARCH/REPLACE edit using the runner's system prompt")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be positive")

    url = os.environ.get("LAB_LLM_URL", "https://api.deepseek.com")
    model = os.environ.get("LAB_LLM_MODEL", "deepseek-reasoner")
    try:
        temperature = float(os.environ.get("LAB_LLM_TEMP", "0.8"))
        max_tokens = int(os.environ.get("LAB_LLM_MAX_TOKENS", "8192"))
    except ValueError:
        parser.error("LAB_LLM_TEMP must be numeric and LAB_LLM_MAX_TOKENS must be a positive integer")
    if max_tokens < 1:
        parser.error("LAB_LLM_MAX_TOKENS must be positive")
    token = os.environ.get("LAB_LLM_TOKEN")

    print(f"LAB_LLM_URL   {url}")
    print(f"LAB_LLM_MODEL {model}")
    print(f"LAB_LLM_TEMP  {temperature}")
    print(f"LAB_LLM_MAX_TOKENS {max_tokens}")
    print(f"LAB_LLM_TOKEN {'set' if token else 'NOT SET'}")

    if not token:
        print("\nLAB_LLM_TOKEN is unset. A local vLLM server still needs one -- "
              "pass --api-key when starting it and export the same string.",
              file=sys.stderr)
        return 2

    try:
        from openai import OpenAI
    except ImportError:
        print("\nthe openai package is not installed in this interpreter",
              file=sys.stderr)
        return 2

    client = OpenAI(api_key=token, base_url=url)

    try:
        served = [m.id for m in client.models.list(timeout=30).data]
    except Exception as e:  # noqa: BLE001 - any failure here is the answer
        print(f"\ncannot list models at {url}: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    print(f"\nserved models: {', '.join(served) or '<none>'}")
    if model not in served:
        print(f"ERROR: LAB_LLM_MODEL={model!r} is not in that list. vLLM "
              f"serves the name given by --served-model-name, or the full "
              f"HuggingFace repo id if that flag was omitted.", file=sys.stderr)
        return 1

    replies = []
    messages = repair_messages() if args.repair_format else [{"role": "user", "content": PROMPT}]
    for i in range(args.repeat):
        started = time.time()
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=300,
            )
            choice = response.choices[0]
            text = (choice.message.content or "").strip()
            finish_reason = getattr(choice, "finish_reason", None)
        except Exception as e:  # noqa: BLE001
            print(f"\nrequest {i + 1} failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return 1
        usage = getattr(response, "usage", None)
        replies.append(text)
        print(f"\nrequest {i + 1}: {time.time() - started:.1f}s, "
              f"{getattr(usage, 'prompt_tokens', '?')} prompt / "
              f"{getattr(usage, 'completion_tokens', '?')} completion tokens")
        print("  " + (text[:200].replace("\n", "\n  ") or "<empty reply>"))
        if finish_reason == "length":
            print("\nFAIL: reply hit the output limit; a partial edit is not safe to apply.", file=sys.stderr)
            return 1
        if not text:
            print("\nFAIL: empty reply. Check the model's output channel and serving configuration.", file=sys.stderr)
            return 1
        if args.repair_format:
            try:
                validate_repair_reply(text)
            except ValueError as error:
                print(f"\nFAIL: v3 repair-format preflight: {error}", file=sys.stderr)
                return 1

    if args.repeat > 1:
        distinct = len(set(replies))
        print(f"\n{distinct} distinct reply/replies out of {args.repeat}")
        if distinct == 1:
            print("Identical answers to this tightly specified toy task are expected; "
                  "they do not establish that sampling is broken.")
    if args.repeat > 1 and temperature == 0:
        print("WARNING: temperature is 0. The study specifies 0.8 for repeated sampling; "
              "the toy response count does not measure sampling quality.", file=sys.stderr)

    label = " and v3 repair format" if args.repair_format else ""
    print(f"\nendpoint{label} OK. This does not validate LLVM repair quality or long prompts. "
          "Next: the held-out pilot with full build/reproducer/regression validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
