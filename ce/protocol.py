"""Load a versioned prospective schedule without consulting repair outcomes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .feedback import CONDITIONS


def load_protocol(path: str) -> tuple[dict, str]:
    raw = Path(path).read_bytes()
    protocol = json.loads(raw)
    if not isinstance(protocol, dict) or protocol.get("schema") != 1:
        raise ValueError("Expected a schema-1 experiment protocol object")
    for field in ("id", "harness_version", "model"):
        if not isinstance(protocol.get(field), str) or not protocol[field]:
            raise ValueError(f"Protocol needs a nonempty {field}")
    bugs, conditions = protocol.get("bugs"), protocol.get("conditions")
    if not isinstance(bugs, list) or not bugs or any(not isinstance(b, str) or not b.isdigit() for b in bugs):
        raise ValueError("Protocol bugs must be a nonempty list of string identifiers")
    if len(set(bugs)) != len(bugs):
        raise ValueError("Protocol contains duplicate bugs")
    if not isinstance(conditions, list) or not conditions or any(c not in CONDITIONS for c in conditions):
        raise ValueError("Protocol contains missing or unknown conditions")
    if len(set(conditions)) != len(conditions):
        raise ValueError("Protocol contains duplicate conditions")
    if protocol.get("condition_order", "fixed") not in ("fixed", "rotate-by-bug-and-trial-v1"):
        raise ValueError("Unknown protocol condition_order")
    if "require_semantic_baseline" in protocol and type(protocol["require_semantic_baseline"]) is not bool:
        raise ValueError("Protocol require_semantic_baseline must be boolean")
    for key in ("repeat", "max_iterations", "oracle_budget", "max_completion_tokens"):
        if type(protocol.get(key)) is not int or protocol[key] < 1:
            raise ValueError(f"Protocol {key} must be a positive integer")
    if protocol.get("oracle_strictness") not in ("any_failure", "error_class", "error_class_and_kind"):
        raise ValueError("Unknown protocol oracle_strictness")
    if not isinstance(protocol.get("temperature"), (int, float)) or not 0 <= protocol["temperature"] <= 2:
        raise ValueError("Protocol temperature must be between 0 and 2")
    if "primary_comparisons" in protocol:
        pairs = protocol["primary_comparisons"]
        if not isinstance(pairs, list) or not pairs:
            raise ValueError("Protocol primary_comparisons must be a nonempty list")
        seen = set()
        for pair in pairs:
            if (not isinstance(pair, dict) or pair.get("better") not in conditions
                    or pair.get("worse") not in conditions or pair["better"] == pair["worse"]
                    or not isinstance(pair.get("question"), str) or not pair["question"]):
                raise ValueError("Protocol primary comparison needs two distinct included conditions and a question")
            key = frozenset((pair["better"], pair["worse"]))
            if key in seen:
                raise ValueError("Protocol contains duplicate primary comparisons")
            seen.add(key)
    if protocol.get("require_pinned_model"):
        if not isinstance(protocol.get("model_repository"), str) or not protocol["model_repository"]:
            raise ValueError("Pinned protocol needs model_repository")
        revision = protocol.get("model_revision")
        if not isinstance(revision, str) or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("Pinned protocol needs an exact 40-character model_revision")
    # Git checkouts may use different newline conventions. Guard the schedule's
    # content, not JSON whitespace or the platform used for offline analysis.
    canonical = json.dumps(protocol, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return protocol, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
