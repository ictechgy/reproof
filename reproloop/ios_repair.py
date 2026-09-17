"""Simulator repair with a frozen original UI runner and a constrained Swift edit."""
from __future__ import annotations
import difflib
from pathlib import Path
import time
from .agents import AgentUnavailable
from .core import ContractError,digest,require
from .repair import CommandError,apply_edits
from .storage import write_json,sha_file
from .replay import write_report
from .ios_core import EVIDENCE_KIND,validate_swift_expression,ios_evidence_kind
from .ios_build import build_ios
from .ios_cases import case_from_fixture
from .ios_storage import copy_ios_source,tree_manifest,load_ios_bundle
from .ios_runner import run_ios_suite,run_logic_test


def repair_ios_job(simulator,bundle,source,output,agent,*,repeats=3,max_attempts=3):
    require(type(max_attempts) is int and 1<=max_attempts<=3,'Patch attempt budget must be 1–3')
    source=Path(source).resolve();output=Path(output).resolve();require(not output.exists(),'iOS repair output exists')
    spec=case_from_fixture(bundle['capture']['fixture']);product_file=spec.product_file
    original=tree_manifest(source,True)
    require(product_file in original and bundle['receipt'].get('sourceDigest')==digest(original),'Original iOS source differs from receipt')
    protected={k:v for k,v in original.items() if k!=product_file}
    environment=getattr(simulator,'execution_environment','simulator');evidence_kind=ios_evidence_kind(environment)
    require(bundle['manifest']['executionEnvironment']==environment,'Runner and bundle execution environments differ')
    output.mkdir(parents=True,mode=0o700)
    policy={'case':spec.name,'productFile':product_file,'logicTest':spec.logic_test,'platform':'ios','executionEnvironment':environment,'bundleDigest':bundle['manifestDigest'],
            'originalSource':original,'protectedSource':protected,'repeats':repeats,'maxAttempts':max_attempts,
            'requiredEvidence':evidence_kind,'frozenRunnerProducts':bundle['receipt']['productsDigest']}
    write_json(output/'policy.json',policy);policy_hash=sha_file(output/'policy.json')
    job={'schemaVersion':2,'case':spec.name,'platform':'ios','executionEnvironment':environment,'evidenceKind':evidence_kind,
         'status':'prepared','attempts':[],'runs':[],'policyDigest':policy_hash,
         'captureMethod':bundle['manifest'].get('captureMethod')}
    if bundle.get('diagnostics') is not None:
        job['diagnostics'] = bundle['diagnostics']
        job['autoProfileDigest'] = bundle['auto_profile'].digest
    def finish(status):
        job['status']=status;write_json(output/'job.json',job);write_report(output/'report.html',job);return job
    finish('prepared');started=time.monotonic()
    try:
        baseline=run_ios_suite(simulator,bundle,bundle['app'],output/'baseline','original',repeats,source_proof=bundle['receipt'])
    except KeyboardInterrupt:return finish('cancelled')
    except ContractError:return finish('invalid_bundle')
    job['runs'].extend(baseline['runs']);job['baselineStatus']=baseline['status']
    if baseline['status']!='reproduced':return finish(baseline['status'])
    feedback=None;fingerprints=set()
    for number in range(1,max_attempts+1):
        if time.monotonic()-started>1800:return finish('budget_exhausted')
        directory=output/f'attempt-{number}';directory.mkdir(mode=0o700)
        attempt={'attemptId':number,'status':'patching'};job['attempts'].append(attempt);finish('patching')
        workspace=directory/'source';copy_ios_source(source,workspace)
        try:
            require(tree_manifest(source,True)==original and sha_file(output/'policy.json')==policy_hash,'Source or policy changed')
            edits=agent.propose({product_file:(workspace/product_file).read_text()},{**bundle['scenario'],'editPolicy':spec.edit_policy},feedback)
            if getattr(agent,'last_receipt',None):attempt['agentReceipt']=agent.last_receipt
            fingerprint=digest(edits)
            if fingerprint in fingerprints:attempt['status']='patch_failed';return finish('patch_failed')
            fingerprints.add(fingerprint)
            apply_edits(workspace,edits,{product_file})
            validate_swift_expression((source/product_file).read_text(),(workspace/product_file).read_text(),spec.name)
            after=tree_manifest(workspace,True)
            require({k:v for k,v in after.items() if k!=product_file}==protected,'Protected iOS source changed')
            write_json(directory/'edits.json',{'edits':edits})
            (directory/'patch.diff').write_text(''.join(difflib.unified_diff((source/product_file).read_text().splitlines(True),
                                    (workspace/product_file).read_text().splitlines(True),fromfile='a/'+product_file,tofile='b/'+product_file)))
            attempt['status']='building';finish('patching')
            build_options={'physical_device':simulator.device} if environment=='physical-iphone' else {}
            built=build_ios(workspace,directory/'build',simulator.udid,include_ui=False,**build_options)
            attempt['regressionTests']=run_logic_test(simulator,built['products'],directory/'logic-tests',test_identifier=spec.logic_test)
            require(tree_manifest(workspace,True)==after and sha_file(output/'policy.json')==policy_hash,'Build altered protected source or policy')
            require(load_ios_bundle(bundle['path'])['manifestDigest']==bundle['manifestDigest'],'Frozen original runner changed')
            attempt['status']='verifying';finish('verifying')
            patched_artifact=digest(tree_manifest(built['app']))
            result=run_ios_suite(simulator,bundle,built['app'],directory/'verification','patched',repeats,
                                 regression_passed=True,source_proof=built['receipt'])
            job['runs'].extend(result['runs']);attempt['status']=result['status']
            require(tree_manifest(source,True)==original and tree_manifest(workspace,True)==after,
                    'Source changed during final verification')
            require(sha_file(output/'policy.json')==policy_hash,'Policy changed during final verification')
            require(load_ios_bundle(bundle['path'])['manifestDigest']==bundle['manifestDigest'],'Frozen runner changed during final verification')
            require(digest(tree_manifest(built['app']))==patched_artifact,'Patched app changed during verification')
            if result['status']=='verified':
                job['patch']=f'attempt-{number}/patch.diff';return finish('verified')
            if result['status']!='verification_failed':return finish(result['status'])
            feedback='The expected UI outcome did not pass consistently.'
        except AgentUnavailable:attempt['status']='agent_unavailable';return finish('agent_unavailable')
        except KeyboardInterrupt:attempt['status']='cancelled';return finish('cancelled')
        except (ContractError,ValueError) as exc:
            attempt['status']='verification_failed';attempt['reason']=str(exc) if isinstance(exc,ContractError) else 'Invalid verification data'
            return finish('verification_failed')
        except CommandError:
            attempt['status']='patch_failed';feedback='The proposed change did not complete the protected build and regression test.'
        finish(attempt['status'])
    return finish(job['attempts'][-1]['status'])
