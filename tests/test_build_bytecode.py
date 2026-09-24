from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "reproof" / "build_instrumentation_templates" / "buildSrc"
FIXTURE = ROOT / "tests" / "fixtures" / "bytecode"
GRADLE = shutil.which("gradle") or "/opt/homebrew/bin/gradle"
JAVA_HOME = Path(os.environ.get(
    "JAVA_HOME", "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home"
))
JAVA = JAVA_HOME / "bin" / "java"
JAVAC = JAVA_HOME / "bin" / "javac"
GRADLE_API = next(
    Path.home().glob(
        ".gradle/caches/modules-2/files-2.1/com.android.tools.build/gradle-api/8.13.2/*/gradle-api-8.13.2.jar"
    ),
    None,
)
ASM = next(
    Path.home().glob(
        ".gradle/caches/modules-2/files-2.1/org.ow2.asm/asm/9.8/*/asm-9.8.jar"
    ),
    None,
)
ASM_TREE = next(
    Path.home().glob(
        ".gradle/caches/modules-2/files-2.1/org.ow2.asm/asm-tree/9.8/*/asm-tree-9.8.jar"
    ),
    None,
)


class BuildBytecodeTests(unittest.TestCase):
    def test_template_buildsrc_compiles_against_cached_agp(self):
        self.assertIsNotNone(GRADLE, "Gradle is required for the buildSrc compile proof")
        self.assertIsNotNone(GRADLE_API, "cached AGP 8.13.2 is required")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            shutil.copytree(TEMPLATE, project / "buildSrc")
            plan = project / "buildSrc/src/main/java/io/reproof/instrumentation/gradle/ReproPlan.java"
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text(
                "package io.reproof.instrumentation.gradle;\n"
                "import java.util.Map;\n"
                "public final class ReproPlan {\n"
                '  public static final String MODULE = ":sample";\n'
                '  public static final String VARIANT = "debug";\n'
                '  public static final String ACTIVITY = "io.reproof.plain.MainActivity";\n'
                '  public static final String PROFILE_DIGEST = "' + "0" * 64 + '";\n'
                "  public static final Map<Integer, String> SITES = Map.ofEntries();\n"
                "}\n"
            )
            (project / "settings.gradle.kts").write_text('rootProject.name = "bytecode-fixture"\n')
            (project / "build.gradle.kts").write_text("\n")
            result = subprocess.run(
                [GRADLE, "--offline", "--no-daemon", "--stacktrace", "help"],
                cwd=project,
                env=_tool_environment(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_real_asm_transformer_executes_callback_and_rejects_bad_plans(self):
        self.assertTrue(JAVAC.is_file(), f"JDK 17 compiler is required: {JAVAC}")
        self.assertTrue(JAVA.is_file(), f"JDK 17 runtime is required: {JAVA}")
        self.assertIsNotNone(ASM, "cached ASM 9.8 is required")
        self.assertIsNotNone(ASM_TREE, "cached ASM tree 9.8 is required")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            classes = root / "classes"
            classes.mkdir()
            java_files = [
                TEMPLATE / "src/main/java/io/reproof/instrumentation/gradle/ReproBytecodeTransformer.java"
            ]
            java_files += [path for path in FIXTURE.rglob("*.java")]
            classpath = os.pathsep.join((str(ASM), str(ASM_TREE)))
            compile_result = subprocess.run(
                [str(JAVAC), "--release", "17", "-cp", classpath, "-d", str(classes)]
                + [str(path) for path in java_files],
                env=_tool_environment(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=120,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stdout)
            run_result = subprocess.run(
                [str(JAVA), "-cp", os.pathsep.join((str(classes), classpath)), "Harness", str(classes)],
                env=_tool_environment(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=120,
            )
            self.assertEqual(run_result.returncode, 0, run_result.stdout)


def _tool_environment():
    """Keep subprocess credentials and unrelated host settings out of test tools."""
    environment = {
        "JAVA_HOME": str(JAVA_HOME),
        "PATH": os.pathsep.join((str(JAVA_HOME / "bin"), "/opt/homebrew/bin", "/usr/bin", "/bin")),
    }
    for name in ("HOME", "GRADLE_USER_HOME", "TMPDIR", "ANDROID_HOME", "ANDROID_SDK_ROOT", "LANG"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


if __name__ == "__main__":
    unittest.main()
