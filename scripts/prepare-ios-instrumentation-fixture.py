#!/usr/bin/env python3
"""Create a plain UIKit fixture with no SDK calls or Report interface."""
import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reproof.core import require
from reproof.ios_storage import copy_ios_source
from reproof.repair import run_command
from reproof.storage import write_json


def remove_function(text, signature):
    begin = text.index(signature)
    start = text.index('{', begin)
    level = 1
    end = start + 1
    # Only used with the fixed, repository-owned fixture methods below.
    while level:
        if text[end] == '{':level += 1
        elif text[end] == '}':level -= 1
        end += 1
    return text[:begin] + text[end:]


def prepare(output):
    output = Path(output).resolve()
    require(not output.exists(), 'Use a new UIKit fixture output')
    output.mkdir(parents=True, mode=0o700)
    source = output / 'source'
    copy_ios_source(ROOT / 'ios', source)
    delegate = source / 'Sample/AppDelegate.swift'
    text = delegate.read_text()
    for name in ['sceneDidBecomeActive', 'sceneDidEnterBackground', 'sceneDidDisconnect']:
        text = remove_function(text, '    func ' + name + '(')
    delegate.write_text(text)
    controller = source / 'Sample/CounterViewController.swift'
    text = remove_function(controller.read_text(), '    @objc private func reportCapture()')
    text = re.sub(r'(?m)^.*Recorder\.shared\.[^\n]*\n', '', text)
    text = re.sub(r'(?m)^.*(?:private let statusLabel|statusLabel\.|let report =|report\.heightAnchor)[^\n]*\n', '', text)
    text = text.replace('[next, report, buildIDLabel, statusLabel, listScrollView]', '[next, buildIDLabel, listScrollView]')
    require('Recorder' not in text and 'reportCapture' not in text and 'counter.report' not in text,
            'Plain UIKit fixture still contains manual recording code')
    controller.write_text(text)
    (source / 'Recorder/Recorder.swift').unlink()
    (source / 'Recorder').rmdir()
    spec = source / 'project.yml'
    text = spec.read_text()
    require(text.count('      - path: Recorder\n') == 1, 'Fixture project recorder dependency changed')
    spec.write_text(text.replace('      - path: Recorder\n', ''))
    run_command(['/opt/homebrew/bin/xcodegen', 'generate', '--spec', str(spec)], str(source), timeout=30)
    write_json(output / 'fixture.json', {'synthetic': True, 'platform': 'ios', 'ui': 'UIKit',
        'manualRecorderCalls': 0, 'reportView': False, 'cases': ['counter', 'duplicate-submit', 'reset']})
    return {'source': str(source), 'manualRecorderCalls': 0, 'reportView': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    print(prepare(parser.parse_args().output))
