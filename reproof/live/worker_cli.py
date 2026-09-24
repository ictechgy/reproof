"""Explicit worker/coordinator configuration with secrets kept off command lines."""
from __future__ import annotations
import argparse
import getpass
import json
from pathlib import Path
import ssl
import subprocess
import sys
import threading
from .model import Lab,check,LiveError
from .providers import demo_device,ios_device
from .worker import WorkerServer,WorkerClient,remote_devices
from ..core import ContractError
from ..storage import _unique_object


def stdin_object(limit=64*1024):
    raw=sys.stdin.read(limit+1)
    check(len(raw.encode())<=limit,'invalid_config','Worker configuration exceeds the limit',400)
    value=json.loads(raw,object_pairs_hook=_unique_object,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    check(isinstance(value,dict),'invalid_config','Expected a worker configuration object',400)
    return value


def configured_remote_devices(value, *, access_store=None):
    check(set(value)=={'workers'} and isinstance(value['workers'],list) and 0<len(value['workers'])<=16,
          'invalid_config','Expected 1–16 worker configurations',400)
    devices=[];ids=set()
    for entry in value['workers']:
        required={'id','url','token','hostCredential'} if access_store is not None else {'id','url','token'}
        allowed=required|{'caFile'}
        check(isinstance(entry,dict) and required<=set(entry)<=allowed,
              'invalid_config','Invalid worker configuration',400)
        check(entry['id'] not in ids,'invalid_config','Worker IDs must be unique',400);ids.add(entry['id'])
        host=None
        if access_store is not None:
            host=access_store.authenticate_host(entry['hostCredential'])
            check(host.host_id==entry['id'],'invalid_config',
                  'Worker identity does not match its host credential',400)
        client=WorkerClient(entry['url'],entry['token'],entry.get('caFile'))
        remote=remote_devices(client,entry['id'])
        if access_store is not None:
            for descriptor in remote:
                assignment=access_store.device_assignment(descriptor['id'])
                check(assignment['hostId']==host.host_id
                      and assignment['hostGeneration']==host.generation
                      and assignment['hostIncarnation']==host.incarnation,
                      'invalid_config',
                      'Remote device is not assigned to its enrolled host',400)
                projects=([assignment['projectId']] if assignment['projectId'] else
                          [project['projectId'] for project in access_store.list_projects()
                           if project['trustGroup']==assignment['trustGroup']])
                check(bool(projects),'invalid_config',
                      'Remote device assignment has no project scope',400)
                for project_id in projects:
                    project=access_store.project(project_id)
                    access_store.authorize_host(
                        host,project_id=project_id,trust_group=project['trustGroup'])
                descriptor['_inventoryBinding']={
                    'hostId':host.host_id,'generation':host.generation,
                    'incarnation':host.incarnation,
                    'alias':descriptor['id'][len(host.host_id)+2:],
                    'profileDigest':descriptor['capabilities'].get('applicationProfileDigest')}
        devices.extend(remote)
    return devices


def connected_worker_devices(devices):
    """Read presence for configured identities; never claim or reset a device."""
    kinds={device['_authority']['deviceKind'] for device in devices if '_authority' in device}
    present={}
    if 'android' in kinds:
        try:
            from ..device import find_adb
            result=subprocess.run([find_adb(),'devices'],stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,timeout=3,check=True)
            present['android']={fields[0] for line in result.stdout.decode().splitlines()[1:]
                                if len(fields:=line.split())==2 and fields[1]=='device'}
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
            present['android']=set()
    if 'ios-physical' in kinds:
        try:
            from .iphone import _devicectl
            result=_devicectl('list','devices',timeout=3)
            present['ios-physical']={
                item['hardwareProperties']['udid'] for item in result['devices']
                if item.get('hardwareProperties',{}).get('deviceType')=='iPhone'
                and item.get('connectionProperties',{}).get('pairingState')=='paired'
                and item.get('connectionProperties',{}).get('tunnelState')=='connected'}
        except (OSError,ValueError,KeyError,TypeError,ContractError,subprocess.SubprocessError):
            present['ios-physical']=set()
    if 'ios-simulator' in kinds:
        try:
            result=subprocess.run(['/usr/bin/xcrun','simctl','list','devices','booted','--json'],
                                  stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                  timeout=3,check=True)
            value=json.loads(result.stdout)
            present['ios-simulator']={item['udid'] for group in value['devices'].values()
                                      for item in group if item.get('state')=='Booted'}
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
            present['ios-simulator']=set()
    return {device['id'] for device in devices if '_authority' not in device
            or device['_authority']['physicalId'] in present.get(
                device['_authority']['deviceKind'],set())}


def _unique_worker_devices(devices):
    from .inventory import canonical_device_digest
    aliases=set();physical=set()
    for device in devices:
        check(device['id'] not in aliases,'duplicate_device','Worker device alias is duplicated',400)
        aliases.add(device['id'])
        if '_authority' in device:
            binding=device['_authority']
            identity=canonical_device_digest(binding['deviceKind'],binding['physicalId'])
            check(identity not in physical,'duplicate_device','Physical worker device is duplicated',400)
            physical.add(identity)
    return devices


def configured_local_devices(path):
    """Load explicitly trusted local product paths and one profile per device."""
    path=Path(path).absolute()
    with path.open('rb') as stream:raw=stream.read(128*1024+1)
    check(len(raw)<=128*1024,'invalid_config','Worker device configuration is too large',400)
    value=json.loads(raw,object_pairs_hook=_unique_object,
                     parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    check(type(value) is dict and set(value)=={'schemaVersion','devices'}
          and type(value['schemaVersion']) is int and value['schemaVersion']==1
          and type(value['devices']) is list and 1<=len(value['devices'])<=16,
          'invalid_config','Invalid worker device configuration',400)
    seen=set()
    for item in value['devices']:
        check(type(item) is dict and type(item.get('platform')) is str
              and item['platform'] in {'android','ios-physical'},
              'invalid_config','Unsupported configured device platform',400)
        product_key='helper' if item['platform']=='android' else 'products'
        check(set(item)=={'platform','deviceId','profile','application',product_key},
              'invalid_config','Invalid configured device fields',400)
        check(all(type(item[key]) is str and 0<len(item[key])<=1024
                  and not any(ord(character)<32 for character in item[key])
                  for key in ('deviceId','profile','application',product_key)),
              'invalid_config','Invalid configured device value',400)
        identity=(item['platform'],item['deviceId'])
        check(identity not in seen,'duplicate_device','Physical worker device is duplicated',400)
        seen.add(identity)
    result=[]
    for item in value['devices']:
        selected=lambda key:path.parent/Path(item[key])
        if item['platform']=='android':
            from ..android_profile import load_android_runtime_profile
            from .android_live import android_live_device
            profile=load_android_runtime_profile(selected('profile'))
            result.append(android_live_device(
                item['deviceId'],selected('helper'),selected('application'),
                runtime_profile=profile,authority_mode='shared-v2'))
        else:
            from ..ios_profile import load_ios_profile
            from .iphone import iphone_device
            profile=load_ios_profile(selected('profile'))
            result.append(iphone_device(
                item['deviceId'],selected('products'),selected('application'),
                profile=profile,authority_mode='shared-v2'))
    return _unique_worker_devices(result)


def main(argv=None):
    parser=argparse.ArgumentParser(prog='reproof live-worker',description='Device worker with authenticated private transport')
    parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=9876)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--advertised-host')
    parser.add_argument('--tls-cert',type=Path);parser.add_argument('--tls-key',type=Path)
    authentication=parser.add_mutually_exclusive_group()
    authentication.add_argument('--token-stdin',action='store_true',
                                help='Legacy transport-only input: {"token":...}')
    authentication.add_argument('--enrollment-stdin',action='store_true',
                                help='Read one-time enrollment and transport credentials from stdin')
    authentication.add_argument('--host-credential-stdin',action='store_true',
                                help='Read enrolled host and transport credentials from stdin')
    parser.add_argument('--coordinator',help='Shared coordinator origin for host enrollment authentication')
    parser.add_argument('--coordinator-ca',type=Path)
    parser.add_argument('--host-id');parser.add_argument('--host-incarnation')
    parser.add_argument('--host-credential-output',type=Path)
    parser.add_argument('--authority-root',type=Path,
                        help='Service-owned G1 authority root, separate from worker output')
    parser.add_argument('--devices-config',type=Path,
                        help='Trusted local device/product configuration with one runtime profile per device')
    parser.add_argument('--android',action='append',default=[],help='ADB serial or auto for a single connected device')
    parser.add_argument('--android-helper',type=Path);parser.add_argument('--android-app',type=Path)
    parser.add_argument('--android-profile',type=Path,
                        help='Trusted version-2 Android runtime application profile')
    parser.add_argument('--simulator',action='append',default=[]);parser.add_argument('--ios-products',type=Path)
    parser.add_argument('--ios-profile',type=Path,
                        help='Trusted version-2 iOS runtime application profile')
    parser.add_argument('--iphone',action='append',default=[],
                        help='Public physical iPhone identifier (never a raw UDID)')
    parser.add_argument('--iphone-app',type=Path)
    parser.add_argument('--project-registration',type=Path,action='append',default=[],
                        help='Trusted local {project,collectionPolicy} registration')
    parser.add_argument('--bundle',default='io.reproof.sample.ios');parser.add_argument('--demo',action='store_true')
    parser.add_argument('--authority-mode',choices=['shared-v2','legacy-offline-v1'],default='shared-v2')
    args=parser.parse_args(argv)
    if args.devices_config and (args.android or args.iphone or args.simulator or args.demo
            or args.android_profile or args.ios_profile or args.android_helper
            or args.android_app or args.ios_products or args.iphone_app
            or args.authority_mode!='shared-v2'):
        parser.error('--devices-config requires shared-v2 and supplies all device and product selections')
    authority=None
    artifact_store=None;artifact_evidence=None;artifact_budget=None
    inventory_stop=None;inventory_thread=None
    credential_output=None
    try:
        host_public=None
        enrolled=args.enrollment_stdin or args.host_credential_stdin
        if enrolled:
            check(args.coordinator and args.host_id and args.host_incarnation
                  and args.authority_root is not None,
                  'invalid_config','Enrolled workers require coordinator, host identity, incarnation, and authority root',400)
            check(args.authority_mode=='shared-v2','invalid_config',
                  'Enrolled workers require shared-v2 authority',400)
            value=stdin_object()
            from .enrollment import EnrollmentClient,PrivateCredentialOutput
            client=EnrollmentClient(args.coordinator,args.coordinator_ca)
            if args.enrollment_stdin:
                check(set(value)=={'enrollmentToken','transportToken'}
                      and args.host_credential_output is not None,
                      'invalid_config','Enrollment input and private credential output are required',400)
                output_path=args.host_credential_output.absolute()
                authority_root=args.authority_root.absolute()
                worker_output=args.output.absolute()
                check(output_path not in {authority_root,worker_output}
                      and authority_root not in output_path.parents
                      and worker_output not in output_path.parents,
                      'invalid_config','Host credential output must be outside worker state',400)
                credential_output=PrivateCredentialOutput(output_path)
                host_public=client.enroll(
                    value['enrollmentToken'],host_id=args.host_id,
                    incarnation=args.host_incarnation)
                credential_output.write({'credential':host_public['credential']})
                host_credential=host_public.pop('credential')
            else:
                check(set(value)=={'hostCredential','transportToken'}
                      and args.host_credential_output is None,
                      'invalid_config','Expected host and transport credentials only',400)
                host_credential=value['hostCredential']
                host_public=client.authenticate(host_credential)
            check(host_public['hostId']==args.host_id
                  and host_public['incarnation']==args.host_incarnation,
                  'invalid_config','Host credential identity is stale',400)
            token=value['transportToken']
        elif args.token_stdin:
            value=stdin_object();check(set(value)=={'token'},'invalid_config','Expected only a worker token',400);token=value['token']
        else:
            check(sys.stdin.isatty(),'token_required','Use --token-stdin for noninteractive worker startup',400)
            token=getpass.getpass('Worker token: ')
        devices=[]
        if args.devices_config is not None:
            devices.extend(configured_local_devices(args.devices_config))
        android_profile=None
        if args.android_profile is not None:
            from ..android_profile import load_android_runtime_profile
            android_profile=load_android_runtime_profile(args.android_profile)
        ios_profile=None
        if args.ios_profile is not None:
            from ..ios_profile import load_ios_profile
            ios_profile=load_ios_profile(args.ios_profile)
        for serial in args.android:
            from ..device import AdbDevice
            actual=AdbDevice(None if serial=='auto' else serial)
            if args.android_helper or args.android_app:
                check(args.android_helper is not None and args.android_app is not None,'missing_build','Supply both Android APK paths',400)
                from .android_live import android_live_device
                devices.append(android_live_device(actual.serial,args.android_helper,args.android_app,
                                                    runtime_profile=android_profile,
                                                    authority_mode=args.authority_mode))
            else:
                from .android import android_device
                devices.append(android_device(actual.serial,authority_mode=args.authority_mode))
        for simulator in args.simulator:
            check(ios_profile is None,'invalid_config',
                  'Version-2 iOS profiles currently require the physical iPhone provider',400)
            check(args.ios_products is not None,'missing_build','Supply Simulator test products',400)
            devices.append(ios_device(simulator,args.ios_products,args.bundle,authority_mode=args.authority_mode))
        for public_id in args.iphone:
            check(args.ios_products is not None and args.iphone_app is not None
                  and ios_profile is not None,'missing_build',
                  'Physical iPhone workers require products, application, and a version-2 profile',400)
            from .iphone import iphone_device
            devices.append(iphone_device(public_id,args.ios_products,args.iphone_app,
                                         profile=ios_profile,
                                         authority_mode=args.authority_mode))
        if args.demo:devices.append(demo_device())
        check(bool(devices),'missing_devices','Configure at least one worker device',400)
        _unique_worker_devices(devices)
        context=None
        if args.tls_cert or args.tls_key:
            check(args.tls_cert is not None and args.tls_key is not None,'invalid_tls','Supply both TLS certificate and key',400)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(args.tls_cert,args.tls_key)
        if enrolled:
            from .authority import HostAuthority
            authority_path=args.authority_root.absolute()/'authority.sqlite3'
            worker_output=args.output.absolute();authority_root=args.authority_root.absolute()
            check(worker_output!=authority_root
                  and authority_root not in worker_output.parents
                  and worker_output not in authority_root.parents
                  and args.authority_root.absolute().name!='coordinator-v2',
                  'invalid_config','Worker authority requires a distinct service namespace',400)
            authority=HostAuthority(authority_path)
            lab=Lab(devices,args.output,authority=authority,parent_grant=None,
                    delegated_authority_only=True)
        else:
            lab=Lab(devices,args.output)
        registrations=[]
        for registration_path in args.project_registration:
            from ..storage import read_json
            document=read_json(registration_path)
            check(type(document) is dict and set(document)=={'project','collectionPolicy'},
                  'invalid_config','Invalid worker project registration',400)
            registrations.append(lab.register_recording_project(
                document['project'],document['collectionPolicy']))
        host_authorizer=None
        if enrolled:
            expected_host=(host_public['hostId'],host_public['generation'],
                           host_public['incarnation'])

            def host_authorizer(project_id=None):
                current=(client.authorize(host_credential,project_id=project_id)
                         if project_id is not None else client.authenticate(host_credential))
                check((current['hostId'],current['generation'],current['incarnation'])
                      == expected_host,'unauthorized',
                      'Enrolled host authorization is stale',401)
                return True

            from .inventory import enrolled_inventory_document
            def inventory_document():
                lab.apply_device_presence(connected_worker_devices(devices))
                return enrolled_inventory_document(
                    lab.inventory_devices(),generation=expected_host[1],
                    incarnation=expected_host[2],authority=authority)
            client.refresh_inventory(host_credential,inventory_document())

            from .disk_budget import DiskBudget
            from .evidence_store import EvidenceStore
            from .artifact_transfer import ArtifactTransferStore
            check(not (args.output.absolute()/'artifact-v1').exists()
                  and not (args.output.absolute()/'evidence-v1'/'transfers').exists(),
                  'migration_required','Earlier artifact storage requires offline migration',409)
            artifact_root=args.output.absolute()/'artifact-v2'
            if lab._recording_budget is not None:
                artifact_budget=lab._recording_budget
            else:
                artifact_budget=DiskBudget(
                    artifact_root/'budget',capacity_bytes=1024*1024*1024,
                    journal_headroom_bytes=4*1024*1024)
            artifact_evidence=EvidenceStore(
                artifact_root/'objects',artifact_budget,max_object_bytes=64*1024*1024)
            artifact_store=ArtifactTransferStore(
                artifact_root/'transfers',artifact_budget,artifact_evidence,
                object_quota_bytes=64*1024*1024,
                project_quota_bytes=256*1024*1024,
                host_quota_bytes=512*1024*1024)

        server=WorkerServer(lab,token,args.host,args.port,context,
                            advertised_host=args.advertised_host,
                            host_authorizer=host_authorizer,
                            host_identity=expected_host if enrolled else None,
                            artifact_store=artifact_store,
                            registered_projects=registrations)
        if enrolled:
            inventory_stop=threading.Event()
            def refresh_inventory():
                while not inventory_stop.wait(5):
                    try:client.refresh_inventory(host_credential,inventory_document())
                    except Exception:pass
            inventory_thread=threading.Thread(
                target=refresh_inventory,name='reproof-inventory-refresh',daemon=True)
            inventory_thread.start()
        public={'worker':server.origin,'devices':len(devices)}
        if host_public is not None:
            public.update(hostId=host_public['hostId'],
                          hostGeneration=host_public['generation'],
                          hostIncarnation=host_public['incarnation'])
        print(json.dumps(public),flush=True)
        try:server.serve_forever(poll_interval=.1)
        except KeyboardInterrupt:pass
        finally:
            if inventory_stop is not None:inventory_stop.set()
            if inventory_thread is not None:
                inventory_thread.join(timeout=25)
                check(not inventory_thread.is_alive(),'shutdown_uncertain',
                      'Worker inventory refresh did not stop')
            draining=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.1},daemon=True);draining.start()
            server.close_operations();server.shutdown();draining.join(timeout=2);server.server_close()
            if authority is not None:
                authority.close();authority=None
            owned_artifact_resources=[artifact_store,artifact_evidence]
            if artifact_budget is not lab._recording_budget:
                owned_artifact_resources.append(artifact_budget)
            for resource in owned_artifact_resources:
                if resource is not None:
                    resource.close()
            artifact_store=None;artifact_evidence=None;artifact_budget=None
        return 0
    except LiveError as error:
        if authority is not None:
            try:authority.close()
            except Exception:pass
        for resource in (artifact_store,artifact_evidence,artifact_budget):
            if resource is not None:
                try:resource.close()
                except Exception:pass
        print(json.dumps({'error':{'code':error.code,'message':str(error)}}));return 2
    except (ContractError,ValueError,TypeError,OSError):
        if authority is not None:
            try:authority.close()
            except Exception:pass
        for resource in (artifact_store,artifact_evidence,artifact_budget):
            if resource is not None:
                try:resource.close()
                except Exception:pass
        print(json.dumps({'error':{'code':'worker_configuration_failed','message':'Check worker configuration and device availability'}}));return 2
    finally:
        if credential_output is not None:
            credential_output.abort()
