"""Native lifetime ownership for selected CoreDevice queries, without qualification."""
from dataclasses import dataclass,field
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import threading
import time

from . import contracts
from .execution.wire import MAX_TRANSFER_BYTES
from .ios_device_tools import IOSDeviceTools,IOSDeviceToolError,_require
from .ios_mobile_native import IOSMobileNativeOwner
from .repair_android_signing import _Collector,_ProcessOwner,_ProcessResult


@dataclass(frozen=True,slots=True)
class IOSDeviceGuardianTools:
    path: Path = field(repr=False)
    sha256: str

    def __post_init__(self):
        object.__setattr__(self,'path',Path(self.path));self.verify()

    def verify(self):
        IOSDeviceTools(self.path,self.sha256)

    @property
    def definition_digest(self):
        return contracts.digest({'kind':'ios-device-query-guardian-v1','path':str(self.path),'sha256':self.sha256})


class _IOSGuardianProcess:
    """One guardian plus its separately grouped SDK child.

    The Python child handle only names the guardian.  The native guardian
    therefore supplies a completion pipe and writes its byte only after it
    has reaped the SDK child and observed the entire separate SDK process
    group disappear.  A dead guardian with no completion byte remains owned;
    process-group emptiness of the guardian alone is not evidence for iOS.
    """
    __slots__ = ('process','collectors','completion_read','live_write','completion',
        'completion_eof','completion_invalid','stdout','stderr','_lock')

    def __init__(self,process,collectors,completion_read,live_write):
        self.process=process;self.collectors=collectors
        self.completion_read=completion_read;self.live_write=live_write
        self.completion=None;self.completion_eof=False;self.completion_invalid=False
        self.stdout=process.stdout if process is not None else None
        self.stderr=process.stderr if process is not None else None
        self._lock=threading.RLock()
        os.set_blocking(completion_read,False)

    def read_completion(self):
        with self._lock:
            if self.completion_read is None or self.completion is not None or self.completion_eof or self.completion_invalid:return
            try:
                value=os.read(self.completion_read,16)
            except BlockingIOError:
                return
            except OSError:
                self.completion_invalid=True;return
            if not value:
                self.completion_eof=True;return
            if value==b'D':self.completion=value
            else:self.completion_invalid=True

    def close_live(self):
        with self._lock:
            if self.live_write is not None:
                # The caller may still own another write descriptor. A single
                # revocation byte wakes the native watcher without relying on EOF.
                try:os.write(self.live_write,b'X')
                except OSError:pass
                try:os.close(self.live_write)
                except OSError:pass
                self.live_write=None

    def close_completion(self):
        with self._lock:
            descriptor=self.completion_read;self.completion_read=None
        if descriptor is not None:
            try:os.close(descriptor)
            except OSError:pass

    def close_streams(self):
        for name in ('stdout','stderr'):
            with self._lock:
                stream=getattr(self,name);setattr(self,name,None)
            if stream is not None:
                try:stream.close()
                except OSError:pass

    @property
    def terminal(self):
        with self._lock:return self.completion==b'D'


class _IOSProcessOwner:
    """iOS-only process ownership with a native child-completion handshake."""
    def __init__(self):
        self._lock=threading.RLock();self._processes=set();self._closed=False

    @property
    def active_processes(self):
        self._refresh()
        with self._lock:return len(self._processes)

    def _refresh(self):
        with self._lock:
            selected=tuple(self._processes)
        for item in selected:
            with self._lock:
                if item not in self._processes:continue
                with item._lock:
                    item.read_completion()
                    if item.terminal and item.process.poll() is not None:
                        try:item.process.wait()
                        except OSError:pass
                        if all(not collector.thread.is_alive() for collector in item.collectors):
                            item.close_streams()
                            self._processes.remove(item);item.close_live();item.close_completion()

    def register(self,process,*,completion_read,live_write,collectors):
        owned_completion=os.dup(completion_read)
        owned_live=None
        try:
            if live_write is not None:owned_live=os.dup(live_write)
            item=_IOSGuardianProcess(process,collectors,owned_completion,owned_live)
        except BaseException:
            try:os.close(owned_completion)
            except OSError:pass
            if owned_live is not None:
                try:os.close(owned_live)
                except OSError:pass
            raise
        with self._lock:
            try:_require(not self._closed)
            except BaseException:
                # The caller retains the originals; this provisional item
                # owns only the duplicated descriptors.
                item.close_live();item.close_completion();raise
            try:self._processes.add(item)
            except BaseException:
                item.close_live();item.close_completion();raise
        return item

    def spawn(self,arguments,*,completion_read,live_write,collectors,**options):
        """Reserve completion ownership before the first child can exist."""
        with self._lock:
            _require(not self._closed and not self._processes)
            item=self.register(None,completion_read=completion_read,live_write=live_write,collectors=collectors)
            process=None
            try:
                process=subprocess.Popen(arguments,**options)
                item.process=process;item.stdout=process.stdout;item.stderr=process.stderr
                return item
            except BaseException:
                if process is None:
                    self._processes.discard(item);item.close_live();item.close_completion()
                else:
                    item.process=process;item.stdout=process.stdout;item.stderr=process.stderr
                raise

    @staticmethod
    def _terminate_guardian(item,deadline):
        with item._lock:
            process=item.process
            item.close_live()
            # Let the native watcher acknowledge cancellation, including a
            # child still initializing before installing signal handlers.
            grace=min(deadline,time.monotonic()+.5)
            while process.poll() is None and time.monotonic()<grace:
                time.sleep(min(.01,max(0,grace-time.monotonic())))
            # Never signal a numeric PID after Popen has reported/reaped it.
            # The completion pipe, rather than this leader's process group,
            # proves that the separate SDK group has stopped.
            if process.poll() is None:
                try:_ProcessOwner._terminate(process,deadline_monotonic=deadline)
                except OSError:pass
            if process.poll() is not None:
                try:process.wait()
                except OSError:pass

    @staticmethod
    def _wait_completion(item,deadline,cap=None):
        stop=deadline if cap is None else min(deadline,time.monotonic()+cap)
        while time.monotonic()<stop:
            item.read_completion()
            with item._lock:
                terminal=item.completion==b'D';invalid=item.completion_invalid
                eof=item.completion_eof;descriptor=item.completion_read
            if terminal or invalid or eof or descriptor is None:break
            try:select.select((descriptor,),(),(),min(.01,max(0,stop-time.monotonic())))
            except (OSError,ValueError):
                with item._lock:item.completion_invalid=True
                break
        item.read_completion()

    def _finished(self,item):
        with self._lock:
            if item not in self._processes:return True
            with item._lock:
                item.read_completion()
                if not item.terminal or item.process.poll() is None:return False
                try:item.process.wait()
                except OSError:return False
                for collector in item.collectors:
                    if collector.thread.ident is not None:
                        collector.thread.join(0)
                        if collector.thread.is_alive():return False
                item.close_streams();self._processes.discard(item);item.close_live();item.close_completion()
                return True

    def run(self,arguments,*,work,input_bytes,pass_fds,cancellation,deadline_monotonic,
            watched_files=(),max_output_bytes=65536,completion_read=None,live_write=None):
        _require(type(arguments) is tuple and arguments and all(type(value) is str and '\0' not in value for value in arguments))
        _require(callable(getattr(cancellation,'is_set',None)) and type(deadline_monotonic) in (int,float))
        _require(type(max_output_bytes) is int and 0<max_output_bytes<=MAX_TRANSFER_BYTES)
        _require(completion_read is not None and live_write is not None)
        for path,maximum in watched_files:
            _require(isinstance(path,Path) and type(maximum) is int and 0<maximum<=MAX_TRANSFER_BYTES)
        interrupted=cancellation.is_set() or time.monotonic()>=deadline_monotonic
        _require(not interrupted)
        environment={'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C','TMPDIR':str(work)}
        process=None;collectors=[];item=None;output_exceeded=False
        try:
            with self._lock:
                _require(not self._closed and not self._processes)
                item=self.spawn(arguments,completion_read=completion_read,live_write=live_write,collectors=collectors,
                    cwd=work,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,env=environment,close_fds=True,pass_fds=pass_fds,start_new_session=True)
                process=item.process
                collectors.extend((_Collector(process.stdout,max_output_bytes),_Collector(process.stderr,max_output_bytes)))
                for collector in collectors:collector.thread.start()
            try:
                process.stdin.write(input_bytes);process.stdin.flush()
            except (BrokenPipeError,OSError):pass
            finally:process.stdin.close()
            while True:
                with item._lock:
                    if process.poll() is not None:break
                item.read_completion()
                if any(collector.oversized for collector in collectors):output_exceeded=True;break
                for path,maximum in watched_files:
                    try:info=path.lstat()
                    except FileNotFoundError:continue
                    except OSError:output_exceeded=True;break
                    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size>maximum:
                        output_exceeded=True;break
                if output_exceeded:break
                if self._closed or cancellation.is_set() or time.monotonic()>=deadline_monotonic:
                    interrupted=True;break
                time.sleep(.01)
            if interrupted or output_exceeded:self._terminate_guardian(item,deadline_monotonic)
            else:
                with item._lock:
                    try:process.wait()
                    except OSError:pass
            # A guardian can have exited just before it reports completion.
            # Allow a short race window; never infer completion from the
            # guardian PID or its process group.
            self._wait_completion(item,deadline_monotonic,cap=.25)
            item.close_live()
            for collector in collectors:
                collector.thread.join(2)
            terminated=self._finished(item)
            bounded=not output_exceeded and all(not collector.oversized for collector in collectors)
            return _ProcessResult(process.returncode,bytes(collectors[0].data),bytes(collectors[1].data),
                terminated,bounded,interrupted)
        except BaseException as error:
            if process is not None and process.stdin is not None:
                try:process.stdin.close()
                except BaseException:pass
            if item is not None:
                self._terminate_guardian(item,time.monotonic()+3)
                self._wait_completion(item,time.monotonic()+3)
                self._finished(item)
            if isinstance(error,(OSError,ValueError,subprocess.SubprocessError)):
                raise IOSDeviceToolError() from None
            raise

    def close(self,*,deadline_monotonic=None):
        deadline=time.monotonic()+5 if deadline_monotonic is None else deadline_monotonic
        _require(type(deadline) in (int,float))
        with self._lock:
            self._closed=True;items=tuple(self._processes)
        for item in items:
            self._terminate_guardian(item,deadline)
            self._wait_completion(item,deadline)
            self._finished(item)
        return self.active_processes==0


class _IOSDeviceQueryGuardian:
    def __init__(self,definition,native_owner,guardian):
        _require(type(native_owner) is IOSMobileNativeOwner and type(guardian) is IOSDeviceGuardianTools)
        self.definition,self.native_owner,self.guardian=definition,native_owner,guardian
        self._processes=_IOSProcessOwner();self._closed=False
        self.verify()
        with native_owner.operations._changed:
            native_owner._check()
            native_owner.operations._native_clients.add(self)

    @property
    def binding_digest(self):return self.native_owner.binding_digest

    @property
    def active_processes(self):return self._processes.active_processes

    def verify(self):
        try:
            _require(not self._closed)
            self.native_owner._check();self.guardian.verify();self.definition.verify()
            _require(type(self.definition.native_guardian) is IOSDeviceGuardianTools
                and self.guardian.definition_digest==self.definition.native_guardian.definition_digest
                and self.definition.definition_digest==self.native_owner.operations.definition.query_definition_digest
                and self.definition.udid==self.native_owner.operations.definition.udid
                and self.definition.bundle==self.native_owner.operations.definition.bundle_id)
        except (OSError,RuntimeError,ValueError,TypeError,AttributeError):
            raise IOSDeviceToolError() from None

    def run(self,arguments,*,work,input_bytes,pass_fds,cancellation,deadline_monotonic,
            watched_files,max_output_bytes):
        self.verify()
        _require(type(arguments) is tuple and input_bytes==b'' and pass_fds==()
            and Path(work).parent==self.definition.work_root and max_output_bytes==65536)
        runtime_copy = arguments == (str(self.definition.tools.devicectl),'device','copy','from',
            '--device',self.definition.identifier,'--domain-type','appDataContainer',
            '--domain-identifier',self.definition.bundle,'--source','Library/Application Support/ReproLoop/runtime-identity.json',
            '--destination',str(work/'identity.json'),'--json-output',str(work/'result.json'))
        if runtime_copy:
            _require(watched_files == ((work/'result.json',256*1024),(work/'identity.json',4096)))
        else:
            _require(len(arguments) in (8,10)
                and arguments[:3]==(str(self.definition.tools.devicectl),'device','info')
                and arguments[3] in ('details','apps','processes')
                and arguments[4:6]==('--device',self.definition.identifier)
                and arguments[-2:]==('--json-output',str(work/'result.json'))
                and (arguments[6:-2]==('--bundle-id',self.definition.bundle) if arguments[3]=='apps'
                     else not arguments[6:-2]))
        reader=writer=completion_read=completion_write=None
        try:
            with self.native_owner.borrow_descriptors() as borrowed:
                reader,writer=os.pipe()
                completion_read,completion_write=os.pipe()
                descriptors=(borrowed.producer_fd,borrowed.operation_directory_fd,
                             borrowed.device_fd,borrowed.device_directory_fd,reader,completion_write)
                command=(str(self.guardian.path),*map(str,descriptors[:5]),
                    self.native_owner.operations.definition.scope_digest,
                    str(self.definition.tools.devicectl),self.definition.tools.sha256,
                    'runtime-identity' if runtime_copy else arguments[3],
                    self.definition.identifier,self.definition.bundle,str(work),str(completion_write))
                self.native_owner.require_descriptors(borrowed);self.verify()
                result=self._processes.run(command,work=work,input_bytes=b'',pass_fds=descriptors,
                    cancellation=cancellation,deadline_monotonic=deadline_monotonic,
                    watched_files=watched_files,max_output_bytes=max_output_bytes,
                    completion_read=completion_read,live_write=writer)
                return result
        finally:
            for descriptor in (reader,writer,completion_read,completion_write):
                if descriptor is not None:
                    try:os.close(descriptor)
                    except OSError:pass

    def close(self,*,deadline_monotonic):
        self._closed=True
        stopped=self._processes.close(deadline_monotonic=deadline_monotonic)
        if stopped:
            with self.native_owner.operations._changed:
                self.native_owner.operations._native_clients.discard(self)
                self.native_owner.operations._changed.notify_all()
        return stopped
