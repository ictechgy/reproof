"""One XCUITest session per scenario, with strict result and identity checks."""
from __future__ import annotations
import base64
import copy
from contextlib import nullcontext
from functools import wraps
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import tempfile
import threading
import time
import uuid
from .core import ContractError,classify_runs,digest,require
from .repair import CommandError,run_command
from .storage import Lease,read_json,write_json,sha_file
from .ios_core import APPLICATION_ID,EVIDENCE_KIND,validate_test_summary,validate_finalization,ios_evidence_kind
from .ios_cases import case_from_fixture
from .ios_storage import app_info,checked_relative,tree_manifest,load_ios_bundle
from .ios_build import UI_TARGET,UI_TEST,LOGIC_TARGET,LOGIC_TEST,xcode_environment
from .replay import write_report

MAX_AUTO_JSON = 1024 * 1024


class _TrackedLease:
    def __init__(self, owner, lease):
        self.owner = owner
        self.lease = lease

    def __enter__(self):
        value = self.lease.__enter__()
        local = self.owner._lease_local
        local.depth = getattr(local, 'depth', 0) + 1
        return value

    def __exit__(self, *args):
        local = self.owner._lease_local
        try:
            return self.lease.__exit__(*args)
        finally:
            local.depth = max(0, getattr(local, 'depth', 1) - 1)


def _leased_effect(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._mutation_lease():
            return method(self, *args, **kwargs)
    return guarded


def _auto_profile_from_app(app, auto_profile=None):
    if auto_profile is not None:
        return auto_profile
    from .ios_instrumentation import profile_from_app
    return profile_from_app(app)


def _auto_profile_digest(profile):
    if profile is None:
        return None
    value = getattr(profile, 'digest', None)
    require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value),
            'Invalid iOS automatic instrumentation profile digest')
    return value


def _require_profile_fixture(profile, fixture):
    require(fixture is not None, 'Automatic capture fixture is missing')
    data = getattr(profile, 'data', {})
    allowed = data.get('cases')
    if isinstance(allowed, list):
        require(case_from_fixture(fixture).name in allowed,
                'Automatic capture fixture is outside the profile case policy')
    return fixture


def validate_ios_auto_marker(*args, **kwargs):
    from .ios_instrumentation import validate_ios_auto_marker as validator
    return validator(*args, **kwargs)


def validate_ios_auto_diagnostics(*args, **kwargs):
    from .ios_instrumentation import validate_ios_auto_diagnostics as validator
    return validator(*args, **kwargs)


def _targets(document):
    version=document.get('__xctestrun_metadata__',{}).get('FormatVersion')
    if version==1:
        return [(None,target) for key,target in document.items()
                if not key.startswith('__') and isinstance(target,dict) and 'BlueprintName' in target]
    require(version==2,'Unsupported xctestrun format version')
    configurations=document.get('TestConfigurations')
    require(isinstance(configurations,list),'Unsupported xctestrun configuration structure')
    return [(config,target) for config in configurations for target in config.get('TestTargets',[])]


def prepare_xctestrun(products,target_name,output,*,payload=None,app=None):
    products=Path(products).resolve();matches=[]
    for path in products.glob('*.xctestrun'):
        with path.open('rb') as f:document=plistlib.load(f)
        for config,target in _targets(document):
            if target.get('BlueprintName')==target_name:matches.append((document,config,target))
    require(len(matches)==1,'Missing or ambiguous fixed test runner')
    document,configuration,target=copy.deepcopy(matches[0])
    # Retain just the requested protected test target.
    version_one=configuration is None
    if version_one:
        document={'__xctestrun_metadata__':document['__xctestrun_metadata__'],target_name:target}
    else:
        configuration['TestTargets']=[target];document['TestConfigurations']=[configuration]
    def resolve(value):
        if isinstance(value,str):return value.replace('__TESTROOT__',str(products))
        if isinstance(value,list):return [resolve(x) for x in value]
        if isinstance(value,dict):return {k:resolve(v) for k,v in value.items()}
        return value
    document=resolve(document);target=document[target_name] if version_one else document['TestConfigurations'][0]['TestTargets'][0]
    host=target.get('TestHostPath','')
    require(isinstance(host,str),'Missing fixed test host')
    def host_resolve(value):
        if isinstance(value,str):return value.replace('__TESTHOST__',host)
        if isinstance(value,list):return [host_resolve(x) for x in value]
        if isinstance(value,dict):return {k:host_resolve(v) for k,v in value.items()}
        return value
    target=host_resolve(target)
    if version_one:document[target_name]=target
    else:document['TestConfigurations'][0]['TestTargets'][0]=target
    for key in ('TestBundlePath','TestHostPath','UITargetAppPath'):
        if key not in target:continue
        value=target[key]
        require(isinstance(value,str) and '__' not in value,'Unsupported test artifact path placeholder')
        require(Path(value).resolve().is_relative_to(products) and Path(value).exists(),'Test runner escapes original products')
    if app is not None:
        app=Path(app).resolve();app_info(app)
        previous=target.get('UITargetAppPath')
        require(isinstance(previous,str) and previous,'UI runner lacks an original target app')
        def relocate(value):
            if isinstance(value,str):return value.replace(previous,str(app))
            if isinstance(value,list):return [relocate(x) for x in value]
            if isinstance(value,dict):return {k:relocate(v) for k,v in value.items()}
            return value
        target=relocate(target)
        if version_one:document[target_name]=target
        else:document['TestConfigurations'][0]['TestTargets'][0]=target
    if payload is not None:
        raw=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
        require(len(raw)<=16*1024,'Scenario payload exceeds 16KiB')
        env=target.setdefault('EnvironmentVariables',{})
        env['REPRO_SCENARIO_B64']=base64.b64encode(raw).decode()
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    with Path(output).open('wb') as f:plistlib.dump(document,f)
    Path(output).chmod(0o600);return Path(output)


def read_xctest_summary(result):
    raw=run_command(['/usr/bin/xcrun','xcresulttool','get','test-results','summary','--path',str(result),'--compact'],'.',timeout=30)
    value=json.loads(raw);validate_test_summary(value)
    return {k:value[k] for k in ('totalTestCount','passedTests','failedTests','skippedTests')}


def extract_attachment(result,output,expected_device=None):
    output=Path(output)
    run_command(['/usr/bin/xcrun','xcresulttool','export','attachments','--path',str(result),
                 '--output-path',str(output),'--filter','*repro-result*'],'.',timeout=30)
    manifest=read_json(output/'manifest.json');require(isinstance(manifest,list),'Unknown attachment export format')
    matches=[]
    for test in manifest:
        identifier=test.get('testIdentifier','').replace('()','')
        if not identifier.endswith('ReproReplayTests/testScenario'):continue
        for item in test.get('attachments',[]):
            if item.get('suggestedHumanReadableName','').startswith('repro-result'):
                if expected_device is not None:
                    require(str(item.get('deviceId','')).lower()==expected_device.lower(),'XCTest ran on a different device')
                matches.append(checked_relative(output,item.get('exportedFileName')))
    require(len(matches)==1,'Expected one matching structured UI test attachment')
    return read_json(matches[0])


class IosSimulator:
    execution_environment='simulator'
    def __init__(self,udid):
        require(isinstance(udid,str) and re.fullmatch(r'[A-Fa-f0-9-]{36}',udid),'Select a Simulator UUID')
        self.udid=udid;self.identity=hashlib.sha256(udid.encode()).hexdigest()[:16]
        self._lease_local=threading.local();self.authority_lease=None
        listing=json.loads(run_command(['/usr/bin/xcrun','simctl','list','devices','--json'],'.',timeout=20))
        found=[d for group in listing['devices'].values() for d in group if d['udid']==udid and d.get('isAvailable')]
        require(len(found)==1 and found[0]['state']=='Booted','Selected Simulator is not booted or available')
    def lease(self):return self._lease_for('ios-simulator:'+self.udid)
    def _lease_for(self,identity):
        if not hasattr(self,'_lease_local'):self._lease_local=threading.local()
        if getattr(self._lease_local,'depth',0)>0:return nullcontext()
        return _TrackedLease(self,Lease(identity))
    def _mutation_lease(self):
        authority_lease=getattr(self,'authority_lease',None)
        return authority_lease if authority_lease is not None else self.lease()
    def simctl(self,*args):return run_command(['/usr/bin/xcrun','simctl',*map(str,args)],'.',timeout=60)
    def _data_container(self):
        value = self.simctl('get_app_container', self.udid, APPLICATION_ID, 'data').strip()
        require(value, 'Missing installed iOS application data container')
        path = Path(value).resolve()
        require(path.is_dir() and not path.is_symlink(), 'Invalid installed iOS application data container')
        return path

    def read_app_json(self, relative, *, max_bytes=MAX_AUTO_JSON):
        base = self._data_container()
        path = checked_relative(base, relative)
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= max_bytes,
                'Missing or oversized iOS application JSON')
        return read_json(path)

    def _installed_build_id(self):
        try:
            app_container = Path(self.simctl('get_app_container', self.udid, APPLICATION_ID, 'app').strip()).resolve()
            return app_info(app_container)['buildId']
        except (ContractError, CommandError, OSError, ValueError):
            return None

    def pin_auto_marker(self, expected_run_id, auto_profile, *, expected_fixture,
                        min_started_at=None, require_finalized=False):
        require(isinstance(expected_run_id, str) and auto_profile is not None,
                'Automatic capture requires a run id and profile')
        _require_profile_fixture(auto_profile, expected_fixture)
        marker = self.read_app_json('Library/Application Support/ReproLoop/auto-session.json')
        build_id = self._installed_build_id()
        require(build_id is not None, 'Installed iOS application build identity is unavailable')
        validate_ios_auto_marker(marker, auto_profile, run_id=expected_run_id,
                                 build_id=build_id, fixture=expected_fixture,
                                 min_started_at=min_started_at)
        if require_finalized:
            require(marker.get('finalized') is True, 'Automatic iOS session is not finalized')
        return marker

    @_leased_effect
    def install(self,app):
        expected=app_info(app);self.simctl('install',self.udid,Path(app).resolve())
        installed=Path(self.simctl('get_app_container',self.udid,APPLICATION_ID,'app').strip())
        actual=app_info(installed);require(actual==expected,'Installed Simulator app identity mismatch')
        return {'applicationId':APPLICATION_ID,'buildId':actual['buildId'],'deviceId':self.identity,
                'executionEnvironment':'simulator','evidenceKind':EVIDENCE_KIND,'installedVerified':True}
    @_leased_effect
    def stop(self):
        try:self.simctl('terminate',self.udid,APPLICATION_ID)
        except CommandError:pass  # XCTest may have already terminated its own app.
    def collect_capture(self,min_started_at=None,*,expected_run_id=None,auto_profile=None,expected_fixture=None):
        container=self._data_container()
        base=container/'Library/Application Support/ReproLoop'
        auto = expected_run_id is not None or auto_profile is not None
        auto_marker = None
        if auto:
            require(expected_run_id is not None and auto_profile is not None,
                    'Automatic capture requires a run id and profile')
            require(expected_fixture is not None, 'Automatic capture requires its expected fixture')
            auto_marker=self.pin_auto_marker(expected_run_id, auto_profile, expected_fixture=expected_fixture,
                                        min_started_at=min_started_at,
                                        require_finalized=True)
        capture=read_json(checked_relative(base,'capture.json'))
        session=capture.get('sessionId')
        require(isinstance(session,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',session),'Invalid capture session path')
        metadata=read_json(checked_relative(base,session+'/metadata.json'))
        finalized_marker=read_json(checked_relative(base,session+'/finalized.json'))
        validate_finalization(capture,metadata,finalized_marker,min_started_at)
        if auto:
            pinned=self.read_app_json('Library/Application Support/ReproLoop/auto-session.json')
            require(pinned==auto_marker and pinned.get('sessionId')==capture.get('sessionId')
                    and pinned.get('endSequence')==capture.get('endSequence')
                    and pinned.get('fixture')==expected_fixture
                    and pinned.get('finalized') is True,
                    'Automatic capture marker differs from finalized capture')
            require(self.read_app_json('Library/Application Support/ReproLoop/auto-session.json')==auto_marker,
                    'Automatic capture marker changed while collecting')
        return capture
    def collect_auto_diagnostics(self,capture,expected_run_id,auto_profile,*,expected_fixture):
        require(isinstance(capture,dict) and isinstance(expected_run_id,str),
                'Automatic diagnostics identity is invalid')
        require(auto_profile is not None, 'Automatic diagnostics requires a profile')
        require(expected_fixture is not None, 'Automatic diagnostics requires its expected fixture')
        marker=self.pin_auto_marker(expected_run_id, auto_profile, expected_fixture=expected_fixture,
                                    require_finalized=True)
        build_id=marker.get('buildId')
        session=capture.get('sessionId')
        require(isinstance(session,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',session),
                'Invalid automatic diagnostics session path')
        require(marker.get('sessionId')==session and marker.get('endSequence')==capture.get('endSequence')
                and marker.get('fixture')==expected_fixture,
                'Automatic diagnostics marker differs from capture')
        diagnostics=self.read_app_json(f'Library/Application Support/ReproLoop/{session}/diagnostics.json')
        require(self.read_app_json('Library/Application Support/ReproLoop/auto-session.json')==marker,
                'Automatic diagnostics marker changed while collecting')
        return validate_ios_auto_diagnostics(diagnostics,capture,auto_profile,
                                             run_id=expected_run_id,build_id=build_id)
    @_leased_effect
    def run_scenario(self,products,app,scenario,output,mode='replay',*,auto_profile=None):
        output=Path(output);require(not output.exists(),'iOS run output already exists');output.mkdir(parents=True,mode=0o700)
        run_id=str(uuid.uuid4());expected=app_info(app)
        auto_profile=_auto_profile_from_app(app,auto_profile)
        profile_digest=_auto_profile_digest(auto_profile) if mode=='record' else None
        if profile_digest is not None:
            _require_profile_fixture(auto_profile, scenario['fixture'])
        payload={'mode':mode,'runId':run_id,'expectedBuildId':expected['buildId'],'scenarioDigest':scenario['scenarioDigest'],
                 'fixture':scenario['fixture'],'steps':scenario['steps'],'oracle':scenario['oracle']}
        if profile_digest is not None:payload['autoProfileDigest']=profile_digest
        environment=self.execution_environment;evidence_kind=ios_evidence_kind(environment)
        evidence={'runId':run_id,'runValid':False,'bugCondition':False,'expectedCondition':False,
                  'evidenceValid':False,'protectedPathsValid':True,'regressionPassed':False,
                  'executionEnvironment':environment,'evidenceKind':evidence_kind,
                  'scenarioDigest':scenario['scenarioDigest'],'startedAtMs':int(time.time()*1000),'steps':[]}
        if profile_digest is not None:evidence['profileDigest']=profile_digest
        temporary=tempfile.TemporaryDirectory(prefix='repro-device-run-') if environment=='physical-iphone' else None
        work=Path(temporary.name) if temporary else output
        result=work/'result.xcresult'
        try:
            # XCTest may write execution/signing metadata. Keep the original kit immutable.
            private_products=work/'test-products';shutil.copytree(products,private_products)
            require(tree_manifest(private_products)==tree_manifest(products),'Test kit copy changed')
            private_app=work/'target/ReproSample.app';private_app.parent.mkdir()
            shutil.copytree(app,private_app)
            require(tree_manifest(private_app)==tree_manifest(app),'Target app copy changed')
            evidence['runnerProductsDigest']=digest(tree_manifest(products))
            evidence['appArtifactDigest']=digest(tree_manifest(app))
            evidence['installation']=self.install(private_app)
            configuration=prepare_xctestrun(private_products,UI_TARGET,work/'run.xctestrun',payload=payload,app=private_app)
            if profile_digest is not None:
                with configuration.open('rb') as stream:document=plistlib.load(stream)
                for _,target in _targets(document):
                    target.setdefault('EnvironmentVariables',{}).update(
                        REPRO_AUTO_RUN_ID=run_id,REPRO_AUTO_PROFILE_DIGEST=profile_digest)
                with configuration.open('wb') as stream:plistlib.dump(document,stream)
            evidence['testConfigurationDigest']=sha_file(configuration)
            command=['/usr/bin/xcodebuild','test-without-building','-xctestrun',str(configuration),
                     '-destination',f'id={self.udid}','-resultBundlePath',str(result),
                     '-parallel-testing-enabled','NO','-maximum-concurrent-test-simulator-destinations','1',
                     '-only-testing:'+UI_TEST]
            log=run_command(command,'.',timeout=180,max_output=4*1024*1024,env_extra=xcode_environment(),log_path=None if temporary else output/'xcodebuild.log')
            if not temporary:(output/'xcodebuild.log').write_text(log)
            evidence['testSummary']=read_xctest_summary(result)
            observed=extract_attachment(result,work/'attachments',self.udid)
            require(isinstance(observed,dict) and observed.get('runId')==run_id
                    and observed.get('scenarioDigest')==scenario['scenarioDigest']
                    and observed.get('buildId')==expected['buildId'] and observed.get('runValid') is True
                    and observed.get('mode')==mode and observed.get('schemaVersion')==1,
                    'Runner attachment identity or validity mismatch')
            if profile_digest is not None:
                require(observed.get('autoRunId')==run_id and observed.get('profileDigest')==profile_digest,
                        'Automatic capture attachment identity mismatch')
            nodes=observed.get('finalNodes');require(isinstance(nodes,dict),'Missing final UI observation')
            require(set(nodes)=={'counter.name','counter.count'},'Unexpected observation channel')
            require(isinstance(nodes['counter.name'],str) and nodes['counter.name'] in {'','QA','Test'} and isinstance(nodes['counter.count'],str)
                    and nodes['counter.count'].isdigit() and len(nodes['counter.count'])<10,'Observation outside sample allowlist')
            steps=observed.get('steps');require(isinstance(steps,list) and len(steps)==len(scenario['steps']),'Incomplete runner steps')
            require([s.get('sourceEventIds') for s in steps]==[s['sourceEventIds'] for s in scenario['steps']],
                    'Runner did not execute recorded event coverage')
            bug=nodes.get(scenario['oracle']['bugCondition']['target'])==scenario['oracle']['bugCondition']['text']
            normal=nodes.get(scenario['oracle']['expectedCondition']['target'])==scenario['oracle']['expectedCondition']['text']
            require(type(observed.get('bugCondition')) is bool and type(observed.get('expectedCondition')) is bool
                    and observed.get('bugCondition')==bug and observed.get('expectedCondition')==normal,'Runner verdict disagrees with observed UI')
            evidence.update(runValid=True,bugCondition=bug,expectedCondition=normal,evidenceValid=True,
                            finalNodes=nodes,steps=steps,buildId=expected['buildId'])
            if profile_digest is not None:evidence['autoRunId']=observed.get('autoRunId')
        except KeyboardInterrupt:evidence['cancelled']=True
        except (ContractError,CommandError,OSError,ValueError) as exc:
            evidence['errorType']=type(exc).__name__
            evidence['error']=str(exc) if isinstance(exc,(ContractError,CommandError)) else 'iOS evidence parsing failed'
        finally:
            try:self.stop()
            finally:
                if temporary:temporary.cleanup()
        write_json(output/'run.json',evidence)
        return evidence


def run_ios_suite(simulator,bundle,app,output,phase='original',repeats=3,*,regression_passed=False,source_proof=None):
    require(type(repeats) is int and 3<=repeats<=20,'Repeat policy must be 3–20')
    output=Path(output);require(not output.exists(),'iOS suite output exists');output.mkdir(parents=True,mode=0o700)
    environment=getattr(simulator,'execution_environment','simulator');evidence_kind=ios_evidence_kind(environment)
    require(bundle['manifest']['executionEnvironment']==environment,'Runner and bundle execution environments differ')
    runs=[]
    for number in range(1,repeats+1):
        current=load_ios_bundle(bundle['path']);require(current['manifestDigest']==bundle['manifestDigest'],'Bundle changed during verification')
        run=simulator.run_scenario(bundle['products'],app,bundle['scenario'],output/f'run-{number}')
        try:
            require(load_ios_bundle(bundle['path'])['manifestDigest']==bundle['manifestDigest'],'Bundle changed during run')
        except ContractError:
            run['protectedPathsValid']=False;run['evidenceValid']=False;run['error']='Bundle integrity changed during run'
        run['regressionPassed']=regression_passed;run['sourceProof']=source_proof;runs.append(run)
        write_json(output/f'run-{number}'/'run.json',run)
        if not run['runValid'] or not run['protectedPathsValid']:break
    status=classify_runs(runs,phase,repeats)
    if runs and not runs[-1]['runValid']:status='cancelled' if runs[-1].get('cancelled') else 'environment_blocked'
    if any(r.get('protectedPathsValid') is False for r in runs):status='verification_failed'
    if status=='verified' and not source_proof:status='inconclusive'
    result={'schemaVersion':2,'case':case_from_fixture(bundle['capture']['fixture']).name,'platform':'ios','executionEnvironment':environment,'evidenceKind':evidence_kind,
            'status':status,'phase':phase,'repeats':repeats,'runs':runs,'bundleDigest':bundle['manifestDigest'],
            'captureMethod':bundle['manifest'].get('captureMethod')}
    write_json(output/'result.json',result);write_report(output/'report.html',result);return result


def run_logic_test(simulator,products,output,*,test_identifier=LOGIC_TEST):
    from .ios_cases import CASES
    require(test_identifier in {c.logic_test for c in CASES.values()},'Unsupported protected logic test')
    output=Path(output);require(not output.exists(),'Logic result output exists');output.mkdir(parents=True,mode=0o700)
    temporary=tempfile.TemporaryDirectory(prefix='repro-device-logic-') if getattr(simulator,'execution_environment','simulator')=='physical-iphone' else None
    try:
        work=Path(temporary.name) if temporary else output
        if temporary:
            copied=work/'products';shutil.copytree(products,copied);products=copied
        config=prepare_xctestrun(products,LOGIC_TARGET,work/'logic.xctestrun')
        result=work/'result.xcresult'
        run_command(['/usr/bin/xcodebuild','test-without-building','-xctestrun',str(config),'-destination',f'id={simulator.udid}',
                     '-resultBundlePath',str(result),'-parallel-testing-enabled','NO','-only-testing:'+test_identifier],'.',
                    timeout=180,max_output=4*1024*1024,env_extra=xcode_environment(),log_path=None if temporary else output/'xcodebuild.log')
        summary=read_xctest_summary(result);write_json(output/'summary.json',summary);return summary
    finally:
        if temporary:temporary.cleanup()
