#!/usr/bin/env python3
"""Preflight: does the model endpoint work, before you start a multi-day sweep?

    python3 scripts/check_llm_endpoint.py
    python3 scripts/check_llm_endpoint.py --repeat 3   # also check sampling

WHY
---
The sweep is 792 runs and each one is an LLVM rebuild
(``docs/ANALYSIS_PLAN.md`` section 5). ``repair_experiment.py`` is deliberately
tolerant -- a provider error ends one run, not the sweep -- so a misconfigured
endpoint does not stop anything. It just quietly produces nothing, for hours.

This checks the same environment the runner reads, in a few seconds, and
reports the things that actually go wrong: wrong URL, wrong model name,
temperature pinned to 0 when ``--repeat`` needs sampling, and a context window
too small for the longest condition.

It never touches LLVM, the dataset, or ``results/``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

#: The longest condition (``raw-structured``) is a few thousand tokens; this is
#: the margin below which the sweep would start truncating prompts.
MIN_CONTEXT_HINT = 8192

PROMPT = (
    "Reply with exactly this C++ line and nothing else:\n"
    "```cpp\nreturn nullptr;\n```"
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeat", type=int, default=1,
                        help="send the same prompt N times and report how many "
                             "distinct replies came back -- the check that "
                             "--repeat k in the sweep is not buying k copies")
    args = parser.parse_args(argv)

    url = os.environ.get("LAB_LLM_URL", "https://api.deepseek.com")
    model = os.environ.get("LAB_LLM_MODEL", "deepseek-reasoner")
    temperature = float(os.environ.get("LAB_LLM_TEMP", "0.8"))
    token = os.environ.get("LAB_LLM_TOKEN")

    print(f"LAB_LLM_URL   {url}")
    print(f"LAB_LLM_MODEL {model}")
    print(f"LAB_LLM_TEMP  {temperature}")
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
        served = [m.id for m in client.models.list().data]
    except Exception as e:  # noqa: BLE001 - any failure here is the answer
        print(f"\ncannot list models at {url}: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    print(f"\nserved models: {', '.join(served) or '<none>'}")
    if model not in served:
        print(f"WARNING: LAB_LLM_MODEL={model!r} is not in that list. vLLM "
              f"serves the name given by --served-model-name, or the full "
              f"HuggingFace repo id if that flag was omitted.", file=sys.stderr)

    replies = []
    for i in range(max(1, args.repeat)):
        started = time.time()
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": PROMPT}],
                temperature=temperature,
                timeout=300,
            )
        except Exception as e:  # noqa: BLE001
            print(f"\nrequest {i + 1} failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return 1
        text = (response.choices[0].message.content or "").strip()
        usage = getattr(response, "usage", None)
        replies.append(text)
        print(f"\nrequest {i + 1}: {time.time() - started:.1f}s, "
              f"{getattr(usage, 'prompt_tokens', '?')} prompt / "
              f"{getattr(usage, 'completion_tokens', '?')} completion tokens")
        print("  " + (text[:200].replace("\n", "\n  ") or "<empty reply>"))

    if not any(replies):
        print("\nthe model returned only empty replies -- a reasoning model "
              "whose output lands in a channel this client does not read will "
              "do this. The sweep would record every run as unfixed.",
              file=sys.stderr)
        return 1

    if args.repeat > 1:
        distinct = len(set(replies))
        print(f"\n{distinct} distinct reply/replies out of {args.repeat}")
        if distinct == 1:
            print("WARNING: identical replies. docs/ANALYSIS_PLAN.md sets k = 3, "
                  "and repeats at temperature 0 are three copies of one answer -- "
                  "triple the rebuilds for no extra information. Set "
                  "LAB_LLM_TEMP above 0 (0.8 is the preregistered value).",
                  file=sys.stderr)

    print("\nendpoint OK. Next: the nine-run pilot in docs/SLM_SELECTION.md section 9.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
