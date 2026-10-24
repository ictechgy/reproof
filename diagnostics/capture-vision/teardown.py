#!/usr/bin/env python3
"""Best-effort exact-owned Simulator teardown; never upgrades capture cleanup."""
import argparse
import importlib.util
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--simulator', required=True)
    parser.add_argument('--owned-simulator-name', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--stage', choices=['preflight', 'runtime_preflight', 'simulator_create',
        'simulator_boot', 'helper_build', 'comparison', 'completed'], required=True)
    parser.add_argument('--exit-code', type=int, required=True)
    args = parser.parse_args(argv)
    # Import-time pin validation and no command if create did not return an ID.
    spec = importlib.util.spec_from_file_location('comparison_teardown', Path(__file__).with_name('compare.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    teardown = {'state': 'not_created', 'shutdownVerified': False, 'deleteConfirmed': False}
    if args.simulator:
        try:
            module.shutdown_simulator(args.simulator, args.owned_simulator_name)
            teardown['shutdownVerified'] = True
            module.command(['/usr/bin/xcrun', 'simctl', 'delete', args.simulator])
            # Exact UUID absence is checked after successful deletion.
            devices = json.loads(module.command(['/usr/bin/xcrun', 'simctl', 'list', 'devices', '--json']))
            module.require(not any(d.get('udid') == args.simulator for values in devices['devices'].values()
                                   for d in values), 'owned_simulator_delete_unconfirmed')
            teardown.update(state='deleted', deleteConfirmed=True)
        except Exception:
            teardown['state'] = 'unconfirmed'
    try:
        if args.report.exists():
            module.require(not args.report.is_symlink() and args.report.stat().st_size <= module.MAX_REPORT_BYTES,
                           'report_invalid')
            report = json.loads(args.report.read_text())
        else:
            report = module.new_report()
            report['outcome'] = 'setup_failed'
        report['setupStage'] = args.stage
        report['ownedSimulatorTeardown'] = teardown
        report['nativeAcceptance'] = False
        if teardown['state'] == 'unconfirmed':
            report.update(comparisonValid=False, diagnosticPassed=False)
        args.report.write_text(module.replay.encode_report(report))
        print(json.dumps({'outcome': report['outcome'], 'observerCalls': report['observerCalls'],
                          'nativeAcceptance': False, 'ownedSimulatorTeardown': teardown}, sort_keys=True))
    except Exception:
        print('{"outcome":"teardown_report_failed","nativeAcceptance":false}')
        return 1
    return 1 if teardown['state'] == 'unconfirmed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
