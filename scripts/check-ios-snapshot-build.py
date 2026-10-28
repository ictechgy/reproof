#!/usr/bin/env python3
"""Compile a tiny public synthetic v3 observation app; never boot or run it.

Output is fresh and intentionally retained, including on build failure.
"""
import argparse
import json
from pathlib import Path
import plistlib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reproof.source_snapshot import capture
from reproof.ios_instrumentation import validate_ios_auto_profile, prepare_ios_instrumentation
from reproof.ios_observation import build_observation_app


def fixture(root):
    def uid(number):return f'{number:024X}'
    project, group, product_group, app, sources, products, app_configs, project_configs = map(uid, range(1, 9))
    swift, swift_build, debug, release, project_debug, project_release = map(uid, range(9, 15))
    settings = {'PRODUCT_BUNDLE_IDENTIFIER': 'com.example.snapshotsynthetic',
        'INFOPLIST_FILE': 'App/Info.plist', 'PRODUCT_NAME': 'SnapshotSynthetic',
        'SWIFT_VERSION': '5.0', 'IPHONEOS_DEPLOYMENT_TARGET': '15.0',
        'CLANG_ENABLE_OBJC_ARC': 'YES', 'CLANG_ENABLE_MODULES': 'YES',
        'TARGETED_DEVICE_FAMILY': '1,2', 'CODE_SIGNING_ALLOWED': 'NO',
        'GENERATE_INFOPLIST_FILE': 'NO', 'SDKROOT': 'iphoneos'}
    objects = {
        project: {'isa':'PBXProject','mainGroup':group,'productRefGroup':product_group,
            'targets':[app],'buildConfigurationList':project_configs,
            'compatibilityVersion':'Xcode 14.0','developmentRegion':'en','knownRegions':['en'],
            'projectDirPath':'','projectRoot':''},
        group:{'isa':'PBXGroup','children':[swift,product_group],'sourceTree':'<group>'},
        product_group:{'isa':'PBXGroup','name':'Products','children':[products],'sourceTree':'<group>'},
        app:{'isa':'PBXNativeTarget','name':'SnapshotSynthetic','productName':'SnapshotSynthetic',
            'productType':'com.apple.product-type.application','productReference':products,
            'buildConfigurationList':app_configs,'buildPhases':[sources],'buildRules':[],'dependencies':[]},
        sources:{'isa':'PBXSourcesBuildPhase','files':[swift_build],'buildActionMask':'2147483647',
            'runOnlyForDeploymentPostprocessing':'0'},
        products:{'isa':'PBXFileReference','explicitFileType':'wrapper.application',
            'path':'SnapshotSynthetic.app','sourceTree':'BUILT_PRODUCTS_DIR'},
        swift:{'isa':'PBXFileReference','lastKnownFileType':'sourcecode.swift',
            'path':'App/AppDelegate.swift','sourceTree':'SOURCE_ROOT'},
        swift_build:{'isa':'PBXBuildFile','fileRef':swift},
        app_configs:{'isa':'XCConfigurationList','buildConfigurations':[debug,release],
            'defaultConfigurationIsVisible':'0','defaultConfigurationName':'Release'},
        project_configs:{'isa':'XCConfigurationList','buildConfigurations':[project_debug,project_release],
            'defaultConfigurationIsVisible':'0','defaultConfigurationName':'Release'},
        debug:{'isa':'XCBuildConfiguration','name':'Debug','buildSettings':dict(settings)},
        release:{'isa':'XCBuildConfiguration','name':'Release','buildSettings':dict(settings)},
        project_debug:{'isa':'XCBuildConfiguration','name':'Debug','buildSettings':{}},
        project_release:{'isa':'XCBuildConfiguration','name':'Release','buildSettings':{}},
    }
    files = {
        'SnapshotSynthetic.xcodeproj/project.pbxproj':plistlib.dumps({
            'archiveVersion':'1','objectVersion':'56','classes':{},'rootObject':project,'objects':objects}),
        'SnapshotSynthetic.xcodeproj/xcshareddata/xcschemes/SnapshotSynthetic.xcscheme':f'''<Scheme version="1.7"><BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForRunning="YES" buildForAnalyzing="YES"><BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{app}" BuildableName="SnapshotSynthetic.app" BlueprintName="SnapshotSynthetic" ReferencedContainer="container:SnapshotSynthetic.xcodeproj"/></BuildActionEntry></BuildActionEntries></BuildAction></Scheme>'''.encode(),
        'App/Info.plist':plistlib.dumps({'CFBundleIdentifier':'$(PRODUCT_BUNDLE_IDENTIFIER)',
            'CFBundleExecutable':'$(EXECUTABLE_NAME)','CFBundleName':'$(PRODUCT_NAME)',
            'CFBundlePackageType':'APPL','CFBundleShortVersionString':'1.0','CFBundleVersion':'1',
            'UILaunchScreen':{}}),
        'App/AppDelegate.swift':b'''import UIKit
@main final class AppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?
    func application(_ application: UIApplication, didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        window = UIWindow(frame: UIScreen.main.bounds)
        let view = UIViewController()
        let button = UIButton(type: .system)
        button.accessibilityIdentifier = "synthetic.save"
        button.addTarget(self, action: #selector(save), for: .touchUpInside)
        view.view.addSubview(button)
        window?.rootViewController = view
        window?.makeKeyAndVisible()
        return true
    }
    @objc private func save() {}
}
'''}
    for name, data in files.items():
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    return sorted(files)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();root=args.output.parent.resolve()/args.output.name;root.mkdir(mode=0o700)
    names=fixture(root/'original')
    summary=capture(root/'original',names,root/'original-snapshot')
    document={'schemaVersion':3,'kind':'uikit-observation-v3',
        'applicationId':'com.example.snapshotsynthetic','project':'SnapshotSynthetic.xcodeproj',
        'target':'SnapshotSynthetic','build':{'scheme':'SnapshotSynthetic','product':'SnapshotSynthetic',
            'infoPlist':'App/Info.plist','debugConfiguration':'Debug'},
        'tapTargets':['synthetic.save'],'screenTargets':{'synthetic.screen':'main'},
        'sourceSnapshot':{'schemaVersion':1,'kind':'reproof-local-source-snapshot',
            **{key:summary[key] for key in ('digest','files','bytes')}}}
    profile=validate_ios_auto_profile(document)
    prepare_ios_instrumentation(root/'original-snapshot',root/'prepared',profile=profile)
    result=build_observation_app(root/'prepared/snapshot',root/'build',profile=profile)
    assert result['receipt']['buildCompleted'] is True
    assert result['receipt']['signed'] is False
    assert result['receipt']['automaticObservations'] is True
    assert result['receipt']['behaviorVerified'] is False
    assert (result['app']/'SnapshotSynthetic').is_file()
    report={'status':'pass','compiledApps':1,'schemaVersion':3,'signed':False,
            'deviceExecution':False,'behaviorVerified':False}
    (root/'compile-proof.json').write_text(json.dumps(report,sort_keys=True)+'\n')
    print('IOS_SNAPSHOT_BUILD '+json.dumps(report,sort_keys=True),flush=True)


if __name__=='__main__':main()
