#!/usr/bin/env bash
# Own only the disposable AVD created by this job. Never select attached devices.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == true && "${RUNNER_ENVIRONMENT:-}" == github-hosted && "${GITHUB_REPOSITORY:-}" == ictechgy/reproof ]]
[[ "$(uname -s)" == Linux && -n "${RUNNER_TEMP:-}" ]]
[[ "${GITHUB_RUN_ID:-}" =~ ^[0-9]+$ && "${GITHUB_RUN_ATTEMPT:-}" =~ ^[0-9]+$ ]]
export HOME="$RUNNER_TEMP/reproof-build-home"
export JAVA_HOME="$JAVA_HOME_17_X64"
export PATH="$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/emulator:$PATH"
export ANDROID_USER_HOME="$RUNNER_TEMP/reproof-android-home"
export ANDROID_AVD_HOME="$ANDROID_USER_HOME/avd"
export GRADLE_USER_HOME="$RUNNER_TEMP/reproof-gradle"
export ANDROID_SERIAL=emulator-5554
export REPROOF_OWNED_ANDROID_EMULATOR=1
mkdir -p "$HOME" "$ANDROID_AVD_HOME" "$RUNNER_TEMP/reproof-runtime-evidence"
name="reproof-ci-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
image='system-images;android-35;default;x86_64'
sdkmanager="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager"
avdmanager="$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager"
[[ -x "$sdkmanager" && -x "$avdmanager" ]]
# No automatic license acceptance. New license prompts fail with stdin closed.
"$sdkmanager" --install emulator "$image" </dev/null
[[ -x "$ANDROID_HOME/emulator/emulator" ]]
# A fresh name in a job-private AVD directory; no overwrite/reset of any AVD.
printf 'no\n' | "$avdmanager" create avd --name "$name" --package "$image" --device pixel_2
pid=''
cleanup() {
  result=$?
  trap - EXIT INT TERM
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
    for _ in {1..30}; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then kill -KILL "$pid" 2>/dev/null || true; fi
    wait "$pid" 2>/dev/null || true
  fi
  "$avdmanager" delete avd --name "$name" >/dev/null 2>&1 || result=1
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Use existing KVM access when available; do not change device/security permissions.
acceleration=off
if [[ -r /dev/kvm && -w /dev/kvm ]]; then acceleration=on; fi
"$ANDROID_HOME/emulator/emulator" -avd "$name" -port 5554 -no-window -no-audio -no-snapshot -no-metrics \
  -no-boot-anim -gpu swiftshader_indirect -accel "$acceleration" \
  > "$RUNNER_TEMP/reproof-runtime-evidence/emulator.log" 2>&1 &
pid=$!
booted=false
for _ in {1..240}; do
  kill -0 "$pid"
  if [[ "$(adb -s "$ANDROID_SERIAL" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" == 1 ]]; then booted=true; break; fi
  sleep 2
done
[[ "$booted" == true ]]
python scripts/android-runtime-fixture-qa.py --serial "$ANDROID_SERIAL" --owned-emulator-name "$name" \
  --helper-apk android/live/build/outputs/apk/debug/live-debug.apk \
  --app-apk android/runtime-fixture/build/outputs/apk/debug/runtime-fixture-debug.apk \
  --output "$RUNNER_TEMP/reproof-runtime-evidence/android"
