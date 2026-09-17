#!/bin/sh
set -eu

tool_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
out="$tool_root/build/classes"
mkdir -p "$out"

if [ -z "${JAVA_HOME:-}" ]; then
    echo "JAVA_HOME must point to JDK 17" >&2
    exit 2
fi

compiler=$(find "${HOME}/.gradle/caches/modules-2/files-2.1/org.jetbrains.kotlin/kotlin-compiler-embeddable/2.2.10" -name 'kotlin-compiler-embeddable-2.2.10.jar' -print -quit)
stdlib=$(find "${HOME}/.gradle/caches/modules-2/files-2.1/org.jetbrains.kotlin/kotlin-stdlib/2.2.10" -name 'kotlin-stdlib-2.2.10.jar' -print -quit)
script_runtime=$(find "${HOME}/.gradle/caches/modules-2/files-2.1/org.jetbrains.kotlin/kotlin-script-runtime/2.2.10" -name 'kotlin-script-runtime-2.2.10.jar' -print -quit)
trove=$(find "${HOME}/.gradle/caches/modules-2/files-2.1/org.jetbrains.intellij.deps/trove4j/1.0.20200330" -name 'trove4j-1.0.20200330.jar' -print -quit)
annotations=$(find "${HOME}/.gradle/caches/modules-2/files-2.1/org.jetbrains/annotations/23.0.0" -name 'annotations-23.0.0.jar' -print -quit)
coroutines=$(find "${HOME}/.gradle/caches/modules-2/files-2.1/org.jetbrains.kotlinx/kotlinx-coroutines-core-jvm/1.8.0" -name 'kotlinx-coroutines-core-jvm-1.8.0.jar' -print -quit)
reflect=$(find "${HOME}/.gradle/caches/modules-2/files-2.1/org.jetbrains.kotlin/kotlin-reflect/1.6.10" -name 'kotlin-reflect-1.6.10.jar' -print -quit)

for required in "$compiler" "$stdlib" "$script_runtime" "$trove" "$annotations" "$coroutines" "$reflect"; do
    if [ ! -f "$required" ]; then
        echo "Kotlin PSI compiler cache is incomplete" >&2
        exit 2
    fi
done

classpath="$compiler:$stdlib:$script_runtime:$trove:$annotations:$coroutines:$reflect"
find "$tool_root/src/main/java" -name '*.java' -print > "$out/sources.list"
"${JAVA_HOME}/bin/javac" -encoding UTF-8 -source 17 -target 17 -cp "$classpath" -d "$out" @"$out/sources.list"
printf '%s\n' "$classpath" > "$out/classpath"
