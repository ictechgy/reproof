from __future__ import annotations
import argparse
from pathlib import Path
import re
from .model import Lab
from .providers import demo_device,ios_device
from .server import LiveServer
from ..resources import resource_root

ROOT=resource_root()


def main(argv=None):
    parser=argparse.ArgumentParser(
        prog='reproof live-serve',
        description='Single-user loopback console or authenticated shared Live coordinator')
    parser.add_argument('command',choices=['live-serve'])
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--output',type=Path,default=Path.cwd()/'artifacts/live')
    parser.add_argument('--simulator',action='append',default=[],help='Already booted iOS Simulator UUID, with target app installed')
    parser.add_argument('--products',type=Path,default=ROOT/'live-ios/build/Build/Products')
    parser.add_argument('--ios-app',type=Path,help='Simulator app artifact to install for Live')
    parser.add_argument('--ios-profile',type=Path,help='Registered general iOS runtime application profile JSON')
    parser.add_argument('--app-logs',action='store_true',help='Record automatic app observations without enabling AI repair')
    parser.add_argument('--bundle',default='io.reproof.sample.ios')
    parser.add_argument('--android',action='append',default=[],help='Authorized ADB serial; batch-input baseline, no reset/replay fixture')
    parser.add_argument('--demo',action='store_true',help='Include explicitly synthetic counter device')
    parser.add_argument('--workers-stdin',action='store_true',help='Read authenticated worker configurations from stdin')
    parser.add_argument('--android-helper',type=Path,help='Persistent Live helper APK for continuous Android input')
    parser.add_argument('--android-app',type=Path,help='Selected Android APK to install for Live')
    parser.add_argument('--android-profile',type=Path,help='Registered general Android runtime application profile JSON')
    parser.add_argument('--app-profile',type=Path,help='Explicit Android application and replay contract JSON')
    parser.add_argument('--iphone',help='Public ID from live-device-doctor for a paired physical iPhone')
    parser.add_argument('--iphone-products',type=Path,help='Signed ReproLive iphoneos Build/Products directory')
    parser.add_argument('--iphone-app',type=Path,help='Signed ReproSample.app to install for the physical fixture')
    parser.add_argument('--demo-count',type=int,default=1,help='Synthetic devices for local queue testing, 1–8')
    parser.add_argument('--idle-timeout',type=int,default=120)
    parser.add_argument('--max-session-seconds',type=int,default=900)
    parser.add_argument('--job-concurrency',type=int,default=2)
    parser.add_argument('--iphone-case',choices=['counter','duplicate-submit','reset'],default='counter')
    parser.add_argument('--repair-source',type=Path,help='Protected sample source for opt-in Live repair')
    parser.add_argument('--repair-build',type=Path,help='Matching ios-build or Android build --output directory with protected replay artifacts')
    parser.add_argument('--repair-agent',choices=['claude'],help='Enable actual Claude requests for the configured sample source and QA')
    parser.add_argument('--authority-mode',choices=['shared-v2','legacy-offline-v1'],default='shared-v2',
                        help='Native ownership protocol; legacy mode is explicit offline-only compatibility')
    parser.add_argument('--shared-config',type=Path,
                        help='Non-secret shared coordinator configuration; credentials remain on stdin')
    parser.add_argument('--issue-config',type=Path,
                        help='Trusted project preparation, variable names and observation registrations')
    parser.add_argument('--protected-recovery-config',type=Path,
                        help='Register authenticated Android operation recovery; protected repair execution stays deferred')
    parser.add_argument('--video-helper',type=Path,
                        help='Owned macOS H.264 encoder executable for issue recordings')
    parser.add_argument('--media-validator',type=Path,
                        help='Owned native media validator executable for package import/export')
    args=parser.parse_args(argv)
    if args.issue_config and not args.shared_config:
        parser.error('--issue-config requires --shared-config')
    if args.protected_recovery_config and not (args.shared_config and args.issue_config):
        parser.error('--protected-recovery-config requires --shared-config and --issue-config')
    if args.protected_recovery_config and (args.android or args.simulator or args.iphone or args.demo
            or args.workers_stdin or args.repair_agent or args.authority_mode!='shared-v2'
            or args.android_helper or args.android_app or args.android_profile or args.app_profile
            or args.ios_app or args.ios_profile or args.iphone_app or args.iphone_products or args.app_logs):
        parser.error('Protected recovery uses only devices from its registered configuration')
    if (args.video_helper or args.media_validator) and not args.issue_config:
        parser.error('Issue media helpers require --issue-config')
    issue_configuration=None;issue_bundle=None
    protected_recovery_configuration=None
    if args.protected_recovery_config:
        try:
            from ..repair_configuration import load_protected_service_configuration
            protected_recovery_configuration=load_protected_service_configuration(args.protected_recovery_config)
        except Exception:
            parser.error('Protected recovery configuration is invalid')
    if args.issue_config:
        try:
            from .issue_configuration import load_issue_configuration
            issue_configuration=load_issue_configuration(args.issue_config)
        except Exception:
            parser.error('Trusted issue runtime configuration is invalid')
    shared_configuration=None;shared_store=None
    if args.shared_config:
        try:
            from .access import AccessStore
            from .configuration import load_shared_configuration
            shared_configuration=load_shared_configuration(args.shared_config)
            shared_store=AccessStore(shared_configuration.state_root)
        except Exception:
            parser.error('Shared coordinator configuration or access store is incompatible')
        if args.repair_agent:
            parser.error('Legacy Live repair is unavailable in shared mode')
        coordinator_output=args.output.absolute()
        access_root=shared_configuration.state_root.absolute()
        if (coordinator_output==access_root or access_root in coordinator_output.parents
                or coordinator_output in access_root.parents):
            parser.error('Coordinator output and access state require distinct namespaces')
    app_logs_only=args.app_logs and not args.repair_agent
    if args.ios_app and not args.simulator:parser.error('--ios-app requires a Simulator')
    if args.app_logs and not (args.simulator or args.iphone or args.android):
        parser.error('--app-logs requires an instrumented native app')
    app_profile=None
    android_profile=None
    ios_profile=None
    if args.ios_profile:
        if sum(bool(value) for value in (args.simulator,args.iphone)) != 1 or args.repair_agent:
            parser.error('--ios-profile requires one iOS device kind and the general issue repair workflow')
        from ..ios_profile import load_ios_profile
        ios_profile=load_ios_profile(args.ios_profile)
        if args.app_logs and ios_profile.data['capabilities']['logAdapter'] is None:
            parser.error('--app-logs requires the declared iOS observation adapter')
    if args.app_profile:
        if not args.android or not args.android_helper or not args.android_app or args.iphone:
            parser.error('App profiles require an Android Live helper and APK')
        from ..android_profile import load_app_profile
        app_profile=load_app_profile(args.app_profile)
    if args.android_profile:
        if not (args.android and args.android_helper and args.android_app) or args.app_profile or args.repair_agent:
            parser.error('--android-profile requires an Android helper and APK, with the general issue repair workflow')
        from ..android_profile import load_android_runtime_profile
        android_profile=load_android_runtime_profile(args.android_profile)
        if args.app_logs and android_profile.data['capabilities']['logAdapter'] is None:
            parser.error('--app-logs requires the declared Android observation adapter')
    if not 0<=args.port<=65535:parser.error('Port must be 0–65535')
    repair_options=[args.repair_source,args.repair_build,args.repair_agent]
    if any(repair_options):
        if not all(repair_options) or sum(bool(value) for value in (args.iphone,args.android,args.simulator)) != 1:
            parser.error('Live repair requires one platform (--iphone, --simulator, or --android), --repair-source, --repair-build, and --repair-agent together')
        if args.simulator and len(args.simulator) != 1:parser.error('Select one Simulator for Live repair')
        if args.android and not (args.android_helper and args.android_app):
            parser.error('Android repair needs --android-helper and --android-app from the protected sample build')
    devices=[]
    if protected_recovery_configuration is not None:
        try:
            from ..protected_mobile_inputs import recovery_device_descriptors
            devices=recovery_device_descriptors(protected_recovery_configuration)
        except Exception:
            parser.error('Protected recovery device metadata is invalid')
    for simulator_id in args.simulator:
        from ..ios_runner import IosSimulator
        IosSimulator(simulator_id)
        if not args.products.is_dir():parser.error('Build live-ios before starting the native provider')
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,200}',args.bundle):parser.error('Invalid bundle identifier')
        sample_app=args.ios_app
        if args.repair_agent:
            from ..storage import read_json
            from ..ios_storage import checked_relative
            receipt=read_json(args.repair_build/'receipt.json')
            if receipt.get('executionEnvironment') != 'simulator':parser.error('Simulator repair needs a Simulator build')
            sample_app=checked_relative(args.repair_build/'DerivedData/Build/Products',receipt['appRelative'])
            if args.ios_app and args.ios_app.resolve()!=sample_app.resolve():parser.error('--ios-app differs from the protected repair build')
        if args.app_logs and sample_app is None:parser.error('--app-logs requires --ios-app or a protected repair build')
        if ios_profile is not None:
            if sample_app is None:parser.error('--ios-profile requires --ios-app')
            devices.append(ios_device(simulator_id,args.products,ios_profile.bundle,app=sample_app,
                authority_mode=args.authority_mode,profile=ios_profile))
        elif args.repair_agent or args.app_logs:
            # Configure identity from the same artifact that start() installs.
            from ..ios_storage import tree_manifest
            from ..core import digest
            from .providers import IosProvider,CAPABILITIES
            from ..ios_instrumentation import app_logs_enabled
            import hashlib
            logs=app_logs_enabled(sample_app)
            if args.app_logs and not logs:parser.error('Selected Simulator app does not contain automatic app logging')
            identity={'bundle':args.bundle,'artifactDigest':digest(tree_manifest(sample_app))}
            devices.append({'id':'ios-simulator-'+hashlib.sha256(simulator_id.encode()).hexdigest()[:16],
                'name':'iOS Simulator','platform':'ios','kind':'ios-simulator',
                'capabilities':dict(CAPABILITIES,applicationIdentity=identity,fixture=args.iphone_case,sdkCapture=not app_logs_only,automaticAppLogs=logs,
                                    resetContract='sample-'+args.iphone_case+'-fixture-v1',authorityMode=args.authority_mode),
                **({'_authority':{'deviceKind':'ios-simulator','physicalId':simulator_id}}
                   if args.authority_mode=='shared-v2' else {}),
                'factory':lambda selected=simulator_id,app=sample_app,value=identity:IosProvider(
                    selected,args.products,args.bundle,value,app=app,fixture=args.iphone_case,record_sdk=True,app_logs_only=app_logs_only)})
        else:devices.append(ios_device(simulator_id,args.products,args.bundle,app=sample_app,authority_mode=args.authority_mode))
    for android_serial in args.android:
        if android_serial=='auto':
            from ..device import AdbDevice
            android_serial=AdbDevice().serial
        from .android import android_device
        if args.app_logs and android_profile is None and not (args.android_helper and args.android_app and app_profile and app_profile.data.get('appLogs')==1):
            parser.error('Android app logs require --android-helper, --android-app and a prepared --app-profile')
        if args.android_helper or args.android_app:
            if not args.android_helper or not args.android_app:parser.error('Use --android-helper and --android-app together')
            from .android_live import android_live_device
            devices.append(android_live_device(android_serial,args.android_helper,args.android_app,
                                                record_sdk=bool(args.repair_agent or args.app_logs),app_profile=app_profile,
                                                runtime_profile=android_profile,
                                                authority_mode=args.authority_mode))
        else:devices.append(android_device(android_serial,authority_mode=args.authority_mode))
    if not 1<=args.demo_count<=8:parser.error('Demo device count must be 1–8')
    if args.iphone:
        if args.iphone_products is None or args.iphone_app is None:parser.error('Physical iPhone needs --iphone-products and --iphone-app from a signed build')
        from .iphone import iphone_device
        if args.app_logs:
            from ..ios_instrumentation import app_logs_enabled
            if not app_logs_enabled(args.iphone_app):parser.error('Selected iPhone app does not contain automatic app logging')
        devices.append(iphone_device(args.iphone,args.iphone_products,args.iphone_app,fixture=args.iphone_case,
                                     record_sdk=bool(args.repair_agent or args.app_logs),app_logs_only=app_logs_only,
                                     authority_mode=args.authority_mode,profile=ios_profile))
    if args.demo:
        for index in range(args.demo_count):
            device=demo_device()
            if index:device.update(id=f'demo-{index+1}',name=f'Synthetic counter {index+1}')
            devices.append(device)
    if args.workers_stdin:
        from .worker_cli import stdin_object,configured_remote_devices
        devices.extend(configured_remote_devices(
            stdin_object(),access_store=shared_store) if shared_store is not None
            else configured_remote_devices(stdin_object()))
    if not devices:parser.error('Select --simulator, --iphone, --android, or --demo')
    if len({d['id'] for d in devices})!=len(devices):parser.error('Each device may be registered only once')
    authority=None;parent_grant=None;project_grant_provider=None
    if shared_configuration is not None and any(
            '_authority' in item or item.get('_remoteAuthority') is True for item in devices):
        try:
            from .authority import HostAuthority
            from .configuration import issue_bounded_project_grant
            authority=HostAuthority(
                shared_configuration.state_root.parent/'host-authority-v1'/'authority.sqlite3')
            def project_grant_provider(registration):
                from .model import check
                current=shared_store.project(registration.project['id'])
                check(current['projectDigest']==registration.project_digest,
                      'recording_identity','Project registration changed',409)
                return issue_bounded_project_grant(
                    authority,registration.project['id'],
                    lifetime_seconds=args.max_session_seconds)
        except Exception:
            if authority is not None:
                try:authority.close()
                except Exception:pass
            parser.error('Shared host authority prerequisite is unavailable')
    lab=Lab(devices,args.output,idle_timeout=args.idle_timeout,
            max_session_seconds=args.max_session_seconds,
            authority=authority,parent_grant=parent_grant,
            project_grant_provider=project_grant_provider)
    repair_project={'source':args.repair_source,'build':args.repair_build,
                    'platform':'android' if args.android else 'ios','app_profile':app_profile} if args.repair_agent else None
    access=None
    if shared_configuration is not None:
        try:
            from .configuration import compose_shared_access,create_server_ssl_context
            access,_registrations=compose_shared_access(
                lab,shared_configuration,store=shared_store)
            ssl_context=create_server_ssl_context(shared_configuration)
            if issue_configuration is not None:
                if protected_recovery_configuration is not None:
                    from .protected_recovery import compose_recovery_workflow
                    issue_bundle=compose_recovery_workflow(lab,access,protected_recovery_configuration,issue_configuration,
                        root=args.output/'issue-runtime-v1',video_helper=args.video_helper,media_helper=args.media_validator)
                else:
                    from .issue_configuration import compose_issue_workflow
                    issue_bundle=compose_issue_workflow(lab,access,issue_configuration,
                        root=args.output/'issue-runtime-v1',video_helper=args.video_helper,media_helper=args.media_validator)
        except Exception:
            if issue_bundle is not None:issue_bundle.close()
            lab.close_all()
            if authority is not None:
                try:authority.close()
                except Exception:pass
            shared_store.close()
            parser.error('Shared coordinator prerequisites are incomplete')
        server=LiveServer(
            lab,shared_configuration.port,max_jobs_running=args.job_concurrency,
            access=access,host=shared_configuration.host,ssl_context=ssl_context,
            origin=shared_configuration.origin,
            issue_workflow=issue_bundle.workflow if issue_bundle is not None else None,
            protected_recovery=issue_bundle.protected_recovery if issue_bundle is not None else None,
            public_configuration=shared_configuration.public())
    else:
        server=LiveServer(
            lab,args.port,max_jobs_running=args.job_concurrency,
            repair_project=repair_project)
    print(f'Reproof Live: {server.origin}',flush=True)
    print(('Shared project coordinator · authenticated membership'
           if access is not None else
           'Local single-user console · sampled frames · gesture-batch input'),flush=True)
    try:server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:pass
    finally:
        # Keep the bridge HTTP server available while the persistent runner exits.
        import threading
        draining=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.1},daemon=True);draining.start()
        server.close_operations();lab.close_all();server.shutdown();draining.join(timeout=2);server.server_close()
        if issue_bundle is not None:
            issue_bundle.close()
        if authority is not None:
            authority.close()
        if shared_store is not None:
            shared_store.close()
    return 0
