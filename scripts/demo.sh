#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
output="${1:-artifacts/demo}"
if [ -e "$output" ]; then
  echo 'Choose a new output directory; existing evidence is never overwritten.' >&2
  exit 2
fi
mkdir -p "$output"
python3 -m reproof doctor
python3 -m reproof build --receipt "$output/build.json"
python3 -m reproof record \
  --apk android/sample/build/outputs/apk/buggy/debug/sample-buggy-debug.apk \
  --driver-apk android/driver/build/outputs/apk/debug/driver-debug.apk \
  --receipt "$output/build.json" --scripted --output "$output/bundle"
python3 -m reproof repair "$output/bundle" \
  --patch-file scripts/sample-fix.json --output "$output/repair"
