"""iOS commands are isolated from the existing Android v1 CLI."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
import uuid
from .core import ContractError,require,digest
from .repair import CommandError,run_command
from .storage import read_json,write_json
from .ios_core import APPLICATION_ID,compile_ios_capture
from .ios_cases import CASES,case_spec
from .ios_build import build_ios
from .ios_storage import create_ios_bundle,load_ios_bundle,tree_manifest,checked_relative,app_info
from .ios_runner import IosSimulator,run_ios_suite
from .ios_repair import repair_ios_job
from .agents import ClaudeAgent,PatchFileAgent
from .replay import write_report

from .resources import resource_root

ROOT=resource_root()
IOS_COMMANDS=('ios-doctor','ios-instrument','ios-app-build','ios-build','ios-record','ios-replay','ios-repair')


def sample_capture(case_name='counter'):
    return case_spec(case_name).capture()


def sample_oracle(case_name='counter'):
    return case_spec(case_name).oracle()


def summary(value):print(json.dumps(value,indent=2,ensure_ascii=False,default=str))


def main(argv):
    parser=argparse.ArgumentParser(prog='reproloop');sub=parser.add_subparsers(dest='command',required=True)
    doctor=sub.add_parser('ios-doctor');doctor.add_argument('--simulator')
    instrument=sub.add_parser('ios-instrument',help='Prepare debug-only UIKit logging without app source hooks')
    instrument.add_argument('--source',type=Path,required=True);instrument.add_argument('--output',type=Path,required=True)
    instrument.add_argument('--profile',type=Path,help='Public configured UIKit observation profile JSON')
    app_build=sub.add_parser('ios-app-build',help='Build configured public UIKit inputs without signing or device access')
    app_build.add_argument('--source',type=Path,required=True);app_build.add_argument('--output',type=Path,required=True)
    app_build.add_argument('--profile',type=Path,help='Profile for an original unprepared app')
    app_build.add_argument('--configuration',help='Exact configured Xcode configuration name')
    app_build.add_argument('--sdk',choices=['simulator','device'],default='simulator')
    build=sub.add_parser('ios-build');build.add_argument('--source',type=Path,default=ROOT/'ios');build.add_argument('--output',type=Path,required=True)
    build.add_argument('--configuration',choices=['Debug','Release'],default='Debug')
    record=sub.add_parser('ios-record');record.add_argument('--build',type=Path,required=True);record.add_argument('--output',type=Path,required=True)
    record.add_argument('--case',choices=list(CASES),default='counter')
    record.add_argument('--manual',action='store_true',help='Record manual sample interaction instead of synthetic XCTest input')
    replay=sub.add_parser('ios-replay');replay.add_argument('bundle',type=Path);replay.add_argument('--output',type=Path,required=True)
    repair=sub.add_parser('ios-repair');repair.add_argument('bundle',type=Path);repair.add_argument('--source',type=Path,default=ROOT/'ios');repair.add_argument('--output',type=Path,required=True)
    group=repair.add_mutually_exclusive_group(required=True);group.add_argument('--agent',choices=['claude']);group.add_argument('--patch-file',type=Path)
    repair.add_argument('--max-attempts',type=int,default=3)
    for command in (build,record,replay,repair):
        devices=command.add_mutually_exclusive_group(required=True)
        devices.add_argument('--simulator',help='Booted Simulator UUID')
        devices.add_argument('--iphone',help='Public ID of an authorized paired physical iPhone; uses local development signing')
    for command in (replay,repair):command.add_argument('--repeats',type=int,default=3)
    args=parser.parse_args(argv)
    try:
        if args.command=='ios-instrument':
            from .ios_instrumentation import prepare_ios_instrumentation
            from .ios_observation import load_observation_profile
            profile=load_observation_profile(args.profile) if args.profile else None
            summary(prepare_ios_instrumentation(args.source,args.output,profile=profile));return 0
        if args.command=='ios-app-build':
            from .ios_observation import build_observation_app,load_observation_profile
            result=build_observation_app(args.source,args.output,
                profile=load_observation_profile(args.profile) if args.profile else None,
                configuration=args.configuration,sdk=args.sdk)
            summary({'status':'built','app':result['app'],'receipt':args.output/'receipt.json',
                     'signed':False,'behaviorVerified':False});return 0
        if args.command=='ios-doctor':
            version=run_command(['/usr/bin/xcodebuild','-version'],'.',timeout=20)
            value={'xcode':version.strip(),'executionEnvironment':'simulator'}
            if args.simulator:value['simulatorReady']=bool(IosSimulator(args.simulator))
            summary(value);return 0
        if args.iphone:
            from .ios_device import IosPhysicalDevice
            simulator=IosPhysicalDevice(args.iphone)
        else:simulator=IosSimulator(args.simulator)
        environment=simulator.execution_environment
        with simulator.lease():
            if args.command=='ios-build':
                options={'physical_device':simulator.device} if args.iphone else {}
                result=build_ios(args.source,args.output,simulator.udid,configuration=args.configuration,**options)
                summary({'status':'built','app':result['app'],'receipt':args.output/'receipt.json','executionEnvironment':environment});return 0
            if args.command=='ios-record':
                require(not args.output.exists(),'iOS record output exists');args.output.mkdir(parents=True,mode=0o700)
                spec=case_spec(args.case)
                receipt=read_json(args.build/'receipt.json');products=(args.build/'DerivedData/Build/Products').resolve()
                require(receipt.get('buildCompleted') is True and receipt.get('productsDigest')==digest(tree_manifest(products)),'Build products changed before recording')
                app=checked_relative(products,receipt['appRelative'])
                require(app_info(app)['buildId']==receipt.get('buildId'),'Recording build identity differs from receipt')
                from .ios_storage import automatic_profile_for_receipt
                auto_profile=automatic_profile_for_receipt(app,receipt)
                diagnostics=None
                if args.manual:
                    require(not args.iphone,'Physical manual SDK capture is not exposed by this CLI; use Live recording or synthetic fixture recording')
                    simulator.stop();simulator.install(app);started=int(time.time()*1000)
                    run_id=str(uuid.uuid4())
                    try:
                        env={'SIMCTL_CHILD_REPRO_MODE':'record','SIMCTL_CHILD_REPRO_CASE':spec.name,
                             'SIMCTL_CHILD_REPRO_FIXTURE_RESET':'1','SIMCTL_CHILD_REPRO_RUN_ID':run_id}
                        if auto_profile:env['SIMCTL_CHILD_REPRO_AUTO_PROFILE_DIGEST']=auto_profile.digest
                        run_command(['/usr/bin/xcrun','simctl','launch',simulator.udid,APPLICATION_ID],'.',timeout=30,
                                    env_extra=env)
                        if auto_profile:
                            deadline=time.monotonic()+15
                            while True:
                                try:
                                    simulator.pin_auto_marker(run_id,auto_profile,expected_fixture=spec.fixture,min_started_at=started)
                                    break
                                except (ContractError,CommandError,OSError):
                                    require(time.monotonic()<deadline,'Automatic iOS recorder did not become ready');time.sleep(.1)
                            print('Record case '+spec.name+': '+spec.expected+'; press Enter here to finish automatic capture.',flush=True)
                        else:print('Record case '+spec.name+': '+spec.expected+'; tap Report, then press Enter here when Capture ready is shown.',flush=True)
                        input()
                        if auto_profile:
                            simulator.simctl('notify_post',simulator.udid,'io.reproloop.auto.freeze.'+run_id)
                            deadline=time.monotonic()+10
                            while True:
                                try:
                                    capture=simulator.collect_capture(started,expected_run_id=run_id,auto_profile=auto_profile,expected_fixture=spec.fixture)
                                    break
                                except (ContractError,CommandError,OSError):
                                    require(time.monotonic()<deadline,'Automatic iOS capture was not finalized');time.sleep(.1)
                            diagnostics=simulator.collect_auto_diagnostics(capture,run_id,auto_profile,expected_fixture=spec.fixture)
                        else:capture=simulator.collect_capture(started)
                    finally:simulator.stop()
                else:
                    scenario=compile_ios_capture(sample_capture(spec.name),sample_oracle(spec.name))
                    run=simulator.run_scenario(products,app,scenario,args.output/'record-run','record')
                    require(run['runValid'],'iOS recording test failed; inspect record-run/run.json and xcresult')
                    if auto_profile:
                        capture=simulator.collect_capture(run['startedAtMs'],expected_run_id=run['runId'],
                            auto_profile=auto_profile,expected_fixture=spec.fixture)
                        diagnostics=simulator.collect_auto_diagnostics(capture,run['runId'],auto_profile,expected_fixture=spec.fixture)
                    else:capture=simulator.collect_capture(run['startedAtMs'])
                bundle=create_ios_bundle(capture,sample_oracle(spec.name),products,receipt,args.output/'bundle',
                                         'manual-QA' if args.manual else 'synthetic-driver',diagnostics=diagnostics)
                summary({'status':'captured','bundle':bundle['path'],'events':len(bundle['scenario']['steps']),'executionEnvironment':environment});return 0
            bundle=load_ios_bundle(args.bundle)
            if args.command=='ios-replay':
                result=run_ios_suite(simulator,bundle,bundle['app'],args.output,'original',args.repeats,source_proof=bundle['receipt'])
            else:
                agent=ClaudeAgent() if args.agent else PatchFileAgent(args.patch_file)
                result=repair_ios_job(simulator,bundle,args.source,args.output,agent,repeats=args.repeats,max_attempts=args.max_attempts)
                result['agent']='claude' if args.agent else 'offline-patch-file'
                write_json(args.output/'job.json',result);write_report(args.output/'report.html',result)
            summary({'status':result['status'],'executionEnvironment':environment,'report':args.output/'report.html'})
            return 0 if result['status'] in {'verified','reproduced'} else 130 if result['status']=='cancelled' else 2
    except (ContractError,CommandError,OSError,ValueError) as exc:
        summary({'status':'blocked','errorType':type(exc).__name__,'message':str(exc) if isinstance(exc,(ContractError,CommandError)) else 'iOS local IO or data parsing failed'})
        return 2
    except (KeyboardInterrupt,EOFError):summary({'status':'cancelled'});return 130
