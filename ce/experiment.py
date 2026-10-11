"""Keep model, prompt and budget changes out of the same experiment table."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .benchmark import load_runs


class ExperimentMismatch(ValueError):
    pass


def record_config(run: dict) -> dict:
    config = run.get("notes", {}).get("experiment_config")
    if config:
        return config
    # Historical records have no manifest. Their stored system prompts still
    # distinguish the old echo-prone harness from the subsequent no-op guard.
    messages = ((run.get("certificate") or {}).get("log") or {}).get("messages", [])
    system = next((m.get("content", "") for m in messages
                   if m.get("role") == "system"), "unknown")
    notes = run.get("notes", {})
    return {
        "model": run.get("model", "unknown"),
        "harness_version": "historical",
        "prompt_hash": hashlib.sha256(system.encode()).hexdigest(),
        "temperature": notes.get("temperature"),
        "max_iterations": run.get("max_iterations"),
        "oracle_budget": notes.get("oracle_budget"),
        "oracle_strictness": notes.get("oracle_strictness"),
    }


def config_key(config: dict) -> str:
    return json.dumps(config, sort_keys=True)


def load_experiment_runs(directory: str, model: str | None = None) -> list[dict]:
    runs = load_runs(directory)
    if model is not None:
        runs = [r for r in runs if r.get("model") == model]
    groups = {config_key(record_config(r)) for r in runs}
    if len(groups) > 1:
        models = sorted({r.get("model", "unknown") for r in runs})
        raise ExperimentMismatch(
            f"{directory} contains {len(groups)} experiment configurations "
            f"(models: {', '.join(models)}). Use --model to select a model; "
            "different prompts or budgets for the same model need separate directories."
        )
    return runs


def prepare_output(directory: str, config: dict, *, write: bool = True) -> None:
    """Reject incompatible records before an LLVM checkout or build, even on overwrite."""
    root = Path(directory)
    manifest = root / ".experiment.json"
    if manifest.exists():
        try:
            saved = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ExperimentMismatch(f"Unreadable experiment manifest: {manifest}") from e
        if saved != config:
            raise ExperimentMismatch(
                f"{directory} belongs to a different model, harness or budget. "
                "Choose a new --out directory; existing records are preserved."
            )
    for run in load_runs(directory):
        if record_config(run) != config:
            raise ExperimentMismatch(
                f"{directory} contains incompatible records, including "
                f"{run.get('bug_id')}/{run.get('condition')} ({run.get('model')}). "
                "Choose a new --out directory; --overwrite does not bypass this check."
            )
    if write and not manifest.exists():
        root.mkdir(parents=True, exist_ok=True)
        # Exclusive creation also prevents a second runner from replacing the
        # manifest established between the checks above and this write.
        try:
            with manifest.open("x", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
        except FileExistsError:
            prepare_output(directory, config, write=False)
