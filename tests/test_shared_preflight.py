"""Public deployment validation must not initialize any operational state."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from reproof import contracts
from reproof.cli import main as cli_main
from reproof.live.preflight import check_configuration, main
from tests.g4_support import runtime_policy
from tests.test_fixture_allocations import project_document, collection_policy


class SharedPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = project_document()
        self.project_path = self.root / 'project.json'
        self.policy_path = self.root / 'policy.json'
        self.shared_path = self.root / 'shared.json'
        self.issue_path = self.root / 'issue.json'
        self.output = self.root / 'output-not-created'
        self.shared = {'schemaVersion':2, 'kind':'reproof-shared-coordinator',
            'stateRoot':str(self.root / 'coordinator-v2'),
            'listen':{'host':'127.0.0.1','port':9443,'origin':'http://127.0.0.1:9443',
                      'tlsCertificateFile':None,'tlsPrivateKeyFile':None},
            'projects':[{'projectFile':str(self.project_path),'collectionPolicyFile':str(self.policy_path)}],
            'browserSessionSeconds':900}
        self.issue = {'schemaVersion':1,'kind':'reproof-issue-runtime','projects':[{
            'projectId':'checkout','projectDigest':contracts.digest(self.project),
            'runtimePolicy':runtime_policy(),'validationRecipeIds':['regression_ui'],
            'fixtures':[], 'variables':[{'variableId':'secret_text','environment':'DONT_READ_ME'}],
            'observations':[{'observationId':'screen','providerIncarnation':'test_adapter',
                'baseUrl':'http://127.0.0.1:12345','coverage':['snapshot']}]}]}
        self.write(self.project_path,self.project)
        self.write(self.policy_path,collection_policy())
        self.write(self.shared_path,self.shared)
        self.write(self.issue_path,self.issue)

    def write(self,path,value): path.write_text(json.dumps(value))

    def snapshot(self):
        return {str(p.relative_to(self.root)):(p.stat().st_mode,p.read_bytes() if p.is_file() else None)
                for p in self.root.rglob('*')}

    def run_check(self, *, issue=True):
        output = io.StringIO()
        args = ['--shared-config',str(self.shared_path),'--output',str(self.output)]
        if issue: args += ['--issue-config',str(self.issue_path)]
        with contextlib.redirect_stdout(output): code = main(args)
        return code,json.loads(output.getvalue())

    def test_success_is_read_only_and_has_no_execution_authority(self):
        before=self.snapshot()
        with contextlib.ExitStack() as stack:
            for target in ('reproof.live.configuration.AccessStore', 'reproof.live.model.Lab',
                           'socket.socket', 'subprocess.Popen',
                           'reproof.live.configuration.create_server_ssl_context',
                           'reproof.live.issue_configuration.EnvironmentVariableResolver.resolve'):
                stack.enter_context(mock.patch(target,side_effect=AssertionError('forbidden effect')))
            stack.enter_context(mock.patch('sys.stdin',mock.Mock(read=mock.Mock(side_effect=AssertionError('credential read')))))
            code,report=self.run_check()
        self.assertEqual(code,0)
        self.assertEqual(report['executionAuthority'],'none')
        self.assertEqual(report['status'],'configuration-validated')
        self.assertIn('durable-authorization',report['notChecked'])
        self.assertEqual(self.snapshot(),before)
        self.assertNotIn(str(self.root),json.dumps(report))
        self.assertNotIn('DONT_READ_ME',json.dumps(report))

    def test_tls_declarations_are_checked_without_reading_private_key(self):
        self.shared['listen'].update(origin='https://127.0.0.1:9443',
            tlsCertificateFile='/never-read/cert.pem',tlsPrivateKeyFile='/never-read/private.key')
        self.write(self.shared_path,self.shared)
        before=self.snapshot()
        with mock.patch('ssl.SSLContext.load_cert_chain',side_effect=AssertionError('key read')):
            code,report=self.run_check()
        self.assertEqual(code,0)
        self.assertIn('tls-key-validity',report['notChecked'])
        self.assertEqual(self.snapshot(),before)

    def test_invalid_public_documents_reject_without_changes_or_echo(self):
        for path,payload in ((self.project_path,b'{"secret":"private-value"'),
                             (self.policy_path,b'[]'),(self.shared_path,b'\xff')):
            original=path.read_bytes();path.write_bytes(payload)
            before=self.snapshot()
            code,report=self.run_check()
            self.assertEqual(code,2)
            self.assertEqual(report['status'],'rejected')
            self.assertNotIn('private-value',json.dumps(report))
            self.assertEqual(self.snapshot(),before)
            path.write_bytes(original)

    def test_duplicate_project_revisions_and_namespace_overlap_reject(self):
        extra=self.root/'project-copy.json';self.write(extra,self.project)
        self.shared['projects'].append({'projectFile':str(extra),'collectionPolicyFile':str(self.policy_path)})
        self.write(self.shared_path,self.shared)
        self.assertEqual(self.run_check()[0],2)
        self.shared['projects'].pop();self.write(self.shared_path,self.shared)
        for output in (self.root/'coordinator-v2',self.root,self.root/'coordinator-v2'/'child'):
            self.output=output
            before=self.snapshot()
            self.assertEqual(self.run_check()[0],2)
            self.assertEqual(self.snapshot(),before)

    def test_unknown_or_stale_issue_bindings_reject(self):
        for field,value in (('projectDigest','0'*64),('validationRecipeIds',['missing']),
                            ('variables',[{'variableId':'missing','environment':'DONT_READ_ME'}]),
                            ('observations',[{'observationId':'missing','providerIncarnation':'test_adapter',
                                'baseUrl':'http://127.0.0.1:12345','coverage':['snapshot']}])):
            issue=copy.deepcopy(self.issue);issue['projects'][0][field]=value
            self.write(self.issue_path,issue)
            before=self.snapshot()
            self.assertEqual(self.run_check()[0],2)
            self.assertEqual(self.snapshot(),before)

    def test_malformed_issue_endpoint_rejects_without_traceback(self):
        self.issue['projects'][0]['observations'][0]['baseUrl']='http://[invalid'
        self.write(self.issue_path,self.issue)
        self.assertEqual(self.run_check()[0],2)

    def test_io_errors_and_interrupts_are_distinct(self):
        for error,code,status in ((PermissionError('private path'),2,'rejected'),
                                  (KeyboardInterrupt(),130,'interrupted')):
            with mock.patch('reproof.live.preflight.load_shared_configuration',side_effect=error):
                actual,report=self.run_check()
            self.assertEqual((actual,report['status']),(code,status))
            self.assertNotIn('private path',json.dumps(report))
        with mock.patch('reproof.live.preflight.load_shared_configuration',side_effect=RuntimeError('bug')):
            with self.assertRaises(RuntimeError): self.run_check()

    def test_oversized_public_file_rejects_without_loading(self):
        self.project_path.write_bytes(b' '*(1024*1024+1))
        self.assertEqual(self.run_check()[0],2)

    def test_shared_only_check_and_cli_dispatch_are_explicit(self):
        code,report=self.run_check(issue=False)
        self.assertEqual(code,0)
        self.assertFalse(report['issueConfigurationChecked'])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code=cli_main(['live-check-config','--shared-config',str(self.shared_path)])
        self.assertEqual(code,0)
        self.assertFalse(json.loads(output.getvalue())['outputNamespaceChecked'])

    def test_fixture_plan_references_and_duplicate_adapters_are_checked(self):
        fixture={'applicationId':'ios_app','fixtureId':'seed_account','endpointId':'fixture_service',
                 'baseUrl':'http://127.0.0.1:12345','checkRecipeIds':['check_account'],
                 'cleanupRecipeId':'cleanup_account','payload':{}}
        self.issue['projects'][0]['fixtures']=[fixture]
        self.write(self.issue_path,self.issue)
        self.assertEqual(self.run_check()[0],0)
        for field,value in (('applicationId','missing'),('cleanupRecipeId','seed_account'),
                            ('checkRecipeIds',['missing']),('endpointId','wrong')):
            altered=copy.deepcopy(self.issue);altered['projects'][0]['fixtures'][0][field]=value
            self.write(self.issue_path,altered)
            self.assertEqual(self.run_check()[0],2)
        duplicate=copy.deepcopy(self.issue)
        duplicate['projects'][0]['variables']*=2
        self.write(self.issue_path,duplicate)
        self.assertEqual(self.run_check()[0],2)

    def test_reads_only_selected_public_files(self):
        allowed={self.shared_path,self.issue_path,self.project_path,self.policy_path}
        original=Path.open
        def guarded(path,*args,**kwargs):
            self.assertIn(path,allowed)
            self.assertNotIn('w',args[0] if args else kwargs.get('mode','r'))
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'open',guarded):
            self.assertEqual(self.run_check()[0],0)

    def test_repair_public_bindings_checked_without_source_or_patch_reads(self):
        self.project['recipes'].append({'id':'build_app','kind':'build','productFile':'recipes/build.json','operation':'build'})
        self.write(self.project_path,self.project)
        self.issue['projects'][0]['projectDigest']=contracts.digest(self.project)
        repair={'sourceRoot':'/never-read/source','sourcePaths':self.project['editablePaths']+[
                item['productFile'] for item in self.project['recipes']+self.project['fixtures']],
                'protectedPaths':[], 'originalArtifactRoot':'/never-read/artifacts',
                'originalArtifactPaths':['app.bin'], 'artifactIdentity':'file-sha256',
                'buildRecipeId':'build_app','agent':{'kind':'local-patch','patchFile':'/never-read/proposal.patch'}}
        self.issue['projects'][0]['repair']=repair
        self.write(self.issue_path,self.issue)
        self.assertEqual(self.run_check()[0],0)
        for field,value in (('buildRecipeId','missing'),('sourcePaths',['src/Checkout.swift']),
                            ('protectedPaths',['src/Checkout.swift'])):
            issue=copy.deepcopy(self.issue);issue['projects'][0]['repair'][field]=value
            self.write(self.issue_path,issue)
            self.assertEqual(self.run_check()[0],2)

    def test_namespace_symlink_loop_has_sanitized_rejection(self):
        loop=self.root/'private-loop'
        loop.symlink_to(loop.name)
        self.output=loop/'output'
        before=self.snapshot_without_following_links()
        code,report=self.run_check()
        self.assertEqual(code,2)
        self.assertNotIn('private-loop',json.dumps(report))
        self.assertEqual(self.snapshot_without_following_links(),before)

    def snapshot_without_following_links(self):
        return {p.name:(p.lstat().st_mode,str(p.readlink()) if p.is_symlink() else p.read_bytes())
                for p in self.root.iterdir()}
