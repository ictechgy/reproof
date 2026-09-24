"""Run immutable scenarios and keep all observations, including failed runs."""
from __future__ import annotations
import copy
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import time
import uuid
from .core import ContractError, classify_runs, digest, require
from .device import DeviceError, DRIVER
from .storage import load_bundle, sha_file, write_json


def _matches(nodes,condition):
    return nodes.get(condition['target'])==condition['text']


def _protected_driver(device):
    value = getattr(device, "protected_driver", None)
    if value is None:
        return None
    require(isinstance(value, (tuple, list)) and len(value) == 2,
            "Invalid protected driver configuration")
    path, expected = value
    path = Path(path)
    require(path.is_file() and not path.is_symlink()
            and isinstance(expected, str) and len(expected) == 64,
            "Protected driver artifact is invalid")
    require(sha_file(path) == expected, "Protected driver artifact changed")
    return path, expected


def _trusted_profile(bundle):
    profile = bundle.get('app_profile')
    if profile is None:
        return None
    # The bundle loader creates this object only from an explicit trusted
    # argument.  Keep the replay boundary defensive for hand-built bundles.
    from .android_profile import AndroidAppProfile
    require(isinstance(profile, AndroidAppProfile), 'Bundle has an invalid trusted app profile')
    return profile


def _verify_device_profile(device, profile):
    if profile is None:
        return
    actual = getattr(device, 'app_profile', None)
    require(actual is not None and actual.digest == profile.digest
            and actual.native_digest == profile.native_digest,
            'Device app profile does not match the trusted bundle profile')


def _verify_profile_proof(proof, profile):
    if profile is None:
        return
    require(isinstance(proof, dict)
            and proof.get('appProfileDigest') == profile.digest
            and proof.get('nativeDigest') == profile.native_digest,
            'Device proof does not match the trusted bundle profile')


def _native_profile_proof(device, profile):
    if profile is None:
        return None
    proof = getattr(device, 'last_profile_receipt', None)
    require(isinstance(proof, dict)
            and proof.get('profileDigest') == profile.digest
            and proof.get('nativeDigest') == profile.native_digest
            and proof.get('operation') in {'observe', 'tap', 'replace', 'scroll_to', 'back'},
            'Missing or mismatched native app profile proof')
    return {'profileDigest': proof['profileDigest'],
            'nativeDigest': proof['nativeDigest'],
            'operation': proof['operation']}


def _install_protected_driver(device, path, expected):
    proof = device.install(path, DRIVER)
    _verify_profile_proof(proof, getattr(device, 'app_profile', None))
    installed = _verify_installed_driver(device, path, expected)
    require(isinstance(proof, dict) and isinstance(installed, dict)
            and proof.get("apkSha256") == expected
            and proof.get("installedVerified") is True,
            "Installed driver does not match protected artifact")
    return installed


def _verify_installed_driver(device, path, expected):
    require(sha_file(path) == expected, "Protected driver artifact changed")
    installed = device.installation_proof(path, DRIVER)
    _verify_profile_proof(installed, getattr(device, 'app_profile', None))
    require(isinstance(installed, dict)
            and installed.get("apkSha256") == expected
            and installed.get("installedVerified") is True,
            "Installed driver does not match protected artifact")
    return installed


def replay_once(device, bundle, apk, run_id, *, source_proof=None, deadline=None):
    scenario=copy.deepcopy(bundle['scenario'])
    profile = _trusted_profile(bundle)
    evidence={'runId':run_id,'runValid':False,'bugCondition':False,'expectedCondition':False,
              'evidenceValid':False,'protectedPathsValid':True,'regressionPassed':False,
              'scenarioDigest':scenario['scenarioDigest'],'bundleDigest':bundle['manifestDigest'],
              'sourceProof':source_proof,'steps':[], 'startedAt':datetime.now(timezone.utc).isoformat()}
    if profile is not None:
        evidence.update(appProfileDigest=profile.digest, nativeDigest=profile.native_digest)
    protected = None
    runner_started = False
    try:
        if deadline is not None and time.monotonic() >= deadline:
            evidence['budgetExhausted']=True;return evidence
        current=load_bundle(bundle['path'], app_profile=profile)
        require(current['manifestDigest']==bundle['manifestDigest'] and current['scenario']==scenario,
                'Bundle changed after compilation')
        current_profile = current.get('app_profile')
        if profile is not None:
            require(current_profile is not None and current_profile.digest == profile.digest
                    and current_profile.native_digest == profile.native_digest,
                    'Bundle profile changed after compilation')
        _verify_device_profile(device, profile)
        protected = _protected_driver(device)
        if protected is not None:
            path, expected = protected
            evidence['runner']={'apkSha256':expected,'installedVerified':False}
            before = _install_protected_driver(device, path, expected)
            _verify_profile_proof(before, profile)
            runner_started = True
            evidence['runner'].update(installedVerified=True,before=before)
        proof=device.prepare(apk,scenario['fixture']);_verify_profile_proof(proof, profile);evidence['installation']=proof
        nodes=device.observe()
        if profile is not None:
            require(nodes == scenario['startState']['nodes'], 'Start state mismatch')
        else:
            require(all(nodes.get(k)==v for k,v in scenario['startState']['nodes'].items()),'Start state mismatch')
        evidence['startStateVerified']=True
        for index,step in enumerate(scenario['steps']):
            if deadline is not None and time.monotonic() >= deadline:
                evidence['budgetExhausted']=True;return evidence
            device.execute(step)
            nodes=device.observe()
            if step['action']=='replace':require(nodes.get(step['target'])==step['parameters']['value'],'Input completion mismatch')
            evidence['steps'].append({'index':index,'sourceEventIds':step['sourceEventIds'],'action':step['action'],
                                      'target':step['target'],'nodes':nodes})
        observation_deadline=min(time.monotonic()+3, deadline) if deadline is not None else time.monotonic()+3
        while True:
            nodes=device.observe()
            b=_matches(nodes,scenario['oracle']['bugCondition']);e=_matches(nodes,scenario['oracle']['expectedCondition'])
            if b or e or time.monotonic()>=observation_deadline:break
            time.sleep(.15)
        if profile is not None:
            evidence['nativeProfileProof'] = _native_profile_proof(device, profile)
        if protected is not None:
            path, expected = protected
            after = _verify_installed_driver(device, path, expected)
            _verify_profile_proof(after, profile)
            evidence['runner']['after'] = after
            evidence['runner']['installedVerified'] = True
        runner_valid = (protected is None or
                        (evidence.get('runner', {}).get('installedVerified') is True and
                         isinstance(evidence.get('runner', {}).get('after'), dict)))
        native_profile_valid = (profile is None or
                                isinstance(evidence.get('nativeProfileProof'), dict))
        evidence.update(runValid=True,bugCondition=b,expectedCondition=e,
                        evidenceValid=proof.get('installedVerified') is True and proof.get('fixtureVerified') is True
                        and runner_valid and native_profile_valid,
                        finalNodes=nodes)
        # Product regression checks are executed by the repair orchestrator, never by the bundle.
    except KeyboardInterrupt:
        evidence['cancelled']=True
    except (ContractError,DeviceError,OSError) as exc:
        evidence['errorType']=type(exc).__name__
        evidence['error']=str(exc) if isinstance(exc,(ContractError,DeviceError)) else 'Local evidence IO failed'
    finally:
        if runner_started and protected is not None and 'after' not in evidence.get('runner', {}):
            try:
                path, expected = protected
                after = _verify_installed_driver(device, path, expected)
                _verify_profile_proof(after, profile)
                evidence['runner']['after'] = after
                evidence['runner']['installedVerified'] = True
            except (ContractError,DeviceError,OSError) as exc:
                evidence['runner']['installedVerified'] = False
                evidence['errorType'] = type(exc).__name__
                evidence['error'] = str(exc) if isinstance(exc,(ContractError,DeviceError)) else 'Local evidence IO failed'
                evidence['runValid'] = False
        try:device.stop()
        except (DeviceError,OSError):
            evidence['runValid']=False;evidence['cleanupFailed']=True
    return evidence


def replay_suite(device,bundle,apk,output,phase='original',repeats=3,source_proof=None,regression_passed=False,deadline=None):
    require(phase in {'original','patched'} and type(repeats) is int and 3<=repeats<=20,'Invalid repeat policy')
    output=Path(output);require(not output.exists(),'Run output already exists');output.mkdir(parents=True,mode=0o700)
    job_id=str(uuid.uuid4());runs=[]
    for n in range(repeats):
        run=replay_once(device,bundle,apk,f'{job_id}-{n+1}',source_proof=source_proof,deadline=deadline)
        run['regressionPassed']=regression_passed
        write_json(output/f'run-{n+1}.json',run);runs.append(run)
        if not run['runValid']:break  # No cherry-picking replacement runs.
    status=classify_runs(runs,phase,repeats)
    if runs and not runs[-1]['runValid']:
        status='cancelled' if runs[-1].get('cancelled') else 'budget_exhausted' if runs[-1].get('budgetExhausted') else 'environment_blocked'
    # Standalone replay can compare behavior, but has no trusted source/build/regression chain.
    if phase=='patched' and status=='verified' and (not source_proof or not regression_passed):status='inconclusive'
    report={'schemaVersion':1,'jobId':job_id,'captureMethod':bundle['manifest'].get('captureMethod','imported'),'phase':phase,'repeats':repeats,'status':status,
            'apkSha256':sha_file(apk),'bundleDigest':bundle['manifestDigest'],'runs':runs}
    profile = _trusted_profile(bundle)
    if profile is not None:
        report.update(appProfileDigest=profile.digest, nativeDigest=profile.native_digest)
    write_json(output/'result.json',report);write_report(output/'report.html',report)
    return report


def write_report(path,report):
    title='Reproof — '+str(report.get('status','unknown'))
    rows=[]
    for run in report.get('runs',[]):
        values=[run.get('runId'),run.get('runValid'),run.get('bugCondition'),run.get('expectedCondition'),
                run.get('evidenceValid'),run.get('error','')]
        rows.append('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in values)+'</tr>')
    content='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<style>body{font:16px system-ui;max-width:1100px;margin:48px auto;padding:0 20px;color:#172030}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:12px;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f6f8;padding:20px}h1{font-size:28px}</style>'''
    content+='<title>'+html.escape(title)+'</title><h1>'+html.escape(title)+'</h1>'
    if report.get('executionEnvironment'):
        content+='<p><strong>Environment: '+html.escape(str(report['executionEnvironment']))+'</strong></p>'
    if report.get('case'):
        content+='<p>Case: '+html.escape(str(report['case']))+'</p>'
    if report.get('agent'):
        content+='<p>Patch source: '+html.escape(str(report['agent']))+'</p>'
    content+='<p>All attempts are retained. Device identifiers are hashed. This report contains only sample allowlisted observations.</p>'
    content+='<table><thead><tr><th>Run</th><th>Valid</th><th>Bug observed</th><th>Expected</th><th>Evidence</th><th>Error</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>'
    content+='<h2>Evidence</h2><pre>'+html.escape(json.dumps(report,ensure_ascii=False,indent=2))+'</pre></html>'
    Path(path).write_text(content,encoding='utf-8');Path(path).chmod(0o600)
