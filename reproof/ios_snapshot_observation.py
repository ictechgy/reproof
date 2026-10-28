"""Bounded local snapshot UIKit observations, never fixture or provenance proof.

The original snapshot reference remains in the application profile. Preparation
is a new snapshot; its sidecars are local content bindings, not authorizations.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re

from .core import ContractError, digest, require
from .execution.artifacts import ArtifactError, open_directory, read_regular
from . import source_snapshot as snapshots
from .ios_observation import (GENERATED, RUNTIME, SUPPORT, _NAME, _input_name,
                              _plist, validate_observation_document)
from .ios_workspace import validate_workspace, workspace_files
from .repair import run_command
from .resources import read_resource
from .source_snapshot_json import bounded_json
from .storage import _unique_object

KIND = 'uikit-observation-v3'
META_BYTES = 16 * 1024 * 1024
RECEIPT = 'instrumentation.json'
ORIGINAL = 'original-manifest.json'
CHANGES = 'changes.json'


def _reference(value):
    require(type(value) is dict and set(value) == {'schemaVersion', 'kind', 'digest', 'files', 'bytes'}
            and type(value['schemaVersion']) is int and value['schemaVersion'] == 1
            and value['kind'] == 'reproof-local-source-snapshot'
            and type(value['digest']) is str and re.fullmatch('[0-9a-f]{64}', value['digest'])
            and type(value['files']) is int and 1 <= value['files'] <= snapshots.MAX_FILES
            and type(value['bytes']) is int and 0 <= value['bytes'] <= snapshots.MAX_BYTES,
            'Invalid UIKit snapshot reference')
    return {key: value[key] for key in ('digest', 'files', 'bytes')}


def _bound(reference):
    return dict(reference, schemaVersion=1, kind='reproof-local-source-snapshot')


def validate_snapshot_observation_document(document):
    required = {'schemaVersion', 'kind', 'applicationId', 'project', 'target', 'build',
                'sourceSnapshot', 'tapTargets', 'screenTargets'}
    require(type(document) is dict and required <= set(document) <= required | {'workspace', 'inputExtensions'}
            and type(document['schemaVersion']) is int and document['schemaVersion'] == 3
            and document['kind'] == KIND, 'Unsupported snapshot UIKit profile')
    _reference(document['sourceSnapshot'])
    # Reuse only the v2 structural contract, not its source selection or limits.
    # The real manifest is independently checked at every snapshot consumption.
    structural = dict(document, schemaVersion=2, kind='uikit-observation-v2')
    del structural['sourceSnapshot']
    require(type(document['project']) is str and type(document['build']) is dict,
            'Unsupported snapshot UIKit build selection')
    info = document['build'].get('infoPlist')
    require(type(info) is str, 'Select an explicit source Info.plist')
    names = [document['project'] + '/project.pbxproj', info]
    if 'workspace' in document:
        require(type(document['workspace']) is str and type(document['build'].get('scheme')) is str,
                'Unsupported snapshot UIKit workspace')
        names += list(workspace_files(document))
    structural['sourceInputs'] = sorted(set(names))
    validate_observation_document(structural)
    return document


def _profile(value):
    from .ios_instrumentation import IosAutoProfile, validate_ios_auto_profile
    value = value.data if isinstance(value, IosAutoProfile) else value
    validate_snapshot_observation_document(value)
    return validate_ios_auto_profile(value)


def _manifest_names(manifest, profile, *, prepared=False):
    names = [item['path'] for item in manifest['files']]
    generated = set(GENERATED) if prepared else set()
    for name in names:
        if name not in generated:
            _input_name(name, extensions=profile.data.get('inputExtensions'))
    required = {profile.data['project'] + '/project.pbxproj', profile.data['build']['infoPlist'],
                *workspace_files(profile.data)} | generated
    require(required <= set(names), 'Missing configured UIKit snapshot inputs')
    return names


def _decoded(value):
    pending = [(value, 0)]; count = 0
    while pending:
        item, depth = pending.pop(); count += 1
        require(count <= 250000 and depth <= 64, 'UIKit metadata structure exceeds limit')
        if type(item) in (dict, list):
            require(count + len(pending) + len(item) <= 250000,
                    'UIKit decoded metadata expansion exceeds limit')
        if type(item) is dict:
            require(all(type(key) is str for key in item), 'Unsupported UIKit metadata key')
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
    return value


def _json_preflight(raw):
    depth = count = 0; quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped: escaped = False
            elif byte == 92: escaped = True
            elif byte == 34: quoted = False
            continue
        if byte == 34: quoted = True; count += 1
        elif byte in (91, 123): depth += 1; count += 1
        elif byte in (93, 125): depth -= 1
        elif byte == 44: count += 1
        require(0 <= depth <= 64 and count <= 250000, 'UIKit metadata structure exceeds limit')
    require(not quoted and depth == 0, 'Invalid UIKit metadata structure')
    return count


def _preflight(raw):
    require(type(raw) is bytes and len(raw) <= 8 * 1024 * 1024, 'UIKit metadata exceeds limit')
    if raw.startswith(b'bplist'):
        require(len(raw) >= 40, 'Invalid UIKit binary property list')
        offset_size, ref_size = raw[-26], raw[-25]
        count = int.from_bytes(raw[-24:-16], 'big')
        table = int.from_bytes(raw[-8:], 'big')
        top = int.from_bytes(raw[-16:-8], 'big')
        require(0 < count <= 250000 and top < count and offset_size in (1, 2, 4, 8) and ref_size in (1, 2, 4, 8)
                and 8 <= table <= len(raw)-32 and count*offset_size <= len(raw)-32-table,
                'UIKit metadata structure exceeds limit')
        references = 0; spans = []
        for index in range(count):
            position = table+index*offset_size
            offset = int.from_bytes(raw[position:position+offset_size], 'big')
            require(8 <= offset < table, 'Invalid UIKit binary property list')
            token = raw[offset]; kind, length = token >> 4, token & 15
            payload = offset + 1; end = payload
            require(kind in (0, 1, 2, 3, 4, 5, 6, 8, 10, 13),
                    'Unsupported UIKit binary object')
            if kind == 0:
                require(length in (0, 8, 9, 15), 'Unsupported UIKit binary scalar')
            elif kind == 1:
                require(length <= 4, 'Unsupported UIKit binary integer')
                end += 1 << length
            elif kind == 2:
                require(length in (2, 3), 'Unsupported UIKit binary real')
                end += 1 << length
            elif kind == 3:
                require(length == 3, 'Unsupported UIKit binary date')
                end += 8
            elif kind == 8:
                end += length + 1
            if kind in (4, 5, 6, 10, 12, 13):
                if length == 15:
                    require(offset+1 < table and raw[offset+1] >> 4 == 1
                            and (raw[offset+1] & 15) <= 3, 'Invalid UIKit binary length')
                    width = 1 << (raw[offset+1] & 15)
                    require(offset+2+width <= table, 'Invalid UIKit binary length')
                    length = int.from_bytes(raw[offset+2:offset+2+width], 'big')
                    payload = offset + 2 + width
                require(length <= 8*1024*1024, 'UIKit binary value exceeds limit')
                unit = 2 if kind == 6 else (ref_size*(2 if kind == 13 else 1) if kind in (10, 12, 13) else 1)
                require(length*unit <= table-payload, 'Invalid UIKit binary payload length')
                end = payload + length*unit
                if kind in (10, 12, 13):
                    references += length * (2 if kind == 13 else 1)
                    require(references <= 250000, 'UIKit binary references exceed limit')
            require(end <= table, 'Invalid UIKit binary object span')
            spans.append((offset, end))
        spans.sort()
        require(all(left[1] <= right[0] for left, right in zip(spans, spans[1:])),
                'Overlapping UIKit binary objects are unsupported')
        require(count + references <= 250000, 'UIKit binary structure exceeds limit')
        return count + references
    if raw.lstrip().startswith(b'<') or raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        import xml.parsers.expat
        parser = xml.parsers.expat.ParserCreate(); depth = count = 0
        def start(name, attrs):
            nonlocal depth, count
            depth += 1; count += 1
            require(depth <= 64 and count <= 250000 and len(attrs) <= 64,
                    'UIKit metadata structure exceeds limit')
        def end(name):
            nonlocal depth
            depth -= 1
        def entity(*args): raise ContractError('Unsupported UIKit metadata entities')
        parser.StartElementHandler = start; parser.EndElementHandler = end
        parser.EntityDeclHandler = entity; parser.ExternalEntityRefHandler = entity
        try: parser.Parse(raw, True)
        except xml.parsers.expat.ExpatError:
            raise ContractError('Invalid UIKit metadata XML') from None
        return count
    # OpenStep shape preflight ignores bounded quoted strings and comments.
    # plutil still performs semantic parsing; its JSON result is preflighted too.
    stack = []; count = index = 0; quoted = escaped = block = line = False
    while index < len(raw):
        byte = raw[index]; following = raw[index+1] if index+1 < len(raw) else None
        if line:
            if byte in (10, 13): line = False
        elif block:
            if byte == 42 and following == 47: block = False; index += 1
        elif quoted:
            if escaped: escaped = False
            elif byte == 92: escaped = True
            elif byte == 34: quoted = False
        elif byte == 47 and following == 47: line = True; index += 1
        elif byte == 47 and following == 42: block = True; index += 1
        elif byte == 34: quoted = True
        elif byte in (123, 40): stack.append(125 if byte == 123 else 41); count += 1
        elif byte in (125, 41):
            require(stack and stack.pop() == byte, 'Invalid UIKit project structure')
        elif byte in (59, 44): count += 1
        require(len(stack) <= 64 and count <= 250000, 'UIKit project structure exceeds limit')
        index += 1
    require(not stack and not quoted and not block, 'Invalid UIKit project structure')
    return max(1, count)


def _bounded_plist(raw):
    _preflight(raw)
    try: return _decoded(_plist(raw))
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise ContractError('Unsupported UIKit property list metadata') from None


def _project_metadata(raw):
    _preflight(raw)
    try:
        if raw.startswith(b'bplist') or raw.lstrip().startswith(b'<') or raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
            return _bounded_plist(raw)
        text = run_command(['/usr/bin/plutil', '-convert', 'json', '-o', '-', '--', '-'],
                           '.', stdin=raw.decode('utf-8'), timeout=15, max_output=8 * 1024 * 1024)
        _json_preflight(text.encode('utf-8'))
        value = json.loads(text, object_pairs_hook=_unique_object)
        require(type(value) is dict, 'Unsupported Xcode project')
        return _decoded(value)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise ContractError('Unsupported UIKit project metadata') from None


def _metadata(source, manifest, profile, expected, budgets, *, prepared=False, cancel=None):
    names = _manifest_names(manifest, profile, prepared=prepared)
    selected = sorted({name for name in names if name.endswith('/project.pbxproj')}
                      | {profile.data['build']['infoPlist']} | set(workspace_files(profile.data)))
    require(len(selected) <= 128 and sum(item['size'] for item in manifest['files']
            if item['path'] in set(selected)) <= META_BYTES, 'UIKit metadata exceeds limit')
    entries = snapshots.read_snapshot_files(source, selected, expected=expected,
        budgets=budgets, cancel=cancel, max_files=128, max_bytes=META_BYTES)
    require(sum(_preflight(raw) for raw in entries.values()) <= 250000,
            'UIKit aggregate metadata structure exceeds limit')
    # Workspace validation needs the complete path set but bytes only for bounded
    # project/plist/scheme metadata. No source payload dictionary is constructed.
    from collections.abc import Mapping
    class Metadata(Mapping):
        def __iter__(self): return iter(names)
        def __len__(self): return len(names)
        def __getitem__(self, key): return entries[key]
        def __contains__(self, key): return key in names
    validate_workspace(profile.data, Metadata(), _project_metadata)
    return entries


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                       allow_nan=False) + '\n').encode()


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
                            'UIKit snapshot output overlaps source')
                    parent = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cursor)
                    try: reached_root = self.identity(parent) == self.identity(cursor)
                    except BaseException:
                        os.close(parent); raise
                    if reached_root:
                        os.close(parent); break
                    os.close(cursor); cursor = parent
                else: raise ContractError('UIKit output ancestry exceeds limit')
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
        try: require(cls.identity(fd) == identity, 'UIKit publication path changed')
        finally: os.close(fd)

    def current(self):
        self._same(self.source, self.source_identity)
        self._same(self.path.parent, self.parent_identity)
        self._same(self.path, self.root_identity)

    def write(self, name, raw):
        require(name in {'auto-profile.json', ORIGINAL, CHANGES, RECEIPT, 'receipt.json'},
                'Unsupported UIKit sidecar')
        self.current()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        self.current()

    def close(self):
        for name in ('fd', 'parent_fd', 'source_fd'):
            fd = getattr(self, name, None)
            if fd is not None: os.close(fd); setattr(self, name, None)


def _small_json(root, name):
    try:
        raw = read_regular(root, name, maximum=256 * 1024)
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ArtifactError, OSError, ValueError, UnicodeError, RecursionError):
        raise ContractError('UIKit preparation metadata unavailable') from None


def _changes(profile, entries, sanitation_policy):
    from .ios_instrumentation import _configure_project, _sanitation_config
    project = profile.data['project'] + '/project.pbxproj'
    try:
        document = _configure_project(_project_metadata(entries[project]), profile)
    except (KeyError, TypeError, AttributeError):
        raise ContractError('Unsupported configured UIKit project structure') from None
    changes = {project: plistlib.dumps(document, sort_keys=True)}
    for name in RUNTIME:
        if name not in {'RLAutoConfig.swift', 'RLSanitationConfig.swift'}:
            changes[f'{SUPPORT}/Runtime/{name}'] = read_resource('reproof/ios_instrumentation_templates/' + name)
    encoded = base64.b64encode(json.dumps(profile.data, sort_keys=True, separators=(',', ':')).encode()).decode()
    changes[f'{SUPPORT}/Runtime/RLAutoConfig.swift'] = ('#if REPRO_OBSERVATIONS\nimport Foundation\nenum RLAutoConfig {\n'
        f'    static let profileJSON = String(data: Data(base64Encoded: "{encoded}")!, encoding: .utf8)!\n'
        f'    static let profileDigest = "{profile.digest}"\n}}\n#endif\n').encode()
    changes[f'{SUPPORT}/Runtime/RLSanitationConfig.swift'] = _sanitation_config(sanitation_policy)
    changes[f'{SUPPORT}/Profile.plist'] = plistlib.dumps(profile.data)
    info = _bounded_plist(entries[profile.data['build']['infoPlist']])
    require(not any(key.startswith('ReproAuto') or key.startswith('ReproSanitation')
                    or key == 'ReproAppLogSchemaVersion' for key in info),
            'UIKit app already declares an automatic runtime')
    info.update(ReproAutoProfile=profile.data, ReproAutoProfileDigest=profile.digest,
                ReproAppLogSchemaVersion=1, ReproRuntimeIdentitySchemaVersion=2 if sanitation_policy else 1,
                ReproBuildID='$(REPRO_BUILD_ID)')
    if sanitation_policy:
        info.update(ReproSanitationPolicy=sanitation_policy.data, ReproSanitationPolicyDigest=sanitation_policy.digest)
    changes[f'{SUPPORT}/Info.plist'] = plistlib.dumps(info)
    require(len(changes) <= 16 and sum(map(len, changes.values())) <= META_BYTES,
            'UIKit generated metadata exceeds limit')
    return changes


def prepare_snapshot_observation(source, output, profile, *, sanitation_policy=None, budgets=None, cancel=None):
    from .ios_instrumentation import _sanitation_policy
    profile = _profile(profile); sanitation_policy = _sanitation_policy(sanitation_policy)
    source, output = Path(source).absolute(), Path(output).absolute()
    require(not output.exists() and not output.is_symlink()
            and not output.is_relative_to(source) and not source.is_relative_to(output),
            'Use a new separate UIKit snapshot output')
    expected = _reference(profile.data['sourceSnapshot'])
    original = snapshots.inspect_snapshot(source, expected=expected, budgets=budgets, cancel=cancel)
    entries = _metadata(source, original, profile, expected, budgets, cancel=cancel)
    code = [item['path'] for item in original['files'] if item['path'].endswith(('.swift', '.m', '.mm', '.h'))]
    def scan(name, raw):
        require(not re.search(rb'\bRecorder\s*\.\s*shared\b|RLAutomaticRecorder|RLAutoConfig|\bREPRO_OBSERVATIONS\b', raw),
                'Existing recorder integration requires the manual recording path')
    if code: snapshots.scan_snapshot_files(source, code, scan, expected=expected, budgets=budgets, cancel=cancel)
    changes = _changes(profile, entries, sanitation_policy)
    publication = _Publication(source, output)
    try:
        publication.write('auto-profile.json', _json(profile.data))
        derived = snapshots.derive_snapshot(source, output / 'snapshot', changes, expected=expected, budgets=budgets, cancel=cancel,
            output_parent_identity=publication.root_identity)
        prepared = {key: derived[key] for key in ('digest', 'files', 'bytes')}
        changed = [{'path': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                   for name, raw in sorted(changes.items())]
        receipt = {'schemaVersion': 3, 'kind': 'ios-uikit-snapshot-preparation',
            'profileDigest': profile.digest, 'originalSnapshot': _bound(expected),
            'preparedSnapshot': _bound(prepared), 'changedFiles': sorted(changes),
            'changesDigest': digest(changed), 'productSourcesUnchanged': True, 'behaviorVerified': False}
        if sanitation_policy: receipt['sanitationPolicyDigest'] = sanitation_policy.digest
        publication.write(ORIGINAL, _json(original))
        publication.write(CHANGES, _json(changed))
        publication.write(RECEIPT, _json(receipt))
        return {'status': 'prepared', 'platform': 'ios', 'mode': 'observations', 'source': output / 'snapshot',
            'snapshot': output / 'snapshot', 'profile': output / 'auto-profile.json',
            'report': output / RECEIPT, 'changes': output / CHANGES,
            'productSourcesUnchanged': True, 'behaviorVerified': False}
    except BaseException as error:
        raise snapshots.SnapshotError('snapshot_cleanup_incomplete') from error
    finally:
        publication.close()


def validate_snapshot_preparation(source, profile, *, budgets=None, cancel=None):
    profile = _profile(profile); source = Path(source).absolute()
    receipt = _small_json(source.parent, RECEIPT)
    fields = {'schemaVersion', 'kind', 'profileDigest', 'originalSnapshot', 'preparedSnapshot',
              'changedFiles', 'changesDigest', 'productSourcesUnchanged', 'behaviorVerified'}
    require(type(receipt) is dict and fields <= set(receipt) <= fields | {'sanitationPolicyDigest'}
            and type(receipt['schemaVersion']) is int and receipt['schemaVersion'] == 3
            and receipt['kind'] == 'ios-uikit-snapshot-preparation'
            and receipt['profileDigest'] == profile.digest
            and receipt['originalSnapshot'] == profile.data['sourceSnapshot']
            and receipt['productSourcesUnchanged'] is True and receipt['behaviorVerified'] is False,
            'Invalid UIKit snapshot preparation receipt')
    require('sanitationPolicyDigest' not in receipt or (type(receipt['sanitationPolicyDigest']) is str
            and re.fullmatch('[0-9a-f]{64}', receipt['sanitationPolicyDigest'])),
            'Invalid UIKit sanitation policy binding')
    prepared = _reference(receipt['preparedSnapshot'])
    original_ref = _reference(receipt['originalSnapshot'])
    try:
        original = bounded_json(read_regular(source.parent, ORIGINAL, maximum=snapshots.MAX_MANIFEST_BYTES),
                                max_items=snapshots.MAX_FILES, max_depth=3)
    except (ArtifactError, OSError, ValueError, UnicodeError):
        raise ContractError('UIKit original snapshot metadata unavailable') from None
    require(snapshots.snapshot_manifest_reference(original, budgets=budgets) == original_ref,
            'UIKit original snapshot reference changed')
    _manifest_names(original, profile)
    current = snapshots.inspect_snapshot(source, expected=prepared, budgets=budgets, cancel=cancel)
    _manifest_names(current, profile, prepared=True)
    before = {item['path']: item for item in original['files']}
    after = {item['path']: item for item in current['files']}
    allowed = {profile.data['project'] + '/project.pbxproj', *GENERATED}
    require(set(after) == set(before) | set(GENERATED)
            and receipt['changedFiles'] == sorted(allowed)
            and all(after[name] == item for name, item in before.items() if name not in allowed),
            'Prepared UIKit product inputs changed')
    changed = _small_json(source.parent, CHANGES)
    require(changed == [after[name] for name in sorted(allowed)]
            and receipt['changesDigest'] == digest(changed), 'UIKit prepared changes differ')
    embedded = snapshots.read_snapshot_files(source, [f'{SUPPORT}/Profile.plist'], expected=prepared,
                                            budgets=budgets, cancel=cancel, max_files=1)
    require(_bounded_plist(embedded[f'{SUPPORT}/Profile.plist']) == profile.data,
            'UIKit embedded snapshot profile differs')
    return receipt


def build_snapshot_observation_app(source, output, *, profile=None, configuration=None,
                                   sdk='simulator', budgets=None, cancel=None):
    from .ios_build import xcode_environment
    from .ios_instrumentation import profile_from_app
    from .ios_sanitation import policy_from_app
    from .ios_storage import tree_manifest
    source, output = Path(source).absolute(), Path(output).absolute()
    require(sdk == 'simulator', 'Snapshot UIKit builds support unsigned Simulator only')
    require(not output.exists() and not output.is_symlink() and not output.is_relative_to(source)
            and not source.is_relative_to(output), 'Use a new separate UIKit snapshot build output')
    if profile is None: profile = _small_json(source.parent, 'auto-profile.json')
    profile = _profile(profile)
    prepared = (source.parent / RECEIPT).exists()
    preparation = validate_snapshot_preparation(source, profile, budgets=budgets, cancel=cancel) if prepared else None
    expected = _reference(preparation['preparedSnapshot'] if prepared else profile.data['sourceSnapshot'])
    manifest = snapshots.inspect_snapshot(source, expected=expected, budgets=budgets, cancel=cancel)
    entries = _metadata(source, manifest, profile, expected, budgets, prepared=prepared, cancel=cancel)
    build = profile.data['build']
    configuration = build['debugConfiguration'] if configuration is None else configuration
    require(type(configuration) is str and _NAME.fullmatch(configuration) and configuration == configuration.strip(),
            'Unsupported UIKit build selection')
    document = _project_metadata(entries[profile.data['project'] + '/project.pbxproj'])
    try:
        objects = document['objects']
        targets = [item for item in objects.values() if item.get('isa') == 'PBXNativeTarget'
                   and item.get('name') == profile.data['target']]
        require(len(targets) == 1 and targets[0].get('productType') == 'com.apple.product-type.application',
                'Select the configured UIKit application target')
        configurations = [objects[key]['name'] for key in objects[targets[0]['buildConfigurationList']]['buildConfigurations']]
        require(configuration in configurations, 'Selected UIKit configuration is unavailable')
    except (KeyError, TypeError, AttributeError):
        raise ContractError('Unsupported configured UIKit project structure') from None
    environment = xcode_environment()
    build_id = expected['digest'][:32]
    publication = _Publication(source, output)
    try:
        publication.write('auto-profile.json', _json(profile.data))
        workspace = output / 'source'
        actual = snapshots.materialize(source, workspace, budgets=budgets, cancel=cancel,
                                       output_parent_identity=publication.root_identity)
        require(all(actual[key] == expected[key] for key in expected), 'UIKit materialized snapshot differs')
        selection = ('-workspace', profile.data['workspace']) if 'workspace' in profile.data else ('-project', profile.data['project'])
        command = ['/usr/bin/xcodebuild', 'build', selection[0], str(workspace / selection[1]),
            '-scheme', build['scheme'], '-configuration', configuration, '-sdk', 'iphonesimulator',
            '-destination', 'generic/platform=iOS Simulator', '-derivedDataPath', str(output / 'DerivedData'),
            '-disableAutomaticPackageResolution', '-skipPackageUpdates', 'CODE_SIGNING_ALLOWED=NO',
            'CODE_SIGNING_REQUIRED=NO', 'COMPILER_INDEX_STORE_ENABLE=NO', 'REPRO_BUILD_ID=' + build_id]
        publication.current()
        if cancel is not None and cancel.is_set(): raise snapshots.SnapshotError('snapshot_cancelled')
        run_command(command, str(workspace), timeout=300, max_output=8 * 1024 * 1024, env_extra=environment)
        publication.current()
        if cancel is not None and cancel.is_set(): raise snapshots.SnapshotError('snapshot_cancelled')
        xcode_version = run_command(['/usr/bin/xcodebuild', '-version'], str(workspace),
                                    timeout=20, env_extra=environment).strip()
        publication.current()
        # Capture selected compiler workspace files anew, streaming under the same
        # explicit budget. Keep this evidence and executed workspace on any failure.
        checked = snapshots.capture(workspace, [item['path'] for item in manifest['files']],
                                    output / 'post-build-snapshot', budgets=budgets, cancel=cancel,
                                    output_parent_identity=publication.root_identity)
        require(all(checked[key] == expected[key] for key in expected), 'UIKit build changed protected inputs')
        snapshots.inspect_snapshot(source, expected=expected, budgets=budgets, cancel=cancel)
        if preparation is not None:
            require(validate_snapshot_preparation(source, profile, budgets=budgets, cancel=cancel) == preparation,
                    'UIKit preparation changed during build')
        products = output / 'DerivedData/Build/Products'
        app = products / f'{configuration}-iphonesimulator' / (build['product'] + '.app')
        info = _bounded_plist(read_regular(app, 'Info.plist', maximum=4 * 1024 * 1024))
        require(info.get('CFBundleIdentifier') == profile.data['applicationId'], 'UIKit product identity changed')
        for key in ('CFBundleShortVersionString', 'CFBundleVersion'):
            require(type(info.get(key)) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', info[key]),
                    'UIKit product version is missing or unsupported')
        automatic, sanitation = profile_from_app(app), policy_from_app(app)
        recording_build = prepared and configuration == build['debugConfiguration']
        if recording_build:
            require(automatic is not None and automatic.digest == profile.digest and info.get('ReproBuildID') == build_id,
                    'Configured UIKit app lost its automatic build identity')
            sanitation_digest = preparation.get('sanitationPolicyDigest')
            require((sanitation is None and sanitation_digest is None)
                    or (sanitation is not None and sanitation.digest == sanitation_digest),
                    'Configured UIKit app lost its sanitation policy')
        else:
            require(automatic is None and sanitation is None and 'ReproAppLogSchemaVersion' not in info,
                    'Non-recording UIKit build must exclude its observation profile')
        app_files = tree_manifest(app)
        receipt = {'schemaVersion': 3, 'kind': 'configured-uikit-snapshot-build', 'platform': 'ios',
            'applicationId': profile.data['applicationId'], 'executionEnvironment': 'simulator',
            'configuration': configuration, 'scheme': build['scheme'], 'sourceSnapshot': _bound(expected),
            'buildId': build_id, 'profileDigest': profile.digest, 'appRelative': app.relative_to(products).as_posix(),
            'artifactDigest': digest(app_files), 'artifactBytes': sum((app / name).stat().st_size for name in app_files),
            'bundleVersion': info['CFBundleShortVersionString'], 'bundleBuild': info['CFBundleVersion'],
            'productsDigest': digest(tree_manifest(products)), 'buildCompleted': True, 'signed': False,
            'automaticObservations': recording_build, 'behaviorVerified': False,
            'xcode': xcode_version}
        if preparation: receipt['preparationDigest'] = digest(preparation)
        if sanitation: receipt['sanitationPolicyDigest'] = sanitation.digest
        publication.write('receipt.json', _json(receipt))
        return {'output': output, 'products': products, 'app': app, 'receipt': receipt}
    except BaseException as error:
        raise snapshots.SnapshotError('snapshot_cleanup_incomplete') from error
    finally:
        publication.close()
