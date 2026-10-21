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
    for key in ("repeat", "max_iterations", "oracle_budget", "max_completion_tokens"):
        if type(protocol.get(key)) is not int or protocol[key] < 1:
            raise ValueError(f"Protocol {key} must be a positive integer")
    if protocol.get("oracle_strictness") not in ("any_failure", "error_class", "error_class_and_kind"):
        raise ValueError("Unknown protocol oracle_strictness")
    if not isinstance(protocol.get("temperature"), (int, float)) or not 0 <= protocol["temperature"] <= 2:
        raise ValueError("Protocol temperature must be between 0 and 2")
    # Git checkouts may use different newline conventions. Guard the schedule's
    # content, not JSON whitespace or the platform used for offline analysis.
    canonical = json.dumps(protocol, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return protocol, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
