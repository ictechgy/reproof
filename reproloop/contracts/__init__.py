"""Versioned, non-executable release contracts for Repro Loop.

These validators only accept data; they never execute commands, resolve URLs,
or grant authority.  Legacy capture/bundle APIs remain in :mod:`reproloop.core`.
"""
from .versions import (CONTRACT_VERSION, ContractError, digest, validate_id, validate_version,
                       bounded_int, bounded_number, validate_digest)
from .project import (
    validate_application_identity, validate_build_identity, validate_fixture_recipe,
    validate_project_revision, validate_recipe, validate_variable,
)
from .evidence import (
    validate_lifecycle_receipt, validate_original_evidence, validate_package_manifest,
    validate_provenance,
    validate_input,
)
from .scenario import (
    TrustedSubstitutionApproval, check_candidate_substitution, classify_predicates,
    issue_substitution_approval, validate_candidate_run, validate_qualification,
    validate_qualification_bindings, validate_specification,
)
from .observation import bind_coverage_requirement, validate_coverage_requirement, validate_observation, observation_result
from .execution import validate_attempt_budget, validate_execution_policy, validate_run, validate_run_sequence

__all__ = [name for name in globals() if name.startswith("validate_")] + [
    "CONTRACT_VERSION", "ContractError", "digest", "classify_predicates", "observation_result",
    "bounded_int", "bounded_number", "validate_digest", "validate_id", "validate_version",
    "TrustedSubstitutionApproval", "check_candidate_substitution", "issue_substitution_approval",
    "bind_coverage_requirement"
]
