# iOS 간편 모드 계획 1 — 기반(설정·빌드·전용 Simulator·화면 요소 찾기)과 spike

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 실무 규모 iOS 앱(SwiftUI+UIKit 혼용, ID 거의 없음, SPM·XcodeGen·비공개 SPM)을
`reproof app init` → `reproof app build` → `reproof app inspect`로 설정·빌드하고, 전용
Simulator에서 화면 요소를 여러 근거로 찾을 수 있게 한다. 계획 2에 필요한 결정을 spike 3개로
측정해 확정한다.

**Architecture:** 엄격 모드 코드와 분리된 새 패키지 `reproof/simple/`에 짓는다. 기존
`reproof.repair.run_command`(프로세스 그룹 정리·시간 제한)를 감싸 재사용한다. Simulator 조작은
`reproof-` 접두사 기기에만 허용한다. 화면 조작·계층 내보내기는 새 Swift UI 테스트 실행기
`ios/SimpleRunner`가 대상 앱을 bundle id로 구동한다. 이 실행기는 회사 프로젝트를 수정하지 않는다.
요소 찾기와 판단은 모두 Python(`hierarchy.py`, `locator.py`)에서 해서 Xcode 없이 테스트한다.

**Tech Stack:** Python 3.11+ 표준 라이브러리(`tomllib`, `dataclasses`, `subprocess`,
`http.server`), unittest, Xcode 27 `xcodebuild`/`xcrun simctl`, XcodeGen, Swift/XCUITest,
Network.framework.

**Spec:** `docs/superpowers/specs/2026-11-01-ios-commercial-readiness-design.md`

## Global Constraints

- Python `>=3.11`, 런타임 의존성 없음(`pyproject.toml`의 `dependencies = []` 유지).
- 새 Python 코드는 `reproof/simple/`, 새 Swift 실행기는 `ios/SimpleRunner/`, 검증용 앱은
  `examples/simple-ios/`에 둔다. 엄격 모드 모듈(`ios_*.py`, `live/*`, `repair_*`)의 동작은
  바꾸지 않는다. 유일한 기존 파일 수정은 `reproof/cli.py`의 진입점 한 곳,
  `distribution-resources.json`, `tests/release_inventory.json`,
  `.github/workflows/portable-tests.yml`이다.
- 테스트는 `unittest`, 파일명은 `tests/test_simple_*.py`.
  - Xcode가 필요 없는 테스트는 새 goal `IOS_SIMPLE_CONTRACTS`(effects `filesystem,process`,
    gate `software`)에 등록한다.
  - Xcode·Simulator가 필요한 테스트는 `IOS_SIMPLE_NATIVE`(gate `environment`)에 등록한다.
  - 등록 형식은 `tests/release_inventory.json`의 기존 `IOS_SNAPSHOT_CONTRACTS` 항목과 같다.
- 사용자에게 보이는 문구와 오류는 한국어로 쓰고, 항상 **원인과 해결 방법**을 함께 준다
  (`SimpleError.user_message()`).
- 새 함수·클래스·타입에는 한국어 docstring을 단다(역할·파라미터·반환·주의, "왜"). 함수 본문은
  10줄 이내를 목표로 한다(사용자 전역 규칙).
- 이름이 `reproof-`로 시작하지 않는 Simulator는 조회만 하고 만들기·지우기·초기화·설치는 하지
  않는다.
- Release 구성은 빌드하지 않고, Release에 아무것도 넣지 않는다.
- 비공개 SPM은 개발자 Mac의 기존 git 인증을 그대로 쓴다. 툴은 인증 값을 읽거나 저장하지
  않는다. 로그·오류의 URL에서는 userinfo를 지운다(`redact_url`).
- 승인 범위: iOS Simulator 런타임 설치, Wikipedia·공개 SPM 다운로드는 승인됐다. 물리 iPhone,
  Tuist 설치, SwiftSyntax 등 도구용 추가 패키지 다운로드는 승인되지 않았다. 계획 1에서는 Claude를
  호출하지 않는다.
- 커밋 전에 `python3 -m compileall -q reproof/simple tests`와 `git diff --check`를 통과시킨다.
  커밋은 Conventional Commits 형식에 본문 한국어로 쓰고, 작업 브랜치
  `feature/ios-commercial-readiness`에만 커밋한다. push하지 않는다.
- 작업 디렉터리는 `/Users/jinhongan/Desktop/qa/reproof-ios-commercial`. 기존 checkout
  `/Users/jinhongan/Desktop/qa/reproof`는 건드리지 않는다.

## Review Focus

1. **공백·한글이 들어간 앱 경로**(예: `~/Desktop/회사 앱/My App`)에서도 탐색·복사·빌드
   argv가 깨지지 않아야 한다. Task 3과 Task 5에 테스트가 있다.
2. **앱 확장·위젯 타깃이 함께 있는 scheme**에서 `.appex`가 아니라 `.app` 타깃을 골라야 한다.
   Task 5에 테스트가 있다.
3. **중간에 끊긴 빌드**(Ctrl-C, 시간 초과)가 남긴 디렉터리를 다음 실행에서 완료된 빌드로
   재사용하면 안 된다. Task 7에 테스트가 있다.
4. **`Package.resolved`가 없는 프로젝트**는 버전 고정 없이 해석하되, 버전이 실행마다 달라질 수
   있다고 경고해야 한다. Task 6에 테스트가 있다.
5. **git 저장소가 아니거나 커밋이 없는 소스**도 빌드는 되어야 하고, 빌드 식별에는 "소스 버전
   미확인"이 표시돼야 한다. Task 4에 테스트가 있다.

---

## File Structure

| 파일 | 책임 |
| --- | --- |
| `reproof/simple/__init__.py` | 패키지 설명만 |
| `reproof/simple/errors.py` | `SimpleError`, `ToolFailure`: 사용자용 원인·해결 메시지 |
| `reproof/simple/process.py` | `ToolRunner` 프로토콜, `HostToolRunner`(`run_command` 래퍼) |
| `reproof/simple/config.py` | `reproof.toml` 읽기·쓰기·검증·digest |
| `reproof/simple/workcopy.py` | 앱 소스 전체의 작업 사본과 manifest digest |
| `reproof/simple/identity.py` | commit SHA·작업 중 변경·digest로 빌드 식별 |
| `reproof/simple/discovery.py` | 프로젝트·scheme·앱 타깃·bundle id 탐색 |
| `reproof/simple/generation.py` | XcodeGen/Tuist 생성 실행 |
| `reproof/simple/packages.py` | SPM 해석·캐시, 인증 실패 분류, URL 가림 |
| `reproof/simple/build.py` | Simulator용 Debug 빌드와 `.app` 찾기 |
| `reproof/simple/pipeline.py` | 식별→사본→생성→SPM→빌드 조합, 빌드 디렉터리 관리 |
| `reproof/simple/simulator.py` | `reproof-` 전용 Simulator 만들기·초기화·설치·표시 고정 |
| `reproof/simple/hierarchy.py` | 실행기 JSON → `Element` 트리, 좌표의 요소 찾기 |
| `reproof/simple/locator.py` | 여러 근거 locator 저장(`LocatorEvidence`)과 해석(`resolve`) |
| `reproof/simple/runner_client.py` | SimpleRunner 빌드·실행·HTTP 명령 |
| `reproof/simple/doctor.py` | 환경 점검 항목과 해결 안내 |
| `reproof/simple/cli.py` | `reproof app init/doctor/build/inspect` |
| `ios/SimpleRunner/**` | XCUITest 기반 범용 조작·계층 내보내기 실행기 |
| `examples/simple-ios/MixedShop/**` | XcodeGen·SwiftUI+UIKit·로컬 패키지 검증 앱 |
| `examples/simple-ios/wikipedia/pin.json` | Wikipedia 고정 commit |
| `scripts/simple-fetch-app.py` | pin 파일대로 공개 앱 소스 받기 |
| `tests/simple_support.py` | `FakeToolRunner`, 합성 프로젝트 생성 도우미 |
| `docs/SIMPLE-MODE-IOS.md` | 사용자 안내(한국어) |
| `docs/superpowers/specs/2026-11-01-ios-commercial-readiness-spike-results.md` | spike 측정·결정 |
| `spikes/**` | 버림용 spike 코드. 패키지·리소스에 넣지 않는다 |

---

### Task 0: 실행 환경 준비 (코드 없음)

- [ ] **Step 1: iOS Simulator 런타임 설치 (승인됨)**

Run: `xcodebuild -downloadPlatform iOS` (오래 걸리므로 백그라운드로 실행)
Expected: 끝난 뒤 `xcrun simctl list runtimes`에 `iOS 27.x ... (available)`이 나온다.

- [ ] **Step 2: 도구 확인**

Run: `xcodebuild -version && xcodegen --version && git --version`
Expected: Xcode 27.0, XcodeGen 버전, git 버전이 출력된다. Tuist는 설치하지 않는다.

---

### Task 1: 패키지 골격·오류 형식·명령 실행기·CLI 진입점

**Files:**
- Create: `reproof/simple/__init__.py`, `reproof/simple/errors.py`, `reproof/simple/process.py`,
  `reproof/simple/cli.py`, `tests/simple_support.py`, `tests/test_simple_process.py`
- Modify: `reproof/cli.py:40-90` (진입점 분기 추가), `tests/release_inventory.json`
  (`IOS_SIMPLE_CONTRACTS` goal 추가), `.github/workflows/portable-tests.yml`(g0 job에 단계 추가)

**Interfaces:**
- Produces:
  - `class SimpleError(Exception)`: `__init__(self, code: str, cause: str, remedy: str, *, log_path: Path | None = None)`, `user_message(self) -> str`
  - `class ToolFailure(SimpleError)`: `__init__(self, command_name: str, *, log_path: Path | None, output_tail: str)`. code는 `"tool_failed"`.
  - `class ToolRunner(Protocol)`: `run(self, argv: list[str], *, cwd: Path, timeout: int, log_path: Path | None = None, env_extra: dict[str, str] | None = None) -> str`
  - `class HostToolRunner`: 위 프로토콜 구현
  - `reproof.simple.cli.main(argv: Sequence[str]) -> int`. 반환 0 성공, 2 `SimpleError`, 130 `KeyboardInterrupt`.
  - `tests/simple_support.FakeToolRunner(responses: list[tuple[tuple[str, ...], str | Exception]])`. `.calls: list[dict]`이며 각 항목은 `{"argv", "cwd", "env_extra"}`. argv 앞부분이 일치하는 첫 응답을 돌려주고, 일치하는 응답이 없으면 `AssertionError`.

- [ ] **Step 1: 실패하는 테스트 작성** (`tests/test_simple_process.py`)

```python
def test_user_message_contains_cause_remedy_and_log(self):
    error = SimpleError("x", "빌드가 실패했습니다.", "로그를 확인하세요.", log_path=Path("/tmp/a.log"))
    self.assertEqual(error.user_message(), "문제: 빌드가 실패했습니다.\n해결: 로그를 확인하세요.\n로그: /tmp/a.log")

def test_host_runner_wraps_command_error_as_tool_failure(self):
    with self.assertRaises(ToolFailure) as caught:
        HostToolRunner().run(["/bin/sh", "-c", "echo boom >&2; exit 3"], cwd=Path(tmp), timeout=10, log_path=Path(tmp) / "x.log")
    self.assertEqual(caught.exception.code, "tool_failed")
    self.assertIn("boom", caught.exception.output_tail)

def test_host_runner_passes_git_terminal_prompt_zero(self):
    output = HostToolRunner().run(["/bin/sh", "-c", "echo $GIT_TERMINAL_PROMPT"], cwd=Path(tmp), timeout=10)
    self.assertEqual(output.strip(), "0")

def test_app_cli_help_exits_zero(self):
    with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as code:
        reproof.cli.main(["app", "--help"])
    self.assertEqual(code.exception.code, 0)
    self.assertIn("init", out.getvalue())
```

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_simple_process -v`
Expected: FAIL (`ModuleNotFoundError: reproof.simple`)

- [ ] **Step 3: 구현**
  - `HostToolRunner.run`은 `reproof.repair.run_command(argv, cwd, timeout=..., max_output=64*1024*1024, env_extra=..., log_path=...)`를 호출한다.
  - `env_extra`에는 기본으로 `DEVELOPER_DIR`(`xcode-select -p` 결과를 한 번 캐시), `GIT_TERMINAL_PROMPT=0`, 그리고 `SSH_AUTH_SOCK`(있을 때만)를 넣고, 호출자가 준 값으로 덮어쓴다.
  - `CommandError`가 나면 `ToolFailure(Path(argv[0]).name, log_path=..., output_tail=로그 끝 4096바이트)`로 바꾼다. 로그가 없을 때 `output_tail`은 빈 문자열이다.
  - `reproof/cli.py`의 `main`에는 `if effective and effective[0] == 'app':` 분기를 `source-snapshot` 분기 바로 앞에 추가한다.
  - `simple/cli.py`는 argparse 하위 명령 `init`, `doctor`, `build`, `inspect`를 등록만 하고, 실제 동작은 Task 12에서 붙인다. 지금은 각 명령이 `SimpleError("not_ready", ...)`를 낸다.

- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.test_simple_process -v`
Expected: PASS 4개

- [ ] **Step 5: goal 등록과 CI 단계 추가**
  - `tests/release_inventory.json`에 `IOS_SIMPLE_CONTRACTS` goal을 만들고 check `simple-process`(command `["python3","-m","unittest","tests.test_simple_process"]`, effects `["filesystem","process"]`, timeoutSeconds 120, evidence `unittest-summary`, gate `software`)를 넣는다.
  - `portable-tests.yml`의 g0 job에 `python scripts/release-check.py --goal IOS_SIMPLE_CONTRACTS --effects filesystem,process,loopback` 단계를 추가한다. Task 11의 클라이언트 테스트가 loopback을 쓰기 때문이다.
  - 이후 Task들이 만드는 portable 테스트 모듈도 각자 이 goal에 check로 추가한다.

Run: `python3 scripts/release-check.py --goal IOS_SIMPLE_CONTRACTS --effects filesystem,process,loopback`
Expected: `simple-process` 통과, exit 0

- [ ] **Step 6: Commit** — `feat: 간편 모드 패키지 골격과 사용자용 오류·명령 실행기 추가`

---

### Task 2: 설정 파일 `reproof.toml`

**Files:**
- Create: `reproof/simple/config.py`, `tests/test_simple_config.py`

**Interfaces:**
- Consumes: `SimpleError`
- Produces:
  - `CONFIG_FILE_NAME = "reproof.toml"`
  - `@dataclass(frozen=True) class SimulatorSettings: device_type: str; runtime: str; locale: str = "ko_KR"; language: str = "ko"; timezone: str = "Asia/Seoul"`
  - `@dataclass(frozen=True) class ProjectConfig`: `name: str; container: str; generator: str; generator_spec: str | None; scheme: str; app_target: str; bundle_id: str; test_scheme: str | None; simulator: SimulatorSettings; launch_arguments: tuple[str, ...] = (); launch_environment: tuple[tuple[str, str], ...] = (); build_timeout_seconds: int = 3600; claude_consent: bool = False`
  - `ProjectConfig.container_kind -> str`: `"workspace"` 또는 `"project"`. 확장자로 정한다.
  - `load_config(path: Path) -> ProjectConfig`
  - `render_config(config: ProjectConfig) -> str`
  - `config_digest(config: ProjectConfig) -> str`: 64자리 hex
  - `resolve_launch_environment(config: ProjectConfig, environ: Mapping[str, str]) -> dict[str, str]`

TOML 형식(정확히 이 키만 허용. 모르는 키는 거절):

```toml
schema = 1
name = "mixedshop"

[project]
container = "MixedShop.xcodeproj"
generator = "xcodegen"        # none | xcodegen | tuist
generator_spec = "project.yml"
scheme = "MixedShop"
app_target = "MixedShop"
bundle_id = "io.reproof.example.mixedshop"
test_scheme = "MixedShopTests"

[simulator]
device_type = "iPhone 16"
runtime = "latest"
locale = "ko_KR"
language = "ko"
timezone = "Asia/Seoul"

[launch]
arguments = []

[launch.environment]
API_TOKEN = "env:REPROOF_API_TOKEN"

[build]
timeout_seconds = 3600

[ai]
claude_consent = false
```

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_round_trip_render_then_load_is_identical(self):
    config = sample_config()           # tests/simple_support.py에 추가
    path.write_text(render_config(config), encoding="utf-8")
    self.assertEqual(load_config(path), config)

def test_unknown_key_is_rejected_with_key_name(self):
    path.write_text(render_config(sample_config()) + "\n[extra]\nfoo = 1\n", encoding="utf-8")
    with self.assertRaises(SimpleError) as caught: load_config(path)
    self.assertEqual(caught.exception.code, "config_invalid")
    self.assertIn("extra", caught.exception.user_message())

def test_name_must_be_lowercase_slug(self):        # 정규식 ^[a-z0-9][a-z0-9-]{0,39}$
    ...assertRaises(SimpleError) for name "My App"

def test_env_reference_is_resolved_and_missing_reference_fails(self):
    config = replace(sample_config(), launch_environment=(("API_TOKEN", "env:REPROOF_API_TOKEN"), ("MODE", "qa")))
    self.assertEqual(resolve_launch_environment(config, {"REPROOF_API_TOKEN": "t"}), {"API_TOKEN": "t", "MODE": "qa"})
    with self.assertRaises(SimpleError) as caught: resolve_launch_environment(config, {})
    self.assertIn("REPROOF_API_TOKEN", caught.exception.user_message())

def test_digest_changes_when_scheme_changes(self):
    self.assertNotEqual(config_digest(sample_config()), config_digest(replace(sample_config(), scheme="Other")))

def test_generator_must_be_known_value(self):      # none|xcodegen|tuist 이외는 거절
```

- [ ] **Step 2: 실패 확인** — Run: `python3 -m unittest tests.test_simple_config -v`, Expected: FAIL (import 오류)
- [ ] **Step 3: 구현**
  - 읽기는 `tomllib`로 한다.
  - `render_config`는 위 형식과 키 순서 그대로 쓰는 결정적 writer다. 문자열은 `json.dumps(value, ensure_ascii=False)`로 따옴표를 처리한다.
  - `config_digest`는 dataclass를 `asdict`한 뒤 `json.dumps(sort_keys=True, ensure_ascii=False)`의 sha256이다.
  - `generator == "none"`이면 `generator_spec`은 `None`이어야 한다.
- [ ] **Step 4: 통과 확인** — 같은 명령, Expected: PASS 6개. `IOS_SIMPLE_CONTRACTS`에 check `simple-config`를 추가한다.
- [ ] **Step 5: Commit** — `feat: 간편 모드 프로젝트 설정 파일 형식 추가`

---

### Task 3: 앱 소스 작업 사본

**Files:**
- Create: `reproof/simple/workcopy.py`, `tests/test_simple_workcopy.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class CopyLimits: max_files: int = 100_000; max_total_bytes: int = 4 * 1024**3; max_file_bytes: int = 1024**3`
  - `@dataclass(frozen=True) class WorkingCopy: root: Path; manifest_digest: str; file_count: int; total_bytes: int`
  - `EXCLUDED_NAMES = frozenset({".git", "DerivedData", ".build", "xcuserdata", ".reproof"})`. 이 이름은 어느 깊이에서든 제외한다. 루트 바로 아래의 `build`도 제외한다.
  - `materialize_working_copy(source: Path, destination: Path, limits: CopyLimits = CopyLimits()) -> WorkingCopy`
  - `tree_digest(root: Path) -> str`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_copies_resources_and_preserves_executable_bit(self):
    # source에 a.swift, Assets.xcassets/Contents.json, scripts/run.sh(0o755)
    copy = materialize_working_copy(source, dest)
    self.assertTrue((dest / "Assets.xcassets/Contents.json").exists())
    self.assertTrue(os.access(dest / "scripts/run.sh", os.X_OK))
    self.assertEqual(copy.file_count, 3)

def test_excludes_git_deriveddata_xcuserdata_and_root_build(self):
    # .git/x, DerivedData/x, App.xcodeproj/xcuserdata/x, build/x, Sources/build/keep.swift
    materialize_working_copy(source, dest)
    self.assertFalse((dest / ".git").exists()); self.assertFalse((dest / "build").exists())
    self.assertTrue((dest / "Sources/build/keep.swift").exists())

def test_internal_relative_symlink_is_recreated_and_external_symlink_rejected(self):
    os.symlink("a.swift", source / "link.swift")
    materialize_working_copy(source, dest); self.assertTrue((dest / "link.swift").is_symlink())
    os.symlink("/etc/hosts", source2 / "bad")
    with self.assertRaises(SimpleError) as caught: materialize_working_copy(source2, dest2)
    self.assertEqual(caught.exception.code, "source_symlink_outside")

def test_file_count_limit_reports_limit_and_remedy(self):
    with self.assertRaises(SimpleError) as caught: materialize_working_copy(source, dest, CopyLimits(max_files=2))
    self.assertEqual(caught.exception.code, "source_too_large")

def test_handles_space_and_korean_path(self):          # Review Focus 1
    source = Path(tmp) / "회사 앱" / "My App"; ...; copy = materialize_working_copy(source, Path(tmp) / "작업 사본")
    self.assertEqual(copy.manifest_digest, tree_digest(Path(tmp) / "작업 사본"))

def test_destination_must_not_exist(self):             # code "destination_exists"
```

- [ ] **Step 2: 실패 확인** — Run: `python3 -m unittest tests.test_simple_workcopy -v`, Expected: FAIL
- [ ] **Step 3: 구현**
  - `os.scandir`로 반복 순회한다. 일반 파일은 1 MiB 단위로 읽고 쓰면서 sha256을 함께 계산하고, `shutil.copymode`로 권한을 보존한다.
  - 상대 symlink는 대상을 `resolve()`해서 source 루트 안이면 같은 상대 링크로 다시 만든다. 그렇지 않으면 거절한다.
  - 장치·FIFO 같은 특수 파일은 `source_special_file`로 거절한다.
  - manifest digest는 정렬된 `(상대경로, 종류, 권한비트 & 0o111, 내용 sha256 또는 링크 대상)` 줄들의 sha256이다. `tree_digest`도 같은 규칙으로 계산한다.
- [ ] **Step 4: 통과 확인** — Expected: PASS 6개. check `simple-workcopy`를 등록한다.
- [ ] **Step 5: Commit** — `feat: 실무 규모 앱 소스 작업 사본 생성 추가`

---

### Task 4: 빌드 식별

**Files:**
- Create: `reproof/simple/identity.py`, `tests/test_simple_identity.py`

**Interfaces:**
- Consumes: `ToolRunner`, `HostToolRunner`(테스트는 진짜 `git`을 쓴다)
- Produces:
  - `@dataclass(frozen=True) class BuildIdentity: commit_sha: str | None; is_dirty: bool; dirty_patch_digest: str | None; untracked_paths: tuple[str, ...]; config_digest: str; source_manifest_digest: str`
  - `BuildIdentity.build_id() -> str`: canonical JSON sha256의 앞 16자
  - `BuildIdentity.to_document() -> dict`
  - `BuildIdentity.source_label() -> str`: `"commit abc1234"`, `"commit abc1234 + 작업 중 변경"`, `"소스 버전 미확인"` 중 하나
  - `capture_build_identity(source_root: Path, *, config_digest: str, source_manifest_digest: str, output_dir: Path, runner: ToolRunner) -> BuildIdentity`. 작업 중 변경이 있으면 `output_dir / "dirty.patch"`를 쓴다.

- [ ] **Step 1: 실패하는 테스트 작성** (임시 git 저장소. `git -c user.name=t -c user.email=t@t commit`)

```python
def test_clean_repository_has_commit_and_not_dirty(self):
    identity = capture(...); self.assertEqual(identity.commit_sha, head_sha); self.assertFalse(identity.is_dirty)
    self.assertEqual(identity.source_label(), f"commit {head_sha[:7]}")

def test_modified_and_untracked_files_are_recorded(self):
    (repo / "a.swift").write_text("changed"); (repo / "new.swift").write_text("x")
    identity = capture(...)
    self.assertTrue(identity.is_dirty); self.assertEqual(identity.untracked_paths, ("new.swift",))
    self.assertTrue((out / "dirty.patch").read_text().startswith("diff --git"))

def test_identity_for_repository_without_commits(self):   # Review Focus 5
    identity = capture(repo_without_commits)
    self.assertIsNone(identity.commit_sha); self.assertEqual(identity.source_label(), "소스 버전 미확인")

def test_non_git_directory_is_unknown_not_error(self):    # 일반 폴더도 같은 결과

def test_build_id_depends_on_config_digest(self):
```

- [ ] **Step 2: 실패 확인** — Expected: FAIL
- [ ] **Step 3: 구현**
  - `git -C <root> rev-parse --verify HEAD`가 실패하면 `commit_sha=None`으로 둔다.
  - `git status --porcelain=v1 -z --untracked-files=all`로 변경을 확인한다.
  - `git diff --binary HEAD`로 patch를 쓰고, `dirty_patch_digest`는 그 patch의 sha256이다.
  - git이 아닌 디렉터리는 `ToolFailure`를 받아 "미확인"으로 처리한다.
- [ ] **Step 4: 통과 확인** — Expected: PASS 5개. check `simple-identity`를 등록한다.
- [ ] **Step 5: Commit** — `feat: commit과 작업 중 변경 기반 빌드 식별 추가`

---

### Task 5: 프로젝트 탐색

**Files:**
- Create: `reproof/simple/discovery.py`, `tests/test_simple_discovery.py`

**Interfaces:**
- Consumes: `ToolRunner`, `SimpleError`
- Produces:
  - `@dataclass(frozen=True) class ContainerScan: workspaces: tuple[str, ...]; projects: tuple[str, ...]; generator: str; generator_spec: str | None`
  - `scan_containers(root: Path) -> ContainerScan`: 깊이 3까지 본다. `.xcodeproj` 안의 `project.xcworkspace`, `EXCLUDED_NAMES`, `Pods`, `node_modules`는 무시한다.
  - `choose_container(scan: ContainerScan) -> str | None`: 모호하면 `None`
  - `list_schemes(working_copy: Path, container: str, runner: ToolRunner) -> tuple[str, ...]`
  - `@dataclass(frozen=True) class AppSettings: target: str; bundle_id: str`
  - `read_app_settings(working_copy: Path, container: str, scheme: str, runner: ToolRunner) -> AppSettings`
  - `guess_test_scheme(schemes: Sequence[str], app_scheme: str) -> str | None`

규칙(설계자 판단으로 고정):
- 생성기 판별: 루트에 `project.yml`이 있으면 `xcodegen`이다. 루트에 `Project.swift`·`Workspace.swift`·`Tuist/` 중 하나가 있으면 `tuist`다. 둘 다 있으면 `SimpleError("generator_ambiguous")`.
- 생성기가 없을 때: 루트 바로 아래 `.xcworkspace`가 하나면 그것을 쓴다. 없고 `.xcodeproj`가 하나면 그것을 쓴다. 그 밖에는 `None`.
- 앱 설정: `xcodebuild -showBuildSettings -json -scheme S -configuration Debug -sdk iphonesimulator (-workspace|-project) C`의 항목 중 `PRODUCT_TYPE == "com.apple.product-type.application"`인 것만 고른다. 여럿이면 `TARGET_NAME == scheme`인 것을 쓰고, 그래도 여럿이면 `SimpleError("app_target_ambiguous")`.
- 테스트 scheme: 이름에 `Test`가 들어가고 `UITest`는 들어가지 않는 것 중, 앱 scheme 이름으로 시작하는 것을 먼저 고른다. 없으면 `None`.

- [ ] **Step 1: 실패하는 테스트 작성** (`FakeToolRunner`에 `xcodebuild -list -json`·`-showBuildSettings` 응답 JSON을 넣는다)

```python
def test_xcodegen_spec_wins_and_is_reported(self):
def test_tuist_and_xcodegen_together_is_ambiguous(self):
def test_single_workspace_preferred_over_project(self):
def test_two_projects_without_workspace_is_ambiguous(self):
def test_list_schemes_reads_workspace_and_project_json_shapes(self):
    # {"workspace": {"schemes": [...]}} 와 {"project": {"schemes": [...]}} 둘 다
def test_read_app_settings_ignores_app_extensions(self):   # Review Focus 2
    # 항목: Widget(PRODUCT_TYPE app-extension), App(application) → AppSettings("App", "com.x.app")
def test_argv_keeps_space_and_korean_container_path_as_single_argument(self):  # Review Focus 1
    read_app_settings(Path("/tmp/회사 앱"), "My App.xcodeproj", "My App", fake)
    self.assertIn("My App.xcodeproj", fake.calls[0]["argv"])
def test_guess_test_scheme_prefers_unit_tests_over_ui_tests(self):
```

- [ ] **Step 2: 실패 확인** — Expected: FAIL
- [ ] **Step 3: 구현** — 위 규칙대로 구현한다. `xcodebuild` 호출은 cwd를 `working_copy`로 하고 timeout은 300초로 둔다.
- [ ] **Step 4: 통과 확인** — Expected: PASS 8개. check `simple-discovery`를 등록한다.
- [ ] **Step 5: Commit** — `feat: Xcode 프로젝트·scheme·앱 타깃 자동 탐색 추가`

---

### Task 6: 프로젝트 생성기와 SPM 해석

**Files:**
- Create: `reproof/simple/generation.py`, `reproof/simple/packages.py`, `tests/test_simple_packages.py`

**Interfaces:**
- Consumes: `ProjectConfig`, `ToolRunner`, `ToolFailure`
- Produces:
  - `generator_tool_path(generator: str) -> Path | None`: `shutil.which("xcodegen" | "tuist")`
  - `generate_project(working_copy: Path, config: ProjectConfig, runner: ToolRunner, *, log_path: Path) -> None`
  - `@dataclass(frozen=True) class PackageResolution: cache_dir: Path; resolved_digest: str | None; is_pinned: bool`
  - `find_package_resolved(working_copy: Path, config: ProjectConfig) -> Path | None`
  - `resolve_packages(working_copy: Path, config: ProjectConfig, *, cache_root: Path, runner: ToolRunner, log_path: Path, warn: Callable[[str], None]) -> PackageResolution`
  - `redact_url(url: str) -> str`
  - `classify_package_failure(failure: ToolFailure) -> SimpleError`

고정 값:
- XcodeGen: `["xcodegen", "generate", "--spec", spec]`. cwd는 spec이 있는 폴더다.
- Tuist: `["tuist", "generate", "--no-open"]`. cwd는 루트다.
- 생성 도구가 없으면 `SimpleError("generator_missing")`. 해결 안내는 XcodeGen이면 `brew install xcodegen`, Tuist면 `mise install tuist`(또는 팀이 쓰는 설치 방법)다.
- `Package.resolved` 위치는 `<container>/xcshareddata/swiftpm/Package.resolved`(workspace) 또는 `<container>/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`(project)다.
- 해석 argv: `xcodebuild -resolvePackageDependencies (-workspace|-project) C -scheme S -clonedSourcePackagesDirPath <cache> -scmProvider system`. `Package.resolved`가 있으면 `-onlyUsePackageVersionsFromResolvedFile`을 붙인다.
- 캐시 위치: `cache_root / "spm" / resolved_digest`. `Package.resolved`가 없으면 `cache_root / "spm" / ("unpinned-" + config_digest[:16])`.
- 인증 실패 판별 문구(대소문자 무시): `authentication failed`, `could not read username`, `permission denied (publickey)`, `terminal prompts disabled`, `repository not found`.
  - 해당하면 `SimpleError("package_auth_failed")`를 낸다. 원인에는 `redact_url`로 가린 저장소 URL을 넣는다.
  - 해결 안내는 "이 Mac 터미널에서 `git ls-remote <URL>`이 되는지 확인한 뒤 다시 실행하세요"다.
  - 해당하지 않으면 `SimpleError("package_resolution_failed")`다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_resolve_uses_system_scm_cache_and_pinned_versions(self):
    argv = fake.calls[0]["argv"]
    self.assertIn("-scmProvider", argv); self.assertEqual(argv[argv.index("-scmProvider") + 1], "system")
    self.assertIn("-onlyUsePackageVersionsFromResolvedFile", argv)
    self.assertTrue(resolution.is_pinned)

def test_resolve_without_resolved_file_warns_and_is_unpinned(self):   # Review Focus 4
    self.assertFalse(resolution.is_pinned); self.assertNotIn("-onlyUsePackageVersionsFromResolvedFile", argv)
    self.assertIn("Package.resolved", warnings[0])

def test_auth_failure_is_classified_and_token_is_redacted(self):
    failure = ToolFailure("xcodebuild", log_path=None, output_tail="fatal: Authentication failed for 'https://bot:s3cret@git.corp/x/Kit.git'")
    error = classify_package_failure(failure)
    self.assertEqual(error.code, "package_auth_failed")
    self.assertIn("https://git.corp/x/Kit.git", error.user_message()); self.assertNotIn("s3cret", error.user_message())

def test_redact_url_removes_userinfo_only(self):
    self.assertEqual(redact_url("https://u:p@h.com:8443/a.git"), "https://h.com:8443/a.git")
    self.assertEqual(redact_url("git@github.com:org/a.git"), "git@github.com:org/a.git")

def test_missing_generator_tool_reports_install_command(self):
def test_xcodegen_runs_in_spec_directory(self):
```

- [ ] **Step 2: 실패 확인** — Expected: FAIL
- [ ] **Step 3: 구현** — 위 고정 값대로 구현한다. 인증 판별은 `failure.output_tail`과 로그 파일의 마지막 64 KiB에서 찾는다.
- [ ] **Step 4: 통과 확인** — Expected: PASS 6개. check `simple-packages`를 등록한다.
- [ ] **Step 5: Commit** — `feat: XcodeGen/Tuist 생성과 SPM 해석·인증 실패 안내 추가`

---

### Task 7: Simulator용 빌드와 빌드 파이프라인

**Files:**
- Create: `reproof/simple/build.py`, `reproof/simple/pipeline.py`, `tests/test_simple_build.py`

**Interfaces:**
- Consumes: Task 2–6의 모든 Produces
- Produces:
  - `@dataclass(frozen=True) class BuildProducts: app_path: Path; bundle_id: str; derived_data: Path; products_digest: str`
  - `simulator_build_argv(config: ProjectConfig, *, derived_data: Path, packages_dir: Path) -> list[str]`
  - `find_app_bundle(products_dir: Path, bundle_id: str) -> Path`
  - `build_for_simulator(working_copy: Path, config: ProjectConfig, *, derived_data: Path, packages: PackageResolution, runner: ToolRunner, log_path: Path) -> BuildProducts`
  - `@dataclass(frozen=True) class PreparedBuild: build_id: str; identity: BuildIdentity; working_copy: WorkingCopy; products: BuildProducts; directory: Path`
  - `default_workspace_root(environ: Mapping[str, str] = os.environ) -> Path`: `REPROOF_HOME`이 있으면 그 값, 없으면 `~/.reproof`
  - `prepare_build(source_root: Path, config: ProjectConfig, *, workspace_root: Path, runner: ToolRunner, progress: Callable[[str], None] = print) -> PreparedBuild`

고정 값:
- 빌드 argv:
  ```
  xcodebuild build (-workspace|-project) C -scheme S -configuration Debug -sdk iphonesimulator
  -destination "generic/platform=iOS Simulator" -derivedDataPath D -clonedSourcePackagesDirPath P
  -disableAutomaticPackageResolution -onlyUsePackageVersionsFromResolvedFile -skipMacroValidation
  -skipPackagePluginValidation CODE_SIGNING_ALLOWED=NO COMPILER_INDEX_STORE_ENABLE=NO
  ```
  - `-onlyUsePackageVersionsFromResolvedFile`은 `is_pinned`일 때만 붙인다.
  - macro·plugin 검증을 건너뛰는 이유: AI는 `Package.swift`·`Package.resolved`를 못 바꾸므로, 실행되는 macro·plugin은 회사가 이미 쓰는 것으로 고정된다. 이 이유를 docstring에 남긴다.
- 빌드 디렉터리: `workspace_root/projects/<name>/builds/<build_id>/`. 그 안에 `source/`, `DerivedData/`, `logs/{generate,packages,build}.log`, `identity.json`, `products.json`, 있으면 `dirty.patch`를 둔다.
- 작업은 `<build_id>.partial/`에서 하고, 성공해야 `<build_id>/`로 rename한다. 같은 `build_id`의 완성 디렉터리(`products.json`이 있고 그 안의 `app_path`가 실제로 존재)가 있으면 다시 빌드하지 않고 재사용한다.
- 시작할 때 남아 있는 `<build_id>.partial/`은 툴이 만든 것이므로 지우고 새로 시작한다.
- `build_id`는 작업 사본의 manifest digest가 있어야 정해진다. 그래서 사본은 먼저 `workspace_root/projects/<name>/staging-<uuid>.partial/`에 만들고, 그 다음 `<build_id>.partial/source`로 옮긴다.
- 진행 메시지(한국어, 순서 고정): `"빌드 식별 중"`, `"작업 사본 만드는 중"`, `"프로젝트 생성 중"`(생성기 있을 때만), `"패키지 받는 중"`, `"앱 빌드 중"`, `"빌드 완료: <app_path>"`

- [ ] **Step 1: 실패하는 테스트 작성** (`FakeToolRunner`. `xcodebuild build` 응답에서 가짜 `.app`과 `Info.plist`(plistlib)를 만드는 콜백을 쓴다. `FakeToolRunner`에 응답 대신 callable을 받는 기능을 추가한다.)

```python
def test_build_argv_has_fixed_flags_and_no_signing(self):
    argv = simulator_build_argv(config, derived_data=Path("/d"), packages_dir=Path("/p"))
    for flag in ("-disableAutomaticPackageResolution", "-skipMacroValidation", "CODE_SIGNING_ALLOWED=NO"): self.assertIn(flag, argv)
    self.assertEqual(argv[argv.index("-configuration") + 1], "Debug")

def test_find_app_bundle_matches_bundle_identifier(self):    # 두 .app 중 bundle id 일치하는 것

def test_prepare_build_produces_documents_and_progress(self):
    prepared = prepare_build(source, config, workspace_root=ws, runner=fake, progress=messages.append)
    self.assertEqual(messages[0], "빌드 식별 중"); self.assertTrue(messages[-1].startswith("빌드 완료"))
    self.assertTrue((prepared.directory / "products.json").exists())

def test_second_prepare_reuses_completed_build_without_xcodebuild(self):

def test_prepare_build_discards_partial_directory_from_interrupted_run(self):   # Review Focus 3
    # 1차: xcodebuild build가 KeyboardInterrupt → <id>.partial만 남고 <id>는 없음
    # 2차: 정상 → 완성 디렉터리 생성, partial 없음, xcodebuild build 다시 호출됨
```

- [ ] **Step 2: 실패 확인** — Expected: FAIL
- [ ] **Step 3: 구현** — 고정 값대로 구현한다. `products_digest`는 `.app` 디렉터리의 `tree_digest`다.
- [ ] **Step 4: 통과 확인** — Expected: PASS 5개. check `simple-build`를 등록한다.
- [ ] **Step 5: Commit** — `feat: 간편 모드 Simulator 빌드 파이프라인 추가`

---

### Task 8: 전용 Simulator 관리

**Files:**
- Create: `reproof/simple/simulator.py`, `tests/test_simple_simulator.py`

**Interfaces:**
- Consumes: `ToolRunner`, `SimulatorSettings`
- Produces:
  - `OWNED_PREFIX = "reproof-"`
  - `@dataclass(frozen=True) class Runtime: identifier: str; name: str; version: str; is_available: bool`
  - `list_runtimes(runner: ToolRunner) -> tuple[Runtime, ...]`: iOS만
  - `pick_runtime(runtimes: Sequence[Runtime], requested: str) -> Runtime`: `"latest"` 또는 `"iOS 27.0"` 같은 이름
  - `class SimulatorManager`:
    - `__init__(self, runner: ToolRunner)`
    - `ensure_device(self, project_name: str, settings: SimulatorSettings) -> str`: udid를 돌려준다
    - `reset(self, udid: str) -> None`
    - `apply_presentation(self, udid: str) -> None`
    - `install_app(self, udid: str, app_path: Path) -> None`
    - `uninstall_app(self, udid: str, bundle_id: str) -> None`
  - `launch_locale_settings(settings: SimulatorSettings) -> tuple[list[str], dict[str, str]]`

고정 값:
- 기기 이름: `f"reproof-{project_name}"`. 같은 이름·device type·runtime의 기기가 있으면 재사용하고, 없으면 `xcrun simctl create`로 만든다.
- 이름에 접두사가 없는 udid를 조작 메서드에 넘기면 `SimpleError("simulator_not_owned")`.
- `reset` 순서: `shutdown`(이미 꺼져 있으면 무시) → `erase` → `boot` → `bootstatus -b`.
- 표시 고정: `simctl status_bar <udid> override --time 9:41 --batteryState charged --batteryLevel 100 --cellularBars 4 --wifiBars 3`, `simctl ui <udid> appearance light`, `simctl ui <udid> content_size large`.
- 로케일: args `["-AppleLanguages", "(ko)", "-AppleLocale", "ko_KR"]`, env `{"TZ": "Asia/Seoul"}`. 값은 설정에서 온다.
- iOS 런타임이 하나도 없으면 `SimpleError("ios_runtime_missing")`. 해결 안내는 `xcodebuild -downloadPlatform iOS`다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_ensure_device_reuses_owned_device_and_creates_when_missing(self):
def test_operations_refuse_device_without_prefix(self):
    manager = SimulatorManager(fake_with_device_named("iPhone 16"))
    with self.assertRaises(SimpleError) as caught: manager.reset(udid)
    self.assertEqual(caught.exception.code, "simulator_not_owned")
    self.assertFalse(any("erase" in call["argv"] for call in fake.calls))
def test_reset_order_is_shutdown_erase_boot_bootstatus(self):
def test_pick_runtime_latest_and_by_name_and_missing(self):
def test_launch_locale_settings_from_config(self):
```

- [ ] **Step 2: 실패 확인** — Expected: FAIL
- [ ] **Step 3: 구현** — 기기 조회는 `xcrun simctl list devices --json`, 런타임은 `list runtimes --json`, device type은 `list devicetypes --json`으로 한다. device type은 이름으로 찾는다.
- [ ] **Step 4: 통과 확인** — Expected: PASS 5개. check `simple-simulator`를 등록한다.
- [ ] **Step 5: Commit** — `feat: reproof 전용 Simulator 생성·초기화·표시 고정 추가`

---

### Task 9: 화면 계층과 여러 근거 locator

**Files:**
- Create: `reproof/simple/hierarchy.py`, `reproof/simple/locator.py`, `tests/test_simple_locator.py`,
  `tests/fixtures/simple/hierarchy-mixed.json`(UIKit 버튼 ID 있음·없음, SwiftUI 같은 이름 버튼 3개, 숫자가 바뀌는 Text 포함)

**Interfaces:**
- Produces:
  - 실행기 JSON 노드 스키마: `{"type": str, "identifier": str, "label": str, "value": str, "placeholder": str, "frame": [x, y, w, h], "enabled": bool, "selected": bool, "children": [...]}`
  - `@dataclass(frozen=True) class Frame: x: float; y: float; width: float; height: float`. 메서드: `contains(self, px: float, py: float) -> bool`, `center(self) -> tuple[float, float]`
  - `@dataclass(frozen=True) class Element: element_type: str; identifier: str; label: str; value: str; placeholder: str; frame: Frame; is_enabled: bool; is_selected: bool; path: tuple[int, ...]; children: tuple["Element", ...]`
  - `parse_hierarchy(document: Mapping) -> Element`: 노드 8000개, 깊이 64를 넘으면 `SimpleError("hierarchy_too_large")`
  - `iter_elements(root: Element) -> Iterator[Element]`: pre-order
  - `ancestors_of(root: Element, element: Element) -> tuple[Element, ...]`: 가까운 순
  - `element_at(root: Element, x: float, y: float) -> Element | None`
  - `INTERACTIVE_TYPES = frozenset({"button", "textField", "secureTextField", "searchField", "switch", "toggle", "cell", "link", "slider", "segmentedControl", "stepper", "picker", "menuItem", "tab"})`
  - `AUTO_ID_PREFIX = "rp."`
  - `@dataclass(frozen=True) class LocatorEvidence: identifier: str; element_type: str; label: str; anchor_identifier: str; label_ordinal: int; position_ordinal: int`. 메서드: `to_document() -> dict`, `from_document(cls, document: Mapping) -> LocatorEvidence`
  - `@dataclass(frozen=True) class Resolution: element: Element | None; strength: str; matched_by: str; candidate_count: int`
    - strength: `"strong" | "weak" | "none"`
    - matched_by: `"identifier" | "auto-identifier" | "type-label-anchor" | "label-ordinal" | "position-ordinal" | "none"`
  - `capture_evidence(root: Element, element: Element) -> LocatorEvidence`
  - `resolve(root: Element, evidence: LocatorEvidence) -> Resolution`

해석 순서(설계자 판단 고정):
1. `identifier`가 있고 그 값을 가진 요소가 정확히 하나면 strong이다. 값이 `rp.`로 시작하면 `auto-identifier`, 아니면 `identifier`.
2. (type, label, anchor)가 같은 요소가 하나면 weak `type-label-anchor`.
3. 여럿이면 그중 `label_ordinal`번째(pre-order)를 weak `label-ordinal`로 고른다.
4. label이 달라졌을 수 있으므로 (type, anchor)가 같은 요소 중 `position_ordinal`번째를 weak `position-ordinal`로 고른다. 숫자가 바뀌는 Text·값이 바뀌는 입력창이 이 경우다.
5. 그래도 없으면 `none`.

`anchor_identifier`는 가장 가까운 조상 중 identifier가 비어 있지 않은 것의 값이다. 없으면 `""`이다.
`element_at`은 좌표를 포함하는 가장 깊은 요소를 찾는다. 같은 깊이면 `INTERACTIVE_TYPES`를 먼저 고르고, 그 조상 중 interactive 요소가 있으면 그 조상을 돌려준다. 버튼 안 Text를 누르면 버튼을 돌려주기 위해서다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_unique_identifier_is_strong(self):
    r = resolve(root, capture_evidence(root, find(root, identifier="cart.clear")))
    self.assertEqual((r.strength, r.matched_by), ("strong", "identifier"))
def test_auto_identifier_prefix_is_reported(self):              # identifier "rp.CartViewController.addButton"
def test_label_without_identifier_is_weak_type_label_anchor(self):
def test_duplicate_labels_use_label_ordinal(self):              # SwiftUI "추가" 버튼 3개 중 2번째
    r = resolve(root, capture_evidence(root, second_add)); self.assertEqual(r.element.path, second_add.path)
    self.assertEqual(r.matched_by, "label-ordinal"); self.assertEqual(r.candidate_count, 3)
def test_changed_label_falls_back_to_position_ordinal(self):    # Text "합계 2" → 나중 트리에서 "합계 1"
    evidence = capture_evidence(before, total_text); r = resolve(after, evidence)
    self.assertEqual(r.matched_by, "position-ordinal"); self.assertEqual(r.element.label, "합계 1")
def test_missing_element_is_none(self):
def test_element_at_returns_button_not_inner_text(self):
def test_evidence_document_round_trip(self):
def test_oversized_hierarchy_is_rejected(self):
```

- [ ] **Step 2: 실패 확인** — Expected: FAIL
- [ ] **Step 3: 구현** — 위 규칙대로 구현한다. 나머지는 해석 순서로 정해지므로 따로 결정할 것은 없다.
- [ ] **Step 4: 통과 확인** — Expected: PASS 9개. check `simple-locator`를 등록한다.
- [ ] **Step 5: Commit** — `feat: 화면 계층 해석과 여러 근거 locator 추가`

---

### Task 10: 검증 앱 MixedShop (XcodeGen·SwiftUI+UIKit·로컬 패키지)

**Files:**
- Create under `examples/simple-ios/MixedShop/`:
  - `project.yml`
  - `MixedShop/AppDelegate.swift`, `MixedShop/SceneDelegate.swift`, `MixedShop/CartViewController.swift`, `MixedShop/OptionsView.swift`, `MixedShop/SummaryView.swift`, `MixedShop/Info.plist`
  - `Packages/ShopKit/Package.swift`, `Packages/ShopKit/Sources/ShopKit/Cart.swift`, `Packages/ShopKit/Tests/ShopKitTests/CartTests.swift`
  - `README.md`
- Create: `tests/test_simple_native_build.py`
- Modify: `tests/release_inventory.json` (`IOS_SIMPLE_NATIVE` goal 생성)

**Interfaces:**
- Produces (뒤 Task들이 의존하는 화면 계약):
  - bundle id `io.reproof.example.mixedshop`, scheme `MixedShop`, 테스트 scheme `MixedShopTests`(ShopKit 단위 테스트), iOS deployment target 17.0
  - 루트는 `UITabBarController`다.
    - 탭 1 "장바구니"는 순수 UIKit이다. `UINavigationController` → `CartViewController`:
      - `UILabel` "담은 수 0"(ID 없음)
      - `UIButton` "담기"(ID 없음)
      - `UIButton` "비우기"(`accessibilityIdentifier = "cart.clear"`)
      - `UIButton` "옵션": `UIHostingController(rootView: OptionsView())`를 push한다
    - `OptionsView`는 UIKit에 끼운 SwiftUI다:
      - `Toggle` "선물 포장"
      - `TextField` "메모"
      - `ForEach` 3행, 행마다 `Text(상품명)`과 `Button("추가")`
      - `Text("합계 \(count)")`
    - 탭 2 "요약"은 순수 SwiftUI 루트(`UIHostingController(rootView: SummaryView())`)다. `Button("새로고침")`과 `Text` 2개가 있다.
  - 로직은 `ShopKit.Cart`(`add()`, `clear()`, `count`)에 둔다. 이 계획에서는 버그를 심지 않는다. 버그는 계획 2에서 심는다.

- [ ] **Step 1: 실패하는 native 테스트 작성** (`tests/test_simple_native_build.py`. Xcode가 없으면 skip하지 않고 실패시킨다. 이 goal은 gate `environment`다.)

```python
def test_pipeline_builds_mixedshop_with_xcodegen_and_local_package(self):
    config = ProjectConfig(name="mixedshop", container="MixedShop.xcodeproj", generator="xcodegen", generator_spec="project.yml",
                           scheme="MixedShop", app_target="MixedShop", bundle_id="io.reproof.example.mixedshop",
                           test_scheme="MixedShopTests", simulator=SimulatorSettings("iPhone 16", "latest"))
    prepared = prepare_build(MIXEDSHOP, config, workspace_root=Path(tmp), runner=HostToolRunner(), progress=lambda _: None)
    self.assertTrue((prepared.products.app_path / "Info.plist").exists())
    self.assertEqual(plistlib.loads((prepared.products.app_path / "Info.plist").read_bytes())["CFBundleIdentifier"], "io.reproof.example.mixedshop")
```

- [ ] **Step 2: 실패 확인** — Run: `python3 -m unittest tests.test_simple_native_build -v`, Expected: FAIL (`examples/simple-ios/MixedShop` 없음)
- [ ] **Step 3: 앱 작성** — 위 화면 계약대로 작성한다. `project.yml`의 `packages`에는 `ShopKit: path: Packages/ShopKit`를 둔다. 생성된 `.xcodeproj`는 커밋하지 않는다. 생성기 경로를 검증하기 위해서다.
- [ ] **Step 4: 통과 확인** — 같은 명령, Expected: PASS. `IOS_SIMPLE_NATIVE` goal에 check `simple-native-build`를 등록한다(effects `["filesystem","process","native-compile"]`, timeoutSeconds 600, gate `environment`).
- [ ] **Step 5: Commit** — `test: XcodeGen·SwiftUI+UIKit 혼용 검증 앱 MixedShop 추가`

---

### Task 11: SimpleRunner (범용 XCUITest 실행기)와 Python 클라이언트

**Files:**
- Create under `ios/SimpleRunner/`:
  - `project.yml`, `SimpleRunner.xcodeproj/**`(XcodeGen으로 생성해 커밋. 사용자 Mac에 XcodeGen이 없어도 되게 하려는 것으로, 기존 `live-ios`와 같은 방식)
  - `RunnerHost/AppDelegate.swift`, `RunnerHost/Info.plist`
  - `RunnerTests/RunnerSessionTests.swift`, `RunnerTests/CommandServer.swift`, `RunnerTests/HierarchyExport.swift`, `RunnerTests/Info.plist`
- Create: `reproof/simple/runner_client.py`, `tests/test_simple_runner_client.py`(portable), `tests/test_simple_native_runner.py`(native)
- Modify: `distribution-resources.json`(SimpleRunner 파일 추가)

**Interfaces:**
- Consumes: `ToolRunner`, `SimulatorManager`, `parse_hierarchy`, `Element`
- Produces:
  - HTTP: `POST http://127.0.0.1:<port>/command`, 본문 `{"action": str, ...}`
    - 성공 응답: `{"ok": true, ...}`
    - 실패 응답: `{"ok": false, "error": str}`
  - action 목록:
    - `status` → `{"ok": true, "targetBundle": str, "targetState": str}`
    - `launch {arguments: [str], environment: {str: str}}`
    - `terminate`
    - `activate`
    - `hierarchy` → `{"ok": true, "root": <노드>}`
    - `tap {x, y}`: 앱 좌표계 포인트, 정규화 아님
    - `long_press {x, y, duration}`
    - `swipe {fromX, fromY, toX, toY, duration}`
    - `type {text}`: 256자 이하
    - `screenshot` → `{"ok": true, "png": base64}`
    - `stop`
  - 실행기는 대상 앱을 `XCUIApplication(bundleIdentifier: env["REPROOF_TARGET_BUNDLE"])`로 구동하고, port는 `env["REPROOF_RUNNER_PORT"]`로 받는다. Python은 이 값을 `TEST_RUNNER_REPROOF_*` 환경 변수로 넘긴다(xcodebuild가 접두사를 떼고 테스트에 전달한다).
  - 명령 처리는 메인 스레드에서 한다(XCUITest 제약). 세션 최대 길이는 6시간이다.
  - `@dataclass(frozen=True) class RunnerProducts: xctestrun: Path`
  - `build_runner(output_root: Path, runner: ToolRunner) -> RunnerProducts`
    - `reproof.resources`로 `ios/SimpleRunner`를 `output_root/runner-src`에 복사한 뒤 `xcodebuild build-for-testing -project SimpleRunner.xcodeproj -scheme SimpleRunner -sdk iphonesimulator -destination "generic/platform=iOS Simulator" -derivedDataPath <output_root>/runner-dd CODE_SIGNING_ALLOWED=NO`를 실행한다.
    - 캐시 키는 `xcodebuild -version` 출력과 실행기 소스 `tree_digest`의 sha256 앞 16자다. `output_root/runner/<key>/`에 이미 있으면 재사용한다.
  - `free_loopback_port() -> int`
  - `class RunnerClient`:
    - `__init__(self, port: int, *, timeout: float = 30.0)`
    - `command(self, action: str, **payload) -> dict`: `ok`가 false면 `SimpleError("runner_command_failed")`
    - `hierarchy(self) -> Element`
    - `tap_point(self, x: float, y: float) -> None`
    - `type_text(self, text: str) -> None`
    - `launch(self, arguments: list[str], environment: dict[str, str]) -> None`
    - `terminate(self) -> None`
  - `class RunnerSession`: context manager
    - `__init__(self, products: RunnerProducts, *, udid: str, target_bundle: str, port: int, log_path: Path, start_timeout: float = 180.0)`
    - `__enter__` → `RunnerClient`
    - `__exit__`: `stop`를 보낸 뒤 프로세스 그룹을 SIGTERM, 2초 뒤 SIGKILL로 정리한다.
    - xcodebuild argv: `xcodebuild test-without-building -xctestrun X -destination id=<udid> -only-testing:SimpleRunnerTests/RunnerSessionTests/testSession`
    - `subprocess.Popen(start_new_session=True)`로 띄운다. 오래 실행되는 세션이라 `run_command`를 쓰지 않는다.

- [ ] **Step 1: 실패하는 portable 테스트 작성** (`tests/test_simple_runner_client.py`. loopback `http.server`로 가짜 실행기를 띄운다.)

```python
def test_command_posts_json_and_returns_payload(self):
def test_not_ok_response_raises_simple_error_with_runner_error(self):
def test_hierarchy_parses_root(self):
def test_session_passes_test_runner_prefixed_environment(self):   # Popen을 patch해 env 확인
    self.assertEqual(env["TEST_RUNNER_REPROOF_TARGET_BUNDLE"], "io.reproof.example.mixedshop")
def test_session_exit_sends_stop_and_kills_process_group(self):
```

- [ ] **Step 2: 실패 확인** — Run: `python3 -m unittest tests.test_simple_runner_client -v`, Expected: FAIL
- [ ] **Step 3: Python 클라이언트 구현** — `urllib.request`로 통신하고, 응답은 최대 32 MiB까지만 읽는다(screenshot 때문).
- [ ] **Step 4: portable 통과 확인** — Expected: PASS 5개. check `simple-runner-client`를 `IOS_SIMPLE_CONTRACTS`에 등록한다(effects `["filesystem","process","loopback"]`).
- [ ] **Step 5: 실패하는 native 테스트 작성** (`tests/test_simple_native_runner.py`. Task 10에서 만든 MixedShop 빌드를 쓴다.)

```python
def test_hierarchy_contains_uikit_and_embedded_swiftui_elements(self):
    with RunnerSession(products, udid=udid, target_bundle=BUNDLE, port=free_loopback_port(), log_path=log) as client:
        client.launch(*launch_locale_settings(config.simulator))
        root = client.hierarchy()
        self.assertTrue(any(e.label == "담기" and e.element_type == "button" for e in iter_elements(root)))
        add = next(e for e in iter_elements(root) if e.label == "담기")
        evidence = capture_evidence(root, add)
        client.tap_point(*resolve(client.hierarchy(), evidence).element.frame.center())
        self.assertTrue(any(e.label == "담은 수 1" for e in iter_elements(client.hierarchy())))
        options = next(e for e in iter_elements(client.hierarchy()) if e.label == "옵션")
        client.tap_point(*options.frame.center())
        self.assertEqual(sum(1 for e in iter_elements(client.hierarchy()) if e.label == "추가" and e.element_type == "button"), 3)
```

- [ ] **Step 6: native 실패 확인** — Run: `python3 -m unittest tests.test_simple_native_runner -v`, Expected: FAIL (`ios/SimpleRunner` 없음)
- [ ] **Step 7: Swift 실행기 구현**
  - `CommandServer`는 `NWListener`로 `127.0.0.1`에만 바인드하는 HTTP/1.1이다. 요청 본문은 1 MiB까지 받고, 받은 명령은 메인 큐에서 실행한다.
  - `HierarchyExport`는 `XCUIApplication.snapshot()`을 재귀로 돌아 노드 스키마로 바꾼다. `elementType`의 rawValue는 위 type 문자열로 매핑하고, 모르는 type은 `"other"`로 둔다.
  - 좌표 탭은 `app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: x, dy: y)).tap()`이다.
  - `testSession`은 `stop`을 받거나 6시간이 지나면 끝난다.
- [ ] **Step 8: native 통과 확인** — 같은 명령, Expected: PASS. `IOS_SIMPLE_NATIVE`에 check `simple-native-runner`를 등록한다(effects `["filesystem","process","native-compile","owned-device","loopback"]`). 이어서 `distribution-resources.json`에 실행기 파일을 추가하고 `python3 scripts/release-check.py --goal RUNTIME --effects filesystem,process`로 리소스 검사가 통과하는지 확인한다.
- [ ] **Step 9: Commit** — 두 번으로 나눈다.
  - `feat: 간편 모드 범용 XCUITest 실행기 추가` (Swift와 리소스)
  - `feat: 실행기 Python 클라이언트와 세션 관리 추가`

---

### Task 12: CLI `init`·`doctor`·`build`·`inspect`와 사용자 문서

**Files:**
- Create: `reproof/simple/doctor.py`, `tests/test_simple_cli.py`, `tests/test_simple_native_cli.py`, `docs/SIMPLE-MODE-IOS.md`
- Modify: `reproof/simple/cli.py`

**Interfaces:**
- Consumes: Task 2–11 전부
- Produces:
  - `@dataclass(frozen=True) class Check: name: str; is_ok: bool; detail: str; remedy: str`
  - `run_doctor(config: ProjectConfig | None, *, runner: ToolRunner, workspace_root: Path) -> tuple[Check, ...]`
    - 항목 순서: `xcode`, `ios_runtime`, `git`, `generator`(설정에 생성기가 있을 때만), `disk`(workspace_root의 남은 공간이 30 GB 이상), `claude`(`shutil.which("claude")`만 본다. 호출하지 않고 "AI 수정에 필요"로 안내)
  - 명령:
    - `reproof app init [--path DIR] [--non-interactive] [--force]`: `DIR/reproof.toml`을 만든다. 이미 있으면 `--force` 없이는 `SimpleError("config_exists")`.
    - `reproof app doctor [--config FILE]`
    - `reproof app build [--config FILE] [--workspace DIR]`
    - `reproof app inspect [--config FILE] [--workspace DIR] [--point X,Y] [--output FILE]`
  - `--config` 기본값은 현재 폴더부터 상위로 올라가며 처음 찾은 `reproof.toml`이다.

`init` 동작(고정):
1. 생성기가 있으면 임시 작업 사본에서 생성해 scheme 목록을 얻는다.
2. container·scheme·앱 타깃 중 모호한 것만 번호 선택 질문으로 묻는다. 질문 수는 기록한다.
3. 테스트 scheme은 추측값을 보여 주고 Enter로 수락하게 한다.
4. Simulator는 `iPhone 16`과 `latest`를 기본값으로 쓰고 묻지 않는다.
5. 이름은 폴더 이름에서 만든 slug를 쓰고 묻지 않는다.
6. 저장 후 doctor를 실행하고, 결과를 "다음 단계: reproof app build"와 함께 출력한다.

`--non-interactive`에서는 모호한 항목이 있으면 질문 대신 `SimpleError("init_needs_choice")`를 내고, 선택지를 메시지에 나열한다.

`inspect` 동작:
1. 빌드한다(완성된 빌드가 있으면 재사용).
2. `ensure_device`, `reset`, `apply_presentation`, `install_app`을 순서대로 한다.
3. 실행기를 빌드하고 세션을 연다. 로케일 설정과 설정의 launch 인자·환경으로 앱을 실행한다.
4. 3초 기다린 뒤 계층 JSON을 `--output`에 저장한다. 기본 경로는 `빌드디렉터리/inspect-<시각>.json`이다.
5. `--point`가 있으면 그 좌표의 요소와 `capture_evidence` 결과, `resolve` 결과(strength·matched_by)를 출력한다.

- [ ] **Step 1: 실패하는 portable 테스트 작성** (`FakeToolRunner`, `input`은 patch로 대체)

```python
def test_init_writes_config_without_questions_for_single_choices(self):
    code = main(["init", "--path", str(app), "--non-interactive"]); self.assertEqual(code, 0)
    self.assertEqual(load_config(app / "reproof.toml").scheme, "MixedShop")
def test_init_asks_only_for_ambiguous_scheme(self):          # input 호출 1회
def test_init_non_interactive_lists_choices_when_ambiguous(self):
def test_init_refuses_overwrite_without_force(self):
def test_doctor_reports_missing_runtime_with_download_command(self):
    self.assertIn("xcodebuild -downloadPlatform iOS", output)
def test_doctor_warns_when_disk_below_30gb(self):            # shutil.disk_usage patch
def test_simple_error_is_printed_in_korean_and_returns_2(self):
```

- [ ] **Step 2: 실패 확인** — Run: `python3 -m unittest tests.test_simple_cli -v`, Expected: FAIL
- [ ] **Step 3: 구현** — 위 동작대로 구현한다. 출력에 해시나 내부 코드명을 노출하지 않는다. `build_id`만 "빌드 ID"로 보여 준다.
- [ ] **Step 4: portable 통과 확인** — Expected: PASS 7개. check `simple-cli`를 등록한다.
- [ ] **Step 5: native e2e 테스트 작성과 통과** (`tests/test_simple_native_cli.py`)
  - MixedShop 복사본에서 `init --non-interactive`, `build`, `inspect --point <담기 버튼 중심>`을 차례로 실행한다.
  - 출력에 `weak`과 `type-label-anchor`가 있는지 확인한다.
  - check `simple-native-cli`를 `IOS_SIMPLE_NATIVE`에 등록한다.

Run: `python3 -m unittest tests.test_simple_native_cli -v`
Expected: PASS

- [ ] **Step 6: 사용자 문서 작성** — `docs/SIMPLE-MODE-IOS.md`(한국어)에 다음을 쓴다.
  - 설치(`pip install` 또는 checkout), `app init`/`doctor`/`build`/`inspect` 사용법
  - `reproof.toml` 키 설명, `env:` 참조 방식
  - 비공개 SPM 인증 확인 방법(`git ls-remote`)
  - 자주 나는 오류의 원인과 해결
  - "현재 범위: 녹화·재현·AI 수정은 다음 단계"
- [ ] **Step 7: Commit** — `feat: reproof app init·doctor·build·inspect 명령과 안내 문서 추가`

---

### Task 13: 비공개 SPM 저장소 검증 (loopback 인증 git 서버)

**Files:**
- Create: `tests/simple_git_server.py`(테스트 도우미), `tests/test_simple_native_private_spm.py`, `examples/simple-ios/PrivateBadge/Package.swift`, `examples/simple-ios/PrivateBadge/Sources/PrivateBadge/Badge.swift`

**Interfaces:**
- Consumes: `resolve_packages`, `prepare_build`, `HostToolRunner`, `ToolRunner.run(..., env_extra=...)`
- Produces:
  - `class AuthenticatedGitServer`: context manager
    - `__init__(self, repositories_root: Path, *, username: str, token: str)`
    - `.url_for(name: str) -> str`
    - `127.0.0.1` 임의 포트에서 Basic 인증을 확인하고, 통과한 요청만 `git http-backend`에 CGI 환경(`GIT_PROJECT_ROOT`, `GIT_HTTP_EXPORT_ALL=1`, `PATH_INFO`, `REQUEST_METHOD`, `QUERY_STRING`, `CONTENT_TYPE`, `REMOTE_USER`)으로 넘긴다.
  - `class HomeOverrideRunner(HostToolRunner)`: `__init__(self, home: Path)`. `run()`이 호출자의 `env_extra`에 `HOME=<home>`을 더해 부모 `run()`을 부른다. 테스트에서 사용자의 실제 HOME 대신 임시 HOME을 쓰게 하려는 것이다.
  - `tests/simple_git_server.py`에 둔다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_private_package_resolves_with_existing_git_credentials(self):
    # PrivateBadge를 bare repo로 만들고 1.0.0 태그. MixedShop 복사본 project.yml에 packages.PrivateBadge(url, from 1.0.0) 추가.
    # 임시 HOME에 .gitconfig([credential] helper=store), .git-credentials(http://user:token@127.0.0.1:port) 작성.
    resolution = resolve_packages(..., runner=HomeOverrideRunner(temp_home))   # env_extra HOME=temp_home
    self.assertTrue(any(p.name.startswith("PrivateBadge") for p in (resolution.cache_dir / "checkouts").iterdir()))

def test_missing_credentials_fail_fast_with_redacted_url(self):
    # 빈 임시 HOME → SimpleError package_auth_failed, 메시지에 url_for("PrivateBadge.git") 포함, token 미포함
    # 60초 안에 끝난다(GIT_TERMINAL_PROMPT=0)

def test_tool_never_reads_credentials_file(self):
    # 원칙: 툴이 인증 파일을 직접 열지 않는다. builtins.open을 감싸 temp_home/.git-credentials 경로가 툴 프로세스(파이썬)에서 열리지 않음을 확인한다.
```

- [ ] **Step 2: 실패 확인** — Run: `python3 -m unittest tests.test_simple_native_private_spm -v`, Expected: FAIL
- [ ] **Step 3: 도우미 서버와 패키지 구현** — 사용자의 실제 HOME·keychain·SSH 키는 쓰지 않는다. 테스트가 끝나면 서버와 임시 HOME을 정리한다.
- [ ] **Step 4: 통과 확인** — 같은 명령, Expected: PASS 3개. 툴 코드에서 결함이 드러나면 해당 Task의 테스트에 회귀 테스트를 먼저 추가한 뒤 고친다. check `simple-native-private-spm`을 `IOS_SIMPLE_NATIVE`에 등록한다(effects에 `loopback` 포함).
- [ ] **Step 5: Commit** — `test: 인증이 필요한 비공개 SPM 저장소 해석 검증 추가`

---

### Task 14: Spike S3 — Wikipedia iOS 빌드·실행

**Files:**
- Create: `examples/simple-ios/wikipedia/pin.json`, `scripts/simple-fetch-app.py`, `tests/test_simple_fetch_app.py`,
  `docs/superpowers/specs/2026-11-01-ios-commercial-readiness-spike-results.md`

**Interfaces:**
- Produces:
  - `pin.json`: `{"repository": "https://github.com/wikimedia/wikipedia-ios.git", "commit": "<40자 SHA>"}`. 실행 시점의 `main` HEAD로 정해 기록한다.
  - `scripts/simple-fetch-app.py --pin FILE --output DIR`:
    - `git clone --filter=blob:none --no-checkout <repo> DIR`, 이어서 `git -C DIR checkout --detach <commit>`
    - 끝나면 `rev-parse HEAD`가 pin과 같은지 확인한다.
    - `DIR`이 이미 있으면 거절한다.

- [ ] **Step 1: fetch 스크립트 테스트 작성과 구현**
  - 로컬 bare repo를 pin 대상으로 써서 다음을 확인한다: 정상 checkout, 이미 있는 DIR 거절, SHA 불일치 거절.
  - check `simple-fetch-app`을 `IOS_SIMPLE_CONTRACTS`에 등록한다.

Run: `python3 -m unittest tests.test_simple_fetch_app -v`
Expected: PASS 3개

- [ ] **Step 2: Wikipedia 받기 (승인됨)**

Run: `python3 scripts/simple-fetch-app.py --pin examples/simple-ios/wikipedia/pin.json --output ~/.reproof/sources/wikipedia-ios`
Expected: 정상 종료, HEAD == pin

- [ ] **Step 3: init·build·inspect 실행과 측정**

Run (`~/.reproof/sources/wikipedia-ios`에서): `reproof app init`, `reproof app build`, `reproof app inspect`

결과 문서에 다음을 기록한다.
- init 질문 수와 사람이 직접 입력한 항목 수(목표 3개 이하)
- init 소요 시간(빌드 제외, 목표 10분 이내)
- 패키지 해석 시간, 빌드 시간, 성공 여부
- 첫 화면 계층의 interactive 요소 수

- [ ] **Step 4: 빌드 실패 대응**
  - 실패하면 원인을 결과 문서에 적는다. 예: SwiftLint build phase, 생성 파일, 스크립트.
  - 툴이 지원해야 할 일반적인 원인이면 해당 모듈 Task의 테스트 파일에 실패 테스트를 먼저 추가하고 고친다. TDD로 하고 커밋은 따로 한다.
  - Wikipedia만의 문제면 문서에 "검증 앱 설정"으로 기록하고, 툴 기본 동작은 바꾸지 않는다.
  - 2시간 안에 빌드하지 못하면 멈추고 결과 문서와 함께 사용자에게 보고한다.
- [ ] **Step 5: Commit** — `docs: Wikipedia iOS 빌드 spike 결과 기록`

---

### Task 15: Spike S1 — SwiftUI 요소 식별 방식 결정

**Files:**
- Create: `spikes/locator-coverage/coverage.py`(버림용, 테스트 없음), `spikes/locator-coverage/README.md`
- Modify: `docs/superpowers/specs/2026-11-01-ios-commercial-readiness-spike-results.md`

측정 대상과 방법(고정):
- 화면:
  - MixedShop: 장바구니, 옵션, 요약
  - Wikipedia: 첫 실행 온보딩, 설정, SwiftUI로 된 설정 하위 화면 1개 이상
  - 각 화면을 `reproof app inspect`로 2회 실행해 계층 JSON을 얻는다. 화면 이동은 `spikes/` 스크립트가 `RunnerClient`로 탭해서 한다.
- `coverage.py`는 interactive 요소마다 같은 화면의 다른 실행 계층에서 `resolve`를 돌려, 다음 비율을 낸다.
  - strong
  - `type-label-anchor`
  - `label-ordinal`
  - `position-ordinal`
  - 실패
  - 다른 요소로 잘못 해석된 경우(path와 label이 다름)
- **AI 수정 뒤 유지율:** MixedShop의 `OptionsView.swift`와 `CartViewController.swift`에 다음 변경을 각각 적용해 다시 빌드하고, 원래 evidence로 해석한다.
  - 해당 요소보다 위에 20줄짜리 코드 추가
  - 같은 화면의 다른 버튼 로직 변경
  - 해당 버튼의 action 내용 변경
- **결정 규칙:**
  - 모든 화면을 합쳐 strong + `type-label-anchor` + `label-ordinal`의 비율이 90% 이상이고, 잘못 해석이 0건이고, 수정 뒤 유지율이 95% 이상이면 계획 2는 **런타임 접근성 방식**(소스 재작성 없음)으로 간다.
  - 아니면 **소스 사본 재작성 방식**이 필요하다. 이 방식에는 SwiftSyntax 다운로드가 필요하고 이는 승인 범위 밖이므로, 측정값과 함께 사용자에게 승인을 요청한다.

- [ ] **Step 1: 계층 수집 스크립트와 coverage.py 작성**
- [ ] **Step 2: 측정 실행** — 결과 표를 결과 문서에 기록한다.
- [ ] **Step 3: 결정 기록** — 결정 규칙을 적용한 결과를 결과 문서와 spec 7장 spike 1 항목 아래에 적는다.
- [ ] **Step 4: Commit** — `docs: SwiftUI 요소 식별 spike 측정과 결정 기록`

---

### Task 16: Spike S2 — 직접 터치 때 앱 안에서 SwiftUI 요소 찾기

**Files:**
- Create: `spikes/inapp-touch/TouchProbe.swift`, `spikes/inapp-touch/apply.py`, `spikes/inapp-touch/README.md`(모두 버림용)
- Modify: 결과 문서

방법(고정):
- `apply.py`는 MixedShop **작업 사본**에 `TouchProbe.swift`를 Debug 전용으로 넣는다(원본 examples는 바꾸지 않는다).
- `TouchProbe`는 `UIApplication.sendEvent(_:)`를 교체해 touch `.ended`의 window 좌표를 얻는다. 그 좌표에서 key window부터 `accessibilityElements`와 subview를 재귀로 돌아, `accessibilityFrame`이 좌표를 포함하는 가장 깊은 요소를 찾는다. 찾은 요소의 `accessibilityLabel`·`accessibilityTraits`·`accessibilityIdentifier`·frame을 앱 데이터 컨테이너의 JSONL로 기록한다.
- 기본 상태에서 SwiftUI 요소가 보이지 않으면, 앱 안에서 접근성 트리를 활성화하는 방법 1가지를 시도하고 그 방법을 문서에 적는다. Debug 전용이고 공개 API가 아니어도 된다.
- 비교: Simulator에서 SwiftUI 요소 30개(옵션·요약 화면)를 `simctl io`가 아닌 **실제 Simulator 창 클릭**으로 누른다. 같은 좌표의 `element_at(runner hierarchy)` 결과와 type·label이 일치하는 비율을 잰다. 사람 클릭이 필요하므로 이 단계는 사용자 또는 실행자가 직접 한다. 자동화가 필요하면 `RunnerClient.tap_point`로 대신하고, 그 사실을 기록한다.
- **결정 규칙:**
  - 일치율이 95% 이상이면 계획 2는 **앱 안 해석**으로 간다.
  - 아니면 spec 6장의 **변환 실행**(녹화 후 같은 시작 조건에서 다시 돌려 XCUITest로 요소 조회)으로 간다.

- [ ] **Step 1: TouchProbe와 apply.py 작성**
- [ ] **Step 2: 측정 실행과 기록**
- [ ] **Step 3: 결정 기록** — 결과 문서와 spec 7장 spike 2 항목 아래에 적는다.
- [ ] **Step 4: Commit** — `docs: 앱 안 SwiftUI 터치 해석 spike 측정과 결정 기록`

---

### Task 17: 마무리 검증과 계획 2 입력 정리

- [ ] **Step 1: portable 전체 확인**

Run: `python3 scripts/release-check.py --goal IOS_SIMPLE_CONTRACTS --effects filesystem,process,loopback`
(Python 3.11은 `/Users/jinhongan/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11`, 3.12는 `/opt/homebrew/bin/python3.12`로 각각 실행한다.)
Expected: 모든 check 통과, exit 0

- [ ] **Step 2: native 전체 확인**

Run: `python3 scripts/release-check.py --goal IOS_SIMPLE_NATIVE --effects filesystem,process,native-compile,owned-device,loopback`
Expected: 모든 check 통과

- [ ] **Step 3: 기존 goal 회귀 확인**

Run: `G0`, `RUNTIME`, `CLI`(`--effects filesystem,process`), `PACKAGE`, `LOCATOR`(`--effects filesystem,process,loopback`)를 각각 실행한다.
Expected: 모두 통과. 실패하면 이번 변경이 원인인지 `main`과 비교해 확인한 뒤 고친다.

- [ ] **Step 4: 정리 확인**
  - `xcrun simctl list devices`에 `reproof-` 이외의 기기가 새로 생기거나 바뀌지 않았는지 확인한다.
  - 작업 중 띄운 실행기·xcodebuild 프로세스가 남아 있지 않은지 확인한다.
- [ ] **Step 5: 계획 2 입력 정리**
  - spike 결과 문서 끝에 "계획 2에 넘기는 결정"을 요약한다.
    - SwiftUI 식별 방식
    - 직접 터치 해석 방식
    - Wikipedia 빌드에서 드러난 지원 요소
    - 측정한 init 입력 수와 시간
  - 미검증으로 남은 항목도 적는다: Tuist, 물리 iPhone.
- [ ] **Step 6: Commit** — `docs: 간편 모드 계획 1 결과와 계획 2 입력 정리`

---

## 다음 계획 (이 계획이 끝난 뒤 spike 결과로 작성)

- **계획 2 — 녹화·판정·재현(로컬 모드):**
  - 브라우저 원격 조작 녹화(입력 직전 계층에서 evidence 저장)
  - 직접 터치 녹화(S2 결정대로)
  - 버그 지점 지정 UI(요소·속성·실제값/기대값, 크래시)
  - 저장 = 기준 확정
  - 원본 3회 재현과 판정, 준비 녹화와 시작 조건
  - MixedShop·Wikipedia에 버그 3개 심기
- **계획 3 — AI 수정 검증:**
  - 격리된 Claude CLI(`--allowedTools Read,Edit`, `--strict-mcp-config`, 별도 설정 디렉터리, cwd=작업 사본)
  - 수정 범위 거부 목록, 후보 자동 위험 검사(`blocked_risky_change`)
  - 기존 테스트 기준선, 후보 3회 재현, 기존 diff 재검증
  - Claude 호출은 수용 검증 전체에서 최대 약 20회
- **계획 4 — 공유 모드·QA 배포·수용:**
  - coordinator 연결(간편 모드 승인 규칙), 공용 Mac 등록 간소화
  - `.app` 전송 한도 조정, 녹화용 IPA와 배포 안내, 공유 시트 내보내기
  - 로컬·공유 동일 판정(같은 diff)
  - Wikipedia 수용 기준 전체, 문서·README·버전 정리
