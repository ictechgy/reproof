"""프로젝트 루트의 reproof.toml을 읽고 쓰는 모듈. 모든 간편 모드 명령이 이 설정을 읽는다."""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from reproof.simple.errors import SimpleError

CONFIG_FILE_NAME = "reproof.toml"
"""설정 파일 이름. 프로젝트 루트에 둔다."""

SCHEMA_VERSION = 1
"""지원하는 설정 스키마 버전. 형식이 바뀌면 올려 옛 파일을 명확히 거절한다."""

GENERATORS = ("none", "xcodegen", "tuist")
"""허용하는 프로젝트 생성기 목록."""

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
"""프로젝트 이름 규칙. 디렉터리·시뮬레이터 이름에 안전하게 쓰려는 소문자 slug.

fullmatch로만 쓴다: `$`는 끝의 개행을 허용해 "abc\\n"이 통과하기 때문이다."""

ENV_PREFIX = "env:"
"""launch.environment 값이 이 접두사로 시작하면 실행 시점 환경 변수에서 읽는다(비밀 값을 파일에 두지 않으려는 것)."""

ENV_NAME_PREFIX = "REPROOF_APP_"
"""env: 참조가 가리킬 수 있는 환경 변수 이름 접두사. AI가 만든 후보 코드가 앱 안에서 실행되므로,
호스트의 비밀(AWS 키 등)은 이 접두사로 이름을 붙여 명시적으로 내보낸 것만 앱에 전달한다."""

ALLOWED_KEYS: dict[str, set[str]] = {
    "": {"schema", "name", "project", "simulator", "launch", "build", "ai", "source"},
    "project": {"container", "generator", "generator_spec", "scheme", "app_target", "bundle_id", "test_scheme"},
    "simulator": {"device_type", "runtime", "locale", "language", "timezone"},
    "launch": {"arguments", "environment"},
    "build": {"timeout_seconds"},
    "ai": {"claude_consent"},
    "source": {"include"},
}
"""표별로 허용하는 키. 여기에 없는 키는 오타로 보고 거절한다."""


@dataclass(frozen=True)
class SimulatorSettings:
    """시뮬레이터 선택 값. device_type은 기기 이름, runtime은 'latest' 또는 버전, 나머지는 지역 설정."""

    device_type: str
    runtime: str
    locale: str = "ko_KR"
    language: str = "ko"
    timezone: str = "Asia/Seoul"


@dataclass(frozen=True)
class ProjectConfig:
    """reproof.toml 전체를 담는 불변 설정. 필드 의미는 TOML 키와 같다."""

    name: str
    container: str
    generator: str
    generator_spec: str | None
    scheme: str
    app_target: str
    bundle_id: str
    test_scheme: str | None
    simulator: SimulatorSettings
    launch_arguments: tuple[str, ...] = ()
    launch_environment: tuple[tuple[str, str], ...] = ()
    build_timeout_seconds: int = 3600
    claude_consent: bool = False
    source_include: tuple[str, ...] = ()
    """비밀 가능 이름이어도 작업 사본에 넣을 파일의 저장소 기준 상대 경로([source] include)."""

    @property
    def container_kind(self) -> str:
        """container 확장자로 'workspace' 또는 'project'를 돌려준다(xcodebuild 플래그 선택용)."""
        return "workspace" if self.container.endswith(".xcworkspace") else "project"


def _invalid(cause: str, remedy: str = "reproof.toml을 열어 해당 항목을 고친 뒤 다시 실행하세요.") -> SimpleError:
    """config_invalid 오류를 만든다. 모든 설정 검증 실패가 같은 코드를 쓰게 하려는 것이다."""
    return SimpleError("config_invalid", cause, remedy)


def _check_keys(table: Mapping[str, Any], section: str) -> None:
    """표 안에 허용되지 않은 키가 있으면 그 이름을 담아 거절한다."""
    unknown = sorted(set(table) - ALLOWED_KEYS[section])
    if unknown:
        where = f"[{section}]" if section else "최상위"
        raise _invalid(f"reproof.toml {where}에 알 수 없는 항목이 있습니다: {', '.join(unknown)}",
                       "오타인지 확인하고 해당 항목을 지우거나 올바른 이름으로 바꾸세요.")


def _section(root: Mapping[str, Any], key: str, required: bool = True) -> Mapping[str, Any]:
    """root에서 표 하나를 꺼내 타입과 키를 검증한다. 선택 표가 없으면 빈 표를 돌려준다."""
    if key not in root and not required:
        return {}
    table = root.get(key)
    if not isinstance(table, dict):
        raise _invalid(f"reproof.toml에 [{key}] 표가 없거나 형식이 틀렸습니다.")
    _check_keys(table, key)
    return table


def _field(table: Mapping[str, Any], key: str, kind: type, *, default: Any = ..., label: str | None = None) -> Any:
    """표에서 값 하나를 꺼내 타입을 확인한다. default가 없으면 필수 항목이다(bool은 int로 취급하지 않는다)."""
    label = label or key
    if key not in table:
        if default is ...:
            raise _invalid(f"reproof.toml에 필수 항목 {label}이(가) 없습니다.")
        return default
    value = table[key]
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise _invalid(f"reproof.toml의 {label} 값 형식이 틀렸습니다({kind.__name__} 필요).")
    return value


def _string_list(table: Mapping[str, Any], key: str, label: str) -> tuple[str, ...]:
    """문자열 배열을 튜플로 읽는다. 문자열이 아닌 원소가 있으면 거절한다."""
    values = _field(table, key, list, default=[], label=label)
    if not all(isinstance(item, str) for item in values):
        raise _invalid(f"reproof.toml의 {label}은(는) 문자열 목록이어야 합니다.")
    return tuple(values)


def _environment(table: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """[launch.environment] 표를 (이름, 값) 튜플로 읽는다. 순서는 파일 순서를 유지한다."""
    raw = _field(table, "environment", dict, default={}, label="launch.environment")
    if not all(isinstance(value, str) for value in raw.values()):
        raise _invalid("reproof.toml의 launch.environment 값은 모두 문자열이어야 합니다.")
    return tuple(raw.items())


def _env_reference_name(key: str, value: str) -> str | None:
    """값이 env: 참조면 변수 이름을 돌려준다(아니면 None). 접두사 규칙을 어기면 config_invalid를 낸다."""
    if not value.startswith(ENV_PREFIX):
        return None
    name = value[len(ENV_PREFIX):]
    if not name.startswith(ENV_NAME_PREFIX):
        raise _invalid(
            f"launch.environment의 {key}가 참조하는 환경 변수 '{name}'은(는) 허용되지 않습니다. "
            f"env: 참조는 {ENV_NAME_PREFIX}로 시작하는 변수만 쓸 수 있습니다.",
            f"앱에 전달할 값은 `export {ENV_NAME_PREFIX}이름=값`으로 따로 만들고 env:{ENV_NAME_PREFIX}이름 으로 참조하세요.")
    return name


CONTAINER_SUFFIXES = (".xcodeproj", ".xcworkspace")
"""project.container가 끝나야 하는 확장자."""

SPEC_SUFFIXES = {"xcodegen": (".yml", ".yaml"), "tuist": (".swift",)}
"""생성기별 project.generator_spec 확장자."""

_REQUIRED_NAMES = ("scheme", "app_target", "bundle_id")


def _check_inside_copy(value: str, label: str, suffixes: tuple[str, ...], example: str) -> None:
    """value가 작업 사본 안을 가리키는(빈 값·절대 경로·'..'가 아닌) 상대 POSIX 경로이고 suffixes로 끝나는지 확인한다.

    왜: 빌드·패키지 해석·생성은 작업 사본 기준 상대 경로로 돈다. 바깥을 가리키면 원본 저장소나 다른 폴더의
    프로젝트를 빌드·생성하게 되어, 원본을 건드리지 않는다는 약속이 깨진다.
    """
    path = PurePosixPath(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts:
        raise _invalid(f"reproof.toml의 {label} '{value}'은(는) 저장소 안의 상대 경로여야 합니다(빈 값·절대 경로·'..' 불가).",
                       f"저장소 루트 기준 경로로 고치세요. 예: {example}")
    if not value.endswith(suffixes):
        raise _invalid(f"reproof.toml의 {label} '{value}'은(는) {' 또는 '.join(suffixes)}로 끝나야 합니다.",
                       f"올바른 파일 경로로 고치세요. 예: {example}")


def _validate_project_paths(config: ProjectConfig) -> None:
    """container·generator_spec 경로와 scheme·app_target·bundle_id 빈 값 여부를 검사한다."""
    _check_inside_copy(config.container, "project.container", CONTAINER_SUFFIXES, 'container = "App.xcodeproj"')
    if config.generator_spec is not None:
        _check_inside_copy(config.generator_spec, "project.generator_spec", SPEC_SUFFIXES[config.generator],
                           'generator_spec = "project.yml"' if config.generator == "xcodegen"
                           else 'generator_spec = "Project.swift"')
    for field in _REQUIRED_NAMES:
        if not getattr(config, field).strip():
            raise _invalid(f"reproof.toml의 project.{field}이(가) 비어 있습니다.",
                           f"project.{field} 값을 채우거나 `reproof app init --force`로 설정을 다시 만드세요.")


def _check_include_path(value: str) -> None:
    """[source] include 항목이 저장소 안의 상대 POSIX 경로(빈 값·절대 경로·'..'·NUL 불가)인지 확인한다.

    왜: 이 목록의 파일은 비밀 가능 이름이어도 작업 사본에 들어가므로, 소스 밖이나 모호한 경로를 받으면 안 된다.
    """
    path = PurePosixPath(value)
    if not value.strip() or "\0" in value or path.is_absolute() or ".." in path.parts:
        raise _invalid(f"reproof.toml의 source.include 항목 {value!r}은(는) 저장소 안의 상대 경로여야 합니다"
                       "(빈 값·절대 경로·'..'·NUL 불가).",
                       '저장소 루트 기준 파일 경로로 고치세요. 예: include = ["Config/Dev.env"]')


def _validate_project(config: ProjectConfig) -> None:
    """이름 규칙, 생성기 값, 생성기-스펙 조합, 경로·필수 이름 등 값 자체의 규칙을 검사한다."""
    if not NAME_PATTERN.fullmatch(config.name):
        raise _invalid(f"name '{config.name}'은(는) 소문자·숫자·하이픈만 쓸 수 있고 40자 이하여야 합니다.",
                       "예: name = \"my-app\" 처럼 고치세요.")
    if config.generator not in GENERATORS:
        raise _invalid(f"project.generator '{config.generator}'은(는) 지원하지 않습니다.",
                       "none, xcodegen, tuist 중 하나로 고치세요.")
    if config.generator == "none" and config.generator_spec is not None:
        raise _invalid("project.generator_spec은 generator가 none이면 쓸 수 없습니다.",
                       "generator_spec 줄을 지우거나 generator를 xcodegen/tuist로 바꾸세요.")
    _validate_project_paths(config)
    if config.build_timeout_seconds <= 0:
        raise _invalid("build.timeout_seconds는 1 이상이어야 합니다.")
    for key, value in config.launch_environment:
        _env_reference_name(key, value)
    for value in config.source_include:
        _check_include_path(value)


def _build_config(root: Mapping[str, Any]) -> ProjectConfig:
    """검증된 TOML 사전에서 ProjectConfig를 조립한다."""
    project, sim = _section(root, "project"), _section(root, "simulator")
    launch = _section(root, "launch", required=False)
    build, ai = _section(root, "build", required=False), _section(root, "ai", required=False)
    source = _section(root, "source", required=False)
    return ProjectConfig(
        name=_field(root, "name", str),
        container=_field(project, "container", str, label="project.container"),
        generator=_field(project, "generator", str, label="project.generator"),
        generator_spec=_field(project, "generator_spec", str, default=None, label="project.generator_spec"),
        scheme=_field(project, "scheme", str, label="project.scheme"),
        app_target=_field(project, "app_target", str, label="project.app_target"),
        bundle_id=_field(project, "bundle_id", str, label="project.bundle_id"),
        test_scheme=_field(project, "test_scheme", str, default=None, label="project.test_scheme"),
        simulator=_build_simulator(sim),
        launch_arguments=_string_list(launch, "arguments", "launch.arguments"),
        launch_environment=_environment(launch),
        build_timeout_seconds=_field(build, "timeout_seconds", int, default=3600, label="build.timeout_seconds"),
        claude_consent=_field(ai, "claude_consent", bool, default=False, label="ai.claude_consent"),
        source_include=_string_list(source, "include", "source.include"),
    )


def _build_simulator(table: Mapping[str, Any]) -> SimulatorSettings:
    """[simulator] 표에서 SimulatorSettings를 만든다. 지역 설정은 기본값(한국)을 쓴다."""
    defaults = SimulatorSettings(device_type="", runtime="")
    return SimulatorSettings(
        device_type=_field(table, "device_type", str, label="simulator.device_type"),
        runtime=_field(table, "runtime", str, label="simulator.runtime"),
        locale=_field(table, "locale", str, default=defaults.locale, label="simulator.locale"),
        language=_field(table, "language", str, default=defaults.language, label="simulator.language"),
        timezone=_field(table, "timezone", str, default=defaults.timezone, label="simulator.timezone"),
    )


def load_config(path: Path) -> ProjectConfig:
    """reproof.toml을 읽어 검증한 ProjectConfig를 돌려준다.

    path: 설정 파일 경로. 파일이 없거나 TOML 문법/스키마/값이 틀리면 SimpleError를 낸다.
    왜 엄격한가: 오타가 조용히 무시되면 빌드 결과가 의도와 달라지기 때문이다.
    """
    try:
        root = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SimpleError("config_missing", f"{path} 파일이 없습니다.", "프로젝트 루트에서 init을 먼저 실행하세요.") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise _invalid(f"reproof.toml을 읽을 수 없습니다: {error}") from error
    _check_keys(root, "")
    if _field(root, "schema", int) != SCHEMA_VERSION:
        raise _invalid(f"schema 값이 {SCHEMA_VERSION}이(가) 아닙니다.", "지원하는 schema = 1로 고치세요.")
    config = _build_config(root)
    _validate_project(config)
    return config


def _quote(value: str) -> str:
    """문자열을 TOML 기본 문자열로 감싼다. JSON 이스케이프는 TOML과 호환되고 한글을 그대로 둔다."""
    return json.dumps(value, ensure_ascii=False)


def _render_list(values: tuple[str, ...]) -> str:
    """문자열 튜플을 TOML 배열 한 줄로 만든다."""
    return "[" + ", ".join(_quote(value) for value in values) + "]"


def _render_project(config: ProjectConfig) -> list[str]:
    """[project] 표 줄들을 만든다. 값이 None인 선택 항목은 줄 자체를 생략한다."""
    lines = ["[project]", f"container = {_quote(config.container)}", f"generator = {_quote(config.generator)}"]
    if config.generator_spec is not None:
        lines.append(f"generator_spec = {_quote(config.generator_spec)}")
    lines += [f"scheme = {_quote(config.scheme)}", f"app_target = {_quote(config.app_target)}",
              f"bundle_id = {_quote(config.bundle_id)}"]
    if config.test_scheme is not None:
        lines.append(f"test_scheme = {_quote(config.test_scheme)}")
    return lines


def render_config(config: ProjectConfig) -> str:
    """ProjectConfig를 정해진 키 순서의 TOML 문자열로 쓴다.

    왜 직접 쓰나: 표준 라이브러리에는 TOML writer가 없고, 키 순서가 고정돼야 diff가 안정적이다.
    환경 변수 표와 [source] 표는 비어 있으면 생략하며, load_config로 읽으면 같은 설정이 된다.
    """
    sim = config.simulator
    blocks = [
        [f"schema = {SCHEMA_VERSION}", f"name = {_quote(config.name)}"],
        _render_project(config),
        ["[simulator]", f"device_type = {_quote(sim.device_type)}", f"runtime = {_quote(sim.runtime)}",
         f"locale = {_quote(sim.locale)}", f"language = {_quote(sim.language)}", f"timezone = {_quote(sim.timezone)}"],
        ["[launch]", f"arguments = {_render_list(config.launch_arguments)}"],
    ]
    if config.launch_environment:
        blocks.append(["[launch.environment]"] + [f"{_quote(k)} = {_quote(v)}" for k, v in config.launch_environment])
    blocks.append(["[build]", f"timeout_seconds = {config.build_timeout_seconds}"])
    blocks.append(["[ai]", f"claude_consent = {'true' if config.claude_consent else 'false'}"])
    if config.source_include:
        blocks.append(["[source]", f"include = {_render_list(config.source_include)}"])
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


def config_digest(config: ProjectConfig) -> str:
    """설정 내용의 sha256 hex(64자)를 돌려준다. 설정이 바뀌면 이전 빌드를 재사용하지 않게 하는 지문이다."""
    payload = json.dumps(asdict(config), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_launch_environment(config: ProjectConfig, environ: Mapping[str, str]) -> dict[str, str]:
    """launch_environment의 'env:NAME' 값을 environ에서 찾아 실제 값으로 바꾼 사전을 돌려준다.

    왜: 토큰 같은 비밀 값을 설정 파일에 쓰지 않고 실행 시점에만 주입하려는 것이다.
    참조한 환경 변수가 없으면 변수 이름을 담은 SimpleError를 낸다.
    """
    resolved: dict[str, str] = {}
    for key, value in config.launch_environment:
        name = _env_reference_name(key, value)
        if name is None:
            resolved[key] = value
            continue
        if name not in environ:
            raise SimpleError("env_missing", f"launch.environment의 {key}가 환경 변수 {name}을(를) 참조하지만 설정돼 있지 않습니다.",
                              f"실행 전에 export {name}=값 으로 설정하세요.")
        resolved[key] = environ[name]
    return resolved
