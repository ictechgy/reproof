"""Explicit bounded bootstrap material; no discovery or execution authority."""
import base64
from dataclasses import dataclass, field
from pathlib import Path
import time

from . import contracts
from .contracts.versions import exact
from .execution.wire import MAX_FRAME_BYTES, decode_json
from .protected_service import PreparedProtectedServiceInputs
from .protected_signing_inputs import AndroidSigningDefinitionInputs, IOSSigningDefinitionInputs
from .protected_validation import ValidationSecretRegistry
from .repair_android_signing import AndroidSigningMaterialResolver
from .ios_signing_inputs import IOSSigningMaterialResolver


MAX_MATERIAL_INPUT_BYTES = MAX_FRAME_BYTES


class ProtectedServiceMaterialsError(RuntimeError):
    def __init__(self):
        super().__init__('protected_service_materials_unavailable')
        self.code = 'protected_service_materials_unavailable'


def _require(value):
    if not value:
        raise ProtectedServiceMaterialsError()


@dataclass(frozen=True, slots=True)
class ProtectedServiceMaterials:
    configuration_digest: str
    signing: object = field(repr=False)
    validation: ValidationSecretRegistry = field(repr=False)
    signing_profiles: int
    validation_references: int

    def public(self):
        return {'schemaVersion': 1, 'kind': 'protected-service-material-bindings',
            'configurationDigest': self.configuration_digest, 'executionAuthority': 'none',
            'signingProfiles': self.signing_profiles, 'validationReferences': self.validation_references}

    def close(self):
        self.signing.close()
        self.validation.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _secret(value, low, high, buffers):
    _require(type(value) is str and 0 < len(value) <= 4*((high+2)//3))
    decoded = bytearray(base64.b64decode(value, validate=True))
    buffers.append(decoded)
    _require(low <= len(decoded) <= high and base64.b64encode(decoded).decode('ascii') == value)
    return decoded


def _material_path(value):
    # Public artifact paths intentionally reject key-like names. This is the
    # separate explicit private-material channel; the resolver binds the file.
    _require(type(value) is str and 0 < len(value) <= 4096 and '\0' not in value)
    path = Path(value)
    _require(path.is_absolute() and str(path) == value and '..' not in path.parts)
    return path


def bind_service_materials(prepared, buffer):
    """Consume an explicit mutable input and register only declared references.

    Mutable input and decoded scratch buffers are cleared on every path.
    Python's immutable JSON/base64 temporaries are not claimed to be zeroized.
    Private key contents are opened later by the fixed signer, not here.
    """
    signing = validation = None
    buffers = []
    document = None
    try:
        _require(type(prepared) is PreparedProtectedServiceInputs and type(buffer) is bytearray
            and 0 < len(buffer) <= MAX_MATERIAL_INPUT_BYTES)
        _require(prepared.configuration_digest == prepared.build_signing.configuration_digest
                 == prepared.mobile.configuration_digest)
        document = decode_json(bytes(buffer))
        exact(document, ('schemaVersion', 'kind', 'configurationDigest', 'signing', 'validation'))
        _require(type(document['schemaVersion']) is int and document['schemaVersion'] == 1
            and document['kind'] == 'protected-service-materials'
            and document['configurationDigest'] == prepared.configuration_digest
            and type(document['signing']) is list and 1 <= len(document['signing']) <= 128
            and type(document['validation']) is list and 1 <= len(document['validation']) <= 128)
        profiles = {row.profile_id: row for row in prepared.mobile._profiles}
        definitions = {row.profile_id: row.signing for row in prepared.build_signing._profiles}
        observers = dict(prepared.validation)
        _require(set(profiles) == set(definitions) == set(observers)
            and all(type(value) in (AndroidSigningDefinitionInputs, IOSSigningDefinitionInputs)
                    for value in definitions.values())
            and len({value.identity.reference_id for value in definitions.values()}) == len(definitions))
        platforms = {('ios' if type(value) is IOSSigningDefinitionInputs else 'android')
                     for value in definitions.values()}
        _require(len(platforms) == 1)
        platform = next(iter(platforms))
        expected_validation = {(profile_id, reference, provider)
            for profile_id, inputs in observers.items()
            for reference, provider in inputs.authentication_references()}
        signing_rows = {}
        validation_rows = {}
        # Validate the entire selection before touching even key file metadata.
        for row in document['signing']:
            if platform == 'android':
                exact(row, ('profileId', 'keystorePath', 'keyAlias', 'storePasswordB64'), ('keyPasswordB64',))
            else:
                exact(row, ('profileId', 'pkcs12Path', 'passwordB64'))
            identifier = row['profileId']
            _require(type(identifier) is str and identifier in profiles and identifier not in signing_rows)
            path = _material_path(row['keystorePath'] if platform == 'android' else row['pkcs12Path'])
            if platform == 'android':
                _require(path.suffix.lower() in ('.p12', '.pfx', '.jks', '.keystore')
                    and type(row['keyAlias']) is str and 0 < len(row['keyAlias']) <= 128)
            else:
                _require(path.suffix.lower() in ('.p12', '.pfx'))
            signing_rows[identifier] = (row, path)
        for row in document['validation']:
            exact(row, ('profileId', 'authenticationReferenceId', 'providerId', 'secretB64'))
            for name in ('profileId', 'authenticationReferenceId', 'providerId'):
                contracts.validate_id(row[name])
            key = (row['profileId'], row['authenticationReferenceId'], row['providerId'])
            _require(key in expected_validation and key not in validation_rows)
            validation_rows[key] = row
        _require(set(signing_rows) == set(profiles) and set(validation_rows) == expected_validation)
        planned_signing = []
        planned_validation = []
        for identifier, (row, path) in signing_rows.items():
            if platform == 'android':
                password = _secret(row['storePasswordB64'], 1, 1024, buffers)
                key_password = _secret(row['keyPasswordB64'], 1, 1024, buffers) if 'keyPasswordB64' in row else None
                for selected in (password, key_password):
                    if selected is not None:
                        _require(not any(byte in selected for byte in (0, 10, 13)))
                planned_signing.append((definitions[identifier].identity, path, row['keyAlias'], password, key_password))
            else:
                password = _secret(row['passwordB64'], 1, 512, buffers)
                _require(not any(byte in password for byte in (0, 10, 13)))
                planned_signing.append((definitions[identifier].identity, path, password))
        for key, row in validation_rows.items():
            planned_validation.append((key, _secret(row['secretB64'], 32, 64, buffers)))
        signing = AndroidSigningMaterialResolver() if platform == 'android' else IOSSigningMaterialResolver()
        validation = ValidationSecretRegistry()
        for selected in planned_signing:
            if platform == 'android':
                identity, path, alias, password, key_password = selected
                signing.register(identity, keystore=path, key_alias=alias,
                                 store_password=password, key_password=key_password)
            else:
                identity, path, password = selected
                signing.register(identity, pkcs12=path, password=bytes(password))
        for (profile_id, reference, provider), secret in planned_validation:
            validation.register(reference, project_digest=profiles[profile_id].config.registration.project_digest,
                                provider_id=provider, secret=secret)
        return ProtectedServiceMaterials(prepared.configuration_digest, signing, validation,
                                         len(signing_rows), len(validation_rows))
    except BaseException as error:
        if signing is not None:
            signing.close()
        if validation is not None:
            validation.close()
        if not isinstance(error, Exception):
            raise
        raise ProtectedServiceMaterialsError() from None
    finally:
        for value in buffers:
            value[:] = b'\0'*len(value)
        if type(buffer) is bytearray:
            buffer[:] = b'\0'*len(buffer)
        if type(document) is dict:
            document.clear()


def read_service_materials(prepared, stream):
    """Read only the explicitly supplied binary stream; never load environment or files."""
    buffer = None
    try:
        _require(type(prepared) is PreparedProtectedServiceInputs and callable(getattr(stream, 'read', None)))
        value = stream.read(MAX_MATERIAL_INPUT_BYTES+1)
        _require(type(value) is bytes)
        buffer = bytearray(value)
        return bind_service_materials(prepared, buffer)
    except (OSError, RuntimeError, ValueError, TypeError):
        raise ProtectedServiceMaterialsError() from None
    finally:
        if buffer is not None:
            buffer[:] = b'\0'*len(buffer)


def compose_service_from_material_stream(configuration, issue_configuration, runtime_bundle, *,
                                         owner, mobile_qualifications, stream):
    """Bind explicit startup material only after current qualification checks."""
    from .protected_service import (load_protected_service_inputs,
        compose_android_protected_service, compose_ios_protected_service)
    from .repair_composition import ProtectedRepairComposition
    bindings = None
    try:
        _require(type(owner) is ProtectedRepairComposition and type(mobile_qualifications) is dict)
        with owner._lock:
            _require(not owner._closed and owner._owner is None and not owner._registrations
                and not owner._building and not owner._build_reports and not owner._android_signing
                and not owner._ios_signing and not owner._android_mobile
                and not owner._ios_mobile)
        prepared = load_protected_service_inputs(configuration, issue_configuration, runtime_bundle)
        rows = configuration.document['profiles']
        platforms = {row['platform'] for row in rows}
        _require(len(platforms) == 1 and platforms <= {'android', 'ios'})
        _require(set(mobile_qualifications) == {row['id'] for row in rows})
        for row in rows:
            route = row['mobile']['route']
            owner.authority.require_qualification(mobile_qualifications[row['id']],
                backend_id=route['backendId'], execution_class='mobile-device',
                environment_digest=route['environmentDigest'], signing_policy_id=row['signing']['policy']['id'],
                evaluated_at_ms=int(time.time()*1000))
        bindings = read_service_materials(prepared, stream)
        current = load_protected_service_inputs(configuration, issue_configuration, runtime_bundle)
        _require(current.public() == prepared.public() and bindings.configuration_digest == configuration.definition_digest)
        compose = (compose_ios_protected_service if platforms == {'ios'}
                   else compose_android_protected_service)
        return compose(configuration, issue_configuration, runtime_bundle,
            owner=owner, signing_materials=bindings.signing, validation_secrets=bindings.validation,
            mobile_qualifications=mobile_qualifications)
    except BaseException as error:
        if bindings is not None:
            bindings.close()
        if not isinstance(error, Exception):
            raise
        raise ProtectedServiceMaterialsError() from None
