#!/usr/bin/env python3
"""Preflight: does the model endpoint work, before you start a multi-day sweep?

    python3 scripts/check_llm_endpoint.py
    python3 scripts/check_llm_endpoint.py --repair-format --repeat 3
    python3 scripts/check_llm_endpoint.py --context-probe

WHY
---
The sweep can require hundreds of LLVM rebuilds. ``repair_experiment.py`` is deliberately
tolerant -- a provider error ends one run, not the sweep -- so a misconfigured
endpoint does not stop anything. It just quietly produces nothing, for hours.

This checks the same environment the runner reads, in a few seconds, and
reports wrong URLs, unavailable models, empty/truncated output and, with
``--repair-format``, whether the endpoint can produce an applicable v3 edit.
``--context-probe`` additionally checks the toy edit with about 50K characters
of immutable C++ comments in its source excerpt and reports server token usage
and request wall throughput. Neither probe validates a real repair, full context
capacity or a multi-turn run; identical toy answers do not diagnose sampling.

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

from ce.llm_api import backend, model_is_listed, request_options  # noqa: E402
from ce.api_budget import budget_limits, reserve_generation  # noqa: E402

PROMPT = (
    "Reply with exactly this C++ line and nothing else:\n"
    "```cpp\nreturn nullptr;\n```"
)

REPAIR_SOURCE = "int adjust(int value) {\n  return value - 1;\n}\n"
REPAIR_EXPECTED = "int adjust(int value) {\n  return value + 1;\n}\n"
CONTEXT_PADDING = "".join(
    f"// Context {index:04d}: Preserve this harmless C++ comment during the endpoint probe.\n"
    for index in range(650)
)


def repair_messages(*, context_probe: bool = False) -> list[dict]:
    """Use the sweep's actual system/format instructions without importing LLVM."""
    from examples.repair_experiment import SYSTEM_PROMPT

    source = (CONTEXT_PADDING if context_probe else "") + REPAIR_SOURCE
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            "This is a patch-format preflight, not a benchmark repair.\n"
            "In this ORIGINAL source excerpt, change `return value - 1;` to "
            "`return value + 1;` and preserve every other line. Return only "
            "the SEARCH/REPLACE edit required by the system prompt.\n"
            f"```cpp\n{source}```"
        )},
    ]


def validate_repair_reply(reply: str, *, context_probe: bool = False) -> None:
    """Reject a malformed, inapplicable, nonediting or incorrect toy edit."""
    from ce.patching import PatchFormatError, apply_edits

    padding = CONTEXT_PADDING if context_probe else ""
    source = padding + REPAIR_SOURCE
    expected = padding + REPAIR_EXPECTED
    patched = apply_edits(source, reply)
    compact = lambda text: re.sub(r"\s+", "", text)
    if compact(patched) == compact(source):
        raise PatchFormatError("The edit leaves the toy source unchanged.")
    if compact(patched) != compact(expected):
        raise PatchFormatError("The edit did not make the specified toy change while preserving the other lines.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeat", type=int, default=1,
                        help="send the same prompt N times and report how many "
                             "distinct replies came back (descriptive only)")
    parser.add_argument("--repair-format", action="store_true",
                        help="require an applicable v3 SEARCH/REPLACE edit using the runner's system prompt")
    parser.add_argument("--context-probe", action="store_true",
                        help="also require the v3 toy edit with about 50K characters of C++ comment context; "
                             "report token usage and request wall throughput (implies --repair-format)")
    parser.add_argument("--list-models", action="store_true", help="list model IDs without generating replies")
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
    try:
        is_vertex = backend() == "vertex"
        options = request_options()
        limits = budget_limits()
    except ValueError as error:
        parser.error(str(error))
    token = os.environ.get("LAB_LLM_TOKEN")

    print(f"LAB_LLM_URL   {url}")
    print(f"LAB_LLM_MODEL {model}")
    print(f"LAB_LLM_TEMP  {temperature}")
    print(f"LAB_LLM_MAX_TOKENS {max_tokens}")
    print(f"LAB_LLM_TOKEN {'set' if token else 'NOT SET'}")

    if not token and not is_vertex:
        print("\nLAB_LLM_TOKEN is unset. Set the provider's API key privately. "
              "For local vLLM, use the string configured by --api-key.",
              file=sys.stderr)
        return 2

    if is_vertex:
        if args.list_models:
            parser.error("Vertex uses publisher-model access checks; use --repair-format instead of --list-models")
        from ce.vertex_api import VertexClient
        try:
            client = VertexClient()
            client.check_access()
        except Exception as error:
            print(f"Vertex access check failed before generation: {error}", file=sys.stderr)
            return 1
        print(f"Vertex ADC/counter access OK for {model}; generation remains to be tested.")
    else:
        try:
            from openai import OpenAI
        except ImportError:
            print("\nthe openai package is not installed in this interpreter", file=sys.stderr)
            return 2
        client = OpenAI(api_key=token, base_url=url, **({"max_retries": 0} if limits else {}))
        try:
            served = [m.id for m in client.models.list(timeout=30).data]
        except Exception as e:  # noqa: BLE001 - any failure here is the answer
            print(f"\ncannot list models at {url}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(f"\nserved models: {', '.join(served) or '<none>'}")
        if args.list_models:
            return 0
        if not model_is_listed(model, served, url):
            print(f"ERROR: LAB_LLM_MODEL={model!r} is not in that list. Select a model "
                  "available to this API project. For local vLLM, match --served-model-name "
                  "or the full HuggingFace repo id if that flag was omitted.", file=sys.stderr)
            return 1

    replies = []
    check_repair_format = args.repair_format or args.context_probe
    messages = repair_messages(context_probe=args.context_probe) if check_repair_format else [{"role": "user", "content": PROMPT}]
    if args.context_probe:
        print(f"\ncontext probe: {len(CONTEXT_PADDING):,} padding characters; "
              f"{len(CONTEXT_PADDING + REPAIR_SOURCE):,} source-excerpt characters")
        print("Wall throughput includes prompt prefill, queueing and network time; "
              "it is not isolated decode speed or a full-context capacity test.")
    for i in range(args.repeat):
        started = time.perf_counter()
        try:
            reserve_generation(messages, model, max_tokens)
            generate = client.generate if is_vertex else client.chat.completions.create
            response = generate(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=300,
                **options,
            )
            choice = response.choices[0]
            text = (choice.message.content or "").strip()
            finish_reason = getattr(choice, "finish_reason", None)
        except Exception as e:  # noqa: BLE001
            print(f"\nrequest {i + 1} failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return 1
        elapsed = time.perf_counter() - started
        usage = getattr(response, "usage", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        wall_rate = completion_tokens / elapsed if isinstance(completion_tokens, int) and elapsed > 0 else None
        replies.append(text)
        print(f"\nrequest {i + 1}: {elapsed:.2f}s wall, "
              f"{getattr(usage, 'prompt_tokens', '?')} prompt / "
              f"{completion_tokens if completion_tokens is not None else '?'} completion tokens")
        print(f"  {wall_rate:.2f} completion tokens/s over request wall time" if wall_rate is not None
              else "  wall token rate unavailable: the server did not report completion-token usage")
        print("  " + (text[:200].replace("\n", "\n  ") or "<empty reply>"))
        if finish_reason == "length":
            print("\nFAIL: reply hit the output limit; a partial edit is not safe to apply.", file=sys.stderr)
            return 1
        if not text:
            print("\nFAIL: empty reply. Check the model's output channel and serving configuration.", file=sys.stderr)
            return 1
        if check_repair_format:
            try:
                validate_repair_reply(text, context_probe=args.context_probe)
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

    label = " and bounded long-input v3 toy probe" if args.context_probe else " and v3 repair format" if check_repair_format else ""
    limitation = ("This does not establish the full 32K window, multi-turn capacity or LLVM repair quality. "
                  if args.context_probe else "This does not validate LLVM repair quality or long prompts. ")
    print(f"\nendpoint{label} OK. {limitation}"
          "Next: the held-out pilot with full build/reproducer/regression validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
