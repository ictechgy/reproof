"""iOS artifact manifests and strict v2 bundles."""
from __future__ import annotations
import os
from pathlib import Path,PurePosixPath
import plistlib
import shutil
from .core import digest,require
from .storage import read_json,write_json,sha_file
from .ios_core import APPLICATION_ID,EVIDENCE_KIND,compile_ios_capture,ios_evidence_kind
from .ios_cases import CASES,case_from_fixture

MAX_TREE_BYTES=1024*1024*1024
SOURCE_SUFFIXES={'.swift','.m','.h','.plist','.pbxproj','.xcscheme','.yml','.yaml','.xcconfig','.xcworkspacedata','.xctestplan'}


def tree_manifest(root,source_only=False):
    root=Path(root);require(not root.is_symlink(),'Linked iOS artifact root is not supported')
    root=root.resolve();require(root.is_dir(),'Missing iOS artifact directory')
    result={};total=0
    for directory,dirs,files in os.walk(root):
        if source_only:dirs[:]=sorted(d for d in dirs if d not in {'build','.build','DerivedData','xcuserdata','.git','.swiftpm'})
        for name in dirs:require(not (Path(directory)/name).is_symlink(),'Linked iOS directory is not supported')
        for name in sorted(files):
            path=Path(directory)/name
            if source_only and path.suffix not in SOURCE_SUFFIXES:continue
            if name=='.DS_Store':continue
            require(not path.is_symlink() and path.is_file(),'Linked iOS artifact is not supported')
            total+=path.stat().st_size;require(total<=MAX_TREE_BYTES,'iOS artifact size limit exceeded')
            result[path.relative_to(root).as_posix()]=sha_file(path)
    require(bool(result),'Empty iOS artifact');return result


def copy_ios_source(source,destination):
    source=Path(source).resolve();destination=Path(destination)
    require(not destination.exists(),'iOS workspace already exists')
    manifest=tree_manifest(source,True);destination.mkdir(parents=True,mode=0o700)
    for name in manifest:
        out=destination/name;out.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/name,out)
    require(tree_manifest(destination,True)==manifest,'iOS source copy changed');return manifest


def checked_relative(root,name):
    require(isinstance(name,str),'Invalid iOS artifact path')
    path=PurePosixPath(name)
    require(not path.is_absolute() and '..' not in path.parts and str(path)==name,'Unsafe iOS artifact path')
    root=Path(root).resolve();target=root/name
    require(target.resolve().is_relative_to(root) and not target.is_symlink(),'Linked or escaped iOS path')
    return target


def app_info(app):
    app=Path(app)
    require((app/'Info.plist').is_file() and not (app/'Info.plist').is_symlink(),'Missing app identity')
    with (app/'Info.plist').open('rb') as f:value=plistlib.load(f)
    require(value.get('CFBundleIdentifier')==APPLICATION_ID,'Wrong iOS application')
    require(isinstance(value.get('ReproBuildID'),str) and len(value['ReproBuildID'])>=8,'Missing iOS build identity')
    return {'applicationId':APPLICATION_ID,'buildId':value['ReproBuildID']}


def automatic_profile_for_receipt(app, receipt):
    from .ios_instrumentation import profile_from_app, validate_ios_auto_profile
    profile = profile_from_app(app)
    automatic = receipt.get('automaticInstrumentation')
    if profile is None:
        require(automatic is None, 'Automatic iOS build receipt has no matching app runtime')
        return None
    require(isinstance(automatic, dict)
            and validate_ios_auto_profile(automatic.get('profile')).digest == profile.digest
            and automatic.get('profileDigest') == profile.digest,
            'Automatic iOS app has no matching protected build profile')
    files = receipt.get('sourceFiles')
    require(isinstance(files, dict) and bool(files) and receipt.get('sourceDigest') == digest(files)
            and automatic.get('sourceFiles') == {name: value for name, value in files.items()
                                                 if name.startswith('ReproLoopInstrumentation/')}
            and bool(automatic['sourceFiles']), 'Automatic iOS runtime is absent from its source proof')
    return profile


def _automatic_scenario(scenario, diagnostics, capture, profile, build_id):
    from .ios_instrumentation import validate_ios_auto_diagnostics
    if profile is None:
        require(diagnostics is None, 'Manual iOS capture cannot add automatic diagnostics')
        return scenario
    diagnostics = validate_ios_auto_diagnostics(diagnostics, capture, profile, build_id=build_id)
    scenario.update(diagnostics=diagnostics, autoProfileDigest=profile.digest)
    scenario.pop('scenarioDigest', None)
    scenario['scenarioDigest'] = digest(scenario)
    return scenario


def create_ios_bundle(capture,oracle,products,receipt,destination,capture_method='synthetic-driver',*,diagnostics=None):
    scenario=compile_ios_capture(capture,oracle);products=Path(products).resolve();destination=Path(destination)
    require(not destination.exists(),'Bundle output already exists')
    files=tree_manifest(products)
    require(receipt.get('productsDigest')==digest(files) and receipt.get('buildCompleted') is True,'Build receipt does not match test products')
    app=checked_relative(products,receipt['appRelative']);identity=app_info(app)
    require(identity['buildId']==receipt.get('buildId'),'Build ID differs from receipt')
    auto_profile = automatic_profile_for_receipt(app, receipt)
    scenario = _automatic_scenario(scenario, diagnostics, capture, auto_profile, identity['buildId'])
    environment=receipt.get('executionEnvironment','simulator');evidence_kind=ios_evidence_kind(environment)
    if environment=='physical-iphone':
        require(receipt.get('signed') is True and receipt['appRelative']=='Debug-iphoneos/ReproSample.app','Physical bundle requires signed iphoneos products')
    destination.mkdir(parents=True,mode=0o700)
    shutil.copytree(products,destination/'products')
    write_json(destination/'capture.json',capture);write_json(destination/'oracle.json',oracle)
    bundle_files = ['capture.json', 'oracle.json']
    if auto_profile:
        write_json(destination/'diagnostics.json', diagnostics)
        bundle_files.append('diagnostics.json')
    manifest={'schemaVersion':2,'platform':'ios','applicationId':APPLICATION_ID,'executionEnvironment':environment,
              'artifact':{'kind':'ios-device-app' if environment=='physical-iphone' else 'ios-simulator-app','appRelative':receipt['appRelative'],
                          'productsDigest':digest(files),'files':files,'receipt':receipt},
              'files':{name:sha_file(destination/name) for name in bundle_files},
              'policy':{'requiredEvidence':evidence_kind,'fixtureAdapter':case_from_fixture(capture['fixture']).policy_adapter},'captureMethod':capture_method}
    write_json(destination/'manifest.json',manifest)
    return load_ios_bundle(destination)


def load_ios_bundle(path):
    path=Path(path).resolve();m=read_json(path/'manifest.json')
    require(isinstance(m,dict) and m.get('schemaVersion')==2 and m.get('platform')=='ios'
            and m.get('applicationId')==APPLICATION_ID and m.get('executionEnvironment') in {'simulator','physical-iphone'},
            'Unsupported iOS bundle platform or execution environment')
    environment=m['executionEnvironment'];evidence_kind=ios_evidence_kind(environment)
    policy=m.get('policy')
    require(isinstance(policy,dict) and set(policy)=={'requiredEvidence','fixtureAdapter'} and policy['requiredEvidence']==evidence_kind
            and policy['fixtureAdapter'] in {c.policy_adapter for c in CASES.values()},'Unsupported iOS evidence policy')
    require(isinstance(m.get('files'),dict) and set(m['files']) in (
        {'capture.json','oracle.json'}, {'capture.json','oracle.json','diagnostics.json'}), 'Unexpected iOS bundle files')
    for name,expected in m['files'].items():require(sha_file(checked_relative(path,name))==expected,'iOS bundle integrity mismatch')
    artifact=m.get('artifact');require(isinstance(artifact,dict) and artifact.get('kind')==('ios-device-app' if environment=='physical-iphone' else 'ios-simulator-app'),'Unsupported iOS artifact')
    files=tree_manifest(path/'products');require(files==artifact.get('files') and digest(files)==artifact.get('productsDigest'),'iOS test kit changed')
    receipt=artifact.get('receipt');require(isinstance(receipt,dict) and receipt.get('productsDigest')==digest(files)
                                          and receipt.get('buildCompleted') is True,'Invalid iOS build receipt')
    require(receipt.get('executionEnvironment','simulator')==environment,'Build environment differs from bundle')
    if environment=='physical-iphone':
        require(receipt.get('signed') is True and artifact['appRelative']=='Debug-iphoneos/ReproSample.app','Physical bundle requires signed iphoneos products')
    app=checked_relative(path/'products',artifact['appRelative']);identity=app_info(app)
    require(identity['buildId']==receipt.get('buildId'),'App build identity differs from receipt')
    auto_profile = automatic_profile_for_receipt(app, receipt)
    expected_files = {'capture.json','oracle.json'} | ({'diagnostics.json'} if auto_profile else set())
    require(set(m['files']) == expected_files, 'Automatic iOS bundle diagnostics are missing or unbound')
    capture=read_json(path/'capture.json');oracle=read_json(path/'oracle.json');scenario=compile_ios_capture(capture,oracle)
    diagnostics = read_json(path/'diagnostics.json') if auto_profile else None
    scenario = _automatic_scenario(scenario, diagnostics, capture, auto_profile, identity['buildId'])
    require(policy['fixtureAdapter']==case_from_fixture(capture['fixture']).policy_adapter,'Bundle policy does not match its recorded case')
    return {'path':path,'manifest':m,'manifestDigest':digest(m),'products':path/'products','app':app,
            'receipt':receipt,'capture':capture,'oracle':oracle,'scenario':scenario,
            'auto_profile':auto_profile,'diagnostics':diagnostics}
