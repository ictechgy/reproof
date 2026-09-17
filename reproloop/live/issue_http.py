"""Authenticated G7 JSON, upload and single-range media routes."""
from __future__ import annotations

import re
import socket
import threading
import time

from .. import contracts
from ..issue_package import PackageError
from ..project_repair import RepairError
from ..repair_journal import RepairJournalError
from .model import LiveError, check


def single_range(value, size):
    """Return inclusive byte bounds; reject ambiguous or unbounded syntax."""
    check(type(size) is int and size > 0, 'range_invalid', 'Media is empty', 416)
    if value is None:
        return 0, size-1, 200
    check(type(value) is str and len(value) <= 96, 'range_invalid', 'Invalid byte range', 416)
    match=re.fullmatch(r'bytes=([0-9]{0,16})-([0-9]{0,16})',value)
    check(match is not None and any(match.groups()),'range_invalid','Only one byte range is supported',416)
    first,last=match.groups()
    if not first:
        suffix=int(last)
        check(suffix>0,'range_invalid','Invalid suffix range',416)
        return max(0,size-suffix),size-1,206
    start=int(first)
    end=int(last) if last else size-1
    check(start<size and start<=end,'range_invalid','Range is outside this object',416)
    return start,min(end,size-1),206


class _Upload:
    def __init__(self,handler):
        self.handler=handler
        self.deadline=time.monotonic()+30

    def read(self,size):
        remaining=self.deadline-time.monotonic()
        check(remaining>0,'request_timeout','Upload deadline elapsed',408)
        self.handler.connection.settimeout(min(remaining,5))
        try:return self.handler.rfile.read1(size)
        except (OSError,TimeoutError):
            raise LiveError('request_timeout','Upload was interrupted',408) from None


def _stream(handler,reader,mime,*,download=False):
    try:
        start,end,status=single_range(handler.headers.get('Range'),reader.bytes)
    except LiveError as error:
        handler.send_response(error.status)
        handler.send_header('Content-Range',f'bytes */{reader.bytes}')
        handler.send_header('Content-Length','0')
        handler.send_header('Cache-Control','no-store')
        handler.end_headers()
        return
    reader._guard()
    handler.send_response(status)
    handler.send_header('Content-Type',mime)
    handler.send_header('Content-Length',str(end-start+1))
    handler.send_header('Accept-Ranges','bytes')
    handler.send_header('ETag','"sha256-'+reader.digest+'"')
    handler.send_header('Cache-Control','private, no-store')
    handler.send_header('X-Content-Type-Options','nosniff')
    handler.send_header('Referrer-Policy','no-referrer')
    if status==206:
        handler.send_header('Content-Range',f'bytes {start}-{end}/{reader.bytes}')
    if download:
        handler.send_header('Content-Disposition','attachment; filename="issue.repro.zip"')
    try:
        handler.end_headers()
        handler.connection.settimeout(5)
        for block in reader.chunks(start=start,end=end):
            handler.wfile.write(block)
    except Exception:
        # The response has begun: terminate it rather than append another HTTP
        # response or deliver bytes after a revocation/retention failure.
        handler.close_connection=True
        try:handler.connection.shutdown(socket.SHUT_RDWR)
        except OSError:pass


def dispatch(handler,principal,parts):
    workflow=handler.server.issue_workflow
    check(workflow is not None,'issue_not_configured','Issue workflow is not configured',409)
    def expire():
        try:handler.connection.shutdown(socket.SHUT_RDWR)
        except OSError:pass
    timer=threading.Timer(65,expire)
    timer.daemon=True
    timer.start()
    try:
        method=handler.command
        route=parts[2:]
        if len(route) >= 2 and (route[0] == 'repairs' or route[0] == 'issues' and route[2:] == ['repairs']):
            repairs = workflow.repairs
            check(repairs is not None, 'repair_not_configured', 'Project repair is not configured', 409)
            if route[0] == 'issues':
                if method == 'GET': return handler.respond(200, repairs.list(principal, route[1]))
                if method == 'POST': return handler.respond(202, repairs.start(principal, route[1], handler.body()))
            if route[0] == 'repairs':
                if len(route) == 2 and method == 'GET': return handler.respond(200, repairs.get(principal, route[1]))
                if route[2:] == ['proposal'] and method == 'GET':
                    return handler.respond(200, repairs.proposal(principal, route[1]))
                if route[2:] == ['diagnostics'] and method == 'GET':
                    return handler.respond(200, repairs.diagnostics(principal, route[1]), download='diagnostics.json')
                if route[2:] == ['cancel'] and method == 'POST':
                    check(not handler.body(), 'invalid_argument', 'Cancellation has no parameters', 400)
                    return handler.respond(202, repairs.cancel(principal, route[1]))
            raise LiveError('not_found', 'Repair route was not found', 404)
        if route==['projects'] and method=='GET':
            return handler.respond(200,workflow.projects(principal))
        if route==['issues']:
            if method=='GET':return handler.respond(200,workflow.list(principal))
            return handler.respond(202,workflow.start(principal,handler.body()))
        if len(route)==3 and route[0]=='projects' and route[2]=='import' and method=='POST':
            check(handler.headers.get('Content-Type')=='application/zip',
                  'invalid_content_type','Expected an issue ZIP archive',415)
            length=handler.headers.get('Content-Length','')
            check(re.fullmatch(r'[0-9]{1,9}',length) is not None,'invalid_body','Invalid archive length',413)
            digest=handler.headers.get('X-Repro-Content-SHA256')
            contracts.validate_digest(digest)
            return handler.respond(201,workflow.import_archive(principal,route[1],_Upload(handler),
                                                               size=int(length),digest=digest))
        if len(route)==5 and route[0]=='projects' and route[2]=='packages' and route[4]=='export' and method=='GET':
            project_id,package_id=route[1],route[3]
            workflow._authorize(principal,project_id,'export.read')
            with workflow.packages.open_archive(package_id,project_id,
                    authorize=workflow._guard(principal,project_id,'export.read')) as reader:
                return _stream(handler,reader,'application/zip',download=True)
        if len(route)>=2 and route[0]=='issues':
            issue_id=route[1]
            contracts.validate_id(issue_id)
            if len(route)==2 and method=='GET':
                return handler.respond(200,workflow.get(principal,issue_id))
            if len(route)==4 and route[2]=='media' and method=='GET':
                with workflow.open_media(principal,issue_id,route[3]) as (reader,mime):
                    return _stream(handler,reader,mime)
            if len(route)==3 and method=='POST':
                body=handler.body()
                action=route[2]
                if action in {'stop','cancel','export'}:
                    check(not body,'invalid_argument','This operation has no parameters',400)
                    result=(workflow.export(principal,issue_id) if action=='export'
                            else workflow.stop(principal,issue_id,cancel=action=='cancel'))
                    return handler.respond(200 if action=='export' else 202,result)
                methods={'input':workflow.input,'specifications':workflow.save_specification,
                         'approve':workflow.approve,'replay':workflow.replay}
                if action in methods:
                    return handler.respond(202 if action=='replay' else 200,methods[action](principal,issue_id,body))
        raise LiveError('not_found','Issue route was not found',404)
    except (RepairError, RepairJournalError) as error:
        raise LiveError(error.code, 'Repair request is unavailable or changed',
                        404 if error.code == 'repair_not_found' else 409) from None
    except PackageError as error:
        status=(403 if error.code=='authorization_revoked' else 410 if error.code in {
            'package_expired','package_removed','package_unavailable'} else 404 if error.code=='package_not_found'
                else 409 if error.code in {'package_storage','package_busy'} else 400)
        raise LiveError(error.code,str(error),status) from None
    finally:
        timer.cancel()
