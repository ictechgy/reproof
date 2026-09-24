"""Inert protected service configuration; no VM, device or private input is opened."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reproof import contracts
from tests.test_execution_protocol import build_route


def configuration(root):
    build = build_route()
    mobile = dict(build,id='mobile-route',backendId='registered-device',executionClass='mobile-device',
        environmentDigest='3'*64,inputKind='validated-artifact',recipeId='candidate-replay',
        artifactPolicyId='signed-ios-ipa',cleanupPolicyId='restore-original',platform='ios',
        applicationId='ios_app',signingPolicyId='ios-signing')
    plan = {'schemaVersion':1,'id':build['validationPlanId'],'projectDigest':build['projectDigest'],
        'checks':[{'id':'regression','recipeId':'regression_ui','kind':'external-observation',
                   'evidenceSourceId':'registered-observer'}],'candidateReports':'supplemental-only'}
    def reference(name): return {'path':str(root/(name+'.json')),'sha256':'a'*64}
    def journal(name,environment):
        return {'root':str(root/name),'environmentDigest':environment,'diskBudgetBytes':4*1024**3}
    return {'schemaVersion':1,'kind':'reproof-protected-service','profiles':[{
        'id':'protected-ios','projectId':'checkout','projectDigest':build['projectDigest'],
        'applicationId':'ios_app','originalBuildId':'original','platform':'ios',
        'deviceId':'owned-device','runtimePolicyDigest':'f'*64,
        'build':{'bundlePath':str(root/'vm-bundle'),'route':build,'journal':journal('build-journal','2'*64)},
        'signing':{'toolsPath':str(root/'signing-tools'),'toolsManifestSha256':'b'*64,
            'definition':reference('signing-definition'),'ownerRoot':str(root/'signing-owner'),
            'journal':journal('signing-journal','4'*64),'policy':{'schemaVersion':1,'id':'ios-signing',
                'platform':'ios','applicationId':'ios_app','identityReferenceId':'registered-key',
                'entitlementsDigest':'c'*64,'provisioningReferenceId':'registered-profiles',
                'tool':'host-codesign-fixed','candidateHooks':'forbidden','artifactRelation':'pre-post-digests'}},
        'mobile':{'definition':reference('mobile-definition'),'route':mobile,
            'journal':journal('mobile-journal','3'*64),'ownerRoot':str(root/'mobile-owner')},
        'validation':{'plan':plan,'observers':reference('validation-observers')},
    }]}


def issue_configuration(profile, runtime_policy):
    root=Path(profile['build']['bundlePath']).parent
    return {'schemaVersion':1,'kind':'reproof-issue-runtime','projects':[{
        'projectId':profile['projectId'],'projectDigest':profile['projectDigest'],
        'runtimePolicy':runtime_policy,'validationRecipeIds':['regression_ui'],
        'fixtures':[],'variables':[],'observations':[],
        'repair':{'protectedProfileId':profile['id'],'buildRecipeId':profile['build']['route']['recipeId'],
            'sourceRoot':str(root/'source'),'sourcePaths':['src/App.swift'],'protectedPaths':[],
            'originalArtifactRoot':str(root/'original'),'originalArtifactPaths':['original.ipa'],
            'artifactIdentity':'file-sha256','agent':{'kind':'local-patch','patchFile':str(root/'proposal.patch')}},
    }]}


class ProtectedServiceConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='owned-protected-config-'); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve(); self.path=self.root/'service.json'

    def load(self, value):
        from reproof.repair_configuration import load_protected_service_configuration
        self.path.write_text(json.dumps(value)); self.path.chmod(0o600)
        return load_protected_service_configuration(self.path)

    def test_configuration_is_immutable_and_opens_only_the_selected_public_document(self):
        from reproof.repair_configuration import ProtectedServiceConfiguration
        value=configuration(self.root)
        with (patch('subprocess.Popen',side_effect=AssertionError('configuration dispatched a process')),
              patch('socket.socket',side_effect=AssertionError('configuration opened a socket'))):
            selected=self.load(value)
            self.assertIs(type(selected),ProtectedServiceConfiguration)
            self.assertEqual(selected.definition_digest,contracts.digest(value))
            self.assertEqual(selected.document,value)
        snapshot=selected.document; snapshot['profiles'][0]['applicationId']='changed'
        value['profiles'][0]['build']['route']['recipeId']='changed'
        self.assertEqual(selected.document['profiles'][0]['applicationId'],'ios_app')
        self.assertEqual(set(path.name for path in self.root.iterdir()),{'service.json'})

    def test_executable_secret_qualification_and_duplicate_fields_are_rejected(self):
        from reproof.repair_configuration import ProtectedServiceConfigurationError
        for change in (
            lambda d:d.update(schemaVersion=True),
            lambda d:d.update(command='OwnedCommandCanary'),
            lambda d:d['profiles'][0].update(qualified=True),
            lambda d:d['profiles'][0]['signing'].update(password='OwnedPasswordCanary'),
            lambda d:d['profiles'][0]['validation'].update(callback='owned.module:callback'),
            lambda d:d['profiles'].append(copy.deepcopy(d['profiles'][0])),
        ):
            value=configuration(self.root); change(value)
            with self.subTest(change=change),self.assertRaises(ProtectedServiceConfigurationError) as caught:
                self.load(value)
            self.assertNotIn('Canary',str(caught.exception))
        from reproof.repair_configuration import load_protected_service_configuration
        self.path.write_text('{"schemaVersion":1,"schemaVersion":1}')
        with self.assertRaises(ProtectedServiceConfigurationError): load_protected_service_configuration(self.path)

    def test_all_phase_project_application_environment_policy_and_plan_bindings_must_match(self):
        from reproof.repair_configuration import ProtectedServiceConfigurationError
        changes=(('build','route','projectDigest','9'*64),('mobile','route','applicationId','other_app'),
            ('mobile','route','signingPolicyId','other-policy'),('mobile','route','platform','android'),
            ('build','journal','environmentDigest','9'*64),('mobile','journal','environmentDigest','9'*64),
            ('validation','plan','projectDigest','9'*64),('build','route','validationPlanId','other-plan'),
            ('signing','policy','applicationId','other_app'))
        for section,part,field,value in changes:
            document=configuration(self.root); document['profiles'][0][section][part][field]=value
            with self.subTest(field=(section,part,field)),self.assertRaises(ProtectedServiceConfigurationError):
                self.load(document)

    def test_mutable_namespaces_cannot_alias_each_other_or_public_input_roots(self):
        from reproof.repair_configuration import ProtectedServiceConfigurationError
        for replacement in ('build-journal','build-journal/child','vm-bundle','signing-tools/nested'):
            document=configuration(self.root); document['profiles'][0]['signing']['ownerRoot']=str(self.root/replacement)
            with self.subTest(replacement=replacement),self.assertRaises(ProtectedServiceConfigurationError):
                self.load(document)
        document=configuration(self.root); document['profiles'][0]['mobile']['definition']['path']=str(self.root/'private.env')
        with self.assertRaises(ProtectedServiceConfigurationError): self.load(document)

    def test_issue_selection_cannot_ignore_missing_or_unused_protected_profiles(self):
        from reproof.repair_configuration import ProtectedServiceConfigurationError
        document=configuration(self.root); policy={'owned':'runtime-policy'}
        document['profiles'][0]['runtimePolicyDigest']=contracts.digest(policy)
        selected=self.load(document); issue=issue_configuration(document['profiles'][0],policy)
        selected.validate_issue_configuration(issue)
        for change in (
            lambda d:d['projects'][0]['repair'].update(protectedProfileId='missing'),
            lambda d:d['projects'][0].update(projectDigest='9'*64),
            lambda d:d['projects'][0].update(runtimePolicy={'changed':True}),
            lambda d:d['projects'][0]['repair'].update(buildRecipeId='other-recipe'),
            lambda d:d['projects'][0].update(validationRecipeIds=[]),
            lambda d:d['projects'][0].pop('repair'),
        ):
            changed=copy.deepcopy(issue); change(changed)
            with self.subTest(change=change),self.assertRaises(ProtectedServiceConfigurationError):
                selected.validate_issue_configuration(changed)

    def test_public_cli_checks_both_documents_without_opening_referenced_inputs(self):
        from reproof.cli import main
        from tests.g4_support import runtime_policy
        value=configuration(self.root); policy=runtime_policy()
        value['profiles'][0]['runtimePolicyDigest']=contracts.digest(policy)
        selected=self.load(value)
        issue_path=self.root/'issue.json'
        issue_path.write_text(json.dumps(issue_configuration(value['profiles'][0],policy)))
        output=io.StringIO()
        with (redirect_stdout(output),patch('subprocess.Popen',side_effect=AssertionError('unexpected process')),
              patch('socket.socket',side_effect=AssertionError('unexpected socket'))):
            code=main(['protected-service','check-config','--config',str(self.path),'--issue-config',str(issue_path)])
        report=json.loads(output.getvalue())
        self.assertEqual(code,0)
        self.assertEqual(report['status'],'configuration-validated')
        self.assertEqual(report['executionAuthority'],'none')
        self.assertEqual(report['configurationDigest'],selected.definition_digest)
        self.assertEqual(report['profileIds'],['protected-ios'])
        self.assertEqual(set(path.name for path in self.root.iterdir()),{'service.json','issue.json'})
        self.assertNotIn(str(self.root),output.getvalue())

    def test_public_cli_rejects_mismatched_issue_document_without_echoing_inputs(self):
        from reproof.cli import main
        value=configuration(self.root); self.load(value)
        issue_path=self.root/'issue.json'; issue_path.write_text('{"OwnedInputCanary":true}')
        output=io.StringIO()
        with redirect_stdout(output):
            code=main(['protected-service','check-config','--config',str(self.path),'--issue-config',str(issue_path)])
        report=json.loads(output.getvalue())
        self.assertEqual(code,2); self.assertEqual(report['status'],'rejected')
        self.assertNotIn('OwnedInputCanary',output.getvalue())
        self.assertNotIn(str(self.root),output.getvalue())


class ProtectedRuntimeConfigurationTests(unittest.TestCase):
    def setUp(self):
        from reproof.live.access import AccessController, AccessStore
        from reproof.live.issue_configuration import compose_issue_workflow
        from reproof.repair_configuration import ProtectedServiceConfiguration
        from tests.g4_support import G4Environment, runtime_policy
        from tests.test_issue_configuration import configuration as issue_document
        self.env=G4Environment(); self.addCleanup(self.env.close)
        store=AccessStore(self.env.root/'configured-access'); self.addCleanup(store.close)
        store.bootstrap_administrator('admin'); store.register_project('admin',self.env.project)
        store.assign_device('admin','device',project_id='checkout')
        self.access=AccessController(store); self.access.bind_project(self.env.registration)
        self.issue=issue_document(self.env)
        self.bundle=compose_issue_workflow(self.env.lab,self.access,self.issue,
            root=self.env.root/'configured-issues',defer_repairs=True)
        self.addCleanup(self.bundle.close)
        self.value=configuration(self.env.root.resolve())
        row=self.value['profiles'][0]
        row.update(projectDigest=self.env.registration.project_digest,deviceId='device',
                   runtimePolicyDigest=contracts.digest(runtime_policy()))
        for phase in ('build','mobile'): row[phase]['route']['projectDigest']=row['projectDigest']
        row['validation']['plan']['projectDigest']=row['projectDigest']
        self.selected=ProtectedServiceConfiguration(self.value)

    def test_actual_issue_runtime_preflight_dispatches_no_device_or_protected_process(self):
        with (patch('subprocess.Popen',side_effect=AssertionError('preflight dispatched a process')),
              patch('socket.socket',side_effect=AssertionError('preflight opened a socket'))):
            self.selected.validate_runtime(self.bundle)
        self.assertIsNone(self.bundle.protected_repairs)
        self.assertIsNone(self.bundle.workflow.repairs)
        self.assertEqual(self.env.control['calls'],[])

    def test_changed_project_runtime_policy_recipe_and_device_selection_are_rejected(self):
        from reproof.repair_configuration import ProtectedServiceConfiguration, ProtectedServiceConfigurationError
        for field,value in (('projectId','missing'),('projectDigest','9'*64),
                            ('runtimePolicyDigest','9'*64),('applicationId','missing-app'),
                            ('originalBuildId','candidate'),('deviceId','missing-device')):
            changed=copy.deepcopy(self.value); row=changed['profiles'][0]; row[field]=value
            if field=='projectDigest':
                for phase in ('build','mobile'): row[phase]['route']['projectDigest']=value
                row['validation']['plan']['projectDigest']=value
            if field=='applicationId':
                row['signing']['policy'][field]=value; row['mobile']['route'][field]=value
            selected=ProtectedServiceConfiguration(changed)
            with self.subTest(field=field),self.assertRaises(ProtectedServiceConfigurationError):
                selected.validate_runtime(self.bundle)
        changed=copy.deepcopy(self.value)
        changed['profiles'][0]['validation']['plan']['checks'][0]['recipeId']='unregistered'
        with self.assertRaises(ProtectedServiceConfigurationError):
            ProtectedServiceConfiguration(changed).validate_runtime(self.bundle)

    def test_same_platform_wrong_installed_app_and_unassigned_device_are_rejected(self):
        from reproof.repair_configuration import ProtectedServiceConfigurationError
        identity=self.bundle.workflow.lab.devices['device']['capabilities']['applicationIdentity']
        for field,value in (('bundle','com.other.app'),('artifactDigest','9'*64)):
            with self.subTest(field=field),patch.dict(identity,{field:value}),self.assertRaises(ProtectedServiceConfigurationError):
                self.selected.validate_runtime(self.bundle)
        with patch.object(self.access.store,'assignment_project_ids',return_value=[]), \
                self.assertRaises(ProtectedServiceConfigurationError):
            self.selected.validate_runtime(self.bundle)

    def test_closed_service_or_different_service_registration_cannot_be_adopted(self):
        from reproof.repair_configuration import ProtectedServiceConfigurationError
        with patch.object(self.access,'registration',return_value=object()), \
                self.assertRaises(ProtectedServiceConfigurationError):
            self.selected.validate_runtime(self.bundle)
        self.bundle.close()
        with self.assertRaises(ProtectedServiceConfigurationError): self.selected.validate_runtime(self.bundle)
