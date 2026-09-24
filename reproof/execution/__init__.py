"""Fail-closed interfaces for protected candidate execution."""

from .backend import (
    DisabledExecutionBackend,
    ExecutionAuthorization,
    ExecutionDenied,
    QualificationAuthority,
)
from .protocol import (
    validate_backend_qualification_record,
    validate_environment_descriptor,
    validate_execution_route,
    validate_execution_request,
    validate_external_validation_plan,
    validate_guest_image_manifest,
    validate_signing_policy,
    validate_toolchain_manifest,
)

__all__ = [
    "DisabledExecutionBackend",
    "ExecutionAuthorization",
    "ExecutionDenied",
    "QualificationAuthority",
    "validate_backend_qualification_record",
    "validate_environment_descriptor",
    "validate_execution_route",
    "validate_execution_request",
    "validate_external_validation_plan",
    "validate_guest_image_manifest",
    "validate_signing_policy",
    "validate_toolchain_manifest",
]
