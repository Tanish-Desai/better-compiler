"""Shared validation for the verifier-eligibility protocol amendment."""

from __future__ import annotations


MUTABLE_PROTOCOL_FIELDS = {
    "id", "written", "status", "bugs", "cohort_plan", "amends_protocol_id",
    "rationale", "notes",
}


def assert_treatment_unchanged(parent: dict, revised: dict) -> None:
    differences = [key for key in sorted((set(parent) | set(revised)) - MUTABLE_PROTOCOL_FIELDS)
                   if parent.get(key) != revised.get(key)]
    if differences:
        raise ValueError("Protocol amendment changes treatment fields: " + ", ".join(differences))


def assert_config_matches_protocol(config: dict, protocol: dict, protocol_sha: str) -> None:
    expected = {
        "model": protocol["model"],
        "harness_version": protocol["harness_version"],
        "prompt_hash": protocol["prompt_hash"],
        "temperature": protocol["temperature"],
        "max_iterations": protocol["max_iterations"],
        "oracle_budget": protocol["oracle_budget"],
        "oracle_strictness": protocol["oracle_strictness"],
        "max_completion_tokens": protocol["max_completion_tokens"],
        "protocol_id": protocol["id"],
        "protocol_sha256": protocol_sha,
        "request_options": protocol["request_options"],
        "api_limits": protocol["api_limits"],
        "revision_context": protocol["revision_context"],
        "require_semantic_baseline": protocol["require_semantic_baseline"],
        "llm_backend": protocol["llm_backend"],
        "vertex_location": protocol["vertex_location"],
        "model_repository": protocol["model_repository"],
        "model_revision": protocol["model_revision"],
    }
    expected["endpoint_url"] = protocol["endpoint_url_template"].format(
        project=config.get("vertex_project", ""))
    differences = [key for key, value in expected.items() if config.get(key) != value]
    if differences:
        raise ValueError("Saved run config does not match its declared protocol: "
                         + ", ".join(differences))
