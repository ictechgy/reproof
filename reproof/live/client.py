"""Local automation client using the same fenced session and replay APIs as Live."""
from __future__ import annotations
import argparse
import getpass
import hashlib
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlsplit
from urllib.request import build_opener,HTTPCookieProcessor,Request,ProxyHandler,HTTPRedirectHandler
from urllib.error import HTTPError
import uuid
from ..issue_package import DEFAULT_LIMITS
from .model import LiveError,check,public_id


MAX_JSON_REQUEST_BYTES = 5 * 1024 * 1024
MAX_LOCAL_JSON_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_SHARED_JSON_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PACKAGE_BYTES = DEFAULT_LIMITS.max_archive_bytes


class LocalOnlyRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise LiveError('redirect_denied','The local client does not follow redirects',400)


class Client:
    def __init__(self,url):
        parsed=urlsplit(url)
        check(parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port is not None
              and not parsed.username and not parsed.password and parsed.path in {'','/'} and not parsed.query and not parsed.fragment,
              'invalid_server','Use the printed http://127.0.0.1:PORT URL',400)
        self.url=url.rstrip('/');self.opener=build_opener(ProxyHandler({}),LocalOnlyRedirect(),HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with self.opener.open(self.url+'/',timeout=10) as response:response.read()
    def download(self,path):
        check(isinstance(path,str) and path.startswith('/api/') and not path.startswith('//'),'invalid_path','Invalid local API path',400)
        with self.opener.open(Request(self.url+path,headers={'Origin':self.url}),timeout=40) as response:
            data=response.read(MAX_LOCAL_JSON_RESPONSE_BYTES+1)
            check(len(data)<=MAX_LOCAL_JSON_RESPONSE_BYTES,'response_limit','Export response is too large')
            return data
    def call(self,path,body=None):
        check(isinstance(path,str) and path.startswith('/api/'),'invalid_path','Invalid local API path',400)
        request=Request(self.url+path,data=json.dumps(body,allow_nan=False).encode() if body is not None else None,
                        headers={'Content-Type':'application/json','Origin':self.url})
        try:
            with self.opener.open(request,timeout=40) as response:
                data=response.read(MAX_LOCAL_JSON_RESPONSE_BYTES+1)
                check(len(data)<=MAX_LOCAL_JSON_RESPONSE_BYTES,'response_limit','Local API response is too large')
                return json.loads(data)
        except HTTPError as error:
            problem=json.load(error).get('error',{})
            raise LiveError(problem.get('code','request_failed'),problem.get('message','Local API failed'),error.code) from None


class IssueClient:
    """Authenticated shared client; the personal credential is used only at login."""
    def __init__(self, url, credential):
        parsed = urlsplit(url)
        try:
            loopback = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            loopback = False
        check(parsed.scheme in {'http', 'https'} and parsed.hostname and parsed.port is not None
              and (parsed.scheme == 'https' or loopback) and not parsed.username and not parsed.password
              and parsed.path in {'', '/'} and not parsed.query and not parsed.fragment,
              'invalid_server', 'Use an explicit shared HTTPS origin or an owned loopback HTTP origin', 400)
        check(type(credential) is str and 1 <= len(credential) <= 512 and '\n' not in credential and '\r' not in credential,
              'invalid_credential', 'A personal credential is required', 400)
        self.url = url.rstrip('/'); self.csrf = None
        self.opener = build_opener(ProxyHandler({}), LocalOnlyRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        result = self._json('/api/auth/session', {}, headers={'Authorization': 'Bearer ' + credential})
        self.csrf = result['csrfToken']
        self.principal_id = result['principalId']

    def _request(self, path, body=None, headers=None):
        check(type(path) is str and path.startswith('/api/') and '?' not in path and '#' not in path
              and '\\' not in path, 'invalid_path', 'Invalid shared API path', 400)
        request = Request(self.url + path, data=body,
            headers={'Origin': self.url, **({'X-Repro-CSRF': self.csrf} if body is not None and self.csrf else {}), **(headers or {})})
        try:
            return self.opener.open(request, timeout=70)
        except HTTPError as error:
            try:
                raw = error.read(64 * 1024 + 1)
                problem = json.loads(raw).get('error', {}) if len(raw) <= 64 * 1024 else {}
            except (ValueError, OSError):
                problem = {}
            finally: error.close()
            raise LiveError(problem.get('code', 'request_failed'), problem.get('message', 'Shared request failed'), error.code) from None

    def _json(self, path, body=None, *, headers=None):
        encoded = json.dumps(body, allow_nan=False, separators=(',', ':')).encode() if body is not None else None
        if encoded is not None:
            check(len(encoded) <= MAX_JSON_REQUEST_BYTES, 'request_limit', 'Issue request is too large', 413)
        with self._request(path, encoded, {'Content-Type': 'application/json', **(headers or {})}) as response:
            raw = response.read(MAX_SHARED_JSON_RESPONSE_BYTES + 1)
            check(len(raw) <= MAX_SHARED_JSON_RESPONSE_BYTES and response.headers.get_content_type() == 'application/json',
                  'response_limit', 'Shared response is unavailable or too large', 409)
            return json.loads(raw)

    def call(self, path, body=None):
        return self._json(path, body)

    def import_package(self, project_id, source):
        public_id(project_id)
        path = Path(source)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            properties = os.fstat(stream.fileno())
            check(stat.S_ISREG(properties.st_mode) and 1 <= properties.st_size <= MAX_PACKAGE_BYTES,
                  'package_size', 'Issue package is unavailable or too large', 413)
            body = stream.read(MAX_PACKAGE_BYTES + 1)
        check(len(body) == properties.st_size, 'package_changed', 'Issue package changed during import', 409)
        digest = hashlib.sha256(body).hexdigest()
        with self._request('/api/release/projects/' + project_id + '/import', body,
                {'Content-Type': 'application/zip', 'X-Repro-Content-SHA256': digest}) as response:
            raw = response.read(128 * 1024 + 1)
            check(len(raw) <= 128 * 1024, 'response_limit', 'Import result is too large', 409)
            return json.loads(raw)

    def export_package(self, issue_id, destination):
        public_id(issue_id)
        package = self.call('/api/release/issues/' + issue_id + '/export', {})['package']
        return self.download_package(package, destination)

    def download_package(self, package, destination):
        from .. import contracts
        from .evidence_store import _fsync_directory
        public_id(package['projectId']); public_id(package['id']); contracts.validate_digest(package['archiveDigest'])
        check(type(package['bytes']) is int and 1 <= package['bytes'] <= MAX_PACKAGE_BYTES,
              'package_size', 'Issue package is too large', 413)
        destination = Path(destination)
        check(not destination.exists() and not destination.is_symlink(), 'output_exists', 'Export output already exists', 409)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.parent / ('.issue-download-' + uuid.uuid4().hex + '.part')
        descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                path = '/api/release/projects/' + package['projectId'] + '/packages/' + package['id'] + '/export'
                with self._request(path) as response:
                    check(response.status == 200 and response.headers.get('Content-Length') == str(package['bytes'])
                          and response.headers.get('ETag') == '"sha256-' + package['archiveDigest'] + '"'
                          and response.headers.get_content_type() == 'application/zip',
                          'package_changed', 'Export response does not match this package', 409)
                    remaining = package['bytes']; digest = hashlib.sha256(); deadline = time.monotonic() + 70
                    while remaining:
                        check(time.monotonic() < deadline, 'request_timeout', 'Export timed out', 408)
                        block = response.read(min(64 * 1024, remaining))
                        check(bool(block), 'package_truncated', 'Export was interrupted', 409)
                        check(len(block) <= remaining, 'package_changed',
                              'Export exceeded its declared size', 409)
                        output.write(block); digest.update(block); remaining -= len(block)
                    check(digest.hexdigest() == package['archiveDigest'], 'package_changed', 'Package checksum changed', 409)
                output.flush(); os.fsync(output.fileno())
            os.link(staging, destination)  # publish without replacing an existing user file
            _fsync_directory(destination.parent)
            return {'package': package, 'exported': str(destination)}
        finally:
            staging.unlink(missing_ok=True)


def replay_recording(recording_id,expected_digest=None,argv=None):
    parser=argparse.ArgumentParser(description='Replay an immutable Live recording on the local session service')
    parser.add_argument('--server',default='http://127.0.0.1:8765')
    parser.add_argument('--session',help='Attach to this existing session and return control when finished')
    args=parser.parse_args(argv);client=Client(args.server)
    record=client.call('/api/recordings/'+recording_id)['recording']
    check(expected_digest is None or record['digest']==expected_digest,'recording_changed','Exported script digest does not match the recording')
    check(record['status']=='complete' and record['replayable'],'not_replayable','Recording cannot be replayed')
    variables={name:getpass.getpass(f'{name}: ') for name in record['variables']}
    controller='script-'+uuid.uuid4().hex;created=not args.session;s=None;previous=None
    try:
        if created:s=client.call('/api/sessions',{'deviceId':record['deviceId'],'clientId':controller})['session']
        else:s=client.call('/api/sessions/'+args.session)['session'];previous=s['controllerId']
        sid=s['id'];path='/api/sessions/'+sid;deadline=time.monotonic()+95
        while s['state']=='connecting' and time.monotonic()<deadline:
            time.sleep(.3);s=client.call(path)['session']
        check(s['state']=='active','session_inactive','Session did not become active')
        s=client.call(path+'/control',{'clientId':controller,'expectedEpoch':s['epoch'],'mode':'automation'})['session']
        response=client.call(path+'/replay',{'controllerId':controller,'epoch':s['epoch'],'recordingId':recording_id,'variables':variables})
        variables.clear();s=response['session'];deadline=time.monotonic()+900
        while s['replay']['state'] in {'running','cancelling'}:
            check(time.monotonic()<deadline,'replay_timeout','Replay exceeded the client deadline')
            time.sleep(.3);s=client.call(path)['session']
        print(json.dumps({'sessionId':sid,'replay':s['replay']}))
        return 0 if s['replay']['state']=='actions_replayed' else 2
    finally:
        variables.clear()
        if s:
            path='/api/sessions/'+s['id'];s=client.call(path)['session']
            if s.get('replay',{} ) and s['replay']['state'] in {'running','cancelling'}:
                client.call(path+'/replay/cancel',{'clientId':controller})
                deadline=time.monotonic()+35
                while time.monotonic()<deadline:
                    s=client.call(path)['session']
                    if s['replay']['state'] not in {'running','cancelling'}:break
                    time.sleep(.2)
            if created:client.call(path+'/close',{'controllerId':s['controllerId'],'epoch':s['epoch']})
            elif s['controllerId']==controller and s['state']=='active':
                client.call(path+'/control',{'clientId':previous,'expectedEpoch':s['epoch'],'mode':'manual'})


def export_script(record):
    check(record['status']=='complete' and record['replayable'],'not_replayable','Only complete replayable recordings can be exported as scripts')
    return ('#!/usr/bin/env python3\n'
            '"""Run from the Reproof checkout. Text variables are prompted without echo.\n'
            'Requires the original recording in the local server; use --session to attach.\n"""\n'
            'from reproof.live.client import replay_recording\n\n'
            'if __name__ == "__main__":\n'
            f'    raise SystemExit(replay_recording({record["id"]!r}, {record["digest"]!r}))\n').encode()
