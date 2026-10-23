#!/usr/bin/env bash
# Create and destroy only this job's uniquely named simulator; never auto-select.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == true && "${RUNNER_ENVIRONMENT:-}" == github-hosted && "${GITHUB_REPOSITORY:-}" == ictechgy/reproof ]]
[[ "$(uname -s)" == Darwin && -n "${RUNNER_TEMP:-}" ]]
[[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ && "${GITHUB_RUN_ATTEMPT:-}" =~ ^[0-9]+$ ]]
export DEVELOPER_DIR=/Applications/Xcode_16.4.app/Contents/Developer
[[ -x "$DEVELOPER_DIR/usr/bin/xcodebuild" ]]
name="reproof-ci-ios-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
runtime=com.apple.CoreSimulator.SimRuntime.iOS-18-5
device=com.apple.CoreSimulator.SimDeviceType.iPhone-16
mkdir -p "$RUNNER_TEMP/reproof-runtime-evidence"
# Preinstalled SDK/runtime only: no download, license acceptance, or host settings.
xcrun simctl list runtimes -j > "$RUNNER_TEMP/reproof-runtime-evidence/ios-runtimes.json"
python - "$RUNNER_TEMP/reproof-runtime-evidence/ios-runtimes.json" "$runtime" <<'PY'
import json, sys
runtimes = json.load(open(sys.argv[1]))['runtimes']
assert any(r['identifier'] == sys.argv[2] and r.get('isAvailable') is True for r in runtimes), 'Required preinstalled iOS runtime unavailable'
PY
simulator=$(xcrun simctl create "$name" "$device" "$runtime")
[[ "$simulator" =~ ^[0-9A-Fa-f-]{36}$ ]]
cleanup() {
  result=$?
  trap - EXIT INT TERM
  xcrun simctl shutdown "$simulator" >/dev/null 2>&1 || true
  xcrun simctl delete "$simulator" || result=1
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
xcrun simctl boot "$simulator"
xcrun simctl bootstatus "$simulator" -b
xcodebuild build-for-testing -project live-ios/ReproLive.xcodeproj -scheme ReproLive \
  -configuration Debug -sdk iphonesimulator -destination "id=$simulator" \
  -derivedDataPath "$RUNNER_TEMP/reproof-ios-helper" \
  -disableAutomaticPackageResolution -skipPackageUpdates \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO COMPILER_INDEX_STORE_ENABLE=NO \
  > "$RUNNER_TEMP/reproof-runtime-evidence/ios-helper-build.log" 2>&1
# Pure startup-budget regressions only: no control session, bridge, app input or authority.
xcodebuild test-without-building -project live-ios/ReproLive.xcodeproj -scheme ReproLive \
  -configuration Debug -sdk iphonesimulator -destination "id=$simulator" \
  -derivedDataPath "$RUNNER_TEMP/reproof-ios-helper" -parallel-testing-enabled NO \
  -only-testing:ReproLiveTests/LiveControlTests/testSimulatorStartupBudgetRejectsInvalidDeadlines \
  -only-testing:ReproLiveTests/LiveControlTests/testSimulatorStartupBudgetUsesOneShrinkingDeadline \
  -only-testing:ReproLiveTests/LiveControlTests/testSimulatorStartupBudgetBoundsTransportWaits \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO \
  > "$RUNNER_TEMP/reproof-runtime-evidence/ios-startup-budget-tests.log" 2>&1
# Xcode can exit successfully when a selector matches no tests. Require all three results.
python - "$RUNNER_TEMP/reproof-runtime-evidence/ios-startup-budget-tests.log" <<'PYTEST'
import re, sys
from pathlib import Path
log = Path(sys.argv[1]).read_text(errors="replace")
expected = {"testSimulatorStartupBudgetRejectsInvalidDeadlines",
            "testSimulatorStartupBudgetUsesOneShrinkingDeadline",
            "testSimulatorStartupBudgetBoundsTransportWaits"}
passed = set(re.findall(r"Test Case '-\[ReproLiveTests\.LiveControlTests ([A-Za-z0-9_]+)\]' passed", log))
assert passed == expected, "Native startup-budget test evidence is missing or unexpected"
assert re.search(r"Executed 3 tests, with 0 failures", log), "Native startup-budget summary is incomplete"
PYTEST
python scripts/ios-native-fixture-qa.py --simulator "$simulator" --owned-simulator-name "$name" \
  --helper-products "$RUNNER_TEMP/reproof-ios-helper/Build/Products" \
  --output "$RUNNER_TEMP/reproof-runtime-evidence/ios"
