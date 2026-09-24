from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from .core import ContractError, compile_capture, digest
from .device import AdbDevice, DeviceError, DRIVER, find_adb
from .storage import MAX_APK, create_bundle,load_bundle,read_json,write_json,sha_file
from .repair import CommandError,build_android,run_command
from .replay import replay_suite, write_report
from .android_build import create_protected_build
from .agents import ClaudeAgent,PatchFileAgent
from .orchestrator import repair_job,BUILD_TASK,APK_RELATIVE
from .android_profile import load_app_profile
from .resources import ResourceError, export_resources, installation_check, resource_root

ROOT=resource_root()


def toolchain(args):
    cached=sorted((Path.home()/'.gradle/wrapper/dists/gradle-8.14.5-bin').glob('*/gradle-*/bin/gradle'))
    gradle=getattr(args,'gradle',None) or (str(cached[0]) if cached else shutil.which('gradle'))
    java=getattr(args,'java_home',None) or os.environ.get('JAVA_HOME') or '/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home'
    sdk=getattr(args,'sdk_home',None) or os.environ.get('ANDROID_HOME') or str(Path.home()/'Library/Android/sdk')
    if not gradle or not Path(java,'bin/java').is_file() or not Path(sdk).is_dir():
        raise ContractError('Provide --gradle, --java-home and --sdk-home for the Android toolchain')
    return dict(gradle=gradle,java_home=java,sdk_home=sdk)


def sample_oracle():
    return {'bugCondition':{'target':'count','text':'2'},'expectedCondition':{'target':'count','text':'1'},
            'actual':'One completed Add increments count twice','expected':'One completed Add increments count once'}


def print_summary(value):print(json.dumps(value,ensure_ascii=False,indent=2,default=str))


def main(argv=None):
    effective=list(sys.argv[1:] if argv is None else argv)
    if effective == ['installation-check']:
        result=installation_check(); print_summary(result)
        return 0 if result['status']=='ready' else 2
    if effective and effective[0]=='live-admin':
        from .live.configuration import admin_main
        return admin_main(effective[1:])
    if effective and effective[0]=='live-worker':
        from .live.worker_cli import main as worker_main
        return worker_main(effective[1:])
    if effective and effective[0] in {'live-device-doctor','live-iphone-build'}:
        from .live.iphone import main as iphone_main
        return iphone_main(effective)
    if effective and effective[0]=='live-tools':
        from .live.tools import main as live_tools
        return live_tools(effective[1:])
    if effective and effective[0] in {'live-jobs','live-recordings','live-issues'}:
        from .live.commands import main as live_commands
        return live_commands(effective)
    if effective and effective[0]=='live-serve':
        from .live.cli import main as live_main
        return live_main(effective)
    if effective and effective[0] == 'protected-service':
        from .repair_configuration_cli import main as protected_service_main
        return protected_service_main(effective[1:])
    if effective and effective[0] == 'ios-signing':
        from .ios_signing_cli import main as ios_signing_main
        return ios_signing_main(effective[1:])
    if effective and effective[0] == 'ios-mobile':
        from .ios_mobile_cli import main as ios_mobile_main
        return ios_mobile_main(effective[1:])
    if effective and effective[0].startswith('ios-'):
        from .ios_cli import main as ios_main
        return ios_main(effective)
    if effective and effective[0] in {'android-instrument', 'android-app-build'}:
        from .android_observation import cli_main
        return cli_main(effective)
    if effective and effective[0] == 'android-signing':
        from .repair_signing_cli import main as signing_main
        return signing_main(effective[1:])
    parser=argparse.ArgumentParser(prog='reproof',description='Record, replay and verify the Android sample with immutable evidence.')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('doctor',help='Check local tools and authorized device count')
    sub.add_parser('installation-check',help='Check public installed resources without accessing devices or credentials')
    sub.add_parser('android-signing',help='Inspect or recover existing Android signing operations; use --help')
    sub.add_parser('ios-signing',help='Build fixed iOS signing tools, inspect or recover operations; use --help')
    sub.add_parser('ios-mobile',help='Inspect or recover iOS preparation operations; use --help')
    sub.add_parser('protected-service',help='Validate public protected service configuration; use --help')
    assets=sub.add_parser('export-resources',help='Copy public native build sources to a new directory')
    assets.add_argument('--output-new',type=Path,required=True)
    instrument=sub.add_parser('instrument',help='Prepare automatic test-build logging in a new workspace')
    instrument.add_argument('--source',type=Path,required=True)
    instrument.add_argument('--output',type=Path,required=True)
    instrument.add_argument('--mode',choices=['build','source'],default='build',
                            help='Build-time bytecode instrumentation (default), or explicit source insertion')
    for name in ('ios-doctor','ios-build','ios-record','ios-replay','ios-repair'):
        sub.add_parser(name,help='iOS Simulator workflow; use this command with --help')
    build=sub.add_parser('build',help='Build one sample variant offline and write source/APK receipt')
    build.add_argument('--source',type=Path,default=ROOT/'android');build.add_argument('--variant',choices=['buggy','fixed'],default='buggy')
    build.add_argument('--receipt',type=Path,help='Write the legacy single-build receipt')
    build.add_argument('--output',type=Path,help='Freeze original.apk, driver.apk and receipt.json in DIRECTORY')
    validate=sub.add_parser('validate',help='Validate and compile an immutable bundle')
    validate.add_argument('bundle',type=Path)
    record=sub.add_parser('record',help='Install sample and record a complete fixture-started session')
    record.add_argument('--apk',type=Path,required=True);record.add_argument('--driver-apk',type=Path,required=True)
    record.add_argument('--receipt',type=Path,required=True);record.add_argument('--output',type=Path,required=True)
    record.add_argument('--scripted',action='store_true',help='Generate synthetic QA actions through the real ID driver')
    replay=sub.add_parser('replay',help='Reproduce an original bundle on a real device')
    replay.add_argument('bundle',type=Path);replay.add_argument('--output',type=Path,required=True)
    repair=sub.add_parser('repair',help='Reproduce, propose isolated edits, rebuild buggy variant and verify')
    repair.add_argument('bundle',type=Path);repair.add_argument('--source',type=Path,default=ROOT/'android')
    repair.add_argument('--output',type=Path,required=True)
    repair.add_argument('--driver-apk',type=Path,help='Frozen original driver APK used for protected replay')
    repair.add_argument('--driver-sha256',help='SHA-256 of --driver-apk; both driver options are required together')
    group=repair.add_mutually_exclusive_group(required=True)
    group.add_argument('--agent',choices=['claude'],help='Explicitly send synthetic sample source/QA packet to Claude')
    group.add_argument('--patch-file',type=Path,help='Offline edit fixture; this is not an AI invocation')
    repair.add_argument('--max-attempts',type=int,default=3)
    tools=sub.add_parser('tools',help='JSON-lines tool bridge for agents; no unrestricted shell')
    tools.add_argument('--bundle',type=Path,required=True);tools.add_argument('--output',type=Path,required=True)
    for cmd in [record,replay,repair,tools]:cmd.add_argument('--serial',help='Select an authorized Android device')
    for cmd in [replay,repair]:cmd.add_argument('--repeats',type=int,default=3)
    for cmd in [build,repair]:
        cmd.add_argument('--gradle');cmd.add_argument('--java-home');cmd.add_argument('--sdk-home')
    for cmd in [build,record,replay,repair,validate,tools,instrument]:
        cmd.add_argument('--app-profile',type=Path,required=cmd is instrument,help='Administrator-selected Android app contract JSON')
    args=parser.parse_args(argv)
    try:
        app_profile=load_app_profile(args.app_profile) if getattr(args,'app_profile',None) else None
        if args.command=='export-resources':
            print_summary(export_resources(args.output_new));return 0
        if args.command=='instrument':
            from .instrumentation import prepare_instrumentation
            print_summary(prepare_instrumentation(args.source,app_profile,args.output,mode=args.mode))
            return 0
        if args.command=='doctor':
            value={'python':sys.version.split()[0],'adbAvailable':False,'deviceReady':False}
            try:
                value['adbAvailable']=bool(find_adb());device=AdbDevice();value['deviceReady']=True;value['deviceId']=device.identity
            except (ContractError,DeviceError):pass
            try:toolchain(args);value['androidToolchainReady']=True
            except ContractError:value['androidToolchainReady']=False
            print_summary(value);return 0 if value['deviceReady'] and value['androidToolchainReady'] else 2
        if args.command=='build':
            if bool(args.receipt) == bool(args.output):
                raise ContractError('Provide exactly one of --receipt or --output')
            if app_profile and not args.output:
                raise ContractError('App profiles require build --output with a protected runner')
            task=f':sample:assemble{args.variant.title()}Debug';relative=f'sample/build/outputs/apk/{args.variant}/debug/sample-{args.variant}-debug.apk'
            selected=toolchain(args)
            if args.output:
                receipt=create_protected_build(args.source,args.output,variant=args.variant,app_profile=app_profile,**selected)
                print_summary({'status':'built','apk':args.output/'original.apk','driverApk':args.output/'driver.apk',
                               'receipt':args.output/'receipt.json','sourceDigest':receipt['sourceDigest']});return 0
            apk,proof=build_android(args.source,task=task,apk_relative=relative,**selected)
            driver,_=build_android(args.source,task=':driver:assembleDebug',apk_relative='driver/build/outputs/apk/debug/driver-debug.apk',**selected)
            write_json(args.receipt,proof);print_summary({'status':'built','apk':apk,'driverApk':driver,'receipt':args.receipt});return 0
        if args.command=='validate':
            b=load_bundle(args.bundle,app_profile=app_profile);print_summary({'status':'compiled','events':len(b['scenario']['steps']),
                                                     'scenarioDigest':b['scenario']['scenarioDigest']});return 0
        if args.command=='repair':
            if bool(args.driver_apk) != bool(args.driver_sha256):
                raise ContractError('--driver-apk and --driver-sha256 must be provided together')
            if args.driver_apk:
                require_driver = (args.driver_apk.is_file() and not args.driver_apk.is_symlink()
                                  and 0 < args.driver_apk.stat().st_size <= MAX_APK)
                if not require_driver or sha_file(args.driver_apk) != args.driver_sha256:
                    raise ContractError('Frozen driver APK does not match --driver-sha256')
            if app_profile and not args.driver_apk:
                raise ContractError('App-profile repair requires the frozen driver APK and SHA-256')
        if app_profile and args.command=='record' and args.scripted:
            raise ContractError('The fixed --scripted demo is unavailable for a custom app profile')
        device=AdbDevice(args.serial,app_profile=app_profile)
        if args.command=='repair' and args.driver_apk:
            # replay.py enforces this immutable pair before and after every run.
            device.protected_driver=(args.driver_apk.resolve(),args.driver_sha256)
        if args.command=='record':
            receipt=read_json(args.receipt)
            if receipt.get('apkSha256')!=sha_file(args.apk):raise ContractError('Receipt does not match recording APK')
            if app_profile and receipt.get('appProfileDigest')!=app_profile.digest:
                raise ContractError('Recording receipt does not match the selected app profile')
            with device.lease():
                device.install(args.driver_apk,DRIVER)
                device.prepare(args.apk,app_profile.data['fixture'] if app_profile else {'id':'default','version':1,'inputs':{}},'record')
                try:
                    if args.scripted:
                        device.driver('replace',target='name',value='QA');device.driver('tap',target='add')
                    else:
                        print('Reproduce the configured issue using QA or Test, then press Enter here.' if app_profile else
                              'Use the sample: enter QA, tap Add once. Press Enter here when the issue is visible.',flush=True)
                        input()
                    capture=device.freeze_capture()
                    diagnostics = (device.collect_instrumentation_diagnostics(capture)
                                   if app_profile and app_profile.data.get('captureMode') == 'debug_receiver' else None)
                    b=create_bundle(capture,app_profile.oracle() if app_profile else sample_oracle(),args.apk,args.output,
                                    receipt,"synthetic-driver" if args.scripted else "manual-QA",app_profile=app_profile,diagnostics=diagnostics)
                finally:device.stop()
            print_summary({'status':'captured','bundle':args.output,'events':len(b['scenario']['steps']),
                           'captureMethod':'synthetic-driver' if args.scripted else 'manual-QA'});return 0
        bundle=load_bundle(args.bundle,app_profile=app_profile)
        with device.lease():
            if args.command=='replay':
                result=replay_suite(device,bundle,bundle['apk'],args.output,'original',args.repeats,bundle['manifest'].get('sourceProof'))
            elif args.command=='repair':
                agent=ClaudeAgent() if args.agent else PatchFileAgent(args.patch_file)
                result=repair_job(device,bundle,args.source,args.output,agent,toolchain(args),repeats=args.repeats,
                                  max_attempts=args.max_attempts,app_profile=app_profile)
                result['agent']='claude' if args.agent else 'offline-patch-file';write_json(args.output/'job.json',result);write_report(args.output/'report.html',result)
            else:
                return serve_tools(device,bundle,args.output)
        print_summary({'status':result['status'],'report':args.output/'report.html','result':args.output})
        return 0 if result['status'] in {'reproduced','verified'} else 2
    except (ContractError,DeviceError,CommandError,ResourceError,OSError) as exc:
        print_summary({'status':'blocked','errorType':type(exc).__name__,'message':str(exc) if isinstance(exc,(ContractError,DeviceError,CommandError,ResourceError)) else 'Local IO failed'})
        return 2
    except (KeyboardInterrupt,EOFError):
        print_summary({'status':'cancelled'});return 130


def serve_tools(device,bundle,output):
    """Line protocol is deliberately small; clients can wrap it as MCP if desired."""
    sequence=0
    for line in sys.stdin:
        try:
            if len(line)>65536:raise ContractError('Tool request too large')
            request=json.loads(line)
            if not isinstance(request,dict) or set(request)!={'tool'}:raise ContractError('Invalid tool request')
            name=request.get('tool')
            if name=='session.inspect':
                response={'scenario':bundle['scenario'],'bundleDigest':bundle['manifestDigest']}
            elif name=='device.observe':response={'nodes':device.observe(),'diagnosticOnly':True}
            elif name=='scenario.run':
                sequence+=1;response=replay_suite(device,bundle,bundle['apk'],Path(output)/f'run-{sequence}')
            else:raise ContractError('Unknown tool; available: session.inspect, device.observe, scenario.run')
            print(json.dumps({'ok':True,'result':response}),flush=True)
        except (ValueError,ContractError,DeviceError,OSError):print(json.dumps({'ok':False,'error':'Tool request could not complete'}),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
