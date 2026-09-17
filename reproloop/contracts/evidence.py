"""Immutable original evidence, later lifecycle receipts, and package manifests."""
from __future__ import annotations
import copy
from .versions import bounded_int, bounded_list, bounded_number, bounded_text, epoch_ms, exact, require, safe_relative_path, validate_digest, validate_id, validate_version
PROVENANCE = ('injected', 'observed', 'reproduced', 'authored', 'attestation')

def validate_provenance(value):
    exact(value, ('kind', 'source'), ('digest', 'author', 'revision'))
    require(value['kind'] in PROVENANCE, 'Invalid evidence provenance')
    require(value['kind'] != 'attestation' or value['source'] != 'import', 'Imported authority is not accepted')
    bounded_text(value['source'], 'provenance source', 160)
    if 'digest' in value:
        validate_digest(value['digest'])
    if 'author' in value:
        validate_id(value['author'], 'provenance author')
    if 'revision' in value:
        bounded_text(value['revision'], 'provenance revision', 120)
    return copy.deepcopy(value)

def _locator(value, depth=0):
    require(depth <= 3, 'Locator ancestry is too deep')
    exact(value, ('kind', 'value'), ('role', 'ancestor'))
    require(value['kind'] in ('accessibility-id', 'resource-id'), 'Invalid locator kind')
    bounded_text(value['value'], 'locator', 512)
    if 'role' in value:
        bounded_text(value['role'], 'locator role', 128)
    if 'ancestor' in value:
        _locator(value['ancestor'], depth + 1)

def _geometry(value):
    exact(value, ('width', 'height', 'rotation', 'version'), ('frameDigest',))
    bounded_int(value['width'], 'geometry width', 1, 4096)
    bounded_int(value['height'], 'geometry height', 1, 4096)
    require(value['width'] * value['height'] <= 4194304, 'Geometry pixel limit exceeded')
    bounded_int(value['rotation'], 'rotation', 0, 270)
    require(value['rotation'] in (0, 90, 180, 270), 'Invalid rotation')
    bounded_int(value['version'], 'geometry version', 1, 2 ** 63 - 1)
    if 'frameDigest' in value:
        validate_digest(value['frameDigest'], 'frame digest')

def validate_input(value):
    exact(value, ('action', 'parameters'), ('target', 'geometry'))
    action = value['action']
    require(action in ('tap', 'long-press', 'swipe', 'pointer', 'text', 'back', 'home', 'rotate', 'launch', 'terminate'), 'Invalid input action')
    params = value['parameters']
    target = 'target' in value
    if target:
        require(action in ('tap', 'long-press', 'text'), 'Operation cannot have a locator')
        require('geometry' not in value, 'Input has conflicting targets')
        _locator(value['target'])
    if 'geometry' in value:
        require(action in ('tap', 'long-press', 'swipe', 'pointer'), 'Operation has no geometry')
        _geometry(value['geometry'])
    if action in ('tap', 'long-press'):
        keys = (() if target else ('x', 'y')) + (('durationMs',) if action == 'long-press' else ())
        exact(params, keys)
        if not target:
            require('geometry' in value, 'Coordinate input needs geometry')
    elif action == 'swipe':
        exact(params, ('x', 'y', 'x2', 'y2', 'durationMs'))
        require('geometry' in value, 'Coordinate input needs geometry')
    elif action == 'pointer':
        exact(params, ('phase', 'pointerId', 'x', 'y'))
        require(params['phase'] in ('down', 'move', 'up', 'cancel'), 'Invalid pointer phase')
        bounded_int(params['pointerId'], 'pointer id', 0, 4)
        require(params['phase'] == 'cancel' or 'geometry' in value, 'Pointer input needs geometry')
    elif action == 'text':
        exact(params, ('variableId',))
        validate_id(params['variableId'], 'text variable id')
        require(target, 'Text input needs locator')
    elif action in ('launch', 'terminate'):
        exact(params, ('applicationId',))
        validate_id(params['applicationId'], 'application id')
    elif action == 'rotate':
        exact(params, ('orientation',))
        require(params['orientation'] in ('portrait', 'landscape-left', 'landscape-right'), 'Invalid orientation')
    else:
        exact(params, ())
    for key in ('x', 'y', 'x2', 'y2'):
        if key in params:
            bounded_number(params[key], 'coordinate', 0, 1)
    if 'durationMs' in params:
        bounded_int(params['durationMs'], 'gesture duration', 1, 5000)
    return copy.deepcopy(value)

def _preparation(value, recording):
    exact(value, ('receiptId', 'recipeId', 'operation', 'status', 'projectId', 'applicationId', 'startedAtMs', 'completedAtMs', 'payloadDigest'))
    validate_id(value['receiptId'], 'preparation receipt id')
    validate_id(value['recipeId'], 'fixture recipe id')
    require(value['operation'] in ('prepare', 'check'), 'Invalid preparation operation')
    require(value['status'] in ('complete', 'failed', 'unknown'), 'Invalid preparation status')
    require(value['projectId'] == recording['projectId'] and value['applicationId'] == recording['applicationId'], 'Preparation identity mismatch')
    epoch_ms(value['startedAtMs'], 'preparation start')
    epoch_ms(value['completedAtMs'], 'preparation completion')
    require(value['startedAtMs'] <= value['completedAtMs'] <= recording['startedAtMs'], 'Preparation was not completed before recording')
    validate_digest(value['payloadDigest'])

def _artifact_ref(value, kind):
    exact(value, ('id', 'digest', 'path', 'bytes', 'mimeType'))
    validate_id(value['id'], f'{kind} id')
    validate_digest(value['digest'])
    safe_relative_path(value['path'], f'{kind} path')
    bounded_int(value['bytes'], f'{kind} size', 1, 10 * 1024 * 1024 * 1024)
    bounded_text(value['mimeType'], 'mime type', 120)

def validate_original_evidence(value):
    required = ('schemaVersion', 'recordingId', 'projectId', 'projectRevision', 'applicationId', 'buildId', 'startedAtMs', 'endSequence', 'events', 'preparation', 'observations', 'media', 'clock', 'interruptions', 'unknowns', 'sealed')
    exact(value, required)
    validate_version(value['schemaVersion'])
    validate_id(value['recordingId'], 'recording id')
    validate_id(value['projectId'], 'project id')
    bounded_text(value['projectRevision'], 'project revision', 120)
    validate_id(value['applicationId'], 'application id')
    validate_id(value['buildId'], 'build id')
    epoch_ms(value['startedAtMs'], 'start time')
    bounded_int(value['endSequence'], 'end sequence', 0, 100000)
    events = bounded_list(value['events'], 'admitted events', 100000)
    for event in events:
        exact(event, ('id', 'operationId', 'generation', 'sequence', 'offsetMs', 'input', 'dispatch', 'receipt', 'provenance'))
        validate_id(event['id'], 'event id')
        validate_id(event['operationId'], 'operation id')
        bounded_int(event['generation'], 'generation', 1, 2 ** 63 - 1)
        bounded_int(event['sequence'], 'event sequence', 1, 100000)
        bounded_int(event['offsetMs'], 'event offset', 0, 600000)
        validate_input(event['input'])
        require(event['dispatch'] in ('injected', 'rejected', 'unknown'), 'Invalid dispatch outcome')
        validate_provenance(event['provenance'])
        expected_provenance = 'injected' if event['dispatch'] == 'injected' else 'observed'
        require(event['provenance']['kind'] == expected_provenance, 'Input provenance contradicts dispatch')
        receipt = event['receipt']
        if receipt is None:
            require(event['dispatch'] == 'unknown', 'Dispatch outcome requires a receipt')
        else:
            exact(receipt, ('operationId', 'generation', 'status', 'providerIncarnation', 'observedAtMs'), ('errorCode',))
            require(receipt['operationId'] == event['operationId'] and type(receipt['generation']) is int and (receipt['generation'] == event['generation']) and (receipt['status'] == event['dispatch']), 'Receipt operation mismatch')
            validate_id(receipt['providerIncarnation'], 'provider incarnation')
            epoch_ms(receipt['observedAtMs'], 'receipt time')
            require(receipt['observedAtMs'] >= value['startedAtMs'] + event['offsetMs'], 'Receipt predates admission')
            if 'errorCode' in receipt:
                validate_id(receipt['errorCode'], 'error code')
    require([x['sequence'] for x in events] == list(range(1, len(events) + 1)) and value['endSequence'] == len(events), 'Invalid recording freeze boundary')
    ids = [x['id'] for x in events]
    require(len(ids) == len(set(ids)), 'Duplicate event id')
    operations = [x['operationId'] for x in events]
    require(len(operations) == len(set(operations)), 'Duplicate admitted operation')
    require([x['offsetMs'] for x in events] == sorted((x['offsetMs'] for x in events)), 'Invalid event time order')
    for receipt in bounded_list(value['preparation'], 'preparation receipts', 128):
        _preparation(receipt, value)
    for ref in bounded_list(value['observations'], 'observation references', 100000):
        _artifact_ref(ref, 'observation')
    for ref in bounded_list(value['media'], 'media references', 10000):
        _artifact_ref(ref, 'media')
    for field, key in (('preparation', 'receiptId'), ('observations', 'id'), ('media', 'id')):
        identifiers = [item[key] for item in value[field]]
        require(len(identifiers) == len(set(identifiers)), 'Duplicate evidence object identity')
    clock = value['clock']
    exact(clock, ('source', 'uncertaintyMs'))
    require(clock['source'] in ('monotonic', 'host-wall'), 'Invalid clock source')
    bounded_int(clock['uncertaintyMs'], 'clock uncertainty', 0, 86400000)
    for gap in bounded_list(value['interruptions'], 'interruptions', 10000):
        exact(gap, ('startMs', 'endMs', 'reason'))
        epoch_ms(gap['startMs'], 'interruption start')
        epoch_ms(gap['endMs'], 'interruption end')
        require(gap['endMs'] >= gap['startMs'], 'Invalid interruption')
        validate_id(gap['reason'], 'interruption reason')
    for item in bounded_list(value['unknowns'], 'unknowns', 10000):
        exact(item, ('kind', 'sequence'))
        validate_id(item['kind'], 'unknown kind')
        bounded_int(item['sequence'], 'unknown sequence', 0, value['endSequence'])
    require(value['sealed'] is True, 'Original evidence must be sealed')
    return copy.deepcopy(value)

def validate_lifecycle_receipt(value):
    exact(value, ('schemaVersion', 'receiptId', 'recordingDigest', 'operationId', 'generation', 'sequence', 'kind', 'status', 'observedAtMs'))
    validate_version(value['schemaVersion'])
    validate_id(value['receiptId'], 'receipt id')
    validate_digest(value['recordingDigest'], 'recording digest')
    validate_id(value['operationId'], 'operation id')
    bounded_int(value['generation'], 'generation', 1, 2 ** 63 - 1)
    bounded_int(value['sequence'], 'receipt sequence', 1, 2 ** 63 - 1)
    require(value['kind'] in ('ack', 'cleanup', 'stop', 'quarantine', 'reconcile'), 'Invalid lifecycle receipt kind')
    require(value['status'] in ('pending', 'complete', 'failed', 'unknown'), 'Invalid lifecycle receipt status')
    epoch_ms(value['observedAtMs'], 'receipt time')
    return copy.deepcopy(value)

def validate_package_manifest(value):
    exact(value, ('schemaVersion', 'packageId', 'recordingDigest', 'specificationDigest', 'objects'))
    validate_version(value['schemaVersion'])
    validate_id(value['packageId'], 'package id')
    validate_digest(value['recordingDigest'])
    validate_digest(value['specificationDigest'])
    objects = bounded_list(value['objects'], 'package objects', 100000, minimum=1)
    for item in objects:
        validate_digest(item, 'package object digest')
    require(len(objects) == len(set(objects)), 'Duplicate package object')
    require(value['recordingDigest'] in objects and value['specificationDigest'] in objects, 'Package omits referenced objects')
    return copy.deepcopy(value)
