"""Original reproduction -> isolated product edit -> rebuild -> independent verification."""
from __future__ import annotations
import difflib
from pathlib import Path
import time
import xml.etree.ElementTree as ET
from .core import ContractError, digest, require
from .agents import AgentUnavailable
from .repair import CommandError, apply_edits, build_android, copy_source, run_command, snapshot_source, validate_numeric_expression
from .replay import replay_suite, write_report
from .storage import load_bundle, write_json, sha_file

PRODUCT_FILE='sample/src/main/java/io/reproof/sample/CounterLogic.kt'
APK_RELATIVE='sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk'
BUILD_TASK=':sample:assembleBuggyDebug'


def repair_job(device,bundle,source,output,agent,toolchain,*,repeats=3,max_attempts=3,budget_seconds=1800,app_profile=None):
    require(type(max_attempts) is int and 1<=max_attempts<=3,'Patch budget must be 1–3')
    require(type(repeats) is int and 3<=repeats<=20,'Repeat policy must be 3–20')
    source=Path(source).resolve();output=Path(output).resolve()
    product_file, function, build_task, apk_relative = PRODUCT_FILE, 'increment', BUILD_TASK, APK_RELATIVE
    regression_task, regression_results = ':sample:testBuggyDebugUnitTest', 'sample/build/test-results/testBuggyDebugUnitTest'
    if app_profile is not None:
        require(bundle.get('app_profile') is not None and bundle['app_profile'].digest == app_profile.digest
                and bundle['scenario'].get('appProfileDigest') == app_profile.digest,
                'Repair bundle does not match the selected app profile')
        config = app_profile.data
        product_file, function = config['edit']['path'], config['edit']['function']
        build_task, apk_relative = config['build']['task'], config['build']['apk']
        regression_task, regression_results = config['build']['regressionTask'], config['build']['regressionResults']
    else:
        require(bundle.get('app_profile') is None, 'Select the app profile for this repair bundle')
    require(not output.exists(),'Repair output already exists')
    output.mkdir(parents=True,mode=0o700)
    inputs=app_profile.data.get('sourceInputs') if app_profile is not None else None
    original=snapshot_source(source, source_inputs=inputs);require(product_file in original,'Missing allowlisted product source')
    origin_proof=bundle['manifest'].get('sourceProof')
    require(isinstance(origin_proof,dict) and origin_proof.get('sourceDigest')==digest(original)
            and origin_proof.get('apkSha256')==sha_file(bundle['apk']) and origin_proof.get('buildCompleted') is True,
            'Original APK has no matching trusted source/build receipt')
    if app_profile is not None:
        require(origin_proof.get('appProfileDigest') == app_profile.digest,
                'Original APK was built for a different app profile')
    protected={k:v for k,v in original.items() if k!=product_file}
    frozen={'bundleDigest':bundle['manifestDigest'],'sourceFiles':original,'protectedFiles':protected,
            'repeats':repeats,'maxAttempts':max_attempts,'budgetSeconds':budget_seconds}
    if app_profile is not None:
        frozen.update(appProfileDigest=app_profile.digest, nativeProfileDigest=app_profile.native_digest)
    write_json(output/'policy.json',frozen)
    policy_hash=sha_file(output/'policy.json');started=time.monotonic()
    job={'schemaVersion':1,'captureMethod':bundle['manifest'].get('captureMethod','imported'),'status':'prepared','attempts':[],'policyDigest':policy_hash,'runs':[]}
    if app_profile is not None:job['appProfileDigest'] = app_profile.digest
    if bundle.get('diagnostics') is not None:
        job['diagnostics'] = bundle['diagnostics']
        job['instrumentationSites'] = app_profile.data['instrumentation']['sites']
    write_json(output/'job.json',job)
    def finish(status):
        job['status']=status;write_json(output/'job.json',job);write_report(output/'report.html',job);return job
    try:
        baseline=replay_suite(device,bundle,bundle['apk'],output/'baseline','original',repeats,origin_proof,deadline=started+budget_seconds)
    except KeyboardInterrupt:return finish('cancelled')
    except ContractError:return finish('invalid_bundle')
    job['runs'].extend(baseline['runs']);job['baselineStatus']=baseline['status']
    if baseline['status']!='reproduced':return finish(baseline['status'])
    job['status']='reproduced';write_json(output/'job.json',job)
    feedback=None;fingerprints=set()
    for number in range(1,max_attempts+1):
        if time.monotonic()-started>=budget_seconds:return finish('budget_exhausted')
        directory=output/f'attempt-{number}';directory.mkdir(mode=0o700)
        attempt={'attemptId':number,'status':'patching'};job['attempts'].append(attempt)
        write_json(output/'job.json',job)
        workspace=directory/'source';copy_source(source,workspace,source_inputs=inputs)
        try:
            require(sha_file(output/'policy.json')==policy_hash,'Policy mutated')
            require(snapshot_source(source,source_inputs=inputs)==original,'Original source changed during repair')
            try:
                edits=agent.propose({product_file:(workspace/product_file).read_text()},bundle['scenario'],feedback)
            finally:
                if getattr(agent, 'last_receipt', None):
                    attempt['agentReceipt']=dict(agent.last_receipt)
                    write_json(directory/'agent-receipt.json',attempt['agentReceipt'])
                    write_json(output/'job.json',job)
            fingerprint=digest(edits)
            if fingerprint in fingerprints:
                attempt['status']='patch_failed';attempt['reason']='Repeated identical patch';return finish('patch_failed')
            fingerprints.add(fingerprint)
            changed=apply_edits(workspace,edits,{product_file});write_json(directory/'edits.json',{'edits':edits})
            validate_numeric_expression((source/product_file).read_text(),(workspace/product_file).read_text(),function)
            after=snapshot_source(workspace,source_inputs=inputs,isolated=True)
            require({k:v for k,v in after.items() if k!=product_file}==protected,'Protected source changed')
            require(after[product_file]!=original[product_file],'Patch did not change product behavior source')
            diff=''.join(difflib.unified_diff((source/product_file).read_text().splitlines(True),
                                            (workspace/product_file).read_text().splitlines(True),
                                            fromfile='a/'+product_file,tofile='b/'+product_file))
            (directory/'patch.diff').write_text(diff)
            attempt['status']='building';write_json(output/'job.json',job)
            remaining=max(1,int(budget_seconds-(time.monotonic()-started)))
            apk,proof=build_android(workspace,task=build_task,apk_relative=apk_relative,
                                    timeout=min(300,remaining),app_profile=app_profile,**toolchain)
            require({k:v for k,v in snapshot_source(workspace,source_inputs=inputs,isolated=True).items() if k!=product_file}==protected,
                    'Build changed protected source')
            proof['patchDigest']=fingerprint;proof['protectedDigest']=digest(protected)
            if app_profile is not None:
                proof.update(appProfileDigest=app_profile.digest, nativeProfileDigest=app_profile.native_digest)
            write_json(directory/'build-receipt.json',proof)
            test_dir=workspace/regression_results
            require(not test_dir.exists(), 'Regression output already existed before the protected test task')
            for component in [test_dir, *test_dir.parents]:
                require(not component.is_symlink(), 'Linked regression output path')
                if component == workspace:break
            # Execute the protected desired-behavior test on the same build variant.
            run_command([str(toolchain['gradle']),'--offline','--no-daemon','--console=plain',regression_task],
                        str(workspace),timeout=min(300,max(1,int(budget_seconds-(time.monotonic()-started)))),
                        env_extra={'JAVA_HOME':str(toolchain['java_home']),'ANDROID_HOME':str(toolchain['sdk_home'])})
            for component in [test_dir, *test_dir.parents]:
                require(not component.is_symlink(), 'Linked regression output path')
                if component == workspace:break
            reports=list(test_dir.glob('TEST-*.xml'))
            require(bool(reports),'Regression task produced no executed-test evidence')
            counts={'tests':0,'failures':0,'errors':0,'skipped':0}
            for report_file in reports:
                require(not report_file.is_symlink() and report_file.stat().st_size <= 2*1024*1024,
                        'Linked or oversized regression report')
                element=ET.parse(report_file).getroot()
                for key in counts:counts[key]+=int(element.attrib.get(key,0))
            require(counts['tests']>0 and not any(counts[k] for k in ('failures','errors','skipped')),
                    'Regression tests missing, failing or skipped')
            attempt['regressionTests']=counts
            require(snapshot_source(workspace,source_inputs=inputs,isolated=True)==after,'Regression command modified source')
            require(sha_file(output/'policy.json')==policy_hash,'Policy mutated')
            attempt['status']='verifying';write_json(output/'job.json',job)
            result=replay_suite(device,bundle,apk,directory/'verification','patched',repeats,proof,True,deadline=started+budget_seconds)
            attempt['status']=result['status'];attempt['changedFiles']=changed;job['runs'].extend(result['runs'])
            require(snapshot_source(source,source_inputs=inputs)==original
                    and snapshot_source(workspace,source_inputs=inputs,isolated=True)==after,
                    'Source changed during final verification')
            require(sha_file(output/'policy.json')==policy_hash,'Policy changed during final verification')
            require(load_bundle(bundle['path'],app_profile=app_profile)['manifestDigest']==bundle['manifestDigest'],'Bundle changed during final verification')
            require(sha_file(apk)==proof['apkSha256'],'Patched APK changed during final verification')
            if result['status']=='verified':
                job['patch']=f'attempt-{number}/patch.diff';job['apk']=str(apk.relative_to(output))
                return finish('verified')
            if result['status']!='verification_failed':return finish(result['status'])
            feedback='Verification failed: the fixed expected outcome was not consistently observed.'
        except KeyboardInterrupt:
            attempt['status']='cancelled';return finish('cancelled')
        except (ContractError,ET.ParseError,ValueError):
            attempt['status']='verification_failed';attempt['reason']='Protected edit, invalid patch, or evidence contract violation'
            return finish('verification_failed')
        except AgentUnavailable:
            attempt['status']='agent_unavailable';return finish('agent_unavailable')
        except CommandError:
            attempt['status']='patch_failed';attempt['reason']='Agent, build, or regression command failed'
            feedback='Previous proposal could not complete the build and regression checks.'
        write_json(output/'job.json',job)
    return finish(job['attempts'][-1]['status'])
