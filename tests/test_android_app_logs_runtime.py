from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from reproloop.kotlin_instrumenter import _ensure_built, _java_home, _subprocess_environment


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "reproloop" / "instrumentation_templates" / "android" / "debug" / "java" / "io" / "reproloop" / "autotrace" / "ReproAppLogs.kt"
FIXTURES = ROOT / "tests" / "fixtures" / "android-app-logs"


class AndroidAppLogsRuntimeTests(unittest.TestCase):
    def test_lifecycle_screen_and_tap_journal_runtime(self):
        java_home = _java_home()
        _ensure_built(java_home)
        java = str(Path(java_home) / "bin" / "java")
        javac = str(Path(java_home) / "bin" / "javac")
        classpath = ROOT / "tools" / "kotlin-instrumenter" / "build" / "classes" / "classpath"
        self.assertTrue(classpath.is_file())
        compiler_cp = classpath.read_text().strip()
        stdlib = next(value for value in compiler_cp.split(os.pathsep) if value.endswith("kotlin-stdlib-2.2.10.jar"))

        with tempfile.TemporaryDirectory(prefix="reproloop-android-app-logs-") as directory:
            classes = Path(directory) / "classes"
            classes.mkdir()
            java_sources = sorted(FIXTURES.rglob("*.java"))
            harness = FIXTURES / "RuntimeHarness.java"
            support = [path for path in java_sources if path != harness]
            self._run([javac, "-encoding", "UTF-8", "-source", "17", "-target", "17",
                       "-d", str(classes), *map(str, support)], java_home)
            self._run([java, "-cp", compiler_cp, "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
                       "-jvm-target", "17", "-classpath", f"{classes}{os.pathsep}{stdlib}",
                       "-d", str(classes), str(RUNTIME)], java_home)
            self._run([javac, "-encoding", "UTF-8", "-source", "17", "-target", "17",
                       "-cp", str(classes), "-d", str(classes), str(harness)], java_home)
            self._run([java, "-cp", f"{classes}{os.pathsep}{stdlib}", "RuntimeHarness"], java_home)

    @staticmethod
    def _run(command: list[str], java_home: str) -> None:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, check=False, timeout=120,
                                env=_subprocess_environment(java_home))
        if result.returncode != 0:
            raise AssertionError(
                f"command failed ({result.returncode}): {Path(command[0]).name}: "
                f"{result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'no diagnostic'}"
            )


if __name__ == "__main__":
    unittest.main()
