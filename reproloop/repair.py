"""Bounded edit application. Agents return edits; they never own the verifier."""
from __future__ import annotations
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import tempfile
import time
from .core import ContractError, digest, require
from .storage import sha_file


class CommandError(RuntimeError):
    pass


def apply_edits(root, edits, allowed):
    root=Path(root).resolve();require(isinstance(edits,list) and 0<len(edits)<=10,'Expected 1–10 explicit edits')
    staged={}
    for edit in edits:
        require(isinstance(edit,dict) and set(edit)=={'path','old','new'},'Invalid edit schema')
        path=edit['path'];require(isinstance(path,str),'Invalid edit path')
        relative=PurePosixPath(path)
        require(not relative.is_absolute() and '..' not in relative.parts and str(relative)==path
                and path in allowed,'Edit touches a protected or invalid path')
        f=root/path
        require(f.is_file() and not any((root/Path(*relative.parts[:n])).is_symlink() for n in range(1,len(relative.parts)+1))
                and f.resolve().is_relative_to(root),'Linked or missing edit target')
        require(f.stat().st_size<=128*1024,'Source file too large')
        old,new=edit['old'],edit['new']
        require(isinstance(old,str) and isinstance(new,str) and old and len(new)<=64000 and old!=new,'Invalid replacement')
        source=staged.get(path,f.read_text())
        require(source.count(old)==1,'Replacement anchor must match exactly once')
        staged[path]=source.replace(old,new,1)
    # Validate all edits before mutating any product source.
    for path,source in staged.items():(root/path).write_text(source)
    return list(staged)



def validate_sample_expression(before, after):
    """MVP permits only an integer increment expression, never agent-supplied host code."""
    return validate_numeric_expression(before, after, 'increment')


def validate_numeric_expression(before, after, function):
    require(isinstance(function, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', function),
            'Unsupported Kotlin function identity')
    pattern = re.compile(r"(?m)^(?P<prefix>\s*fun " + re.escape(function) + r"\(\)(?:: Int)? = )(?P<expr>[^\n]+)$")
    original = list(pattern.finditer(before)); patched = list(pattern.finditer(after))
    require(len(original) == 1 and len(patched) == 1, 'Unsupported product expression shape')
    a, b = original[0], patched[0]
    require(before[:a.start('expr')] == after[:b.start('expr')]
            and before[a.end('expr'):] == after[b.end('expr'):], 'Patch changes protected product structure')
    require(re.fullmatch(r'(?:[0-9]{1,2}|if \(BuildConfig\.BUGGY\) [0-9]{1,2} else [0-9]{1,2})', b.group('expr')) is not None,
            'MVP only accepts a numeric increment expression')


def snapshot_source(root, *, source_inputs=None, isolated=False):
    if source_inputs is not None:
        from .android_sources import freeze_source, require_declared_tree, source_hashes
        if isolated:require_declared_tree(root, source_inputs)
        return source_hashes(freeze_source(root, source_inputs))
    root=Path(root).resolve();files={}
    for directory,dirs,names in os.walk(root):
        dirs[:]=sorted(d for d in dirs if d not in {'build','.gradle','.git','.idea','artifacts','runs','.codex'}
                       and not (Path(directory)/d).is_symlink())
        for name in sorted(names):
            f=Path(directory)/name
            if f.suffix not in {'.kt','.kts','.xml','.java'}:continue
            require(not f.is_symlink(),'Source contains a linked file')
            files[f.relative_to(root).as_posix()]=sha_file(f)
    require(bool(files),'No Android source files found')
    return files


def copy_source(root,destination, *, source_inputs=None):
    root=Path(root);destination=Path(destination);require(not destination.exists(),'Source copy already exists')
    if source_inputs is not None:
        from .android_sources import freeze_source, source_hashes
        from .execution.artifacts import ArtifactError
        frozen=freeze_source(root, source_inputs)
        destination.parent.mkdir(parents=True,exist_ok=True)
        try:frozen.write_new(destination)
        except ArtifactError:raise ContractError('Public Android source publication failed') from None
        return source_hashes(frozen)
    snapshot=snapshot_source(root)
    destination.mkdir(parents=True,mode=0o700)
    for name in snapshot:
        out=destination/name;out.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/name,out)
    require(snapshot_source(destination)==snapshot,'Source copy differs from source receipt')
    return snapshot


def run_command(command,cwd,*,stdin=None,timeout=300,max_output=2*1024*1024,env_extra=None,log_path=None,cancellation=None):
    require(isinstance(command,list) and command and all(isinstance(x,str) for x in command),'Expected executable argv')
    def active():
        if cancellation is not None:
            if not callable(getattr(cancellation,'is_set',None)) or cancellation.is_set():
                raise CommandError('Command was cancelled')
    active()
    env={k:v for k,v in os.environ.items() if k in {'HOME','USER','LANG','LC_ALL','PATH','CLAUDE_CONFIG_DIR'}}
    if env_extra:env.update(env_extra)
    with tempfile.TemporaryDirectory(prefix='repro-command-') as tmp:
        env.update(TMPDIR=tmp,TMP=tmp,TEMP=tmp)
        with open(Path(tmp)/'stdout','w+b') as out,open(Path(tmp)/'stderr','w+b') as err,open(Path(tmp)/'stdin','w+b') as inp:
            if stdin:inp.write(stdin.encode());inp.seek(0)
            active()
            try:
                process=subprocess.Popen(command,cwd=cwd,env=env,stdin=inp,stdout=out,stderr=err,start_new_session=True)
            except OSError as exc:raise CommandError('Command could not start') from exc
            started=time.monotonic()
            try:
                while process.poll() is None:
                    active()
                    if time.monotonic()-started>timeout:raise CommandError('Command time budget exhausted')
                    if os.fstat(out.fileno()).st_size>max_output or os.fstat(err.fileno()).st_size>max_output:
                        raise CommandError('Command output limit exceeded')
                    time.sleep(.1)
                active()
                require(os.fstat(out.fileno()).st_size<=max_output and os.fstat(err.fileno()).st_size<=max_output,
                        'Command output limit exceeded')
                if process.returncode:raise CommandError(f'Command failed with exit code {process.returncode}')
                out.seek(0);return out.read().decode('utf-8','replace')
            finally:
                if log_path is not None:
                    try:
                        out.flush();err.flush();out.seek(0);err.seek(0)
                        target=Path(log_path);target.parent.mkdir(parents=True,exist_ok=True)
                        target.write_bytes(out.read(max_output)+b"\n--- stderr ---\n"+err.read(max_output));target.chmod(0o600)
                    except OSError:
                        pass  # Optional diagnostics must never prevent child-process cleanup.
                # Terminate the whole job group, including children left by a completed parent.
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                finally:
                    # A completed parent does not imply that its children exited.
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    process.wait()


def build_android(source,gradle,java_home,sdk_home,task,apk_relative,timeout=300,app_profile=None,android_user_home=None):
    source_inputs=app_profile.data.get('sourceInputs') if app_profile is not None else None
    source=Path(source);before=snapshot_source(source, source_inputs=source_inputs, isolated=True)
    environment={'JAVA_HOME':str(java_home),'ANDROID_HOME':str(sdk_home),'ANDROID_SDK_ROOT':str(sdk_home)}
    if android_user_home is not None:environment['ANDROID_USER_HOME']=str(android_user_home)
    output=run_command([str(gradle),'--offline','--no-daemon','--console=plain',task],str(source),timeout=timeout,
                       env_extra=environment)
    require(snapshot_source(source, source_inputs=source_inputs, isolated=True)==before,'Build modified protected source')
    apk=source/apk_relative;require(apk.is_file(),'Build produced no expected APK')
    proof={'sourceDigest':digest(before),'sourceFiles':before,'apkSha256':sha_file(apk),
           'buildTask':task,'toolchain':{'gradleExecutable':Path(gradle).name,'java':Path(java_home).name},
           'buildCompleted':True}
    from .build_instrumentation import is_build_instrumented, validate_bytecode_artifacts
    if is_build_instrumented(app_profile):
        require(task == app_profile.data['build']['task'], 'Bytecode proof requires the selected test-build task')
        proof['bytecodeInstrumentation'] = validate_bytecode_artifacts(source, app_profile)
    return apk,proof
