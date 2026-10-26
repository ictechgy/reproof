"""Closed, local workspace selection for configured observation builds.

This validates selected source bytes, not arbitrary Xcode build code or runtime
behavior. Package resolution and workspace scripts are outside this subset.
"""
from pathlib import PurePosixPath
import re
import xml.etree.ElementTree as ET

from .core import ContractError, require


def workspace_files(document):
    workspace = document.get('workspace')
    if workspace is None:
        return ()
    return (workspace + '/contents.xcworkspacedata',
            workspace + '/xcshareddata/xcschemes/' + document['build']['scheme'] + '.xcscheme')


def _xml(raw, root_tag):
    require(type(raw) is bytes and len(raw) <= 256 * 1024, 'Workspace XML size limit exceeded')
    # Decode first: byte scans alone miss UTF-16 encoded DTD/entity declarations.
    try:
        text = raw.decode('utf-8')
        require('<!DOCTYPE' not in text.upper() and '<!ENTITY' not in text.upper()
                and '\x00' not in text, 'Unsupported workspace XML declarations')
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError, ValueError):
        raise ContractError('Invalid workspace XML') from None
    require(root.tag == root_tag, 'Unsupported workspace XML root')
    pending = [(root, 0)]; count = 0
    while pending:
        item, depth = pending.pop(); count += 1
        require(count <= 2048 and depth <= 32, 'Workspace XML structure limit exceeded')
        pending.extend((child, depth + 1) for child in item)
    return root


def _relative(value):
    require(type(value) is str and re.fullmatch(r'[A-Za-z0-9_./ -]{1,512}', value)
            and not value.startswith('/') and all(part not in {'', '.', '..'}
            for part in value.split('/')), 'Unsupported workspace reference')
    return value


def _location(value, group, container, *, empty=False):
    require(type(value) is str and ':' in value, 'Unsupported workspace reference')
    kind, path = value.split(':', 1)
    require(kind in {'group', 'container'}, 'Only local workspace references are supported')
    base = group if kind == 'group' else container
    if path == '' and empty:
        return base
    return base / _relative(path)


def validate_workspace(document, entries, project_loader):
    """Check the complete workspace/project/scheme reference set before output."""
    if 'workspace' not in document:
        return
    workspace = PurePosixPath(document['workspace'])
    container = workspace.parent
    data_name, scheme_name = workspace_files(document)
    require(data_name in entries and scheme_name in entries, 'Missing declared workspace inputs')
    root = _xml(entries[data_name], 'Workspace')
    require(set(root.attrib) <= {'version'}, 'Unsupported workspace attributes')
    projects = []

    def visit(parent, group):
        for item in parent:
            require(item.tag in {'Group', 'FileRef'} and not (item.text or '').strip()
                    and not (item.tail or '').strip(), 'Unsupported workspace member')
            if item.tag == 'Group':
                require(set(item.attrib) <= {'location', 'name'} and 'location' in item.attrib,
                        'Unsupported workspace group')
                visit(item, _location(item.attrib['location'], group, container, empty=True))
            else:
                require(set(item.attrib) == {'location'} and len(item) == 0,
                        'Unsupported workspace file reference')
                path = _location(item.attrib['location'], group, container).as_posix()
                require(path.endswith('.xcodeproj') and path + '/project.pbxproj' in entries,
                        'Workspace project is not a declared input')
                projects.append(path)
    visit(root, container)
    require(1 <= len(projects) <= 64 and len({p.casefold() for p in projects}) == len(projects)
            and document['project'] in projects, 'Ambiguous workspace project membership')
    # No extra project can enter the build snapshot without workspace membership.
    declared = {name[:-len('/project.pbxproj')] for name in entries if name.endswith('/project.pbxproj')}
    require(declared == set(projects), 'Workspace project closure differs from declared inputs')
    parsed = {}
    for project in projects:
        value = project_loader(entries[project + '/project.pbxproj'])
        objects = value.get('objects')
        require(type(objects) is dict and objects and all(type(obj) is dict for obj in objects.values()),
                'Unsupported workspace project structure')
        require(not any(obj.get('isa') in {'XCRemoteSwiftPackageReference', 'XCLocalSwiftPackageReference'}
                        or obj.get('packageReferences') or obj.get('packageProductDependencies')
                        for obj in objects.values()), 'Workspace package references are not supported')
        # Cross-project references need exact root-relative ownership; grouped and
        # implicit proxy references are rejected rather than guessing their base.
        for obj in objects.values():
            if obj.get('isa') == 'PBXProject':
                require(not obj.get('projectReferences'), 'Implicit cross-project references are unsupported')
            if obj.get('isa') == 'PBXFileReference' and str(obj.get('path', '')).endswith('.xcodeproj'):
                require(obj.get('sourceTree') == 'SOURCE_ROOT', 'Ambiguous cross-project reference')
                reference = (PurePosixPath(project).parent / _relative(obj['path'])).as_posix()
                require(reference in projects, 'External cross-project reference')
        parsed[project] = objects
    require([name for name in entries if name.endswith('/xcshareddata/xcschemes/'
            + document['build']['scheme'] + '.xcscheme')] == [scheme_name],
            'Ambiguous shared workspace scheme')
    scheme = _xml(entries[scheme_name], 'Scheme')
    allowed_tags = {'Scheme', 'BuildAction', 'BuildActionEntries', 'BuildActionEntry',
        'BuildableReference', 'TestAction', 'Testables', 'TestableReference',
        'MacroExpansion', 'LaunchAction', 'BuildableProductRunnable', 'ProfileAction',
        'AnalyzeAction', 'ArchiveAction'}
    for item in scheme.iter():
        require(item.tag in allowed_tags and not (item.text or '').strip()
                and not (item.tail or '').strip(), 'Unsupported workspace scheme member')
        require(not any(key.lower().endswith('scripttext') or key in {
            'FilePath', 'reference', 'customWorkingDirectory'} for key in item.attrib)
            and item.get('useCustomWorkingDirectory', 'NO') == 'NO',
            'External workspace scheme paths are unsupported')
        require('ReferencedContainer' not in item.attrib or item.tag == 'BuildableReference',
                'Unsupported scheme reference')
    selected = []
    for reference in scheme.iter('BuildableReference'):
        attrs = reference.attrib
        require(attrs.get('ReferencedContainer', '').startswith('container:'), 'Invalid scheme project reference')
        project = (container / _relative(attrs['ReferencedContainer'][10:])).as_posix()
        require(project in parsed, 'Scheme project is outside workspace')
        key = attrs.get('BlueprintIdentifier')
        target = parsed[project].get(key)
        require(type(target) is dict and target.get('isa') == 'PBXNativeTarget'
                and target.get('name') == attrs.get('BlueprintName'), 'Scheme target identity mismatch')
        selected.append((project, key))
    require(selected, 'Workspace scheme has no explicit target')
    targets = [(key, item) for key, item in parsed[document['project']].items()
               if item.get('isa') == 'PBXNativeTarget' and item.get('name') == document['target']]
    require(len(targets) == 1 and targets[0][1].get('productType') == 'com.apple.product-type.application',
            'Ambiguous instrumented workspace target')
    wanted = (document['project'], targets[0][0])
    require(len(scheme.findall('BuildAction')) == 1
            and len(scheme.findall('./BuildAction/BuildActionEntries')) == 1,
            'Ambiguous scheme build action')
    build_entries = scheme.findall('./BuildAction/BuildActionEntries/BuildActionEntry')
    builds = []
    for item in build_entries:
        refs = item.findall('BuildableReference')
        require(len(refs) == 1 and item.get('buildForRunning') in {'YES', 'NO'}, 'Unsupported scheme build entry')
        ref = refs[0]
        project = (container / _relative(ref.attrib['ReferencedContainer'][10:])).as_posix()
        builds.append((project, ref.attrib['BlueprintIdentifier']))
        if builds[-1] == wanted:
            require(item.get('buildForRunning') == 'YES', 'Instrumented target is not selected for running')
    require(wanted in builds and len(builds) == len(set(builds)), 'Workspace scheme does not select the instrumented target')
