"""Strict non-secret G5 configuration and local administrator CLI."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import ssl
import sys
from urllib.parse import urlsplit

from .. import contracts
from ..core import ContractError
from ..storage import _unique_object, read_json
from .access import AccessController, AccessError, AccessStore
from .enrollment import PrivateCredentialOutput


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _require(value, message="Invalid shared coordinator configuration"):
    if not value:
        raise ContractError(message)


def _id(value, label="identifier"):
    _require(isinstance(value, str) and _ID.fullmatch(value), f"Invalid {label}")
    return value


def _is_loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class SharedConfiguration:
    state_root: Path
    host: str
    port: int
    origin: str
    tls_certificate_file: Path | None
    tls_private_key_file: Path | None
    project_files: tuple[tuple[Path, Path], ...]
    browser_session_seconds: int

    def public(self):
        return {"schemaVersion": 2, "kind": "reproloop-shared-public-configuration",
                "origin": self.origin,
                "browserSessionSeconds": self.browser_session_seconds,
                "projectCount": len(self.project_files)}


def load_shared_configuration(path):
    source = Path(path).absolute()
    try:
        value = read_json(source)
    except Exception:
        raise ContractError("Shared coordinator configuration could not be loaded") from None
    _require(type(value) is dict and set(value) == {
        "schemaVersion", "kind", "stateRoot", "listen", "projects",
        "browserSessionSeconds"})
    _require(value["schemaVersion"] == 2 and type(value["schemaVersion"]) is int
             and value["kind"] == "reproloop-shared-coordinator")
    _require(isinstance(value["stateRoot"], str) and value["stateRoot"])
    state_root = Path(value["stateRoot"]).absolute()
    _require(state_root.name == "coordinator-v2",
             "Shared access state must use a coordinator-v2 namespace")
    listen = value["listen"]
    _require(type(listen) is dict and set(listen) == {
        "host", "port", "origin", "tlsCertificateFile", "tlsPrivateKeyFile"})
    host = listen["host"]
    _require(isinstance(host, str) and host and not any(char in host for char in "/\\"))
    port = listen["port"]
    _require(type(port) is int and 1 <= port <= 65535)
    origin = listen["origin"]
    parsed = urlsplit(origin)
    _require(parsed.scheme in {"http", "https"} and parsed.hostname is not None
             and parsed.port is not None and not parsed.username and not parsed.password
             and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    certificate = listen["tlsCertificateFile"]
    private_key = listen["tlsPrivateKeyFile"]
    _require((certificate is None) == (private_key is None))
    certificate_path = Path(certificate).absolute() if certificate is not None else None
    private_key_path = Path(private_key).absolute() if private_key is not None else None
    if not _is_loopback(host):
        _require(certificate_path is not None and parsed.scheme == "https",
                 "Non-loopback shared coordination requires TLS")
    if certificate_path is not None:
        _require(parsed.scheme == "https", "TLS configuration requires an HTTPS origin")
    if host in {"0.0.0.0", "::"}:
        _require(parsed.hostname not in {"0.0.0.0", "::", "localhost"},
                 "Wildcard listeners require an explicit advertised origin")
    elif parsed.hostname not in {host, "localhost" if _is_loopback(host) else host}:
        _require(False, "Configured origin does not match the listener")
    configured_port = parsed.port
    _require(configured_port == port,
             "Configured origin port does not match the listener")
    projects = value["projects"]
    _require(isinstance(projects, list) and 1 <= len(projects) <= 128)
    project_files = []
    for item in projects:
        _require(type(item) is dict and set(item) == {
            "projectFile", "collectionPolicyFile"}
            and all(isinstance(item[key], str) and item[key]
                    for key in item))
        project_files.append((Path(item["projectFile"]).absolute(),
                              Path(item["collectionPolicyFile"]).absolute()))
    _require(len(set(project_files)) == len(project_files),
             "Shared projects must be unique")
    browser_seconds = value["browserSessionSeconds"]
    _require(type(browser_seconds) is int and 60 <= browser_seconds <= 12 * 3600)
    return SharedConfiguration(
        state_root, host, port, origin.rstrip("/"), certificate_path,
        private_key_path, tuple(project_files), browser_seconds)


def create_server_ssl_context(configuration):
    _require(type(configuration) is SharedConfiguration)
    if configuration.tls_certificate_file is None:
        return None
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(configuration.tls_certificate_file,
                                configuration.tls_private_key_file)
        return context
    except (OSError, ssl.SSLError):
        raise ContractError("Shared TLS configuration could not be loaded") from None


def compose_shared_access(lab, configuration, *, store=None):
    _require(type(configuration) is SharedConfiguration)
    selected = store or AccessStore(configuration.state_root)
    controller = AccessController(
        selected, browser_session_seconds=configuration.browser_session_seconds)
    registrations = []
    try:
        prepared = []
        historical_bindings = selected.list_resource_bindings()
        configured_revisions = set()
        for project_path, policy_path in configuration.project_files:
            project = contracts.validate_project_revision(read_json(project_path))
            from .recording_session import validate_collection_policy
            policy = validate_collection_policy(read_json(policy_path))
            durable = selected.project(project["id"])
            project_digest = contracts.digest(project)
            revision_key = (project["id"], project_digest)
            _require(revision_key not in configured_revisions,
                     "Configured project revision is duplicated")
            configured_revisions.add(revision_key)
            current = (durable["revision"] == project["revision"]
                       and durable["projectDigest"] == project_digest
                       and durable["trustGroup"] == project["trustGroup"])
            historical = any(
                binding.project_id == project["id"]
                and binding.project_digest == project_digest
                for binding in historical_bindings)
            _require(current or historical,
                     "Configured project does not match local administration")
            prepared.append((project, policy, current))
        configured_projects = {project["id"] for project, _policy, current in prepared
                               if current}
        _require(bool(configured_projects),
                 "Shared coordination requires a current trusted project")
        for device in lab.list_devices():
            eligible = set(selected.assignment_project_ids(device["id"]))
            _require(bool(eligible & configured_projects),
                     "Shared device assignment is outside configured projects")
        for project, policy, _current in prepared:
            registration = lab.register_recording_project(project, policy)
            controller.bind_project(registration)
            registrations.append(registration)
    except Exception:
        if store is None:
            selected.close()
        raise ContractError("Shared trusted projects could not be composed") from None
    _require(registrations, "Shared coordination requires a trusted project")
    return controller, tuple(registrations)


def issue_bounded_project_grant(authority, project_id, *, lifetime_seconds):
    """Issue one non-renewing grant from trusted coordinator composition."""
    _id(project_id, "project")
    _require(type(lifetime_seconds) is int and 10 <= lifetime_seconds <= 7200,
             "Invalid coordinator grant lifetime")
    first = authority.clock_sync.sample()
    second = authority.clock_sync.sample()
    mapping = authority.clock_sync.record_exchange(
        coordinator_clock_id="shared-coordinator",
        coordinator_send_ns=first.nanoseconds,
        host_received=first, host_sent=second,
        coordinator_receive_ns=second.nanoseconds, max_drift_ppm=0)
    import uuid
    return authority.issue_parent_grant(
        mapping, grant_id="grant_" + uuid.uuid4().hex,
        project_id=project_id, controller_id="shared-coordinator",
        renewal_sequence=1,
        coordinator_deadline_ns=second.nanoseconds + lifetime_seconds * 1_000_000_000)


def _result(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")), flush=True)


def _stdin_credential(enabled):
    _require(enabled is True, "Administrator credential stdin is required")
    raw = sys.stdin.read(4097)
    _require(len(raw.encode("utf-8")) <= 4096,
             "Administrator credential input is too large")
    try:
        value = json.loads(
            raw, object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ContractError("Administrator credential input is invalid") from None
    _require(type(value) is dict and set(value) == {"credential"}
             and isinstance(value["credential"], str),
             "Administrator credential input is invalid")
    return value["credential"]


def admin_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="reproloop live-admin",
        description="Local administrator operations for the isolated coordinator-v2 store")
    sub = parser.add_subparsers(dest="operation", required=True)

    def common(command, *, authenticated=True):
        command.add_argument("--state-root", type=Path, required=True)
        if authenticated:
            command.add_argument("--credential-stdin", action="store_true")

    init = sub.add_parser("init")
    common(init, authenticated=False);init.add_argument("--administrator", required=True)
    init.add_argument("--lifetime-seconds", type=int, required=True)
    init.add_argument("--output", type=Path, required=True)
    identity = sub.add_parser("identity-add")
    common(identity);identity.add_argument("--identity", required=True)
    identity.add_argument("--administrator", action="store_true")
    identity_revoke = sub.add_parser("identity-revoke")
    common(identity_revoke);identity_revoke.add_argument("--identity", required=True)
    project = sub.add_parser("project-register")
    common(project);project.add_argument("--project", type=Path, required=True)
    membership = sub.add_parser("membership-grant")
    common(membership);membership.add_argument("--project", required=True)
    membership.add_argument("--identity", required=True)
    membership.add_argument("--role", choices=["viewer", "operator", "maintainer"], required=True)
    membership_revoke = sub.add_parser("membership-revoke")
    common(membership_revoke);membership_revoke.add_argument("--project", required=True)
    membership_revoke.add_argument("--identity", required=True)
    membership_revoke.add_argument("--role", choices=["viewer", "operator", "maintainer"], required=True)
    credential = sub.add_parser("credential-issue")
    common(credential);credential.add_argument("--identity", required=True)
    credential.add_argument("--lifetime-seconds", type=int, required=True)
    credential.add_argument("--output", type=Path, required=True)
    credential_revoke = sub.add_parser("credential-revoke")
    common(credential_revoke);credential_revoke.add_argument("--credential-id", required=True)
    enrollment = sub.add_parser("host-enrollment-create")
    common(enrollment);enrollment.add_argument("--host-id", required=True)
    enrollment.add_argument("--project", action="append", default=[], required=True)
    enrollment.add_argument("--trust-group", action="append", default=[])
    enrollment.add_argument("--lifetime-seconds", type=int, default=600)
    enrollment.add_argument("--credential-lifetime-seconds", type=int, default=86400)
    enrollment.add_argument("--output", type=Path, required=True)
    enrollment_revoke = sub.add_parser("host-enrollment-revoke")
    common(enrollment_revoke);enrollment_revoke.add_argument(
        "--enrollment-id", required=True)
    host_revoke = sub.add_parser("host-revoke")
    common(host_revoke);host_revoke.add_argument("--host-id", required=True)
    assignment = sub.add_parser("device-assign")
    common(assignment);assignment.add_argument("--device-id", required=True)
    scope = assignment.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project");scope.add_argument("--trust-group")
    assignment.add_argument("--host-id")
    assignment.add_argument("--sanitation-receipt", type=Path)
    adopt = sub.add_parser("legacy-adopt")
    common(adopt);adopt.add_argument("--adoption-id", required=True)
    adopt.add_argument("--source", type=Path, required=True)
    adopt.add_argument("--format", required=True)
    adopt.add_argument("--meaning", choices=[
        "legacy-recording-only", "legacy-result-only", "legacy-authority-history",
        "legacy-configuration-reference"], required=True)
    authorize_legacy = sub.add_parser("legacy-recording-authorize")
    common(authorize_legacy);authorize_legacy.add_argument("--recording-id", required=True)
    authorize_legacy.add_argument("--adoption-id", required=True)
    authorize_legacy.add_argument("--recording-digest", required=True)
    listing = sub.add_parser("list")
    common(listing);listing.add_argument(
        "--kind", choices=["identities", "projects", "memberships", "credentials",
                           "enrollments", "hosts", "devices", "adoptions"], required=True)
    args = parser.parse_args(argv)

    store = None
    credential_output = None
    try:
        if hasattr(args, "output"):
            state_root = Path(args.state_root).absolute()
            destination = Path(args.output).absolute()
            _require(destination != state_root and state_root not in destination.parents,
                     "Credential output must be outside coordinator state")
            credential_output = PrivateCredentialOutput(destination)
        store = AccessStore(args.state_root)
        operation = args.operation
        if operation == "init":
            actor = _id(args.administrator, "administrator")
            identity, issued = store.bootstrap_administrator_credential(
                actor, lifetime_seconds=args.lifetime_seconds)
            credential_output.write({"credential": issued.pop("token")})
            value = {"identity": identity, "credential": dict(
                issued, credentialOutput=str(args.output.absolute()))}
        else:
            principal = store.authenticate_principal(
                _stdin_credential(args.credential_stdin))
            store.authorize(principal, None, "administration.read")
            actor = principal.principal_id

        if operation == "init":
            pass
        elif operation == "identity-add":
            value = store.create_identity(actor, args.identity,
                                          administrator=args.administrator)
        elif operation == "identity-revoke":
            store.revoke_identity(actor, args.identity);value = {"revoked": args.identity}
        elif operation == "project-register":
            value = store.register_project(actor, read_json(args.project))
        elif operation == "membership-grant":
            value = store.grant_membership(
                actor, args.project, args.identity, args.role)
        elif operation == "membership-revoke":
            store.revoke_membership(actor, args.project, args.identity, args.role)
            value = {"projectId": args.project, "identityId": args.identity,
                     "role": args.role, "active": False}
        elif operation == "credential-issue":
            issued = store.issue_principal_credential(
                actor, args.identity, lifetime_seconds=args.lifetime_seconds)
            credential_output.write({"credential": issued.pop("token")})
            value = dict(issued, credentialOutput=str(args.output.absolute()))
        elif operation == "credential-revoke":
            store.revoke_credential(actor, args.credential_id)
            value = {"credentialId": args.credential_id, "revoked": True}
        elif operation == "host-enrollment-create":
            issued = store.create_host_enrollment(
                actor, host_id=args.host_id, project_ids=args.project,
                trust_groups=args.trust_group, lifetime_seconds=args.lifetime_seconds,
                credential_lifetime_seconds=args.credential_lifetime_seconds)
            credential_output.write({"credential": issued.pop("token")})
            value = dict(issued, enrollmentOutput=str(args.output.absolute()))
        elif operation == "host-enrollment-revoke":
            store.revoke_host_enrollment(actor, args.enrollment_id)
            value = {"enrollmentId": args.enrollment_id, "revoked": True}
        elif operation == "host-revoke":
            store.revoke_host(actor, args.host_id)
            value = {"hostId": args.host_id, "revoked": True}
        elif operation == "device-assign":
            receipt = read_json(args.sanitation_receipt) if args.sanitation_receipt else None
            value = store.assign_device(
                actor, args.device_id, project_id=args.project,
                trust_group=args.trust_group, host_id=args.host_id,
                sanitation_receipt=receipt)
        elif operation == "legacy-adopt":
            value = store.adopt_legacy(
                args.adoption_id, args.source, original_format=args.format,
                meaning=args.meaning)
        elif operation == "legacy-recording-authorize":
            binding = store.authorize_legacy_resource(
                actor, "recording", args.recording_id,
                adoption_id=args.adoption_id,
                resource_digest=args.recording_digest)
            value = {
                "resourceKind": binding.kind,
                "resourceId": binding.resource_id,
                "projectId": binding.project_id,
                "meaning": binding.meaning,
                "adoptionId": binding.adoption_id,
                "authorizedDigest": binding.authorized_digest,
            }
        else:
            methods = {
                "identities": store.list_identities, "projects": store.list_projects,
                "memberships": store.list_memberships,
                "credentials": store.list_credentials,
                "enrollments": store.list_host_enrollments,
                "hosts": store.list_hosts,
                "devices": store.list_device_assignments,
                "adoptions": store.list_legacy_adoptions,
            }
            value = methods[args.kind]()
        _result({"ok": True, "result": value})
        return 0
    except (AccessError, ContractError, OSError, ValueError, TypeError):
        _result({"ok": False, "error": {"code": "administration_failed",
                                         "message": "Administrator operation failed"}})
        return 2
    finally:
        if credential_output is not None:
            credential_output.abort()
        if store is not None:
            store.close()


__all__ = [
    "SharedConfiguration", "admin_main", "compose_shared_access",
    "create_server_ssl_context", "issue_bounded_project_grant",
    "load_shared_configuration",
]
