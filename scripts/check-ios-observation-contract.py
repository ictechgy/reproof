#!/usr/bin/env python3
"""Run actual UIKit observation profile parser as a pure Foundation host program.

This does not launch an app or Simulator and cannot prove snapshot source contents.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'reproof/ios_instrumentation_templates/RLAutomaticRecorder.swift'


def vectors():
    base = {'schemaVersion': 3, 'kind': 'uikit-observation-v3',
            'applicationId': 'io.reproof.synthetic', 'project': 'Synthetic.xcodeproj',
            'target': 'Synthetic', 'build': {'scheme': 'Synthetic', 'product': 'Synthetic',
            'infoPlist': 'App/Info.plist', 'debugConfiguration': 'Debug'},
            'sourceSnapshot': {'schemaVersion': 1, 'kind': 'reproof-local-source-snapshot',
                               'digest': 'a' * 64, 'files': 32768, 'bytes': 268435456},
            'tapTargets': ['button.confirm'], 'screenTargets': {'screen.main': 'main'}}
    cases = []
    def add(document, accepted):
        cases.append({'profile': json.dumps(document, sort_keys=True, separators=(',', ':')),
                      'accepted': accepted, 'observationOnly': accepted})
    add(base, True)
    doc = copy.deepcopy(base); doc['sourceSnapshot'].update(files=1, bytes=0); add(doc, True)
    doc = copy.deepcopy(base); doc['workspace'] = 'Synthetic.xcworkspace'; add(doc, True)
    doc = copy.deepcopy(base); doc['inputExtensions'] = ['.metal', '.css']; add(doc, True)
    doc = copy.deepcopy(base); doc['project'] = '앱/Synthetic.xcodeproj'; add(doc, True)
    for key, values in {
        'schemaVersion': [True, 1.0, 0, 2], 'files': [True, 1.0, 0, 32769, '1', 18446744073709551617],
        'bytes': [True, 0.0, -1, 268435457, '0', float('nan'), float('inf'), float('-inf')],
        'digest': ['A' * 64, 'a' * 63, 'a' * 64 + '\n'],
        'kind': ['other'],
    }.items():
        for value in values:
            doc = copy.deepcopy(base); doc['sourceSnapshot'][key] = value; add(doc, False)
    for key in base['sourceSnapshot']:
        doc = copy.deepcopy(base); del doc['sourceSnapshot'][key]; add(doc, False)
    for value in [True, 3.0, 2, '3']:
        doc = copy.deepcopy(base); doc['schemaVersion'] = value; add(doc, False)
    for key, value in [('path', '/tmp/source'), ('command', 'build')]:
        doc = copy.deepcopy(base); doc['sourceSnapshot'][key] = value; add(doc, False)
    for key, value in [('kind', 'unknown'), ('sourceInputs', ['App/Info.plist']),
                       ('workspace', '../Synthetic.xcworkspace'), ('workspace', 'Synthetic.xcworkspace\n'),
                       ('project', 'Synthetic.xcodeproj\n'), ('target', 'Synthetic\n'),
                       ('applicationId', 'io.reproof.synthetic\n'), ('tapTargets', ['button\n']),
                       ('screenTargets', {'screen.main': 'main\n'}), ('inputExtensions', ['.sh'])]:
        doc = copy.deepcopy(base); doc[key] = value; add(doc, False)
    for key, value in [('scheme', '-scheme'), ('infoPlist', '../Info.plist'),
                       ('product', 'Synthetic\n'), ('debugConfiguration', 'Debug\n')]:
        doc = copy.deepcopy(base); doc['build'][key] = value; add(doc, False)
    v2 = copy.deepcopy(base); del v2['sourceSnapshot']
    v2.update(schemaVersion=2, kind='uikit-observation-v2',
              sourceInputs=['Synthetic.xcodeproj/project.pbxproj', 'App/Info.plist'])
    add(v2, True)
    # Preserve the existing v2 native parser's numeric behavior, not a new v3 rule.
    doc = copy.deepcopy(v2); doc['schemaVersion'] = 2.0; add(doc, True)
    doc = copy.deepcopy(v2); doc['sourceInputs'] += [f'App/File{i}.swift' for i in range(898)]; add(doc, True)
    doc = copy.deepcopy(doc); doc['sourceInputs'].append('App/TooMany.swift'); add(doc, False)
    doc = copy.deepcopy(v2); doc['sourceSnapshot'] = base['sourceSnapshot']; add(doc, False)
    doc = copy.deepcopy(v2); doc['sourceInputs'] = ['App/Info.plist']; add(doc, False)
    doc = copy.deepcopy(v2); doc['workspace'] = 'Synthetic.xcworkspace'; add(doc, False)
    doc['sourceInputs'] += ['Synthetic.xcworkspace/contents.xcworkspacedata',
                           'Synthetic.xcworkspace/xcshareddata/xcschemes/Synthetic.xcscheme']; add(doc, True)
    return cases


def swift_source():
    source = SOURCE.read_text()
    profile = source[source.index('    private struct Profile {'):source.index('    private struct Event {')]
    methods = source[source.index('    private func loadProfile()'):source.index('    private func scheduleStartupAttempt()')]
    constants = '\n'.join(line for line in source.splitlines() if any(
        line.startswith('    private static let ' + name + ' ') for name in (
            'supportedApplicationID', 'supportedProject', 'supportedTarget', 'supportedKind', 'allowedCases')))
    return '''import Foundation
import CoreFoundation
private enum RLAutoConfig { static var profileJSON = "" }
private final class Probe {
    private let caseID = ""
''' + constants + '\n' + profile + methods + '''
    func parse(_ json: String) -> (Bool, Bool) {
        RLAutoConfig.profileJSON = json
        let result = loadProfile()
        return (result != nil, result?.observationOnly == true)
    }
}
let cases = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [[String: Any]]
precondition(!cases.isEmpty)
for (index, item) in cases.enumerated() {
    let result = Probe().parse(item["profile"] as! String)
    guard result.0 == item["accepted"] as! Bool,
          result.1 == item["observationOnly"] as! Bool else {
        fatalError("observation_contract_vector_\\(index)")
    }
}
print("iOS observation contract: \\(cases.count) synthetic vectors passed")
'''


def main():
    with tempfile.TemporaryDirectory(prefix='reproof-ios-observation-contract-') as temporary:
        directory = Path(temporary)
        source = directory / 'main.swift'; binary = directory / 'contract'
        source.write_text(swift_source())
        cases = directory / 'vectors.json'; cases.write_text(json.dumps(vectors()))
        subprocess.run(['/usr/bin/xcrun', 'swiftc', '-swift-version', '5', '-warnings-as-errors',
                        str(source), '-o', str(binary)], check=True, timeout=120)
        subprocess.run([str(binary), str(cases)], check=True, timeout=30)


if __name__ == '__main__':
    main()
