#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$#" -lt 2 ]; then
  echo 'Usage: bash scripts/ios-demo.sh <booted-simulator-uuid> <new-output-directory>' >&2
  exit 2
fi
simulator="$1"
output="$2"
if [ -e "$output" ]; then
  echo 'Choose a new output directory; existing evidence is retained.' >&2
  exit 2
fi
mkdir -p "$output"
python3 -m reproloop ios-doctor --simulator "$simulator"
python3 -m reproloop ios-build --simulator "$simulator" --output "$output/build"
python3 -m reproloop ios-record --simulator "$simulator" --build "$output/build" --output "$output/record"
python3 -m reproloop ios-repair "$output/record/bundle" --simulator "$simulator" \
  --patch-file scripts/ios-sample-fix.json --output "$output/repair"
