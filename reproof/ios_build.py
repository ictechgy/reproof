"""Pinned local Xcode builds without device signing or network dependency resolution."""
from __future__ import annotations
import json
from pathlib import Path
from .core import digest,require
from .repair import run_command
from .storage import write_json
from .ios_storage import tree_manifest,app_info

PRODUCT_FILE='Sample/CounterLogic.swift'
UI_TARGET='ReproReplayTests'
UI_TEST='ReproReplayTests/ReproReplayTests/testScenario'
LOGIC_TARGET='ReproLogicTests'
LOGIC_TEST='ReproLogicTests/CounterLogicTests/testIncrementIsOne'


def xcode_environment():
    selected=run_command(['/usr/bin/xcode-select','-p'],'.',timeout=10).strip()
    require(Path(selected).is_dir(),'Selected Xcode is unavailable')
    return {'DEVELOPER_DIR':selected}


def build_ios(source,output,simulator_id,*,include_ui=True,physical_device=None,configuration='Debug'):
    source=Path(source).resolve();output=Path(output).resolve()
    require(not output.exists(),'iOS build output already exists')
    require(configuration in {'Debug','Release'}, 'Unsupported iOS build configuration')
    from .ios_instrumentation import profile_from_source, profile_from_app, validate_ios_preparation, MARKER
    profile = profile_from_source(source)
    preparation = validate_ios_preparation(source) if profile and (source / MARKER).exists() else None
    before=tree_manifest(source,True);build_id=digest(before)[:32]
    project=source/'Reproof.xcodeproj'
    require(project.is_dir(),'Missing generated iOS project')
    env=xcode_environment();output.mkdir(parents=True,mode=0o700)
    physical=physical_device is not None
    environment='physical-iphone' if physical else 'simulator'
    schemes=['ReproReplay','ReproLogic'] if include_ui else ['ReproLogic']
    for scheme in schemes:
        command=['/usr/bin/xcodebuild','build-for-testing','-project',str(project),'-scheme',scheme,
                 '-configuration',configuration,'-sdk','iphoneos' if physical else 'iphonesimulator','-destination','generic/platform=iOS' if physical else f'id={simulator_id}',
                 '-derivedDataPath',str(output/'DerivedData'),'-disableAutomaticPackageResolution',
                 'CODE_SIGNING_ALLOWED=NO','CODE_SIGNING_REQUIRED=NO','COMPILER_INDEX_STORE_ENABLE=NO',
                 'REPRO_BUILD_ID='+build_id]
        log=run_command(command,str(source),timeout=300,max_output=4*1024*1024,env_extra=env,log_path=None if physical else output/f'{scheme}-build.log')
        if not physical:(output/f'{scheme}-build.log').write_text(log)
    require(tree_manifest(source,True)==before,'Xcode modified protected source')
    products=output/'DerivedData/Build/Products'
    if physical:
        from .ios_signing import sign_products
        sign_products(products,physical_device)
    apps=list(products.glob(configuration+('-iphoneos' if physical else '-iphonesimulator')+'/ReproSample.app'))
    require(len(apps)==1,'Missing or ambiguous sample app')
    identity=app_info(apps[0]);require(identity['buildId']==build_id,'Embedded build identity was not set')
    receipt={'schemaVersion':1,'platform':'ios','executionEnvironment':environment,'sourceDigest':digest(before),
             'sourceFiles':before,'buildId':build_id,'appRelative':apps[0].relative_to(products).as_posix(),
             'productsDigest':digest(tree_manifest(products)),'buildCompleted':True,
             'xcode':run_command(['/usr/bin/xcodebuild','-version'],str(source),timeout=20,env_extra=env).strip(),
             'configuration':configuration,'schemes':schemes}
    if profile and configuration == 'Debug':
        embedded = profile_from_app(apps[0])
        require(embedded is not None and embedded.digest == profile.digest, 'Automatic iOS app lost its build profile')
        receipt['automaticInstrumentation'] = {'profile': profile.data, 'profileDigest': profile.digest,
            'sourceFiles': {name: checksum for name, checksum in before.items() if name.startswith('ReproofInstrumentation/')}}
        if preparation is not None:
            receipt['automaticInstrumentation']['preparationDigest'] = digest(preparation)
            write_json(output/'instrumentation.json', preparation)
        write_json(output/'auto-profile.json', profile.data)
    elif profile:
        require(profile_from_app(apps[0]) is None, 'Release app must exclude the automatic recording profile')
        receipt['automaticInstrumentationExcluded'] = profile.digest
    if physical:receipt['signed']=True
    write_json(output/'receipt.json',receipt)
    return {'products':products,'app':apps[0],'receipt':receipt,'output':output}
