"""Opt-in bounded Android source snapshots, independent of replay/repair trust."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import stat
import zipfile

from . import source_snapshot as snapshots
from .core import ContractError, digest, require
from .android_observation import AndroidObservationProfile, render_runtime_config, MAX_PROFILE, APK_PROFILE
from .android_sources import validate_source_inputs, require_declared_tree
from .build_instrumentation import build_coordinates, activity_class, validate_bytecode_artifacts
from .execution.artifacts import ArtifactError, open_directory, open_regular, read_regular
from .resources import read_resource
from .repair import run_command
from .source_snapshot_json import bounded_json
from .storage import _unique_object, MAX_APK

KIND = 'views-observation-v3'
ACTIVITY_BYTES = 4 * 1024 * 1024
META_BYTES = 16 * 1024 * 1024
ORIGINAL = 'original-manifest.json'
CHANGES = 'changes.json'
RECEIPT = 'instrumentation.json'


def _active(cancel):
    if cancel is not None and cancel.is_set():raise snapshots.SnapshotError('snapshot_cancelled')


def _reference(value):
    require(type(value) is dict and set(value) == {'schemaVersion','kind','digest','files','bytes'}
        and type(value['schemaVersion']) is int and value['schemaVersion'] == 1
        and value['kind'] == 'reproof-local-source-snapshot'
        and type(value['digest']) is str and re.fullmatch('[0-9a-f]{64}',value['digest'])
        and type(value['files']) is int and 1 <= value['files'] <= snapshots.MAX_FILES
        and type(value['bytes']) is int and 0 <= value['bytes'] <= snapshots.MAX_BYTES,
        'Invalid Views snapshot reference')
    return {key:value[key] for key in ('digest','files','bytes')}


def _bound(value):
    return dict({key:value[key] for key in ('digest','files','bytes')},
                schemaVersion=1,kind='reproof-local-source-snapshot')


def _coordinates(document):
    parts = document['build']['task'].split(':')
    module = Path(*parts[1:-1])
    suffix = '.gradle' if document['build'].get('dsl') == 'groovy' else '.gradle.kts'
    return module, 'settings'+suffix, (module/('build'+suffix)).as_posix()


def validate_snapshot_observation_profile(document):
    from .android_observation import validate_observation_profile
    required = {'schemaVersion','kind','package','activity','build','sourceSnapshot',
                'activitySource','tapTargets','screenTargets'}
    require(type(document) is dict and required <= set(document) <= required|{'instrumentation','inputExtensions'}
        and type(document['schemaVersion']) is int and document['schemaVersion'] == 3
        and document['kind'] == KIND,'Unsupported snapshot Views profile')
    _reference(document['sourceSnapshot'])
    activity = document['activitySource']
    validate_source_inputs([activity],extensions=document.get('inputExtensions'))
    require(activity.endswith('.kt'),'Select one explicit Kotlin activity source')
    build = document['build']
    require(type(build) is dict and type(build.get('task')) is str
        and re.fullmatch(r':(?:[A-Za-z][A-Za-z0-9_-]*:)*assemble(?:[A-Za-z][A-Za-z0-9]*)?Debug',build['task']),
        'Select one Views Debug assemble task')
    module, settings, gradle = _coordinates(document)
    prefix = (module/'src/main').as_posix()+'/'
    require(any(activity.startswith(prefix+folder+'/') for folder in ('java','kotlin')),
            'Activity source is outside the selected module')
    structural = dict(document,schemaVersion=2,kind='views-observation-v2',
        sourceInputs=sorted({activity,settings,gradle,(module/'src/main/AndroidManifest.xml').as_posix()}))
    del structural['sourceSnapshot']; del structural['activitySource']
    validate_observation_profile(structural)
    if 'instrumentation' in document:
        require(all(site['path']==activity for site in document['instrumentation']['sites']),
                'Every Views site must bind the selected activity source')
    require(len(json.dumps(document,indent=2).encode())+1 <= MAX_PROFILE,'Views profile size limit exceeded')
    return AndroidObservationProfile(json.dumps(document,sort_keys=True,separators=(',',':'),allow_nan=False))


def _profile(profile):
    return validate_snapshot_observation_profile(profile.data if isinstance(profile,AndroidObservationProfile) else profile)


def _generated(profile):
    module,_ = build_coordinates(profile)
    plugin = ['reproof-build-logic/'+name for name in ('settings.gradle.kts','build.gradle.kts',
        *(f'src/main/java/io/reproof/instrumentation/gradle/{name}.java' for name in
          ('ReproBytecodeTransformer','ReproInstrumentationPlugin','ReproInstrumentationTask','ReproPlan')))]
    support = module/'reproof-instrumentation'
    return plugin+[(support/'AndroidManifest.xml').as_posix(),(support/'assets/reproof-observation.json').as_posix()]+[
        (support/'runtime/io/reproof/autotrace'/name).as_posix()
        for name in ('ReproAuto.kt','ReproAppLogs.kt','ReproHooks.kt','ReproConfig.kt')]


def _manifest(manifest,profile,*,prepared=False):
    names = {item['path'] for item in manifest['files']}
    allowed = set(_generated(profile)) if prepared else set()
    for item in manifest['files']:
        name = item['path']; validate_source_inputs([name],extensions=profile.data.get('inputExtensions'))
        require(name in allowed or (not any(part.casefold() in {'reproof-build-logic','reproof-instrumentation'}
                for part in name.split('/')) and not name.casefold().endswith('/'+APK_PROFILE)),
                'Reserved Views instrumentation input')
    module,settings,gradle = _coordinates(profile.data)
    require({settings,gradle,profile.data['activitySource'],(module/'src/main/AndroidManifest.xml').as_posix()}|allowed <= names,
            'Missing declared Views snapshot inputs')
    other = '.gradle.kts' if profile.data['build'].get('dsl')=='groovy' else '.gradle'
    require(not {'settings'+other,(module/('build'+other)).as_posix()} & names,'Ambiguous Views Gradle DSL inputs')
    selected = next(item for item in manifest['files'] if item['path']==profile.data['activitySource'])
    require(selected['size'] <= ACTIVITY_BYTES,'Selected activity exceeds the PSI input limit')
    return names


def _json(value):
    return (json.dumps(value,sort_keys=True,indent=2,ensure_ascii=False,allow_nan=False)+'\n').encode()


def _small_json(root,name):
    try:
        return json.loads(read_regular(root,name,maximum=MAX_PROFILE),object_pairs_hook=_unique_object,
                          parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ArtifactError,OSError,ValueError,UnicodeError,RecursionError):
        raise ContractError('Views preparation metadata unavailable') from None

class _Publication:
    """Pinned new output root. Failures retain evidence, never delete trees."""
    def __init__(self, source, output):
        self.source = Path(source); self.path = Path(output)
        self.source_fd = self.parent_fd = self.fd = None
        created = False
        try:
            self.source_fd = open_directory(self.source)
            self.parent_fd = open_directory(self.path.parent)
            self.source_identity = self.identity(self.source_fd)
            self.parent_identity = self.identity(self.parent_fd)
            cursor = os.dup(self.parent_fd)
            try:
                for _ in range(128):
                    require(self.identity(cursor) != self.source_identity,
                            'Views snapshot output overlaps source')
                    parent = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cursor)
                    try: reached_root = self.identity(parent) == self.identity(cursor)
                    except BaseException:
                        os.close(parent); raise
                    if reached_root:
                        os.close(parent); break
                    os.close(cursor); cursor = parent
                else: raise ContractError('Views output ancestry exceeds limit')
            finally: os.close(cursor)
            self._same(self.source, self.source_identity)
            self._same(self.path.parent, self.parent_identity)
            os.mkdir(self.path.name, mode=0o700, dir_fd=self.parent_fd)
            created = True
            self.fd = os.open(self.path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.parent_fd)
            self.root_identity = self.identity(self.fd)
            self.current()
        except BaseException as error:
            self.close()
            if created: raise snapshots.SnapshotError('snapshot_cleanup_incomplete') from error
            raise

    @staticmethod
    def identity(fd):
        value = os.fstat(fd); return value.st_dev, value.st_ino

    @classmethod
    def _same(cls, path, identity):
        fd = open_directory(path)
        try: require(cls.identity(fd) == identity, 'Views publication path changed')
        finally: os.close(fd)

    def current(self):
        self._same(self.source, self.source_identity)
        self._same(self.path.parent, self.parent_identity)
        self._same(self.path, self.root_identity)

    def write(self, name, raw):
        require(name in {'app-profile.json', ORIGINAL, CHANGES, RECEIPT, 'receipt.json'},
                'Unsupported Views sidecar')
        self.current()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        self.current()

    def close(self):
        for name in ('fd', 'parent_fd', 'source_fd'):
            fd = getattr(self, name, None)
            if fd is not None: os.close(fd); setattr(self, name, None)


def _changes(original,profile,metadata,sites):
    module,variant=build_coordinates(profile); _,settings,gradle=_coordinates(profile.data)
    groovy=profile.data['build'].get('dsl')=='groovy'
    try:settings_text=metadata[settings].decode('utf-8');module_text=metadata[gradle].decode('utf-8')
    except UnicodeError:raise ContractError('Invalid Views Gradle source encoding') from None
    if groovy:
        from .groovy_integration import configure_groovy
        integration=configure_groovy(settings_text,module_text)
        # AGP's decorated Groovy objects still dynamically overload typed null.
        # Resolve the exact public JVM setter descriptor, never a dynamic setter.
        signing = """
def __reproofClearUnsignedSigning = { Object dimension ->
    if (!(dimension instanceof com.android.build.api.dsl.ApplicationVariantDimension)) {
        throw new GradleException('Unsupported unsigned Android dimension')
    }
    def dimensionType = com.android.build.api.dsl.ApplicationVariantDimension.class
    def signingType = com.android.build.api.dsl.ApkSigningConfig.class
    def setter = dimensionType.getMethod('setSigningConfig', [signingType] as Class[])
    def getter = dimensionType.getMethod('getSigningConfig', new Class[0])
    if (setter.getDeclaringClass() != dimensionType || setter.getReturnType() != Void.TYPE ||
        getter.getDeclaringClass() != dimensionType || getter.getReturnType() != signingType) {
        throw new GradleException('Unsupported unsigned Android public API')
    }
    setter.invoke(dimension, new Object[1])
    if (getter.invoke(dimension, new Object[0]) != null) {
        throw new GradleException('Android signing configuration was not cleared')
    }
}
android {
    __reproofClearUnsignedSigning(defaultConfig)
    productFlavors.configureEach { dimension -> __reproofClearUnsignedSigning(dimension) }
    __reproofClearUnsignedSigning(buildTypes.getByName('debug'))
}
"""
    else:
        from .kotlin_instrumenter import configure_gradle
        integration=configure_gradle(settings_text,module_text)
        signing=('\nandroid {\n    defaultConfig { signingConfig = null }\n'
                 '    productFlavors.configureEach { signingConfig = null }\n'
                 '    buildTypes { getByName("debug") { signingConfig = null } }\n}\n')
    changes={settings:integration[settings].encode(),
             gradle:(integration['module.gradle' if groovy else 'module.gradle.kts']+signing).encode()}
    for name in ('settings.gradle.kts','build.gradle.kts',*(f'src/main/java/io/reproof/instrumentation/gradle/{name}.java'
            for name in ('ReproBytecodeTransformer','ReproInstrumentationPlugin','ReproInstrumentationTask'))):
        changes['reproof-build-logic/'+name]=read_resource('reproof/build_instrumentation_templates/buildSrc/'+name)
    changes['reproof-build-logic/build.gradle.kts']+=('\ngradlePlugin {\n    plugins {\n        create("reproInstrumentation") {\n'
        '            id = "io.reproof.instrumentation"\n'
        '            implementationClass = "io.reproof.instrumentation.gradle.ReproInstrumentationPlugin"\n'
        '        }\n    }\n}\n').encode()
    entries=',\n        '.join(f'Map.entry({site["line"]}, {json.dumps(site["id"])})' for site in sites)
    changes['reproof-build-logic/src/main/java/io/reproof/instrumentation/gradle/ReproPlan.java']=(
        'package io.reproof.instrumentation.gradle;\n\nimport java.util.Map;\n\npublic final class ReproPlan {\n'
        f'    public static final String MODULE = {json.dumps(":"+":".join(profile.data["build"]["task"].split(":")[1:-1]))};\n'
        f'    public static final String VARIANT = {json.dumps(variant)};\n'
        f'    public static final String ACTIVITY = {json.dumps(activity_class(profile))};\n'
        f'    public static final String PROFILE_DIGEST = {json.dumps(profile.digest)};\n'
        f'    public static final Map<Integer, String> SITES = Map.ofEntries(\n        {entries}\n    );\n'
        '    private ReproPlan() {}\n}\n').encode()
    support=module/'reproof-instrumentation';runtime=support/'runtime/io/reproof/autotrace'
    for name,template in (
        ('ReproAuto.kt','reproof/observation_templates/android/io/reproof/autotrace/ReproAuto.kt'),
        ('ReproAppLogs.kt','reproof/instrumentation_templates/android/debug/java/io/reproof/autotrace/ReproAppLogs.kt'),
        ('ReproHooks.kt','reproof/build_instrumentation_templates/runtime/io/reproof/autotrace/ReproHooks.kt')):
        changes[(runtime/name).as_posix()]=read_resource(template)
    changes[(runtime/'ReproConfig.kt').as_posix()]=render_runtime_config(profile,sites).encode()
    changes[(support/'assets/reproof-observation.json').as_posix()]=_json(profile.data)
    debug=(module/'src/debug/AndroidManifest.xml').as_posix()
    changes[(support/'AndroidManifest.xml').as_posix()]=metadata.get(debug,b'<manifest/>\n')
    require(set(changes)==set(_generated(profile))|{settings,gradle} and len(changes)<=16
        and sum(map(len,changes.values()))<=META_BYTES,'Views generated changes exceed limits')
    return changes


def prepare_snapshot_observation(source,profile,output,*,analyzer=None,budgets=None,cancel=None):
    profile=_profile(profile);require('instrumentation' not in profile.data,'Select an original Views snapshot profile')
    source,output=Path(source).absolute(),Path(output).absolute()
    require(not output.exists() and not output.is_symlink() and not output.is_relative_to(source)
        and not source.is_relative_to(output),'Use a new separate Views preparation output')
    expected=_reference(profile.data['sourceSnapshot'])
    manifest=snapshots.inspect_snapshot(source,expected=expected,budgets=budgets,cancel=cancel)
    names=_manifest(manifest,profile)
    module,settings,gradle=_coordinates(profile.data);activity=profile.data['activitySource']
    debug=(module/'src/debug/AndroidManifest.xml').as_posix()
    selected=[activity,settings,gradle]+([debug] if debug in names else [])
    metadata=snapshots.read_snapshot_files(source,selected,expected=expected,budgets=budgets,cancel=cancel,
                                           max_files=4,max_bytes=META_BYTES)
    require(all(len(metadata[name])<=ACTIVITY_BYTES for name in (activity,settings,gradle)),
            'Views activity or Gradle metadata exceeds parser limit')
    if analyzer is None:
        from .kotlin_instrumenter import instrument_kotlin
        analyzer=instrument_kotlin
    try:plan=analyzer({activity:metadata[activity].decode('utf-8')},activity_class(profile),profile.data['tapTargets'])
    except UnicodeError:raise ContractError('Invalid Views source encoding') from None
    require(type(plan) is dict and type(plan.get('schemaVersion')) is int and plan['schemaVersion']==1
        and plan.get('activityPath')==activity and type(plan.get('sites')) is list,'Invalid selected activity site analysis')
    original_profile=profile
    profile=_profile(dict(profile.data,instrumentation={'kind':'android_asm_v1','sites':plan['sites']}))
    sites=sorted(profile.data['instrumentation']['sites'],key=lambda item:item['line'])
    changes=_changes(original_profile,profile,metadata,sites)
    publication=_Publication(source,output)
    try:
        publication.write('app-profile.json',_json(profile.data))
        prepared=snapshots.derive_snapshot(source,output/'snapshot',changes,expected=expected,budgets=budgets,
            cancel=cancel,output_parent_identity=publication.root_identity)
        changed=[{'path':name,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in sorted(changes.items())]
        receipt={'schemaVersion':3,'kind':'android-views-snapshot-preparation','profileDigest':profile.digest,
            'originalProfileDigest':original_profile.digest,'originalSnapshot':_bound(expected),
            'preparedSnapshot':_bound(prepared),'changedFiles':sorted(changes),'changesDigest':digest(changed),
            'productSourcesUnchanged':True,'behaviorVerified':False,'parser':'kotlin-psi','transformer':'android-asm'}
        _active(cancel)
        publication.write(ORIGINAL,_json(manifest));publication.write(CHANGES,_json(changed));publication.write(RECEIPT,_json(receipt))
        publication.current();_active(cancel)
        return {'status':'prepared','mode':'build','source':output/'snapshot','snapshot':output/'snapshot',
            'appProfile':output/'app-profile.json','report':output/RECEIPT,'changes':output/CHANGES,
            'sites':len(sites),'productSourcesUnchanged':True,'behaviorVerified':False}
    except BaseException as error:raise snapshots.SnapshotError('snapshot_cleanup_incomplete') from error
    finally:publication.close()


def validate_snapshot_preparation(source,profile,*,budgets=None,cancel=None):
    source=Path(source).absolute();profile=_profile(profile)
    require('instrumentation' in profile.data,'Select a prepared Views snapshot profile')
    receipt=_small_json(source.parent,RECEIPT)
    fields={'schemaVersion','kind','profileDigest','originalProfileDigest','originalSnapshot','preparedSnapshot',
            'changedFiles','changesDigest','productSourcesUnchanged','behaviorVerified','parser','transformer'}
    original_document=profile.data;del original_document['instrumentation'];original_profile=_profile(original_document)
    require(type(receipt) is dict and set(receipt)==fields and type(receipt['schemaVersion']) is int
        and receipt['schemaVersion']==3 and receipt['kind']=='android-views-snapshot-preparation'
        and receipt['profileDigest']==profile.digest and receipt['originalProfileDigest']==original_profile.digest
        and receipt['originalSnapshot']==profile.data['sourceSnapshot'] and receipt['productSourcesUnchanged'] is True
        and receipt['behaviorVerified'] is False and receipt['parser']=='kotlin-psi' and receipt['transformer']=='android-asm',
        'Invalid Views snapshot preparation receipt')
    try:original=bounded_json(read_regular(source.parent,ORIGINAL,maximum=snapshots.MAX_MANIFEST_BYTES),
                              max_items=snapshots.MAX_FILES,max_depth=3)
    except (ArtifactError,OSError,ValueError,UnicodeError):raise ContractError('Views original snapshot metadata unavailable') from None
    require(snapshots.snapshot_manifest_reference(original,budgets=budgets)==_reference(profile.data['sourceSnapshot']),
            'Views original snapshot reference changed')
    _manifest(original,original_profile)
    expected=_reference(receipt['preparedSnapshot'])
    current=snapshots.inspect_snapshot(source,expected=expected,budgets=budgets,cancel=cancel);_manifest(current,profile,prepared=True)
    before={item['path']:item for item in original['files']};after={item['path']:item for item in current['files']}
    _,settings,gradle=_coordinates(profile.data);generated=set(_generated(profile));allowed=generated|{settings,gradle}
    require(set(after)==set(before)|generated and receipt['changedFiles']==sorted(allowed)
        and all(after[name]==item for name,item in before.items() if name not in {settings,gradle}),
        'Prepared Views product inputs changed')
    changed=_small_json(source.parent,CHANGES)
    require(changed==[after[name] for name in sorted(allowed)] and receipt['changesDigest']==digest(changed),
            'Prepared Views changes differ')
    module,_=build_coordinates(profile);embedded=(module/'reproof-instrumentation/assets/reproof-observation.json').as_posix()
    raw=snapshots.read_snapshot_files(source,[embedded],expected=expected,budgets=budgets,cancel=cancel,max_files=1,max_bytes=MAX_PROFILE)[embedded]
    try:
        depth=0;quoted=escaped=False
        for byte in raw:
            if quoted:
                if escaped:escaped=False
                elif byte==92:escaped=True
                elif byte==34:quoted=False
            elif byte==34:quoted=True
            elif byte in (91,123):depth+=1
            elif byte in (93,125):depth-=1
            if not 0<=depth<=8:raise ValueError()
        if quoted or depth:raise ValueError()
        embedded_document=json.loads(raw,object_pairs_hook=_unique_object,
            parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError,RecursionError):
        raise ContractError('Embedded Views profile is invalid') from None
    require(embedded_document==profile.data,'Embedded Views profile differs')
    _active(cancel)
    return receipt


def _apk_identity(path):
    with os.fdopen(open_regular(path.parent,path.name),'rb') as stream:
        before=os.fstat(stream.fileno());require(before.st_nlink==1 and 0<before.st_size<=MAX_APK,'Invalid Views APK size')
        sha=hashlib.sha256();total=0
        while True:
            raw=stream.read(1024*1024)
            if not raw:break
            total+=len(raw);require(total<=MAX_APK,'Views APK exceeds size limit');sha.update(raw)
        after=os.fstat(stream.fileno())
        require((before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_nlink)==(after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_nlink)
            and total==before.st_size,'Views APK changed during inspection')
        return sha.hexdigest(),total


def _unsigned_apk(path):
    """Reject v1 metadata and v2/v3 signing blocks before claiming unsigned."""
    with os.fdopen(open_regular(path.parent,path.name),'rb') as stream:
        before=os.fstat(stream.fileno());size=before.st_size
        require(before.st_nlink==1 and 22<=size<=MAX_APK,'Invalid Views APK size')
        stream.seek(max(0,size-65557));tail=stream.read(65557)
        offset=tail.rfind(b'PK\x05\x06');require(offset>=0 and offset+22<=len(tail),'Invalid APK directory')
        eocd=tail[offset:offset+22];_,disk,cd_disk,disk_count,count,cd_size,cd_offset,comment=struct.unpack('<4s4H2LH',eocd)
        absolute=max(0,size-65557)+offset
        require(disk==cd_disk==0 and disk_count==count and count<65535 and cd_offset<0xffffffff
            and cd_size<0xffffffff and absolute+22+comment==size and cd_offset+cd_size==absolute,
            'Unsupported APK directory layout')
        if cd_offset>=16:
            stream.seek(cd_offset-16);require(stream.read(16)!=b'APK Sig Block 42','Signed APK is unsupported')
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            names=archive.namelist();require(len(names)==count and len(set(names))==len(names),'Ambiguous APK archive')
            require(not any(name.upper().startswith('META-INF/') and name.upper().endswith(('.SF','.RSA','.DSA','.EC'))
                            for name in names),'Signed APK is unsupported')
        stream.seek(0);sha=hashlib.sha256();total=0
        while True:
            raw=stream.read(1024*1024)
            if not raw:break
            total+=len(raw);require(total<=size,'Views APK changed during inspection');sha.update(raw)
        after=os.fstat(stream.fileno())
        require((before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_nlink)==
            (after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_nlink) and total==size,
            'Views APK changed during inspection')
        return sha.hexdigest(),size


def _no_signing_material(path,root_fd):
    identity=_Publication.identity(root_fd);_Publication._same(path,identity)
    seen=0
    def visit(fd,depth):
        nonlocal seen
        require(depth<=16,'Android user-home traversal exceeds limit')
        with os.scandir(fd) as entries:
            for entry in entries:
                seen+=1;require(seen<=8192,'Android user-home traversal exceeds limit')
                require(not entry.is_symlink(),'Linked Android user-home entry')
                require(not entry.name.lower().endswith(('.keystore','.jks')), 'Unexpected Android signing material')
                child=os.open(entry.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                try:
                    info=os.fstat(child)
                    if stat.S_ISDIR(info.st_mode):visit(child,depth+1)
                    else:require(stat.S_ISREG(info.st_mode) and info.st_nlink==1,
                                 'Unsupported Android user-home entry')
                finally:os.close(child)
    visit(root_fd,0)
    _Publication._same(path,identity)


def _stage_apk(source,publication,expected,expected_bytes):
    publication.current()
    with os.fdopen(open_regular(source.parent,source.name),'rb') as src:
        info=os.fstat(src.fileno());require(info.st_nlink==1 and info.st_size==expected_bytes,'Selected APK changed')
        fd=os.open('app.apk',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=publication.fd)
        with os.fdopen(fd,'wb') as dst:
            sha=hashlib.sha256();total=0
            while True:
                raw=src.read(1024*1024)
                if not raw:break
                total+=len(raw);require(total<=expected_bytes,'Selected APK changed');sha.update(raw);dst.write(raw)
            require(total==expected_bytes and sha.hexdigest()==expected,'Selected APK changed')
        after=os.fstat(src.fileno())
        require((info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_nlink)==
                (after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_nlink),'Selected APK changed')
    publication.current()
    return publication.path/'app.apk'


def build_snapshot_observation_app(source,output,*,profile,gradle,java_home,sdk_home,timeout=300,budgets=None,cancel=None):
    from .android_observation import profile_from_apk
    source,output=Path(source).absolute(),Path(output).absolute();profile=_profile(profile)
    require('instrumentation' in profile.data,'Build requires a prepared Views snapshot profile')
    require(type(timeout) is int and 1<=timeout<=3600,'Invalid Views build timeout')
    require(not output.exists() and not output.is_symlink() and not output.is_relative_to(source)
        and not source.is_relative_to(output),'Use a new separate Views build output')
    preparation=validate_snapshot_preparation(source,profile,budgets=budgets,cancel=cancel)
    expected=_reference(preparation['preparedSnapshot'])
    manifest=snapshots.inspect_snapshot(source,expected=expected,budgets=budgets,cancel=cancel)
    names=_manifest(manifest,profile,prepared=True)
    cache=os.environ.get('GRADLE_USER_HOME')
    require(cache is None or (type(cache) is str and 0<len(cache)<=4096 and Path(cache).is_absolute()
        and not any(ord(char)<32 for char in cache)),'Invalid operator Gradle cache selection')
    publication=_Publication(source,output)
    workspace_fd=home_fd=None
    try:
        publication.write('app-profile.json',_json(profile.data))
        workspace=output/'source'
        materialized=snapshots.materialize(source,workspace,budgets=budgets,cancel=cancel,
                                           output_parent_identity=publication.root_identity)
        require(all(materialized[key]==expected[key] for key in expected),'Views materialized snapshot differs')
        workspace_fd=open_directory(workspace);workspace_identity=_Publication.identity(workspace_fd)
        publication.current();os.mkdir('android-user-home',mode=0o700,dir_fd=publication.fd)
        android_home=output/'android-user-home'
        home_fd=open_directory(android_home)
        env={'JAVA_HOME':str(java_home),'ANDROID_HOME':str(sdk_home),'ANDROID_SDK_ROOT':str(sdk_home),
             'ANDROID_USER_HOME':str(android_home)}
        if cache is not None:env['GRADLE_USER_HOME']=cache
        _Publication._same(workspace,workspace_identity);_active(cancel)
        run_command([str(gradle),'--offline','--no-daemon','--console=plain',profile.data['build']['task']],
                    str(workspace),timeout=timeout,env_extra=env,cancellation=cancel)
        publication.current()
        _Publication._same(workspace,workspace_identity)
        _no_signing_material(android_home,home_fd)
        require_declared_tree(workspace,sorted(names))
        checked=snapshots.capture(workspace,sorted(names),output/'post-build-snapshot',budgets=budgets,cancel=cancel,
                                  output_parent_identity=publication.root_identity)
        require(all(checked[key]==expected[key] for key in expected),'Views build changed protected inputs')
        require(validate_snapshot_preparation(source,profile,budgets=budgets,cancel=cancel)==preparation,
                'Views preparation changed during build')
        bytecode=validate_bytecode_artifacts(workspace,profile)
        apk=workspace/profile.data['build']['apk'];apk_digest,apk_bytes=_apk_identity(apk)
        require(_unsigned_apk(apk)==(apk_digest,apk_bytes),'Views unsigned APK identity changed')
        app=_stage_apk(apk,publication,apk_digest,apk_bytes)
        automatic=profile_from_apk(app)
        require(automatic is not None and automatic.digest==profile.digest,'Views APK differs from prepared profile')
        require(_unsigned_apk(app)==(apk_digest,apk_bytes),'Views unsigned APK identity changed')
        require(_apk_identity(app)==(apk_digest,apk_bytes),'Staged Views APK changed')
        receipt={'schemaVersion':3,'kind':'configured-views-snapshot-build','platform':'android',
            'package':profile.data['package'],'activity':profile.data['activity'],'sourceSnapshot':_bound(expected),
            'profileDigest':profile.digest,'preparationDigest':digest(preparation),'apkRelative':'app.apk',
            'artifactDigest':apk_digest,'artifactBytes':apk_bytes,'buildCompleted':True,'signed':False,
            'automaticObservations':True,'behaviorVerified':False,'bytecodeInstrumentation':bytecode,
            'buildTask':profile.data['build']['task']}
        _Publication._same(workspace,workspace_identity)
        _no_signing_material(android_home,home_fd)
        _active(cancel)
        publication.write('receipt.json',_json(receipt))
        publication.current();_active(cancel)
        return {'output':output,'app':app,'receipt':receipt}
    except BaseException as error:raise snapshots.SnapshotError('snapshot_cleanup_incomplete') from error
    finally:
        for fd in (workspace_fd,home_fd):
            if fd is not None:os.close(fd)
        publication.close()
