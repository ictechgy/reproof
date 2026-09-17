"""Assemble fixed protected mobile service chains using current capabilities."""
from dataclasses import dataclass, field
import io
import json
import plistlib
import time
import zipfile

from . import contracts
from .execution.artifacts import ArtifactValidationAuthority, BlobSet
from .execution.journal import RunStore
from .execution.wire import canonical
from .protected_build_signing_inputs import load_protected_build_signing_inputs
from .protected_mobile_inputs import load_protected_mobile_inputs
from .protected_signing_inputs import AndroidSigningDefinitionInputs, IOSSigningDefinitionInputs
from .protected_validation import ValidationSecretRegistry
from .protected_validation_inputs import load_android_validation_inputs, load_ios_validation_inputs
from .repair_android_signing import AndroidSigningMaterialResolver, _structural_apk
from .ios_signing_inputs import IOSSigningMaterialResolver
from .repair_composition import ProtectedRepairComposition
from .repair_verification import ProtectedRepairExecutor


class ProtectedServiceAssemblyError(RuntimeError):
    def __init__(self, code='protected_service_assembly'):
        self.code = code
        super().__init__(code)


def _require(value):
    if not value:
        raise ProtectedServiceAssemblyError()


@dataclass(frozen=True, slots=True)
class PreparedProtectedServiceInputs:
    configuration_digest: str
    build_signing: object = field(repr=False)
    mobile: object = field(repr=False)
    validation: tuple = field(repr=False)

    def public(self):
        return {'schemaVersion': 1, 'kind': 'protected-service-inputs', 'executionAuthority': 'none',
            'configurationDigest': self.configuration_digest,
            'buildSigning': self.build_signing.public(), 'mobile': self.mobile.public(),
            'validation': [{'profileId': identifier, 'definitionDigest': inputs.definition_digest}
                           for identifier, inputs in self.validation]}


def load_protected_service_inputs(configuration, issue_configuration, runtime_bundle):
    """Read fixed inputs without reading keys, probing a VM or claiming a device."""
    try:
        build = load_protected_build_signing_inputs(configuration, issue_configuration, runtime_bundle)
        mobile = load_protected_mobile_inputs(configuration, issue_configuration, runtime_bundle)
        validation = tuple((row['id'], (load_android_validation_inputs if row['platform']=='android'
            else load_ios_validation_inputs)(row['validation']['observers'],
            plan=row['validation']['plan'], mobile_inputs=mobile.profile(row['id'])))
            for row in configuration.document['profiles'])
        _require(build.configuration_digest == mobile.configuration_digest == configuration.definition_digest)
        configuration.validate_issue_configuration(issue_configuration)
        configuration.validate_runtime(runtime_bundle)
        return PreparedProtectedServiceInputs(configuration.definition_digest, build, mobile, validation)
    except (contracts.ContractError, OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError):
        raise ProtectedServiceAssemblyError() from None


def _journal(value):
    return RunStore(value['root'], environment_digest=value['environmentDigest'], disk_limit=value['diskBudgetBytes'])


def _unsigned_authority(policy_id, maximum):
    def check(blobs):
        return (type(blobs) is BlobSet and len(blobs.entries) == 1
                and blobs.entries[0][0] == 'candidate.apk' and _structural_apk(blobs.entries[0][1], maximum))
    authority = ArtifactValidationAuthority()
    authority.register(policy_id, paths=('candidate.apk',), max_bytes=maximum, checker=check)
    return authority


def _structural_ipa(body, maximum):
    """Check the fixed unsigned IPA container without extracting or executing it."""
    if type(body) is not bytes or not 22 <= len(body) <= maximum:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(body), 'r') as archive:
            entries=archive.infolist()
            if not 1 <= len(entries) <= 100000:
                return False
            names=set();files=set();total=0;apps=set()
            for entry in entries:
                name=entry.filename
                parts=name.rstrip('/').split('/')
                if (type(name) is not str or not name or name.startswith('/') or '\\' in name
                        or '\x00' in name or any(part in {'','.','..'} for part in parts)
                        or name.casefold() in names or entry.flag_bits & 0x1):
                    return False
                names.add(name.casefold())
                if len(parts)>=2 and parts[0]=='Payload' and parts[1].endswith('.app'):
                    apps.add('Payload/'+parts[1])
                if entry.is_dir():
                    continue
                if not 0 <= entry.compress_size <= maximum or not 0 <= entry.file_size <= maximum:
                    return False
                if entry.compress_size == 0 and entry.file_size > 0:
                    return False
                if entry.compress_size and entry.file_size > entry.compress_size * 1000:
                    return False
                total += entry.file_size
                if total > 4 * maximum:
                    return False
                files.add(name)
            if len(apps) != 1:
                return False
            root=next(iter(apps));info_name=root+'/Info.plist'
            if info_name not in files:
                return False
            info_entry=archive.getinfo(info_name)
            if info_entry.file_size > 4 * 1024 * 1024:
                return False
            info=plistlib.loads(archive.read(info_name))
            if (type(info) is not dict or info.get('CFBundlePackageType') != 'APPL'
                    or type(info.get('CFBundleExecutable')) is not str
                    or not info['CFBundleExecutable'] or '/' in info['CFBundleExecutable']):
                return False
            executable=root+'/'+info['CFBundleExecutable']
            return executable in files
    except (OSError, ValueError, TypeError, zipfile.BadZipFile, zipfile.LargeZipFile,
            plistlib.InvalidFileException, KeyError):
        return False


def _unsigned_ipa_authority(policy_id, maximum):
    authority = ArtifactValidationAuthority()
    authority.register(policy_id, paths=('candidate.ipa',), max_bytes=maximum,
                       checker=lambda blobs: (type(blobs) is BlobSet and len(blobs.entries) == 1
                           and blobs.entries[0][0] == 'candidate.ipa'
                           and _structural_ipa(blobs.entries[0][1], maximum)))
    return authority


def _require_ios_material_registered(resolver, identity):
    """Check registration metadata without opening the PKCS#12 file."""
    _require(type(resolver) is IOSSigningMaterialResolver)
    try:
        with resolver._lock:
            row = resolver._materials.get(identity.reference_id)
            _require(not resolver._closed and row is not None and row[0] == identity)
    except (AttributeError, TypeError, KeyError):
        raise ProtectedServiceAssemblyError() from None


def compose_android_protected_service(configuration, issue_configuration, runtime_bundle, *, owner,
                                      signing_materials, validation_secrets, mobile_qualifications):
    """Attach real factories; callers supply live qualifications and scoped registries.

    No serialized qualification or secret source is accepted here. VM probes
    run in this composition's authority. A failed partial assembly is revoked
    and collected; the caller retains ``owner`` to retry an incomplete close.
    """
    _require(type(owner) is ProtectedRepairComposition
        and type(signing_materials) is AndroidSigningMaterialResolver
        and type(validation_secrets) is ValidationSecretRegistry
        and type(mobile_qualifications) is dict)
    created = False
    stage = 'inputs'
    try:
        issue = json.loads(canonical(issue_configuration))
        prepared = load_protected_service_inputs(configuration, issue, runtime_bundle)
        stage = 'capabilities'
        rows = configuration.document['profiles']
        _require(all(row['platform']=='android' for row in rows)
            and set(mobile_qualifications) == {row['id'] for row in rows})
        with owner._lock:
            _require(not owner._closed and owner._owner is None and not owner._registrations
                and not owner._building and not owner._build_reports and not owner._android_signing
                and not owner._ios_signing and not owner._android_mobile)
        validators = dict(prepared.validation)
        def preflight():
            configuration.validate_issue_configuration(issue)
            configuration.validate_runtime(runtime_bundle)
            for row in rows:
                profile = prepared.mobile.profile(row['id'])
                signing = prepared.build_signing.profile(row['id']).signing
                _require(type(signing) is AndroidSigningDefinitionInputs
                    and profile.config.native_guardian is not None and profile.config.adb_endpoint is not None
                    and not profile.config.lab.devices[profile.config.device_id].get('_recoveryOnly', False)
                    and signing.identity.application_id == profile.config.application_id
                    and signing.identity.package_name == profile.config.package)
                route = row['mobile']['route']
                owner.authority.require_qualification(mobile_qualifications[row['id']], backend_id=route['backendId'],
                    execution_class='mobile-device', environment_digest=route['environmentDigest'],
                    signing_policy_id=row['signing']['policy']['id'], evaluated_at_ms=int(time.time()*1000))
                signing_materials.require_registered(signing.identity)
                validators[row['id']].require_secrets(validation_secrets, owner)
                current = load_android_validation_inputs(row['validation']['observers'],
                    plan=row['validation']['plan'], mobile_inputs=profile)
                _require(current.definition_digest == validators[row['id']].definition_digest)
        preflight()
        created = True
        for row in rows:
            preflight()
            build = prepared.build_signing.profile(row['id'])
            mobile = prepared.mobile.profile(row['id'])
            recipe = build.tools.build_bundle.recipe(row['build']['route']['recipeId'])
            stage = 'build'
            builder = owner.qualify_build(bundle=build.tools.build_bundle, store=_journal(row['build']['journal']),
                route=row['build']['route'], validation_plan=row['validation']['plan'],
                artifact_authority=_unsigned_authority(row['build']['route']['artifactPolicyId'], recipe['maxOutputBytes']),
                application_id=row['applicationId'])
            preflight()
            stage = 'signing'
            signer = owner.configure_android_signing_owner(builder, tools=build.tools.signing_tools,
                material_resolver=signing_materials, identity=build.signing.identity,
                policy_document=row['signing']['policy'], store=_journal(row['signing']['journal']),
                work_root=row['signing']['ownerRoot'], artifact_policy_id=row['mobile']['route']['artifactPolicyId'])
            preflight()
            stage = 'mobile'
            supervisor = owner.configure_android_mobile(config=mobile.config, signer=signer,
                qualification=mobile_qualifications[row['id']], route_document=row['mobile']['route'],
                store=_journal(row['mobile']['journal']), work_root=row['mobile']['ownerRoot'],
                adapter_id=row['mobile']['route']['backendId'], validation_inputs=validators[row['id']],
                validation_secrets=validation_secrets)
            executor = ProtectedRepairExecutor(builder, signer, supervisor)
            executor.ready(project_digest=row['projectDigest'], build_recipe_id=row['build']['route']['recipeId'],
                validation_recipe_ids=[item['recipeId'] for item in row['validation']['plan']['checks']],
                runtime_policy_digest=row['runtimePolicyDigest'], application_id=row['applicationId'])
            owner.register(row['id'], executor)
        stage = 'recheck'
        prepared.build_signing.verify(configuration, issue, runtime_bundle)
        prepared.mobile.verify(configuration, issue, runtime_bundle)
        preflight()
        from .live.issue_configuration import compose_issue_repairs
        stage = 'attach'
        compose_issue_repairs(runtime_bundle, issue, protected_repairs=owner)
        _require(runtime_bundle.protected_repairs is owner and runtime_bundle.workflow.repairs is not None)
        return owner
    except BaseException as error:
        if created:
            try:
                owner.close()
            except Exception:
                raise ProtectedServiceAssemblyError('protected_service_cleanup_unknown') from None
        if not isinstance(error, Exception):
            raise
        raise ProtectedServiceAssemblyError('protected_service_'+stage) from None


def compose_android_protected_workflow(lab, access, configuration, issue_configuration, *, root, owner,
                                       signing_materials, validation_secrets, mobile_qualifications,
                                       video_helper=None, media_helper=None):
    """Create the service runtime and attach only its current qualified chain."""
    from .live.issue_configuration import compose_issue_workflow
    bundle = compose_issue_workflow(lab, access, issue_configuration, root=root,
        video_helper=video_helper, media_helper=media_helper, defer_repairs=True)
    try:
        compose_android_protected_service(configuration, issue_configuration, bundle, owner=owner,
            signing_materials=signing_materials, validation_secrets=validation_secrets,
            mobile_qualifications=mobile_qualifications)
        return bundle
    except BaseException:
        bundle.close()
        raise


def compose_ios_protected_service(configuration, issue_configuration, runtime_bundle, *, owner,
                                  signing_materials, validation_secrets, mobile_qualifications):
    """Assemble the fixed iOS build, signing and physical-device chain."""
    _require(type(owner) is ProtectedRepairComposition
        and type(signing_materials) is IOSSigningMaterialResolver
        and type(validation_secrets) is ValidationSecretRegistry
        and type(mobile_qualifications) is dict)
    created=False;stage='inputs'
    try:
        issue=json.loads(canonical(issue_configuration))
        prepared=load_protected_service_inputs(configuration,issue,runtime_bundle)
        stage='capabilities';rows=configuration.document['profiles']
        _require(all(row['platform']=='ios' for row in rows)
            and set(mobile_qualifications)=={row['id'] for row in rows})
        with owner._lock:
            _require(not owner._closed and owner._owner is None and not owner._registrations
                and not owner._building and not owner._build_reports and not owner._android_signing
                and not owner._ios_signing and not owner._android_mobile and not owner._ios_mobile)
        validators=dict(prepared.validation)

        def preflight():
            configuration.validate_issue_configuration(issue)
            configuration.validate_runtime(runtime_bundle)
            for row in rows:
                profile=prepared.mobile.profile(row['id'])
                signing=prepared.build_signing.profile(row['id']).signing
                _require(type(signing) is IOSSigningDefinitionInputs
                    and profile.config.xctest is not None and profile.config.sanitation is not None
                    and not profile.config.lab.devices[profile.config.device_id].get('_recoveryOnly', False)
                    and signing.identity.application_id == profile.config.application_id)
                route=row['mobile']['route']
                owner.authority.require_qualification(mobile_qualifications[row['id']],
                    backend_id=route['backendId'],execution_class='mobile-device',
                    environment_digest=route['environmentDigest'],
                    signing_policy_id=row['signing']['policy']['id'],evaluated_at_ms=int(time.time()*1000))
                _require_ios_material_registered(signing_materials,signing.identity)
                validators[row['id']].require_secrets(validation_secrets,owner)
                current=load_ios_validation_inputs(row['validation']['observers'],
                    plan=row['validation']['plan'],mobile_inputs=profile)
                _require(current.definition_digest==validators[row['id']].definition_digest)

        preflight();created=True
        for row in rows:
            preflight();build=prepared.build_signing.profile(row['id']);mobile=prepared.mobile.profile(row['id'])
            recipe=build.tools.build_bundle.recipe(row['build']['route']['recipeId'])
            stage='build'
            builder=owner.qualify_build(bundle=build.tools.build_bundle,store=_journal(row['build']['journal']),
                route=row['build']['route'],validation_plan=row['validation']['plan'],
                artifact_authority=_unsigned_ipa_authority(row['build']['route']['artifactPolicyId'],recipe['maxOutputBytes']),
                application_id=row['applicationId'])
            preflight();stage='signing'
            signer=owner.configure_ios_signing_owner(builder,tools=build.tools.signing_tools,
                material_resolver=signing_materials,definition=build.signing.definition,
                provisioning=build.signing.provisioning,policy_document=row['signing']['policy'],
                store=_journal(row['signing']['journal']),work_root=row['signing']['ownerRoot'],
                artifact_policy_id=row['mobile']['route']['artifactPolicyId'])
            preflight();stage='mobile'
            supervisor=owner.configure_ios_mobile(config=mobile.config,signer=signer,
                qualification=mobile_qualifications[row['id']],route_document=row['mobile']['route'],
                store=_journal(row['mobile']['journal']),work_root=row['mobile']['ownerRoot'],
                adapter_id=row['mobile']['route']['backendId'],validation_inputs=validators[row['id']],
                validation_secrets=validation_secrets)
            executor=ProtectedRepairExecutor(builder,signer,supervisor)
            executor.ready(project_digest=row['projectDigest'],build_recipe_id=row['build']['route']['recipeId'],
                validation_recipe_ids=[item['recipeId'] for item in row['validation']['plan']['checks']],
                runtime_policy_digest=row['runtimePolicyDigest'],application_id=row['applicationId'])
            owner.register(row['id'],executor)
        stage='recheck';prepared.build_signing.verify(configuration,issue,runtime_bundle)
        prepared.mobile.verify(configuration,issue,runtime_bundle);preflight()
        from .live.issue_configuration import compose_issue_repairs
        stage='attach';compose_issue_repairs(runtime_bundle,issue,protected_repairs=owner)
        _require(runtime_bundle.protected_repairs is owner and runtime_bundle.workflow.repairs is not None)
        return owner
    except BaseException as error:
        if created:
            try: owner.close()
            except Exception: raise ProtectedServiceAssemblyError('protected_service_cleanup_unknown') from None
        if not isinstance(error,Exception): raise
        raise ProtectedServiceAssemblyError('protected_service_'+stage) from None


def compose_ios_protected_workflow(lab, access, configuration, issue_configuration, *, root, owner,
                                   signing_materials, validation_secrets, mobile_qualifications,
                                   video_helper=None, media_helper=None):
    """Create an issue workflow and attach its current qualified iOS chain."""
    from .live.issue_configuration import compose_issue_workflow
    bundle=compose_issue_workflow(lab,access,issue_configuration,root=root,
        video_helper=video_helper,media_helper=media_helper,defer_repairs=True)
    try:
        compose_ios_protected_service(configuration,issue_configuration,bundle,owner=owner,
            signing_materials=signing_materials,validation_secrets=validation_secrets,
            mobile_qualifications=mobile_qualifications)
        return bundle
    except BaseException:
        bundle.close();raise
