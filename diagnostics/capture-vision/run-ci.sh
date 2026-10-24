#!/usr/bin/env bash
# One approved opened-PR run. All native output stays private; only JSON is uploaded.
set -euo pipefail
export DEVELOPER_DIR=/Applications/Xcode_16.4.app/Contents/Developer
: "${RUNNER_TEMP:?}"
[[ "${GITHUB_ACTIONS:-}" == true && "${RUNNER_ENVIRONMENT:-}" == github-hosted ]]
[[ "${GITHUB_REPOSITORY:-}" == ictechgy/reproof && "${RUNNER_OS:-}" == macOS ]]
[[ "${GITHUB_EVENT_NAME:-}" == pull_request && "${GITHUB_RUN_ATTEMPT:-}" == 1 ]]
[[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ ]]
report="$RUNNER_TEMP/capture-vision-report.json"
private="$RUNNER_TEMP/capture-vision-private-setup-${GITHUB_RUN_ID}-1"
[[ ! -e "$report" && ! -e "$private" ]]
mkdir -m 700 "$private"
simulator=''
name="reproof-ci-ios-${GITHUB_RUN_ID}-1"
stage=preflight
finish() {
  result=$?
  trap - EXIT INT TERM
  # The simulator variable contains only this job's exact create result.
  # Teardown is separate from the diagnostic cleanup verdict and cannot repair it.
  python3 diagnostics/capture-vision/teardown.py --simulator "$simulator" --owned-simulator-name "$name" \
    --report "$report" --stage "$stage" --exit-code "$result" || result=1
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Validate all original bytes before any Apple tool; verify public opened event too.
python3 - "$private" > "$private/preflight.log" 2>&1 <<'PY'
import importlib.util, json, os, sys
from pathlib import Path
path = Path('diagnostics/capture-vision/compare.py').resolve()
spec = importlib.util.spec_from_file_location('comparison_preflight', path)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.replay.validate_inputs(module.OFFLINE)
event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
assert event['action'] == 'opened'
assert event['repository']['private'] is False
assert event['repository']['full_name'] == 'ictechgy/reproof'
assert event['pull_request']['head']['repo']['full_name'] == 'ictechgy/reproof'
assert event['pull_request']['base']['ref'] == 'codex/owned-native-runtime-acceptance'
module.replay.verify_host(Path(sys.argv[1]), os.environ.copy())
PY
stage=runtime_preflight
/usr/bin/xcrun simctl list runtimes --json > "$private/runtimes.json" 2> "$private/runtime-stderr.log"
python3 - "$private/runtimes.json" > "$private/runtime-check.log" 2>&1 <<'PY'
import json, sys
runtimes = json.load(open(sys.argv[1]))['runtimes']
assert sum(r['identifier'] == 'com.apple.CoreSimulator.SimRuntime.iOS-18-5' and r.get('isAvailable') is True for r in runtimes) == 1
PY
stage=simulator_create
simulator=$(/usr/bin/xcrun simctl create "$name" com.apple.CoreSimulator.SimDeviceType.iPhone-16 \
  com.apple.CoreSimulator.SimRuntime.iOS-18-5 2> "$private/create-stderr.log")
[[ "$simulator" =~ ^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$ ]]
stage=simulator_boot
/usr/bin/xcrun simctl boot "$simulator" > "$private/boot.log" 2>&1
/usr/bin/xcrun simctl bootstatus "$simulator" -b > "$private/bootstatus.log" 2>&1
stage=helper_build
/usr/bin/xcodebuild build-for-testing -project live-ios/ReproLive.xcodeproj -scheme ReproLive \
  -configuration Debug -sdk iphonesimulator -destination "id=$simulator" \
  -derivedDataPath "$private/helper" -disableAutomaticPackageResolution -skipPackageUpdates \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO COMPILER_INDEX_STORE_ENABLE=NO \
  > "$private/helper-build.log" 2>&1
stage=comparison
python3 diagnostics/capture-vision/compare.py --simulator "$simulator" --owned-simulator-name "$name" \
  --helper-products "$private/helper/Build/Products" --output "$report"
stage=completed
