"""Explicit, ephemeral human-authorized action playback, never replay evidence.

Captured actions are data. This module grants neither a trusted starting state nor
permission to bypass the ordinary session input/identity/authority gates.
"""
from __future__ import annotations
import copy
import secrets
import re
import threading
import time
import uuid

from ..core import ContractError, digest


class ActionPlaybackMixin:
    def _playback_scope(self, s):
        from .model import check
        check(s.get('releaseRecorder') is None and s.get('releaseRecordingId') is None
              and s.get('releaseRegistration') is None and s.get('_startupBinding') is None
              and s.get('_retainedDeviceScope') is None and s.get('_candidateIdentity') is None
              and s.get('mode') == 'manual',
              'playback_scope', 'Unverified playback requires an ordinary manual session', 409)
        check(not s.get('activePointers') and not s.get('inflightCommands'),
              'playback_busy', 'Finish pending inputs before action playback', 409)
        check(not s.get('playbackThread') or not s['playbackThread'].is_alive(),
              'playback_busy', 'Wait for the prior action playback to stop', 409)
        rid = s.get('recordingId')
        check(not rid or self.recordings[rid]['data']['status'] != 'recording',
              'recording_busy', 'Stop recording before action playback', 409)

    def _action_origin(self, r, owner):
        # A process-local origin binding, not a reset contract or replay grant.
        return (owner, r['digest'], r['id'], r['sessionId'])

    def _playback_source(self, s, owner, rid):
        from .model import check
        from .recordings import validate_recording, _validate_pointer_trace
        r = self.recording(rid, owner)
        entry = self.recordings[rid]
        check(entry.get('actionOrigin') == self._action_origin(r, owner)
              and r['sessionId'] == s['id'] and r['status'] == 'complete'
              and r['events'], 'playback_source',
              'Use a complete recording captured in this current server session; imports are inert', 409)
        check(r['deviceId'] == s['deviceId']
              and r['providerKind'] == self.devices[s['deviceId']]['kind']
              and r.get('applicationIdentity') == self.devices[s['deviceId']]['capabilities'].get('applicationIdentity'),
              'application_identity_mismatch', 'Action recording target differs', 409)
        try:
            validate_recording(r)
            _validate_pointer_trace(r['events'], True)
        except ContractError:
            check(False, 'playback_source', 'Action trace is not complete and balanced', 409)
        capabilities = self.devices[s['deviceId']]['capabilities']
        check(all(e['action'] in {'tap','long_press','swipe','text','back','pointer'}
                  and e['action'] in capabilities['actions']
                  and e['status'] == 'injected' for e in r['events']),
              'playback_actions', 'Only acknowledged non-reset supported actions can be played', 409)
        return r

    def _playback_execution_binding(self, s):
        handle = s.get('deviceAuthority')
        return (s['owner'], s['id'], s['deviceId'],
                self.devices[s['deviceId']]['kind'], id(s['provider']),
                s['geometryVersion'], s['providerIncarnation'], id(handle), getattr(handle, 'generation', None), getattr(handle, 'helper_incarnation', None),
                id(self.authority), getattr(self.authority, 'host_incarnation', None),
                digest(self.devices[s['deviceId']]['capabilities']))

    def _playback_binding(self, s):
        return (s['controllerId'], s['epoch'], s['lastSequence'], self._playback_execution_binding(s))

    def _playback_frame(self, s, frame_id, geometry_version):
        from .model import check
        with s['frameLock']:
            frame = s['frame']
            check(frame is not None, 'frame_pending', 'No current frame', 409)
            captured = next((f for f in s['frameHistory'] if f[0] == frame_id), None)
            check(type(frame_id) is int and type(geometry_version) is int
                  and frame['geometryVersion'] == geometry_version
                  and captured is not None and time.monotonic() - captured[1] <= 10
                  and captured[2] == geometry_version and 0 <= frame['id'] - frame_id <= 20,
                  'stale_frame', 'Review a fresh current frame before confirming action playback', 409)
            return {k: frame[k] for k in ('width', 'height', 'orientation')}

    def plan_playback(self, sid, owner, controller, epoch, rid, variables, frame_id, geometry_version):
        from .model import check, validate_gesture
        s = self._session(sid, owner)
        with s['lock']:
            self._control(s, controller, epoch)
            self._playback_scope(s)
            # Replacing a plan invalidates its prior one-use confirmation token.
            s.pop('playbackPlan', None)
            r = self._playback_source(s, owner, rid)
            geometry = self._playback_frame(s, frame_id, geometry_version)
            check(geometry == r['geometry'], 'geometry_mismatch', 'Action geometry differs', 409)
            variables = {} if variables is None else copy.deepcopy(variables)
            check(isinstance(variables, dict) and set(variables) == set(r['variables']),
                  'missing_variables', 'Provide exactly the recorded text variables', 400)
            for value in variables.values(): validate_gesture('text', {'value': value})
            actions = []
            for e in r['events']:
                payload = ({'value': variables[e['payload']['variable']]} if e['action'] == 'text'
                           else copy.deepcopy(e['payload']))
                actions.append({'action': e['action'], 'payload': payload, 'offsetMs': e['offsetMs']})
            token = secrets.token_urlsafe(32)
            public = {'token': token, 'recordingId': rid, 'recordingDigest': r['digest'],
                      'actions': actions, 'startingState': 'unknown', 'verified': False,
                      'expiresInSeconds': 30,
                      'warning': 'Starts at the CURRENT screen without reset. Review every action and text value. Coordinates do not identify controls. This is NOT reproduced or verified evidence.'}
            s['playbackPlan'] = {'public': copy.deepcopy(public), 'binding': self._playback_binding(s),
                                 'deadline': self.clock() + 30, 'frameId': frame_id,
                                 'geometryVersion': geometry_version, 'geometry': geometry}
            return public

    def confirm_playback(self, sid, owner, controller, epoch, token, acknowledge_unknown_start,
                         frame_id, geometry_version):
        from .model import check
        s = self._session(sid, owner)
        with s['lock']:
            self._control(s, controller, epoch)
            self._playback_scope(s)
            check(type(token) is str and re.fullmatch(r'[A-Za-z0-9_-]{43}', token) is not None,
                  'playback_confirmation', 'Invalid action plan token format', 400)
            plan = s.pop('playbackPlan', None)  # A well-formed attempted confirmation is one-use.
            check(plan is not None and secrets.compare_digest(token, plan['public']['token'])
                  and acknowledge_unknown_start is True,
                  'playback_confirmation', 'Explicit one-use reviewed plan confirmation required', 409)
            check(self.clock() < plan['deadline'] and plan['binding'] == self._playback_binding(s),
                  'playback_stale', 'Plan expired or session changed; review a new plan', 409)
            check(geometry_version == plan['geometryVersion'], 'geometry_mismatch', 'Review changed geometry again', 409)
            self._playback_frame(s, frame_id, geometry_version)
            handle = s.get('deviceAuthority')
            if handle is not None: handle.check_ownership()
            r = self._playback_source(s, owner, plan['public']['recordingId'])
            check(r['digest'] == plan['public']['recordingDigest'], 'playback_stale', 'Source changed', 409)
            identifier = uuid.uuid4().hex
            s['knownStart'] = False
            s.pop('knownStartBinding', None)
            s['epoch'] += 1
            s.update(controllerId='playback-' + identifier, lastSequence=0, mode='playback')
            s['cancel'] = threading.Event()
            s['playback'] = {'id': identifier, 'recordingId': r['id'], 'state': 'running',
                             'index': 0, 'total': len(plan['public']['actions']),
                             'startingState': 'unknown', 'verified': False, 'reproduced': False}
            s['playbackExecutionBinding'] = self._playback_execution_binding(s)
            actions = plan['public']['actions']
            # Token and actual text are never written into logs or result artifacts.
            thread = threading.Thread(target=self._play_actions,
                args=(s, owner, r, actions, controller, s['controllerId'], s['epoch'], s['cancel'],
                      s['playback'], s['playbackExecutionBinding']), daemon=True)
            s['playbackThread'] = thread
            result = copy.deepcopy(s['playback'])
            thread.start()
            return result

    def _play_actions(self, s, owner, r, actions, return_controller, controller, epoch, cancel, run, binding):
        from .model import check, LiveError
        state = 'actions_played_unverified'
        try:
            started = time.monotonic()
            for index, action in enumerate(actions, 1):
                deadline = started + action['offsetMs'] / 1000
                while time.monotonic() < deadline and not cancel.is_set():
                    cancel.wait(min(max(0, deadline-time.monotonic()), self.idle_timeout/3, 10))
                    with s['lock']: self._control(s, controller, epoch)
                if cancel.is_set(): state = 'cancelled'; break
                with s['lock']:
                    self._control(s, controller, epoch)
                    check(not cancel.is_set() and binding == self._playback_execution_binding(s),
                          'playback_stale', 'Playback cancelled or execution scope changed', 409)
                    self._playback_source(s, owner, r['id'])
                    handle = s.get('deviceAuthority')
                    if handle is not None: handle.check_ownership()
                    frame = self._frame_meta(s)
                    check({k: frame[k] for k in r['geometry']} == r['geometry'],
                          'geometry_mismatch', 'Action geometry changed', 409)
                    command = {'controllerId': controller, 'epoch': epoch, 'sequence': index,
                               'commandId': uuid.uuid4().hex, 'frameId': frame['id'],
                               'geometryVersion': frame['geometryVersion'],
                               'action': action['action'], 'payload': copy.deepcopy(action['payload'])}
                receipt = self.input(s['id'], owner, command, _playback_run=run)
                check(receipt['status'] == 'injected', 'input_unknown', 'Input was not confirmed', 409)
                with s['lock']: run['index'] = index
        except Exception:
            state = 'cancelled' if cancel.is_set() and s['state'] == 'active' else 'failed'
        finally:
            actions.clear()
            try:
                if s['controllerId'] == controller and s['epoch'] == epoch and s.get('activePointers'):
                    self._cancel_active_pointers(s)
            except LiveError: state = 'failed'
            with s['lock']:
                if state == 'actions_played_unverified' and (cancel.is_set() or s['controllerId'] != controller or s['epoch'] != epoch):
                    state = 'cancelled'
                run['state'] = state
                if state == 'failed': run['error'] = 'Action playback stopped; no automatic retry'
                if s['controllerId'] == controller and s['epoch'] == epoch:
                    s.pop('playbackExecutionBinding', None)
                    s['knownStart'] = False
                    s.pop('knownStartBinding', None)
                    s.update(controllerId=return_controller, epoch=epoch+1, lastSequence=0, mode='manual')

    def cancel_playback(self, sid, owner):
        s = self._session(sid, owner)
        with s['lock']:
            s.pop('playbackPlan', None)
            if s.get('playback', {}).get('state') == 'running':
                s['cancel'].set()
                s['playback']['state'] = 'cancelling'
            return copy.deepcopy(s.get('playback'))
