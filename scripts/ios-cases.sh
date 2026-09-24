#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$#" -lt 2 ]; then
  echo 'Usage: bash scripts/ios-cases.sh <booted-simulator-uuid> <new-output-directory> [offline|claude]' >&2
  exit 2
fi
simulator="$1"
output="$2"
mode="${3:-offline}"
case "$mode" in offline|claude) ;; *) echo 'Mode must be offline or claude.' >&2; exit 2 ;; esac
if [ -e "$output" ]; then
  echo 'Choose a new output directory; existing evidence is retained.' >&2
  exit 2
fi
mkdir -p "$output"
python3 -m reproof ios-build --simulator "$simulator" --output "$output/build"
for case_name in counter duplicate-submit reset; do
  python3 -m reproof ios-record --case "$case_name" --simulator "$simulator" \
    --build "$output/build" --output "$output/$case_name/record"
  if [ "$mode" = claude ]; then
    python3 -m reproof ios-repair "$output/$case_name/record/bundle" --simulator "$simulator" \
      --agent claude --output "$output/$case_name/repair"
  else
    patch="scripts/ios-$case_name-fix.json"
    if [ "$case_name" = counter ]; then patch="scripts/ios-sample-fix.json"; fi
    python3 -m reproof ios-repair "$output/$case_name/record/bundle" --simulator "$simulator" \
      --patch-file "$patch" --output "$output/$case_name/repair"
  fi
done
