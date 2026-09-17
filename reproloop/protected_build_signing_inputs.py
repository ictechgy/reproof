"""Prepare fixed build/signing inputs for a preflighted service, without owners."""
from __future__ import annotations

from dataclasses import dataclass,field

from . import contracts
from .ios_provisioning_cms import MAX_RETAINED_PROFILE_BYTES
from .protected_signing_inputs import (AndroidSigningDefinitionInputs,IOSSigningDefinitionInputs,
                                      load_signing_definition)
from .protected_tool_inputs import ProtectedToolProfile,load_protected_tool_inputs


class ProtectedBuildSigningInputsError(RuntimeError):
    def __init__(self):
        self.code='protected_build_signing_inputs'
        super().__init__('Protected build/signing inputs or service binding changed')


@dataclass(frozen=True,slots=True)
class ProtectedBuildSigningProfile:
    tools: ProtectedToolProfile = field(repr=False)
    signing: AndroidSigningDefinitionInputs | IOSSigningDefinitionInputs = field(repr=False)

    @property
    def profile_id(self): return self.tools.profile_id

    def public(self):
        return {**self.tools.public(),'signingDefinitionDigest':self.signing.definition_digest}


@dataclass(frozen=True,slots=True)
class PreparedBuildSigningInputs:
    configuration_digest: str
    _profiles: tuple[ProtectedBuildSigningProfile,...] = field(repr=False)

    def profile(self, identifier):
        for row in self._profiles:
            if row.profile_id==identifier: return row
        raise ProtectedBuildSigningInputsError()

    def public(self):
        return {'schemaVersion':1,'kind':'protected-build-signing-inputs','executionAuthority':'none',
            'configurationDigest':self.configuration_digest,'profiles':[row.public() for row in self._profiles]}

    def verify(self, configuration, issue_configuration, runtime_bundle):
        current=load_protected_build_signing_inputs(configuration,issue_configuration,runtime_bundle)
        if current.public()!=self.public(): raise ProtectedBuildSigningInputsError()


def load_protected_build_signing_inputs(configuration, issue_configuration, runtime_bundle):
    """Read bounded fixed inputs; defer material, validation/mobile setup and execution."""
    try:
        tools=load_protected_tool_inputs(configuration,issue_configuration,runtime_bundle)
        profiles=[];remaining=MAX_RETAINED_PROFILE_BYTES
        for row in configuration.document['profiles']:
            runtime=runtime_bundle.workflow.runtimes[row['projectId']]
            application=next(item for item in runtime.registration.project['applications']
                             if item['id']==row['applicationId'])
            signing=load_signing_definition(row['signing']['definition'],policy_document=row['signing']['policy'],
                application=application,profile_bytes_limit=remaining)
            if type(signing) is IOSSigningDefinitionInputs:
                remaining-=signing.definition.profile_bytes_total
            profiles.append(ProtectedBuildSigningProfile(tools.profile(row['id']),signing))
        configuration.validate_issue_configuration(issue_configuration)
        configuration.validate_runtime(runtime_bundle)
        if contracts.digest(configuration.document)!=tools.configuration_digest:
            raise ProtectedBuildSigningInputsError()
        return PreparedBuildSigningInputs(tools.configuration_digest,tuple(profiles))
    except (contracts.ContractError,OSError,RuntimeError,TypeError,ValueError,KeyError,AttributeError,StopIteration):
        raise ProtectedBuildSigningInputsError() from None


__all__=['ProtectedBuildSigningInputsError','ProtectedBuildSigningProfile','PreparedBuildSigningInputs',
         'load_protected_build_signing_inputs']
