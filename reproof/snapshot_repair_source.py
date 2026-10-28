"""Bounded proposal-only repair inputs backed by explicit local snapshots.

These objects authorize neither builds nor execution. Snapshot content identity
is separate from the legacy BlobSet metadata source digest used by G0 builds.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import threading
import weakref

from . import contracts, source_snapshot as snapshots
from .execution.artifacts import ArtifactError, open_regular
from .execution.wire import canonical, ProtocolError
from .project_repair import RepairError
from .source_input_policy import inert_input_path
from .storage import MAX_APK

MAX_EDITABLE_FILES = 128
MAX_EDITABLE_FILE_BYTES = 1024 * 1024
MAX_EDITABLE_BYTES = 8 * 1024 * 1024
MAX_CHANGED_FILES = 16


def _require(condition, code='source_policy'):
    if not condition: raise RepairError(code)


def _active(cancel):
    if cancel is not None and cancel.is_set():
        raise snapshots.SnapshotError('snapshot_cancelled')


def _reference(value):
    _require(type(value) is dict and set(value) == {'schemaVersion','kind','digest','files','bytes'}
        and type(value['schemaVersion']) is int and value['schemaVersion'] == 1
        and value['kind'] == 'reproof-local-source-snapshot'
        and type(value['digest']) is str and len(value['digest']) == 64
        and all(character in '0123456789abcdef' for character in value['digest'])
        and type(value['files']) is int and 1 <= value['files'] <= snapshots.MAX_FILES
        and type(value['bytes']) is int and 0 <= value['bytes'] <= snapshots.MAX_BYTES,
        'source_policy')
    return {key:value[key] for key in ('digest','files','bytes')}


def _bound(value):
    return dict({key:value[key] for key in ('digest','files','bytes')},
                schemaVersion=1,kind='reproof-local-source-snapshot')


def legacy_source_digest(manifest):
    """Compute BlobSet-compatible metadata digest; not actual-content proof.

    A caller must independently validate and bind the snapshot manifest. No
    source payload is read, transported or retained by this helper.
    """
    _require(type(manifest) is dict and type(manifest.get('files')) is list
             and 1 <= len(manifest['files']) <= snapshots.MAX_FILES,'source_policy')
    entries=[]
    for item in manifest['files']:
        _require(type(item) is dict and set(item)=={'path','sha256','size'}
            and type(item['path']) is str and type(item['sha256']) is str
            and len(item['sha256'])==64 and all(c in '0123456789abcdef' for c in item['sha256'])
            and type(item['size']) is int and 0<=item['size']<=snapshots.MAX_FILE_BYTES,'source_policy')
        entries.append({'path':item['path'],'digest':item['sha256'],'size':item['size']})
    entries.sort(key=lambda item:item['path'])
    # Incremental encoding preserves canonical BlobSet bytes without assembling
    # an extra potentially large encoded metadata string.
    sha=hashlib.sha256()
    encoder=json.JSONEncoder(sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    for chunk in encoder.iterencode(entries):sha.update(chunk.encode('utf-8'))
    return sha.hexdigest()


def freeze_edits(edits):
    """Own a bounded exact-string patch before any source read or callback."""
    _require(type(edits) is list and 1<=len(edits)<=64,'patch_invalid')
    # Bound fields before canonicalizing so arbitrary nested/oversized input
    # cannot turn patch validation into an unbounded serialization step.
    validated=[]
    for edit in edits[:65]:
        _require(len(validated)<64,'patch_invalid')
        _require(type(edit) is dict and len(edit)==3 and set(edit)=={'path','old','new'},'patch_invalid')
        path,old,new=edit.get('path'),edit.get('old'),edit.get('new')
        _require(type(path) is str and 1<=len(path)<=1024,'protected_path')
        _require(type(old) is str and type(new) is str and old and old!=new
            and len(old)<=128*1024 and len(new)<=128*1024 and '\0' not in old+new,'patch_invalid')
        try:_require(len(path.encode('utf-8'))<=4096 and len(old.encode('utf-8'))<=128*1024 and len(new.encode('utf-8'))<=128*1024,'patch_invalid')
        except UnicodeError:raise RepairError('patch_invalid') from None
        validated.append({'path':path,'old':old,'new':new})
    _require(1<=len(validated)<=64,'patch_invalid')
    edits=validated
    patch_bytes=0
    encoder=json.JSONEncoder(sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    for chunk in encoder.iterencode(edits):
        patch_bytes+=len(chunk.encode('utf-8'));_require(patch_bytes<=256*1024,'patch_limit')
    _require(len({edit['path'] for edit in edits})<=MAX_CHANGED_FILES,'patch_limit')
    return edits


@dataclass(frozen=True,slots=True,eq=False)
class FrozenSnapshot:
    digest: str
    _reference_json: str = field(repr=False)
    build_id: str
    project_digest: str
    policy_digest: str
    artifact_digest: str
    artifact_bytes: int
    _files: tuple = field(repr=False)

    @property
    def reference(self):return json.loads(self._reference_json)

    @property
    def source_paths(self):return tuple(item[0] for item in self._files)


@dataclass(frozen=True,slots=True,weakref_slot=True,eq=False)
class SnapshotCandidate:
    digest: str
    _reference_json: str = field(repr=False)
    changed_paths: tuple[str,...]
    path: Path = field(repr=False)

    @property
    def reference(self):return json.loads(self._reference_json)


class SnapshotRepairSource:
    """Operator-selected original source/APK and a bounded editable subset.

    Construction validates declarations only and performs no source/artifact
    reads. Each use revalidates full snapshot contents and the original APK.
    Frozen objects are process-owned selections, not serializable authority.
    """
    __slots__=('_project_json','_snapshot_root','_reference_json','_protected_paths',
               '_editable_paths','_original_artifact','_budgets','_frozen','_candidates','_lock')

    def __init__(self,project,snapshot_root,*,snapshot_reference,protected_paths,
                 original_artifact,budgets=None):
        try:
            selected=contracts.validate_project_revision(project)
            self._project_json=canonical(selected).decode('utf-8')
            _reference(snapshot_reference)
            self._reference_json=canonical(snapshot_reference).decode('utf-8')
            self._snapshot_root=Path(snapshot_root).absolute()
            self._original_artifact=Path(original_artifact).absolute()
            _require(self._original_artifact.suffix=='.apk','artifact_identity')
            _require(budgets is None or type(budgets) is snapshots.SnapshotBudget,'source_policy')
            self._budgets=snapshots.SnapshotBudget() if budgets is None else budgets
            _require(type(protected_paths) in (list,tuple) and len(protected_paths)<=snapshots.MAX_FILES,'source_policy')
            _require(len(set(protected_paths))==len(protected_paths),'source_policy')
            recipes={item['productFile'] for item in selected['recipes']+selected['fixtures']}
            protected=set(protected_paths)|recipes
            editable=tuple(sorted(selected['editablePaths']))
            _require(1<=len(editable)<=MAX_EDITABLE_FILES and not set(editable)&protected,'source_policy')
            for name in (*editable,*protected):inert_input_path(name)
            self._editable_paths=editable;self._protected_paths=tuple(sorted(protected))
            self._frozen={};self._candidates=weakref.WeakValueDictionary();self._lock=threading.RLock()
        except (contracts.ContractError,ProtocolError,TypeError,ValueError,UnicodeError):
            raise RepairError('source_policy') from None

    @property
    def project(self):return json.loads(self._project_json)
    @property
    def project_digest(self):return contracts.digest(self.project)
    @property
    def snapshot_root(self):return self._snapshot_root
    @property
    def snapshot_reference(self):return json.loads(self._reference_json)
    @property
    def editable_paths(self):return self._editable_paths
    @property
    def protected_paths(self):return self._protected_paths
    @property
    def original_artifact(self):return self._original_artifact
    @property
    def budgets(self):return self._budgets
    @property
    def snapshot_reservation_bytes(self):return self._budgets.max_bytes+self._budgets.max_manifest_bytes
    @property
    def policy_digest(self):
        return contracts.digest({'projectDigest':self.project_digest,
            'snapshotReference':self.snapshot_reference,'editablePaths':self.editable_paths,
            'protectedPaths':self.protected_paths,'artifactIdentity':'file-sha256',
            'editableFileBytes':MAX_EDITABLE_FILE_BYTES,'editableBytes':MAX_EDITABLE_BYTES,
            'changedFiles':MAX_CHANGED_FILES,'budgets':{
                'maxFiles':self._budgets.max_files,'maxBytes':self._budgets.max_bytes,
                'maxFileBytes':self._budgets.max_file_bytes,'maxManifestBytes':self._budgets.max_manifest_bytes}})

    def _artifact_identity(self,cancel):
        _active(cancel)
        try:
            path=self._original_artifact
            with os.fdopen(open_regular(path.parent,path.name),'rb') as stream:
                before=os.fstat(stream.fileno())
                _require(before.st_nlink==1 and 0<before.st_size<=MAX_APK,'artifact_identity')
                sha=hashlib.sha256();total=0
                while True:
                    _active(cancel);raw=stream.read(1024*1024)
                    if not raw:break
                    total+=len(raw);_require(total<=before.st_size,'original_changed');sha.update(raw)
                after=os.fstat(stream.fileno())
                _require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns,before.st_nlink)==
                    (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_nlink)
                    and total==before.st_size,'original_changed')
                # Reopen through the same no-follow ancestor policy so a replaced
                # pathname cannot silently bind an otherwise valid open inode.
                current=open_regular(path.parent,path.name)
                try:actual=os.fstat(current)
                finally:os.close(current)
                _require((actual.st_dev,actual.st_ino,actual.st_size,actual.st_mtime_ns,actual.st_ctime_ns,actual.st_nlink)==
                    (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_nlink),
                    'original_changed')
            _active(cancel)
            return sha.hexdigest(),total
        except (ArtifactError,OSError):raise RepairError('original_changed') from None

    def _inspect(self,cancel):
        manifest=snapshots.inspect_snapshot(self._snapshot_root,expected=_reference(self.snapshot_reference),
                                            budgets=self._budgets,cancel=cancel)
        files={item['path']:item for item in manifest['files']}
        _require(set(self.editable_paths)|set(self.protected_paths)<=set(files),'source_policy')
        _require(all(files[name]['size']<=MAX_EDITABLE_FILE_BYTES for name in self.editable_paths)
            and sum(files[name]['size'] for name in self.editable_paths)<=MAX_EDITABLE_BYTES,'patch_limit')
        return manifest

    def freeze(self,build_id,cancel=None):
        _active(cancel)
        project=self.project
        build=next((item for item in project['builds'] if item['id']==build_id),None)
        _require(build is not None and 'sourceDigest' in build,'source_provenance')
        app=next((item for item in project['applications'] if item['id']==build['applicationId']),None)
        _require(app is not None and app['platform']=='android','artifact_identity')
        manifest=self._inspect(cancel)
        source_digest=legacy_source_digest(manifest)
        artifact_digest,artifact_bytes=self._artifact_identity(cancel)
        _require(source_digest==build['sourceDigest'] and artifact_digest==build['artifactDigest'],'source_provenance')
        files=tuple((item['path'],item['sha256'],item['size']) for item in manifest['files'])
        measured=(source_digest,self._reference_json,build_id,self.project_digest,self.policy_digest,
                  artifact_digest,artifact_bytes,files)
        _active(cancel)
        with self._lock:
            # Every build is bound to this one immutable snapshot reference.
            # Reuse its metadata tuple rather than retaining one full manifest
            # per build in a long-lived proposal service.
            if self._frozen:
                shared=next(iter(self._frozen.values()))._files
                _require(files==shared,'original_changed')
                measured=(*measured[:-1],shared)
            previous=self._frozen.get(build_id)
            if previous is not None:
                _require((previous.digest,previous._reference_json,previous.build_id,previous.project_digest,
                    previous.policy_digest,previous.artifact_digest,previous.artifact_bytes,previous._files)==measured,
                    'original_changed')
                return previous
            frozen=FrozenSnapshot(*measured)
            self._frozen[build_id]=frozen
            return frozen

    def _owned(self,frozen,build_id=None):
        _require(type(frozen) is FrozenSnapshot,'original_changed')
        with self._lock:
            _require(self._frozen.get(frozen.build_id) is frozen
                and (build_id is None or frozen.build_id==build_id)
                and frozen.project_digest==self.project_digest and frozen.policy_digest==self.policy_digest,
                'original_changed')
        return frozen

    def require_original(self,frozen,build_id,cancel=None):
        self._owned(frozen,build_id)
        try:current=self.freeze(build_id,cancel=cancel)
        except RepairError:raise RepairError('original_changed') from None
        _require(current is frozen,'original_changed')
        return frozen

    def editable_entries(self,frozen,cancel=None):
        self._owned(frozen)
        self.require_original(frozen,frozen.build_id,cancel=cancel)
        selected=snapshots.read_snapshot_files(self._snapshot_root,list(self.editable_paths),
            expected=_reference(frozen.reference),budgets=self._budgets,cancel=cancel,
            max_files=MAX_EDITABLE_FILES,max_bytes=MAX_EDITABLE_BYTES)
        _require(all(len(raw)<=MAX_EDITABLE_FILE_BYTES for raw in selected.values()),'patch_limit')
        _active(cancel)
        return tuple(sorted(selected.items()))

    def apply(self,frozen,edits,output,cancel=None,output_parent_identity=None):
        self._owned(frozen)
        edits=freeze_edits(edits)
        _require(all(edit['path'] in self.editable_paths for edit in edits),'protected_path')
        entries=dict(self.editable_entries(frozen,cancel=cancel));replacements={}
        try:
            for edit in edits:
                path,old,new=edit['path'],edit['old'],edit['new'];text=entries[path].decode('utf-8')
                _require('\0' not in text,'patch_invalid')
                _require(text.count(old)==1,'patch_anchor')
                start=text.index(old);end=start+len(old);ranges=replacements.setdefault(path,[])
                _require(all(end<=first or start>=last for first,last,_ in ranges),'patch_overlap')
                ranges.append((start,end,new))
            changes={}
            for path,ranges in replacements.items():
                text=entries[path].decode('utf-8')
                for start,end,new in sorted(ranges,reverse=True):text=text[:start]+new+text[end:]
                raw=text.encode('utf-8');_require(len(raw)<=MAX_EDITABLE_FILE_BYTES,'patch_limit')
                if raw!=entries[path]:changes[path]=raw
        except (UnicodeError,ValueError,KeyError,TypeError):raise RepairError('patch_invalid') from None
        _require(changes,'patch_noop')
        _require(sum(map(len,changes.values()))<=MAX_EDITABLE_BYTES
            and sum(len(changes.get(name,raw)) for name,raw in entries.items())<=MAX_EDITABLE_BYTES,'patch_limit')
        self.require_original(frozen,frozen.build_id,cancel=cancel)
        output=Path(output).absolute();published=False
        try:
            derived=snapshots.derive_snapshot(self._snapshot_root,output,changes,
                expected=_reference(frozen.reference),budgets=self._budgets,cancel=cancel,
                output_parent_identity=output_parent_identity)
            published=True
            reference=_bound(derived)
            candidate=snapshots.inspect_snapshot(output,expected=_reference(reference),budgets=self._budgets,cancel=cancel)
            current={item['path']:(item['sha256'],item['size']) for item in candidate['files']}
            _require(set(current)==set(frozen.source_paths) and all(current[name]==(sha,size)
                for name,sha,size in frozen._files if name not in changes),'protected_path')
            candidate_digest=legacy_source_digest(candidate)
            _require(candidate_digest!=frozen.digest,'patch_noop')
            self.require_original(frozen,frozen.build_id,cancel=cancel);_active(cancel)
            selected=SnapshotCandidate(candidate_digest,canonical(reference).decode('utf-8'),tuple(sorted(changes)),output)
            with self._lock:self._candidates[id(selected)]=selected
            return self.require_candidate(selected,cancel=cancel)
        except BaseException as error:
            if published:raise snapshots.SnapshotError('snapshot_cleanup_incomplete') from error
            raise

    def require_candidate(self,candidate,cancel=None):
        _require(type(candidate) is SnapshotCandidate,'candidate_changed')
        with self._lock:
            _require(self._candidates.get(id(candidate)) is candidate,'candidate_changed')
        manifest=snapshots.inspect_snapshot(candidate.path,expected=_reference(candidate.reference),
                                            budgets=self._budgets,cancel=cancel)
        _require(legacy_source_digest(manifest)==candidate.digest,'candidate_changed')
        _active(cancel)
        return candidate
