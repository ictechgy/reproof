"""Exact, read-only APK inspection policy and staged artifact binding."""
import json
import os
from pathlib import Path

from .repair_android_operation import (
    AndroidOperationError, _require, _open_child_directory, _open_regular_at, _file_digest,
)
from .storage import MAX_APK


def inspector_sandbox(tool, apk, support):
    directories=('/usr/lib','/System/Library','/System/Volumes/Preboot/Cryptexes/OS',
        '/System/Cryptexes/OS','/private/preboot/Cryptexes/OS','/dev/fd')
    files=('/dev/null','/dev/random','/dev/urandom',str(tool),str(apk),'/',
        '/System','/System/Volumes','/System/Volumes/Preboot','/System/Volumes/Preboot/Cryptexes','/System/Cryptexes',
        *(str(path) for path,_ in support))
    readable=' '.join('(subpath '+json.dumps(path)+')' for path in directories)
    readable+=' '+' '.join('(literal '+json.dumps(path)+')' for path in files)
    return ('(version 1)(allow default)(deny network*)(deny mach-lookup)(deny process-fork)'
        '(deny file-read* file-write*)(allow file-read-metadata)(allow file-read* '+readable+')'
        '(deny file-read-data file-read-xattr (subpath "/System/Library/Keychains")'
        ' (subpath "/System/Volumes/Preboot/Cryptexes/OS/System/Library/Keychains"))')


def inspector_command(operations, descriptors, apk):
    operations.require_native_descriptors(descriptors)
    from .android_recovery import require_command
    require_command(operations,descriptors,apk=Path(apk))
    tools=operations.config.tools;tools.verify()
    path=Path(apk)
    work=operations._root(descriptors.operation_id)/'staging'
    _require(path.parent==work and path.name in {'candidate.apk','original.apk','helper.apk'},
             'android_inspector_artifact')
    intent=operations._intent(descriptors.operation_id,descriptors.operation_directory_fd)
    directory=_open_child_directory(descriptors.operation_directory_fd,'staging',expected=intent['stagingIdentity'])
    descriptor=None
    try:
        from .android_recovery import AndroidRecoveryDispatch
        from .android_recovery_materials import expected_apk, RECORD
        if RECORD in os.listdir(descriptors.operation_directory_fd):
            _require(type(descriptors) is AndroidRecoveryDispatch, 'android_inspector_artifact')
            expected=expected_apk(descriptors.operation_directory_fd,intent,
                operations._state(descriptors.operation_id,descriptors.operation_directory_fd),path.name)
        else:
            expected=intent['files'][path.name]
        descriptor=_open_regular_at(directory,path.name,expected=expected['identity'])
        digest,size=_file_digest(descriptor,MAX_APK)
        _require((digest,size)==(expected['digest'],expected['bytes']),'android_inspector_artifact')
    except OSError:
        raise AndroidOperationError('android_inspector_artifact') from None
    finally:
        if descriptor is not None:os.close(descriptor)
        os.close(directory)
    command=('/usr/bin/sandbox-exec','-p',inspector_sandbox(tools.package_inspector,path,tools.inspector_support),
        str(tools.package_inspector),'dump','badging',str(path))
    fields={'toolKind':'apk-inspector','packageInspectorPath':str(tools.package_inspector),
        'packageInspectorSha256':tools.package_inspector_digest,
        'packageInspectorSupport':[{'path':str(library),'sha256':sha} for library,sha in tools.inspector_support],
        'apkPath':str(path),'apkSha256':digest,'apkBytes':str(size)}
    return command,fields
