"""Snapshot Views consumer contracts with synthetic PSI/Gradle, no native execution."""
import hashlib
import json
import struct
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from reproof import source_snapshot as snapshots
from reproof.core import ContractError,digest
from reproof.android_snapshot_observation import (
    validate_snapshot_observation_profile,prepare_snapshot_observation,
    validate_snapshot_preparation,build_snapshot_observation_app,_unsigned_apk,
)
from tests.test_android_observation import ordinary_views_project

ACTIVITY='app/src/main/java/example/MainActivity.kt'


def fixture(root,*,extra_files=0,asset_bytes=0,budgets=None,groovy=False,kotlin_files=0):
    original=root/'original';document=ordinary_views_project(original);names=document.pop('sourceInputs')
    if groovy:
        for before,after in [('settings.gradle.kts','settings.gradle'),('app/build.gradle.kts','app/build.gradle')]:
            (original/before).rename(original/after);names.remove(before);names.append(after)
        (original/'settings.gradle').write_text("pluginManagement {}\ninclude ':app'\n")
        (original/'app/build.gradle').write_text("plugins { id 'com.android.application' }\n")
        document['build']['dsl']='groovy'
    for index in range(kotlin_files):
        name=f'app/src/main/java/other/File{index:05}.kt';path=original/name
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'package unrelated\nclass Unrelated\n');names.append(name)
    for index in range(extra_files):
        name=f'app/src/main/assets/items/item{index:05}.json';path=original/name
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'{}');names.append(name)
    for index in range((asset_bytes+1024*1024-1)//(1024*1024)):
        name=f'app/src/main/assets/blob{index:03}.png';path=original/name
        path.write_bytes(b'x'*min(1024*1024,asset_bytes-index*1024*1024));names.append(name)
    summary=snapshots.capture(original,names,root/'original-snapshot',budgets=budgets)
    document.update(schemaVersion=3,kind='views-observation-v3',activitySource=ACTIVITY,
        sourceSnapshot={'schemaVersion':1,'kind':'reproof-local-source-snapshot',
            **{key:summary[key] for key in ('digest','files','bytes')}})
    return root/'original-snapshot',validate_snapshot_observation_profile(document)


def analyzer():
    return Mock(return_value={'schemaVersion':1,'activityPath':ACTIVITY,'sites':[
        {'id':'s123abc','path':ACTIVITY,'line':12,'target':'save','kind':'tap'}],
        'files':{ACTIVITY:'MUST NOT REPLACE PRODUCT'}})


def integration(settings,module):
    return {'settings.gradle.kts':settings+'// included fixed plugin\n',
            'module.gradle.kts':module+'// fixed plugin applied\n'}


def compiler(profile,*,mutate=None,signed=False,bad_class=False):
    def run(argv,cwd,**kwargs):
        root=Path(cwd)
        if mutate:mutate(root)
        directory=root/'app/build/reproof/debug';directory.mkdir(parents=True)
        activity_bytes=b'SYNTHETIC_TRANSFORMED_ACTIVITY'
        jar=directory/'classes.jar'
        with zipfile.ZipFile(jar,'w') as archive:archive.writestr('com/example/inventory/MainActivity.class',activity_bytes)
        report={'schemaVersion':1,'kind':'android_asm_v1','appProfileDigest':profile.digest,
            'activityClass':'com.example.inventory.MainActivity','instrumentedClasses':1,
            'sites':[{'id':'s123abc','line':12}],'lifecycle':{'onCreate':True,'onDestroy':True},
            'inputClassSha256':'0'*64,'outputClassSha256':hashlib.sha256(activity_bytes).hexdigest(),
            'outputJarSha256':hashlib.sha256(jar.read_bytes()).hexdigest()}
        if bad_class:report['outputClassSha256']='f'*64
        (directory/'report.json').write_text(json.dumps(report))
        apk=root/profile.data['build']['apk'];apk.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(apk,'w') as archive:
            archive.writestr('assets/reproof-observation.json',json.dumps(profile.data))
            archive.writestr('classes.dex',b'SYNTHETIC_DEX')
            if signed:archive.writestr('META-INF/CERT.RSA',b'not real certificate')
        return 'Synthetic Gradle build'
    return run


class AndroidSnapshotObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.source,self.profile=fixture(self.root)

    def prepare(self,**options):
        selected=analyzer()
        with patch('reproof.kotlin_instrumenter.configure_gradle',side_effect=integration):
            result=prepare_snapshot_observation(self.source,self.profile,self.root/'prepared',analyzer=selected,**options)
        profile=validate_snapshot_observation_profile(json.loads(result['appProfile'].read_bytes()))
        return result,profile,selected

    def build(self,prepared,profile,output,**options):
        with patch('reproof.android_snapshot_observation.run_command',side_effect=compiler(profile,**options)) as run:
            result=build_snapshot_observation_app(prepared['source'],output,profile=profile,
                gradle='/synthetic/gradle',java_home='/synthetic/java',sdk_home='/synthetic/sdk')
        return result,run

    def test_preparation_reads_only_selected_activity_and_preserves_all_product_files(self):
        prepared,profile,selected=self.prepare()
        self.assertEqual(set(selected.call_args.args[0]),{ACTIVITY})
        receipt=validate_snapshot_preparation(prepared['source'],profile)
        self.assertEqual(profile.data['sourceSnapshot'],self.profile.data['sourceSnapshot'])
        self.assertNotEqual(profile.digest,self.profile.digest)
        self.assertEqual(receipt['originalProfileDigest'],self.profile.digest)
        self.assertLessEqual(len(receipt['changedFiles']),16)
        before=snapshots.inspect_snapshot(self.source);after=snapshots.inspect_snapshot(prepared['source'])
        mapped={item['path']:item for item in after['files']}
        for item in before['files']:
            if item['path'] not in ('settings.gradle.kts','app/build.gradle.kts'):
                self.assertEqual(mapped[item['path']],item)
        self.assertNotIn('MUST NOT REPLACE', (prepared['source']/'data'/ACTIVITY).read_text())
        self.assertIn('signingConfig = null',(prepared['source']/'data/app/build.gradle.kts').read_text())
        self.assertFalse(receipt['behaviorVerified'])

    def test_profile_rejects_unbound_activity_and_v2_lists(self):
        for changes in ({'activitySource':'elsewhere/Main.kt'},{'activitySource':ACTIVITY[:-3]+'.java'},
                        {'sourceInputs':[]},{'oracle':{}}):
            with self.subTest(changes=changes),self.assertRaises(ContractError):
                validate_snapshot_observation_profile(dict(self.profile.data,**changes))

    def test_sites_must_bind_exact_selected_activity(self):
        bad=analyzer();bad.return_value['sites'][0]['path']='app/src/main/java/other/Other.kt'
        with self.assertRaises(ContractError):
            prepare_snapshot_observation(self.source,self.profile,self.root/'bad',analyzer=bad)
        self.assertFalse((self.root/'bad').exists())

    def test_original_reference_and_budget_never_inferred_from_profile(self):
        value=self.profile.data;value['sourceSnapshot']['digest']='f'*64
        with self.assertRaises(snapshots.SnapshotError):
            prepare_snapshot_observation(self.source,validate_snapshot_observation_profile(value),self.root/'bad',analyzer=analyzer())
        self.assertFalse((self.root/'bad').exists())

    def test_tampered_product_or_receipt_rejected_before_gradle(self):
        prepared,profile,_=self.prepare();(prepared['source']/'data'/ACTIVITY).write_bytes(b'changed')
        with patch('reproof.android_snapshot_observation.run_command') as run,self.assertRaises(snapshots.SnapshotError):
            build_snapshot_observation_app(prepared['source'],self.root/'bad',profile=profile,gradle='gradle',java_home='java',sdk_home='sdk')
        run.assert_not_called()

    def test_build_checks_actual_asm_and_apk_and_protected_snapshot(self):
        prepared,profile,_=self.prepare();built,run=self.build(prepared,profile,self.root/'built')
        self.assertEqual(run.call_args.args[0][1:4],['--offline','--no-daemon','--console=plain'])
        receipt=built['receipt'];self.assertTrue(receipt['buildCompleted']);self.assertFalse(receipt['signed'])
        self.assertTrue(receipt['automaticObservations']);self.assertFalse(receipt['behaviorVerified'])
        self.assertEqual(receipt['bytecodeInstrumentation']['report']['instrumentedClasses'],1)
        self.assertEqual(snapshots.verify(self.root/'built/post-build-snapshot')['digest'],receipt['sourceSnapshot']['digest'])

    def test_signed_apk_or_bad_asm_never_gets_build_receipt(self):
        prepared,profile,_=self.prepare()
        for name,options in [('signed',{'signed':True}),('bad-class',{'bad_class':True})]:
            output=self.root/name
            with self.subTest(name=name),self.assertRaises(snapshots.SnapshotError) as error:
                self.build(prepared,profile,output,**options)
            self.assertTrue(error.exception.cleanup_incomplete)
            self.assertTrue((output/'source').exists());self.assertFalse((output/'receipt.json').exists())

    def test_compiler_mutation_retains_executed_tree_and_no_receipt(self):
        prepared,profile,_=self.prepare();output=self.root/'mutated'
        with self.assertRaises(snapshots.SnapshotError):
            self.build(prepared,profile,output,mutate=lambda root:(root/ACTIVITY).write_bytes(b'changed'))
        self.assertTrue((output/'source'/ACTIVITY).exists());self.assertFalse((output/'receipt.json').exists())

    def test_groovy_preparation_keeps_original_activity_and_disables_debug_signing(self):
        other=self.root/'groovy';other.mkdir();source,profile=fixture(other,groovy=True)
        result=prepare_snapshot_observation(source,profile,other/'prepared',analyzer=analyzer())
        actual=(result['source']/'data/app/build.gradle').read_text()
        self.assertIn('setSigningConfig((com.android.build.api.dsl.ApkSigningConfig) null)',actual)
        self.assertEqual((source/'data'/ACTIVITY).read_bytes(),(result['source']/'data'/ACTIVITY).read_bytes())

    def test_unsigned_inspection_rejects_v2_block_malformed_directory_and_zip64(self):
        path=self.root/'unsigned.apk'
        with zipfile.ZipFile(path,'w') as archive:archive.writestr('classes.dex',b'dex')
        original=path.read_bytes();self.assertEqual(_unsigned_apk(path),(hashlib.sha256(original).hexdigest(),len(original)))
        eocd=original.rfind(b'PK\x05\x06');cd=struct.unpack_from('<L',original,eocd+16)[0]
        block=struct.pack('<Q',24)*2+b'APK Sig Block 42'
        changed=bytearray(original[:cd]+block+original[cd:]);struct.pack_into('<L',changed,eocd+len(block)+16,cd+len(block))
        path.write_bytes(changed)
        with self.assertRaises(ContractError):_unsigned_apk(path)
        changed=bytearray(original);struct.pack_into('<L',changed,eocd+16,0xffffffff);path.write_bytes(changed)
        with self.assertRaises(ContractError):_unsigned_apk(path)
        path.write_bytes(original[:-10])
        with self.assertRaises(ContractError):_unsigned_apk(path)

    def test_activity_over_4mib_fails_before_psi_and_output(self):
        path=self.root/'original'/ACTIVITY;path.write_bytes(b'x'*(4*1024*1024+1))
        original=snapshots.inspect_snapshot(self.source)
        summary=snapshots.capture(self.root/'original',[item['path'] for item in original['files']],self.root/'large-source')
        document=self.profile.data;document['sourceSnapshot'].update({key:summary[key] for key in ('digest','files','bytes')})
        selected=analyzer()
        with self.assertRaises(ContractError):
            prepare_snapshot_observation(self.root/'large-source',validate_snapshot_observation_profile(document),self.root/'large',analyzer=selected)
        selected.assert_not_called();self.assertFalse((self.root/'large').exists())

    def test_case_alias_parent_denied_before_publication(self):
        from reproof import android_snapshot_observation as module
        actual=module.open_directory
        def alias(path):return actual(self.source if Path(path)==self.root/'alias' else path)
        before=snapshots.verify(self.source)
        with patch.object(module,'open_directory',side_effect=alias),self.assertRaises(ContractError):
            module._Publication(self.source,self.root/'alias'/'new')
        self.assertEqual(snapshots.verify(self.source),before);self.assertFalse((self.source/'new').exists())

    def test_outer_root_replacement_preserves_foreign_directory(self):
        from reproof import android_snapshot_observation as module
        actual=snapshots.derive_snapshot;output=self.root/'prepared'
        def derive(*args,**kwargs):
            result=actual(*args,**kwargs);output.rename(self.root/'owned-output')
            output.mkdir();(output/'foreign').write_bytes(b'preserve');return result
        with patch.object(module.snapshots,'derive_snapshot',side_effect=derive),self.assertRaises(snapshots.SnapshotError) as error:
            self.prepare()
        self.assertTrue(error.exception.cleanup_incomplete)
        self.assertEqual([path.name for path in output.iterdir()],['foreign'])

    def test_cancelled_build_retains_materialization_and_no_receipt(self):
        import threading
        prepared,profile,_=self.prepare();cancel=threading.Event();output=self.root/'cancelled-build'
        actual=compiler(profile)
        def run(*args,**kwargs):
            result=actual(*args,**kwargs);cancel.set();return result
        with patch('reproof.android_snapshot_observation.run_command',side_effect=run),self.assertRaises(snapshots.SnapshotError) as error:
            build_snapshot_observation_app(prepared['source'],output,profile=profile,gradle='gradle',java_home='java',sdk_home='sdk',cancel=cancel)
        self.assertTrue(error.exception.cleanup_incomplete);self.assertTrue((output/'source').is_dir())
        self.assertFalse((output/'receipt.json').exists())

    def test_home_link_and_key_material_rejected_without_external_reads(self):
        prepared,profile,_=self.prepare();external=self.root/'external';external.mkdir();(external/'keep').write_bytes(b'preserve')
        for name in ('linked','key'):
            output=self.root/name;actual=compiler(profile)
            def run(*args,**kwargs):
                home=Path(kwargs['env_extra']['ANDROID_USER_HOME'])
                if name=='linked':(home/'escape').symlink_to(external,target_is_directory=True)
                else:(home/'debug.jks').write_bytes(b'SYNTHETIC_NOT_A_KEY')
                return actual(*args,**kwargs)
            with patch('reproof.android_snapshot_observation.run_command',side_effect=run),self.assertRaises(snapshots.SnapshotError):
                build_snapshot_observation_app(prepared['source'],output,profile=profile,gradle='gradle',java_home='java',sdk_home='sdk')
            self.assertFalse((output/'receipt.json').exists())
        self.assertEqual((external/'keep').read_bytes(),b'preserve')

    def test_bad_embedded_profile_is_static_error_even_with_consistent_sidecars(self):
        prepared,profile,_=self.prepare();parent=prepared['source'].parent
        embedded='app/reproof-instrumentation/assets/reproof-observation.json'
        for index,raw in enumerate((b'['*2000+b'0'+b']'*2000,b'\xff')):
            receipt=json.loads((parent/'instrumentation.json').read_bytes())
            reference=snapshots.verify(prepared['source'])
            altered=parent/f'altered{index}'
            summary=snapshots.derive_snapshot(prepared['source'],altered,{embedded:raw},
                expected={key:reference[key] for key in ('digest','files','bytes')})
            receipt['preparedSnapshot'].update({key:summary[key] for key in ('digest','files','bytes')})
            manifest=snapshots.inspect_snapshot(altered);entries={item['path']:item for item in manifest['files']}
            changes=[entries[name] for name in receipt['changedFiles']]
            receipt['changesDigest']=digest(changes)
            (parent/'changes.json').write_text(json.dumps(changes));(parent/'instrumentation.json').write_text(json.dumps(receipt))
            with patch('reproof.android_snapshot_observation.run_command') as run,self.assertRaises(ContractError) as error:
                build_snapshot_observation_app(altered,self.root/f'invalid{index}',profile=profile,gradle='gradle',java_home='java',sdk_home='sdk')
            run.assert_not_called();self.assertEqual(str(error.exception),'Embedded Views profile is invalid')

    def test_invalid_gradle_utf8_is_static_and_never_published(self):
        (self.root/'original/settings.gradle.kts').write_bytes(b'private-marker-\xff')
        original=snapshots.inspect_snapshot(self.source)
        summary=snapshots.capture(self.root/'original',[item['path'] for item in original['files']],self.root/'invalid-text-source')
        document=self.profile.data;document['sourceSnapshot'].update({key:summary[key] for key in ('digest','files','bytes')})
        with self.assertRaises(ContractError) as error:
            prepare_snapshot_observation(self.root/'invalid-text-source',validate_snapshot_observation_profile(document),self.root/'badtext',analyzer=analyzer())
        self.assertEqual(str(error.exception),'Invalid Views Gradle source encoding')
        self.assertFalse((self.root/'badtext').exists())

    def test_operator_cache_is_forwarded_but_not_profile_controlled(self):
        prepared,profile,_=self.prepare()
        with patch.dict('os.environ',{'GRADLE_USER_HOME':'/synthetic/approved-cache'}):
            _,run=self.build(prepared,profile,self.root/'cached')
        self.assertEqual(run.call_args.kwargs['env_extra']['GRADLE_USER_HOME'],'/synthetic/approved-cache')
        document=profile.data;document['gradleCache']='/synthetic/unapproved'
        with self.assertRaises(ContractError):validate_snapshot_observation_profile(document)

    def test_existing_embedded_contract_asset_is_rejected_before_psi(self):
        original=snapshots.inspect_snapshot(self.source)
        path='app/src/main/assets/reproof-observation.json'
        (self.root/'original'/path).write_text('{}')
        summary=snapshots.capture(self.root/'original',[item['path'] for item in original['files']]+[path],self.root/'reserved-source')
        document=self.profile.data;document['sourceSnapshot'].update({key:summary[key] for key in ('digest','files','bytes')})
        selected=analyzer()
        with self.assertRaises(ContractError):
            prepare_snapshot_observation(self.root/'reserved-source',validate_snapshot_observation_profile(document),self.root/'reserved',analyzer=selected)
        selected.assert_not_called();self.assertFalse((self.root/'reserved').exists())

    def test_unsigned_overlay_clears_default_flavor_and_debug_without_touching_originals(self):
        for groovy in (False,True):
            root=self.root/('groovy-flavor' if groovy else 'kotlin-flavor');root.mkdir()
            source,profile=fixture(root,groovy=groovy)
            document=profile.data;document['build']['task']=':app:assembleQaDebug'
            profile=validate_snapshot_observation_profile(document)
            if groovy:
                result=prepare_snapshot_observation(source,profile,root/'prepared',analyzer=analyzer())
            else:
                with patch('reproof.kotlin_instrumenter.configure_gradle',side_effect=integration):
                    result=prepare_snapshot_observation(source,profile,root/'prepared',analyzer=analyzer())
            suffix='.gradle' if groovy else '.gradle.kts'
            relative='app/build'+suffix
            changed=(result['source']/'data'/relative).read_text()
            clear = ('setSigningConfig((com.android.build.api.dsl.ApkSigningConfig) null)'
                     if groovy else 'signingConfig = null')
            self.assertIn('defaultConfig { ' + clear + ' }',changed)
            self.assertIn('productFlavors.configureEach { ' + clear + ' }',changed)
            self.assertIn(('debug' if groovy else 'getByName("debug")') + ' { ' + clear + ' }',changed)
            self.assertEqual(changed.count(clear),3)
            if groovy:
                self.assertNotIn('signingConfig = null',changed)
                self.assertNotIn('setSigningConfig(null)',changed)
            self.assertNotIn('signingConfig = null',(source/'data'/relative).read_text())
            selected=validate_snapshot_observation_profile(json.loads(result['appProfile'].read_bytes()))
            self.assertEqual(selected.data['build']['task'],':app:assembleQaDebug')

    def test_cancellation_before_publication_is_not_quiescence_proof(self):
        import threading
        cancel=threading.Event();cancel.set()
        with self.assertRaises(snapshots.SnapshotError):
            prepare_snapshot_observation(self.source,self.profile,self.root/'cancel',analyzer=analyzer(),cancel=cancel)
        self.assertFalse((self.root/'cancel').exists())


if __name__=='__main__':unittest.main()
