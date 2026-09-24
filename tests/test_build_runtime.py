from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from reproof.kotlin_instrumenter import _ensure_built, _java_home, _subprocess_environment


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "reproof" / "build_instrumentation_templates" / "runtime" / "io" / "reproof" / "autotrace" / "ReproHooks.kt"
FIXTURES = ROOT / "tests" / "fixtures" / "build-runtime"


class BuildRuntimeTests(unittest.TestCase):
    def test_jvm_behavior_preserves_callbacks_and_failure_paths(self):
        java_home = _java_home()
        _ensure_built(java_home)
        java = str(Path(java_home) / "bin" / "java")
        javac = str(Path(java_home) / "bin" / "javac")

        with tempfile.TemporaryDirectory(prefix="reproof-build-runtime-") as directory:
            classes = Path(directory) / "classes"
            classes.mkdir()
            java_sources = sorted(FIXTURES.rglob("*.java"))
            harness = FIXTURES / "RuntimeHarness.java"
            support_sources = [path for path in java_sources if path != harness]
            _run([javac, "-encoding", "UTF-8", "-source", "17", "-target", "17",
                   "-d", str(classes), *map(str, support_sources)], java_home)
            compiler_cp = _compiler_classpath()
            stdlib = next(path for path in compiler_cp.split(os.pathsep) if path.endswith("kotlin-stdlib-2.2.10.jar"))
            _run([java, "-cp", compiler_cp, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
                   "-jvm-target", "17", "-classpath", f"{classes}{os.pathsep}{stdlib}",
                   "-d", str(classes), str(RUNTIME)], java_home)
            _run([javac, "-encoding", "UTF-8", "-source", "17", "-target", "17",
                   "-cp", str(classes), "-d", str(classes), str(harness)], java_home)
            _run([java, "-cp", f"{classes}{os.pathsep}{stdlib}", "RuntimeHarness"], java_home)


def _compiler_classpath() -> str:
    classpath_file = ROOT / "tools" / "kotlin-instrumenter" / "build" / "classes" / "classpath"
    if not classpath_file.is_file():
        raise AssertionError("offline Kotlin compiler classpath was not built")
    classpath = classpath_file.read_text().strip()
    if not classpath:
        raise AssertionError("offline Kotlin compiler classpath is empty")
    return classpath


def _run(command: list[str], java_home: str) -> None:
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, check=False, timeout=120,
                            env=_subprocess_environment(java_home))
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no diagnostic"
        raise AssertionError(f"command failed ({result.returncode}): {Path(command[0]).name}: {detail}")


if __name__ == "__main__":
    unittest.main()
