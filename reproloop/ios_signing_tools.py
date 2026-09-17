"""Offline build/load of the fixed iOS signer, guardian and verifier resources."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import stat
import sys
import time
import uuid

from . import contracts
from .android_signing_tools import (AndroidSigningToolsError, _open_directory, _publish_new,
    _run_fixed, _sha_at, _sha_file, _write_new, _sync_output)
from .execution.wire import canonical, decode_json
from .ios_code_signature import _remove_owned_contents
from .ios_provisioning_cms import _public_file_digest
from .ios_signing_inputs import IOSSigningOwnerTools
from .resources import read_resource


_SOURCES = ('native/ios-signing-owner/main.c','native/ios-signing-owner/ownership.h',
            'native/ios-process-guardian/main.c','native/ios-code-verifier/main.c')
_OUTPUTS = {'signer':'ios-signing-owner','guardian':'ios-process-guardian','verifier':'ios-code-verifier'}


class IOSSigningToolsError(RuntimeError):
    def __init__(self, code='ios_signing_tools_configuration', *, cleanup_confirmed=True):
        self.code = code
        self.cleanup_confirmed = cleanup_confirmed is True
        super().__init__(code)


def _require(value, code='ios_signing_tools_configuration'):
    if not value: raise IOSSigningToolsError(code)


def _active(cancellation, deadline):
    _require(callable(getattr(cancellation,'is_set',None)) and type(deadline) in (int,float) and math.isfinite(deadline))
    _require(not cancellation.is_set(),'ios_signing_tools_cancelled')
    _require(time.monotonic()<deadline,'ios_signing_tools_timeout')


@dataclass(frozen=True, slots=True)
class IOSSigningBuildTools:
    clang: Path
    clang_sha256: str
    sdk_root: Path
    sdk_settings_sha256: str

    def __post_init__(self):
        try:
            _require(sys.platform == 'darwin')
            for name in ('clang','sdk_root'):
                path = Path(getattr(self,name)); _require(path.is_absolute())
                object.__setattr__(self,name,path.resolve(strict=True))
            self.verify()
        except Exception:
            raise IOSSigningToolsError('ios_signing_tools_tool') from None

    def verify(self):
        try:
            contracts.validate_digest(self.clang_sha256); contracts.validate_digest(self.sdk_settings_sha256)
            _require(self.sdk_root.is_dir() and os.access(self.clang,os.X_OK)
                and _sha_file(self.clang)[0]==self.clang_sha256
                and _public_file_digest(self.sdk_root/'SDKSettings.json')==self.sdk_settings_sha256,
                'ios_signing_tools_tool')
        except Exception:
            raise IOSSigningToolsError('ios_signing_tools_tool') from None

    def public(self):
        return {'clang':str(self.clang),'clangSha256':self.clang_sha256,
            'sdkRoot':str(self.sdk_root),'sdkSettingsSha256':self.sdk_settings_sha256}


@dataclass(frozen=True, slots=True)
class IOSSigningToolsBuild:
    tools: IOSSigningOwnerTools
    manifest_digest: str
    output_digest: str


def _sources():
    values = {name:read_resource(name) for name in _SOURCES}
    _require(all(0<len(body)<=1024*1024 for body in values.values()),'ios_signing_tools_source')
    return values


def _discard(parent, name, identity):
    descriptor = None
    try:
        descriptor = os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        opened = os.fstat(descriptor)
        _require((opened.st_dev,opened.st_ino)==identity,'ios_signing_tools_cleanup')
        _remove_owned_contents(descriptor,[4096])
        named = os.stat(name,dir_fd=parent,follow_symlinks=False)
        _require((named.st_dev,named.st_ino)==identity,'ios_signing_tools_cleanup')
        os.rmdir(name,dir_fd=parent); os.fsync(parent)
        return True
    except Exception:
        return False
    finally:
        if descriptor is not None: os.close(descriptor)


def build_ios_signing_owner(output, build_tools, *, cancellation, deadline_monotonic):
    _require(type(build_tools) is IOSSigningBuildTools)
    _active(cancellation,deadline_monotonic)
    selected = Path(output)
    _require(selected.is_absolute() and selected.name and '..' not in selected.parts
        and not selected.exists() and not selected.is_symlink(),'ios_signing_tools_output')
    parent = None
    temporary = selected.parent/('.'+selected.name+'.'+uuid.uuid4().hex)
    identity = None; published=False; cleanup=True
    try:
        parent = _open_directory(selected.parent)
        os.mkdir(temporary.name,mode=0o700,dir_fd=parent)
        directory = os.open(temporary.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        try: info=os.fstat(directory); identity=info.st_dev,info.st_ino
        finally: os.close(directory)
        work = temporary/'.work'; work.mkdir(mode=0o700)
        sources = _sources()
        for name,body in sources.items():
            path=work/name; path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            _write_new(path,body)
        outputs={}
        for kind,filename in _OUTPUTS.items():
            build_tools.verify(); _active(cancellation,deadline_monotonic)
            source = work/('native/'+filename+'/main.c')
            binary = temporary/filename
            _run_fixed((build_tools.clang,'-std=c11','-fblocks','-Wall','-Wextra','-Werror',
                '-mmacosx-version-min=15.0','-isysroot',build_tools.sdk_root,source,
                '-framework','Security','-framework','CoreFoundation','-o',binary),
                work,cancellation,deadline_monotonic)
            build_tools.verify()
            directory=_open_directory(temporary)
            try:
                checksum,size,_ = _sha_at(directory,filename,maximum=16*1024*1024)
            finally: os.close(directory)
            _require(os.access(binary,os.X_OK),'ios_signing_tools_output')
            outputs[kind]={'file':filename,'sha256':checksum,'bytes':size}
        sandbox_digest=_public_file_digest(Path('/usr/bin/sandbox-exec'))
        native=IOSSigningOwnerTools(temporary/_OUTPUTS['signer'],outputs['signer']['sha256'],
            temporary/_OUTPUTS['verifier'],outputs['verifier']['sha256'],sandbox_digest,
            temporary/_OUTPUTS['guardian'],outputs['guardian']['sha256'])
        source_digests={name:hashlib.sha256(body).hexdigest() for name,body in sources.items()}
        output_digest=contracts.digest({'sources':source_digests,'outputs':outputs})
        manifest={'schemaVersion':1,'kind':'ios-signing-tools-v1','sources':source_digests,
            'build':build_tools.public(),'outputs':outputs,'sandboxSha256':sandbox_digest,
            'ownerDefinitionDigest':native.definition_digest,'outputDigest':output_digest}
        encoded=canonical(manifest)
        _require(len(encoded)<=64*1024,'ios_signing_tools_manifest')
        _write_new(temporary/'tools-manifest.json',encoded)
        work_info=work.stat(); directory=_open_directory(temporary)
        try: _require(_discard(directory,'.work',(work_info.st_dev,work_info.st_ino)),'ios_signing_tools_cleanup')
        finally: os.close(directory)
        build_tools.verify(); _require(_sources()==sources,'ios_signing_tools_source')
        for name in (*_OUTPUTS.values(),'tools-manifest.json'): _sync_output(temporary/name)
        _active(cancellation,deadline_monotonic)
        _publish_new(parent,temporary,selected); published=True
        digest=hashlib.sha256(encoded).hexdigest()
        loaded=load_ios_signing_owner(selected,digest)
        return IOSSigningToolsBuild(loaded,digest,output_digest)
    except AndroidSigningToolsError as error:
        cleanup=error.cleanup_confirmed
        raise IOSSigningToolsError(error.code.replace('android_signing_tools','ios_signing_tools'),
            cleanup_confirmed=cleanup) from None
    except IOSSigningToolsError as error:
        cleanup=error.cleanup_confirmed; raise
    except (OSError,ValueError,TypeError,RuntimeError):
        raise IOSSigningToolsError('ios_signing_tools_output') from None
    finally:
        if identity is not None and not published and cleanup:
            cleanup=_discard(parent,temporary.name,identity)
        if parent is not None: os.close(parent)
        if not cleanup: raise IOSSigningToolsError('ios_signing_tools_cleanup',cleanup_confirmed=False)


def load_ios_signing_owner(output, manifest_digest):
    directory=None
    try:
        contracts.validate_digest(manifest_digest)
        selected=Path(output); directory=_open_directory(selected)
        _require(stat.S_IMODE(os.fstat(directory).st_mode)==0o700,'ios_signing_tools_manifest')
        _require(set(os.listdir(directory))=={'tools-manifest.json',*_OUTPUTS.values()},'ios_signing_tools_manifest')
        checksum,size,identity=_sha_at(directory,'tools-manifest.json',maximum=64*1024)
        _require(checksum==manifest_digest,'ios_signing_tools_manifest')
        fd=os.open('tools-manifest.json',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        try:
            info=os.fstat(fd)
            _require((info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)==identity)
            raw=os.read(fd,size+1)
            _require(len(raw)==size and hashlib.sha256(raw).hexdigest()==manifest_digest)
        finally:os.close(fd)
        value=decode_json(raw)
        _require(type(value) is dict and set(value)=={'schemaVersion','kind','sources','build','outputs',
            'sandboxSha256','ownerDefinitionDigest','outputDigest'} and type(value['schemaVersion']) is int
            and value['schemaVersion']==1 and value['kind']=='ios-signing-tools-v1','ios_signing_tools_manifest')
        _require(value['sources']=={name:hashlib.sha256(body).hexdigest() for name,body in _sources().items()})
        build=value['build']
        _require(type(build) is dict and set(build)=={'clang','clangSha256','sdkRoot','sdkSettingsSha256'})
        for name in ('clang','sdkRoot'): _require(type(build[name]) is str and Path(build[name]).is_absolute())
        for name in ('clangSha256','sdkSettingsSha256'): contracts.validate_digest(build[name])
        outputs=value['outputs'];_require(type(outputs) is dict and set(outputs)==set(_OUTPUTS))
        for kind,filename in _OUTPUTS.items():
            row=outputs[kind]
            _require(type(row) is dict and set(row)=={'file','sha256','bytes'} and row['file']==filename
                and type(row['bytes']) is int)
            checksum,size,_=_sha_at(directory,filename,maximum=16*1024*1024)
            _require((checksum,size)==(row['sha256'],row['bytes']))
        native=IOSSigningOwnerTools(selected/_OUTPUTS['signer'],outputs['signer']['sha256'],
            selected/_OUTPUTS['verifier'],outputs['verifier']['sha256'],value['sandboxSha256'],
            selected/_OUTPUTS['guardian'],outputs['guardian']['sha256'])
        _require(native.definition_digest==value['ownerDefinitionDigest']
            and contracts.digest({'sources':value['sources'],'outputs':outputs})==value['outputDigest'])
        return native
    except Exception:
        raise IOSSigningToolsError('ios_signing_tools_manifest') from None
    finally:
        if directory is not None:os.close(directory)


__all__=['IOSSigningBuildTools','IOSSigningToolsBuild','IOSSigningToolsError',
         'build_ios_signing_owner','load_ios_signing_owner']
