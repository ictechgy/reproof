"""Spike S2(버림용, 테스트 없음): 앱 안 터치 해석(TouchProbe)과 실행기 계층의 element_at을 같은 좌표에서 비교한다.

명령(먼저 `apply.py`로 사본을 만든다):
  measure.py build                                   빌드하고 실행 파일에 프로브가 들어갔는지 확인
  measure.py taps --config default|activated         실행기 tap_point로 30번 탭(XCUITest가 붙은 상태)
  measure.py selfcheck --config default|activated    XCUITest 없이(simctl launch) 같은 30개 좌표를 앱 안에서만 해석
  measure.py report                                  일치율 표(Markdown) 출력 + details.json

측정 정의(측정 전에 정함):
- 앱 안 요소(A, 판정용): TouchProbe가 찾은 "가장 깊은 접근성 요소". 종류는 아래 대응표(`inapp_type`)로 정한다.
- 참고 요소(B): A에 element_at과 같은 승격 규칙(interactive가 아니면 앱 안 경로에서 가장 가까운 interactive 조상)을
  적용한 것. 판정에 쓰지 않는다.
- 일치: (앱 안 종류, label) == element_at(실행기 계층, 같은 좌표)의 (type, label). 앱 안에서 못 찾으면 불일치.
- 조건: taps(실행기 tap_point로 실제 터치, XCUITest 연결됨)와 selfcheck(XCUITest 없음, 터치 없이 같은 해석).
  selfcheck의 정답은 taps --config default 실행에서 화면별로 처음 읽은 계층(초기 상태)으로 계산한다.
- 판정 일치율: A 기준, 같은 앱 구성(기본, 또는 기본에서 SwiftUI 요소가 안 보일 때 활성화 방법 1가지)의 taps와
  selfcheck 중 낮은 값. 왜: 브리프의 조건(실제 창 클릭 = 실제 터치 경로 + XCUITest 없음)을 두 대체 측정이 각각
  절반씩만 갖기 때문에, 더 나쁜 쪽을 쓴다.

저장 위치: ~/.reproof/spikes/s2/runs/<구성>-<조건>/ (커밋하지 않음).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from reproof.simple.cli import launch_app, prepare_simulator  # noqa: E402
from reproof.simple.config import ProjectConfig, SimulatorSettings  # noqa: E402
from reproof.simple.hierarchy import INTERACTIVE_TYPES, Element, Frame, element_at, iter_elements, parse_hierarchy  # noqa: E402
from reproof.simple.pipeline import prepare_build  # noqa: E402
from reproof.simple.process import HostToolRunner  # noqa: E402
from reproof.simple.runner_client import RunnerClient, RunnerSession, build_runner, free_loopback_port  # noqa: E402
from reproof.simple.simulator import launch_locale_settings  # noqa: E402

SPIKE_HOME = Path.home() / ".reproof" / "spikes" / "s2"
SOURCE = SPIKE_HOME / "source" / "MixedShop"
WORKSPACE = SPIKE_HOME / "workspace"
RUNS = SPIKE_HOME / "runs"
PROBE_LOG = "touch-probe.jsonl"
CONFIGS = ("default", "activated")
RUN_ORDER = ("default-taps", "default-selfcheck", "activated-selfcheck", "activated-taps")
# 가장자리 탭이 요소 안쪽으로 들어가는 거리(pt).
INSET = 1.5
# 탭 뒤 동작·애니메이션이 끝나기를 기다리는 시간(초)과 화면 전환 대기 상한·간격(초).
TAP_SETTLE = 1.0
WAIT_TIMEOUT = 30.0
WAIT_INTERVAL = 0.5
SELFCHECK_TIMEOUT = 90.0

SUMMARY_TITLE = {"type": "staticText", "label": "주문 요약"}
REFRESH_TEXT = {"type": "staticText", "pattern": r"새로고침 \d+회"}
REFRESH_BUTTON = {"type": "button", "label": "새로고침"}
GIFT_TEXT = {"type": "staticText", "label": "선물 포장"}
GIFT_ROW = {"type": "switch", "label": "선물 포장"}
GIFT_KNOB = {"type": "switch", "label": ""}
TOTAL_TEXT = {"type": "staticText", "pattern": r"합계 \d+"}
MEMO_FIELD = {"type": "textField", "ordinal": 0}


def add_button(ordinal: int) -> dict:
    """옵션 화면의 ordinal번째(0=사과, 1=배, 2=귤 행) "추가" 버튼 조건."""
    return {"type": "button", "label": "추가", "ordinal": ordinal}


def product_text(name: str) -> dict:
    """상품 행 이름 Text 조건."""
    return {"type": "staticText", "label": name}


# 측정 탭 30개(측정 전에 정함): (id, 화면, 분류, 대상 조건, 위치). 위치는 탭 직전 계층의 대상 frame 기준이다.
MEASURED_TAPS = [
    ("S01", "summary", "text", SUMMARY_TITLE, "center"),
    ("S02", "summary", "text", REFRESH_TEXT, "center"),
    ("S03", "summary", "button", REFRESH_BUTTON, "center"),
    ("S04", "summary", "button", REFRESH_BUTTON, "left"),
    ("S05", "summary", "button", REFRESH_BUTTON, "right"),
    ("S06", "summary", "button", REFRESH_BUTTON, "top"),
    ("S07", "summary", "text", REFRESH_TEXT, "left"),
    ("S08", "summary", "text", SUMMARY_TITLE, "right"),
    ("S09", "summary", "button", REFRESH_BUTTON, "bottom"),
    ("S10", "summary", "text", SUMMARY_TITLE, "bottom"),
    ("S11", "summary", "text", REFRESH_TEXT, "right"),
    ("S12", "summary", "button", REFRESH_BUTTON, "center"),
    ("O01", "options", "text", GIFT_TEXT, "center"),
    ("O02", "options", "toggle", GIFT_KNOB, "center"),
    ("O03", "options", "toggle", GIFT_ROW, "center"),
    ("O04", "options", "toggle", GIFT_KNOB, "left"),
    ("O05", "options", "text", product_text("사과"), "center"),
    ("O06", "options", "button", add_button(0), "center"),
    ("O07", "options", "text", product_text("배"), "center"),
    ("O08", "options", "button", add_button(1), "center"),
    ("O09", "options", "text", product_text("귤"), "center"),
    ("O10", "options", "button", add_button(2), "center"),
    ("O11", "options", "button", add_button(0), "left"),
    ("O12", "options", "button", add_button(1), "right"),
    ("O13", "options", "button", add_button(2), "bottom"),
    ("O14", "options", "text", TOTAL_TEXT, "center"),
    ("O15", "options", "text", TOTAL_TEXT, "left"),
    ("O16", "options", "textField", MEMO_FIELD, "center"),
    ("O17", "options", "textField", MEMO_FIELD, "left"),
    ("O18", "options", "textField", MEMO_FIELD, "right"),
]
# 화면 이동(UIKit 탭 바·버튼, 측정에 넣지 않음): (탭할 요소 조건, 다음 화면 표시 조건).
NAVIGATION = {
    "summary": [({"type": "button", "label": "요약"}, SUMMARY_TITLE)],
    "options": [({"type": "button", "label": "장바구니"}, {"type": "button", "label": "옵션"}),
                ({"type": "button", "label": "옵션"}, GIFT_ROW)],
}

# 앱 안 객체 → XCUIElement 종류 대응표(측정 전에 정함, 먼저 맞는 규칙이 이긴다).
# 1) 공개 UIKit 클래스 계통(TouchProbe의 uikitClass). UIButton·UILabel·UIImageView는 trait으로 정한다.
CLASS_TYPES = {
    "UISwitch": "switch", "UISearchTextField": "searchField", "UITextField": "textField", "UITextView": "textView",
    "UISlider": "slider", "UISegmentedControl": "segmentedControl", "UICollectionViewCell": "cell",
    "UITableViewCell": "cell", "UINavigationBar": "navigationBar", "UITabBar": "tabBar", "UIWindow": "window",
    "UICollectionView": "collectionView", "UITableView": "table", "UIScrollView": "scrollView",
}
# 2) trait(공개 이름, 비공개는 bitN). 비공개 비트 이름은 추정이다: bit18=textEntry, bit24=secureTextField, bit47=textArea.
TEXT_ENTRY_BIT, SECURE_BIT, TEXT_AREA_BIT = "bit18", "bit24", "bit47"
TRAIT_TYPES = (("searchField", "searchField"), ("adjustable", "slider"), ("link", "link"), ("button", "button"),
               ("image", "image"), ("staticText", "staticText"))


# ---------------------------------------------------------------- 공통

def probe_config(config_name: str) -> ProjectConfig:
    """MixedShop 설정(S1 collect.py와 같은 값). activated면 시작 환경에 접근성 활성화 요청을 넣는다."""
    config = ProjectConfig(name="mixedshop", container="MixedShop.xcodeproj", generator="xcodegen",
                           generator_spec="project.yml", scheme="MixedShop", app_target="MixedShop",
                           bundle_id="io.reproof.example.mixedshop", test_scheme="MixedShopTests",
                           simulator=SimulatorSettings("iPhone 16", "latest"))
    if config_name == "activated":
        return replace(config, launch_environment=(("TOUCH_PROBE_ACTIVATE_AX", "1"),))
    return config


def build(runner: HostToolRunner, config: ProjectConfig):
    """사본을 빌드(완성 빌드는 재사용)하고 PreparedBuild를 돌려준다. 사본이 없으면 apply.py 안내."""
    if not (SOURCE / "TouchProbe" / "TouchProbe.swift").is_file():
        raise RuntimeError(f"{SOURCE}에 TouchProbe가 없습니다. 먼저 `python3 spikes/inapp-touch/apply.py`를 실행하세요.")
    return prepare_build(SOURCE, config, workspace_root=WORKSPACE, runner=runner)


def fresh_run_dir(name: str) -> Path:
    """runs/<name>을 비우고 새로 만든다(이전 결과와 섞이지 않게)."""
    output = RUNS / name
    if output.exists():
        shutil.rmtree(output)
    (output / "hierarchies").mkdir(parents=True)
    return output


def write_json(path: Path, value) -> None:
    """UTF-8 JSON으로 저장한다."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def read_json(path: Path):
    """UTF-8 JSON을 읽는다."""
    return json.loads(path.read_text(encoding="utf-8"))


def data_container(runner: HostToolRunner, udid: str, bundle_id: str) -> Path:
    """앱 데이터 컨테이너 경로(simctl get_app_container ... data)."""
    output = runner.run(["xcrun", "simctl", "get_app_container", udid, bundle_id, "data"], cwd=Path.cwd(), timeout=60)
    return Path(output.strip())


def read_probe_lines(path: Path) -> list[dict]:
    """JSONL을 줄마다 읽는다. 깨진 줄은 줄 번호와 함께 RuntimeError(조용히 건너뛰지 않는다)."""
    lines = []
    for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            lines.append(json.loads(text))
        except json.JSONDecodeError as error:
            raise RuntimeError(f"{path}:{number} 줄이 JSON이 아닙니다: {error}. 앱이 기록 도중 끝났는지 확인하세요.") from error
    return lines


def copy_probe_log(runner: HostToolRunner, udid: str, bundle_id: str, output: Path) -> Path:
    """앱 컨테이너의 JSONL을 run 폴더로 복사한다. 없으면 RuntimeError(프로브가 빌드에 없거나 설치 실패)."""
    source = data_container(runner, udid, bundle_id) / "Documents" / PROBE_LOG
    if not source.is_file():
        raise RuntimeError(f"{source}가 없습니다. 빌드에 TouchProbe가 들어갔는지(`measure.py build`) 확인하세요.")
    target = output / PROBE_LOG
    shutil.copyfile(source, target)
    return target


# ---------------------------------------------------------------- 대상 고르기

def matches(element: Element, condition: dict) -> bool:
    """element가 조건(type, label 또는 pattern, identifier)을 만족하는지."""
    if "type" in condition and element.element_type != condition["type"]:
        return False
    if "label" in condition and element.label != condition["label"]:
        return False
    if "pattern" in condition and not re.fullmatch(condition["pattern"], element.label):
        return False
    return "identifier" not in condition or element.identifier == condition["identifier"]


def find_target(root: Element, condition: dict) -> Element:
    """조건에 맞는 요소 중 ordinal번째(pre-order)를 돌려준다. ordinal이 없으면 정확히 하나여야 한다."""
    found = [element for element in iter_elements(root) if matches(element, condition)]
    ordinal = condition.get("ordinal")
    if ordinal is None and len(found) == 1:
        return found[0]
    if ordinal is not None and ordinal < len(found):
        return found[ordinal]
    raise RuntimeError(f"대상 조건 {condition}에 맞는 요소가 {len(found)}개라 고를 수 없습니다. 화면이 예상과 다른지 확인하세요.")


def position(frame: Frame, where: str) -> tuple[float, float]:
    """frame 안의 탭 좌표. center 또는 left/right/top/bottom(가장자리에서 INSET만큼 안쪽, 다른 축은 가운데)."""
    center_x, center_y = frame.center()
    points = {"center": (center_x, center_y), "left": (frame.x + INSET, center_y),
              "right": (frame.x + frame.width - INSET, center_y), "top": (center_x, frame.y + INSET),
              "bottom": (center_x, frame.y + frame.height - INSET)}
    return points[where]


def describe(element: Element | None) -> dict | None:
    """요소를 기록용 사전으로 바꾼다."""
    if element is None:
        return None
    frame = element.frame
    return {"type": element.element_type, "label": element.label, "identifier": element.identifier,
            "frame": [frame.x, frame.y, frame.width, frame.height], "path": list(element.path)}


def plan_point(root: Element, spec: tuple) -> dict:
    """측정 탭 하나의 좌표와 정답(element_at)을 계층 root에서 계산한다."""
    tap_id, screen, kind, condition, where = spec
    target = find_target(root, condition)
    point = position(target.frame, where)
    return {"id": tap_id, "screen": screen, "kind": kind, "position": where, "isMeasured": True,
            "point": list(point), "target": describe(target), "expected": describe(element_at(root, *point))}


# ---------------------------------------------------------------- taps (XCUITest tap_point)

def wait_for(client: RunnerClient, condition: dict) -> None:
    """조건에 맞는 요소가 계층에 나타날 때까지 기다린다. 시간이 지나면 RuntimeError."""
    started = time.monotonic()
    while time.monotonic() - started < WAIT_TIMEOUT:
        if any(matches(element, condition) for element in iter_elements(client.hierarchy())):
            return
        time.sleep(WAIT_INTERVAL)
    raise RuntimeError(f"{WAIT_TIMEOUT:.0f}초 안에 {condition}이(가) 나타나지 않았습니다. 세션 로그를 확인하세요.")


def save_document(client: RunnerClient, path: Path) -> Element:
    """실행기 계층 문서를 그대로 저장하고 루트 요소를 돌려준다."""
    document = client.command("hierarchy")
    write_json(path, document)
    return parse_hierarchy(document["root"])


def navigate(client: RunnerClient, screen: str) -> list[dict]:
    """화면 이동 탭(측정 밖)을 하고 기록을 돌려준다. 프로브가 이 탭도 기록하므로 순서 맞추기에 필요하다."""
    records = []
    for tap_condition, next_condition in NAVIGATION[screen]:
        root = client.hierarchy()
        point = find_target(root, tap_condition).frame.center()
        records.append({"id": f"nav-{tap_condition['label']}", "screen": "nav", "kind": "uikit", "isMeasured": False,
                        "point": list(point), "expected": describe(element_at(root, *point))})
        client.tap_point(*point)
        wait_for(client, next_condition)
    time.sleep(TAP_SETTLE)
    return records


def measure_tap(client: RunnerClient, spec: tuple, output: Path) -> dict:
    """탭 직전 계층을 저장하고 좌표·정답을 계산한 뒤 tap_point로 탭한다."""
    root = save_document(client, output / "hierarchies" / f"{spec[0]}.json")
    record = plan_point(root, spec)
    client.tap_point(*record["point"])
    time.sleep(TAP_SETTLE)
    print(f"  {record['id']} {record['point']} → {record['expected']['type']} {record['expected']['label']!r}", flush=True)
    return record


def drive_taps(client: RunnerClient, output: Path) -> list[dict]:
    """요약 → 옵션 순서로 이동하며 30번 탭한다. 화면마다 첫 계층을 <화면>-initial.json으로 남긴다."""
    wait_for(client, {"type": "button", "label": "담기"})
    records: list[dict] = []
    for screen in ("summary", "options"):
        records += navigate(client, screen)
        save_document(client, output / "hierarchies" / f"{screen}-initial.json")
        records += [measure_tap(client, spec, output) for spec in MEASURED_TAPS if spec[1] == screen]
    return records


def run_taps(config_name: str) -> None:
    """taps 조건 한 번: 초기화·설치 → 실행기 → 앱 실행 → 30번 탭 → 프로브 기록 복사."""
    runner, config = HostToolRunner(), probe_config(config_name)
    prepared = build(runner, config)
    output = fresh_run_dir(f"{config_name}-taps")
    udid = prepare_simulator(config, prepared, runner)
    products = build_runner(Path.home() / ".reproof" / "runner", runner)
    session = RunnerSession(products, udid=udid, target_bundle=config.bundle_id, port=free_loopback_port(),
                            log_path=output / "session.log")
    with session as client:
        launch_app(client, config)
        records = drive_taps(client, output)
        time.sleep(TAP_SETTLE)
        copy_probe_log(runner, udid, config.bundle_id, output)
    write_json(output / "taps.json", records)
    write_json(output / "meta.json", {"build_id": prepared.build_id, "udid": udid, "config": config_name})


# ---------------------------------------------------------------- selfcheck (XCUITest 없음)

def selfcheck_plan() -> list[dict]:
    """taps --config default의 화면별 초기 계층에서 30개 좌표와 정답을 계산한다(selfcheck는 동작 없이 초기 상태)."""
    hierarchies = RUNS / "default-taps" / "hierarchies"
    records = []
    for screen in ("summary", "options"):
        path = hierarchies / f"{screen}-initial.json"
        if not path.is_file():
            raise RuntimeError(f"{path}가 없습니다. 먼저 `measure.py taps --config default`를 실행하세요.")
        root = parse_hierarchy(read_json(path)["root"])
        specs = [spec for spec in MEASURED_TAPS if spec[1] == screen]
        records += [{**plan_point(root, spec), "index": index} for index, spec in enumerate(specs)]
    return records


def launch_without_xcuitest(runner: HostToolRunner, udid: str, config: ProjectConfig, records: list[dict]) -> None:
    """simctl launch로 앱을 띄운다(XCUITest 없음). 좌표 계획·로케일·구성 환경은 SIMCTL_CHILD_ 변수로 넘긴다."""
    arguments, environment = launch_locale_settings(config.simulator)
    plan = {screen: [record["point"] for record in records if record["screen"] == screen]
            for screen in ("summary", "options")}
    child = {**environment, **dict(config.launch_environment), "TOUCH_PROBE_SELFCHECK": json.dumps(plan)}
    runner.run(["xcrun", "simctl", "launch", "--terminate-running-process", udid, config.bundle_id, *arguments],
               cwd=Path.cwd(), timeout=60, env_extra={f"SIMCTL_CHILD_{key}": value for key, value in child.items()})


def wait_for_selfcheck(log_path: Path) -> None:
    """프로브가 selfcheck-done을 쓸 때까지 기다린다. selfcheck-error면 그 사유로 RuntimeError."""
    started = time.monotonic()
    while time.monotonic() - started < SELFCHECK_TIMEOUT:
        events = read_probe_lines(log_path) if log_path.is_file() else []
        errors = [line["reason"] for line in events if line.get("event") == "selfcheck-error"]
        if errors:
            raise RuntimeError(f"self-check가 실패했습니다: {errors[0]}")
        if any(line.get("event") == "selfcheck-done" for line in events):
            return
        time.sleep(1.0)
    raise RuntimeError(f"{SELFCHECK_TIMEOUT:.0f}초 안에 self-check가 끝나지 않았습니다. 앱이 실행됐는지 확인하세요.")


def run_selfcheck(config_name: str) -> None:
    """selfcheck 조건 한 번: 초기화·설치 → simctl launch(실행기 없음) → 끝 표시 대기 → 기록 복사 → 앱 종료."""
    records = selfcheck_plan()
    runner, config = HostToolRunner(), probe_config(config_name)
    prepared = build(runner, config)
    output = fresh_run_dir(f"{config_name}-selfcheck")
    udid = prepare_simulator(config, prepared, runner)
    launch_without_xcuitest(runner, udid, config, records)
    wait_for_selfcheck(data_container(runner, udid, config.bundle_id) / "Documents" / PROBE_LOG)
    copy_probe_log(runner, udid, config.bundle_id, output)
    runner.run(["xcrun", "simctl", "terminate", udid, config.bundle_id], cwd=Path.cwd(), timeout=60)
    write_json(output / "taps.json", records)
    write_json(output / "meta.json", {"build_id": prepared.build_id, "udid": udid, "config": config_name})


# ---------------------------------------------------------------- build 확인

def run_build() -> None:
    """빌드하고 실행 파일에 프로브 클래스 이름이 들어갔는지 확인한다(#if DEBUG·Release 제외 설정 점검).

    Xcode Debug 빌드는 앱 코드를 `MixedShop.debug.dylib`에 넣고 `MixedShop`은 얇은 시작 파일만 두므로 둘 다 본다.
    """
    runner = HostToolRunner()
    prepared = build(runner, probe_config("default"))
    binaries = [prepared.products.app_path / "MixedShop", *prepared.products.app_path.glob("*.dylib")]
    symbols = "".join(subprocess.run(["nm", "-gj", str(binary)], capture_output=True, text=True, check=True).stdout
                      for binary in binaries)
    names = ("MixedShopTouchProbe", "MixedShopTouchProbeBootstrap")
    missing = [name for name in names if f"_OBJC_CLASS_$_{name}" not in symbols]
    print(f"빌드: {prepared.build_id} {prepared.products.app_path}")
    if missing:
        raise RuntimeError(f"실행 파일에 {missing} 클래스가 없습니다. 사본 project.yml과 #if DEBUG 조건을 확인하세요.")
    print(f"프로브 클래스 확인: {', '.join(names)}")


# ---------------------------------------------------------------- report

def inapp_type(node: dict | None) -> str | None:
    """앱 안 객체 기록을 XCUIElement 종류 이름으로 바꾼다(위 대응표). node가 없으면 None."""
    if not node:
        return None
    if node.get("uikitClass") in CLASS_TYPES:
        return CLASS_TYPES[node["uikitClass"]]
    names = set(node.get("traitNames", []))
    if "toggleButton" in names:
        return "switch"
    if TEXT_ENTRY_BIT in names:
        return "secureTextField" if SECURE_BIT in names else "searchField" if "searchField" in names \
            else "textView" if TEXT_AREA_BIT in names else "textField"
    return next((kind for name, kind in TRAIT_TYPES if name in names), "other")


def promoted(chain: list[dict]) -> dict | None:
    """element_at과 같은 승격: 가장 깊은 요소부터 위로 올라가며 처음 만나는 interactive 종류. 없으면 가장 깊은 요소."""
    for node in reversed(chain):
        if inapp_type(node) in INTERACTIVE_TYPES:
            return node
    return chain[-1] if chain else None


def pair(node: dict | None) -> tuple[str, str] | None:
    """(앱 안 종류, label). 요소가 없으면 None."""
    return (inapp_type(node), node.get("label", "")) if node else None


def align(run: str, records: list[dict], lines: list[dict]) -> list[tuple[dict, dict]]:
    """기록(탭)과 프로브 줄을 맞춘다. taps는 터치 순서·좌표로, selfcheck는 (화면, 순번)으로 맞춘다."""
    if run.endswith("-taps"):
        touches = [line for line in lines if line["event"] == "touch"]
        if len(touches) != len(records):
            raise RuntimeError(f"{run}: 탭 {len(records)}번인데 터치 기록이 {len(touches)}개입니다.")
        for record, touch in zip(records, touches):
            if max(abs(a - b) for a, b in zip(record["point"], touch["windowPoint"])) > 1.0:
                raise RuntimeError(f"{run} {record['id']}: 탭 {record['point']}과 터치 {touch['windowPoint']}가 다릅니다.")
        return list(zip(records, touches))
    checks = {(line["screen"], line["index"]): line for line in lines if line["event"] == "selfcheck"}
    return [(record, checks[(record["screen"], record["index"])]) for record in records]


def classify(record: dict, line: dict) -> dict:
    """탭 하나의 A·B 결과를 만든다."""
    expected = (record["expected"]["type"], record["expected"]["label"]) if record["expected"] else None
    found = line.get("found") or None
    reference = promoted(line.get("chain", []))
    return {"id": record["id"], "screen": record["screen"], "kind": record["kind"],
            "position": record.get("position", ""), "point": record["point"], "expected": expected,
            "a": pair(found), "aClass": (found or {}).get("class", ""), "aIsMatch": pair(found) == expected,
            "b": pair(reference), "bClass": (reference or {}).get("class", ""), "bIsMatch": pair(reference) == expected,
            "hits": len(line.get("hits", [])), "elapsedMs": line.get("elapsedMs"), "axState": line.get("axState")}


def ratio(rows: list[dict], key: str) -> str:
    """일치 수/전체(%)."""
    hits = sum(1 for row in rows if row[key])
    return f"{hits}/{len(rows)} ({100 * hits / len(rows):.1f}%)" if rows else "-"


def short(value: tuple | None) -> str:
    """(종류, label)을 표 칸 문자열로."""
    return "(없음)" if value is None else f"{value[0]} `{value[1]}`"


def print_run(run: str, rows: list[dict], start: dict) -> None:
    """run 하나의 탭별 표와 요약을 Markdown으로 출력한다."""
    print(f"\n### {run}\n")
    print(f"probe-start: activation={json.dumps(start.get('activation'), ensure_ascii=False)} "
          f"axState={json.dumps(start.get('axState'))}\n")
    print("| id | 분류 | 위치 | 좌표 | element_at | 앱 안 A (클래스) | A | 참고 B | B |")
    print("|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        point = f"{row['point'][0]:.1f},{row['point'][1]:.1f}"
        print(f"| {row['id']} | {row['kind']} | {row['position']} | {point} | {short(row['expected'])} | "
              f"{short(row['a'])} ({row['aClass']}) | {'✓' if row['aIsMatch'] else '✗'} | "
              f"{short(row['b'])} | {'✓' if row['bIsMatch'] else '✗'} |")


def summarize(run: str, rows: list[dict]) -> dict:
    """run 하나의 요약 수치(A·B 전체, 화면·분류별, 시간)."""
    measured = [row for row in rows if row["screen"] != "nav"]
    times = [row["elapsedMs"] for row in measured if row["elapsedMs"] is not None]
    groups = {"summary": [r for r in measured if r["screen"] == "summary"],
              "options": [r for r in measured if r["screen"] == "options"],
              "text": [r for r in measured if r["kind"] == "text"],
              "edge": [r for r in measured if r["position"] != "center"]}
    return {"run": run, "a": ratio(measured, "aIsMatch"), "b": ratio(measured, "bIsMatch"),
            "aCount": sum(1 for r in measured if r["aIsMatch"]), "total": len(measured),
            **{f"a_{name}": ratio(group, "aIsMatch") for name, group in groups.items()},
            **{f"b_{name}": ratio(group, "bIsMatch") for name, group in groups.items()},
            "msMedian": round(statistics.median(times), 1) if times else None,
            "msMax": round(max(times), 1) if times else None}


def screen_evidence(classes: dict[str, int]) -> str:
    """census의 방문 클래스에서 화면이 떠 있었다는 근거(SwiftUI 호스팅 view·List 셀·탭 버튼 수)를 뽑는다."""
    keys = {"hosting": "_UIHostingView", "cell": "ListCollectionViewCell", "tabButton": "_UITabButton"}
    counts = {name: sum(count for cls, count in classes.items() if marker in cls) for name, marker in keys.items()}
    return ", ".join(f"{name} {count}" for name, count in counts.items())


def census_lines(lines: list[dict]) -> list[str]:
    """selfcheck census에서 화면별 방문 객체 수, 화면 근거, 접근성 요소 수와 label 목록을 문자열로."""
    texts = []
    for line in (line for line in lines if line["event"] == "census"):
        labels = [element["label"] for element in line["elements"] if element["label"]]
        texts.append(f"census {line['screen']}: 방문 {line['visitedCount']}개({screen_evidence(line.get('visitedClasses', {}))}), "
                     f"접근성 요소 {len(line['elements'])}개, label {labels}, axState {json.dumps(line.get('axState'))}")
    return texts


def run_report() -> None:
    """있는 run을 모두 읽어 표와 요약을 출력하고 details.json을 쓴다."""
    details, summaries = {}, []
    for run in RUN_ORDER:
        if not (RUNS / run / "taps.json").is_file():
            continue
        lines = read_probe_lines(RUNS / run / PROBE_LOG)
        rows = [classify(record, line) for record, line in align(run, read_json(RUNS / run / "taps.json"), lines)]
        start = next((line for line in lines if line["event"] == "probe-start"), {})
        print_run(run, rows, start)
        for text in census_lines(lines):
            print(f"\n{text}")
        summaries.append(summarize(run, rows))
        details[run] = {"rows": rows, "start": start}
    print("\n### 요약\n")
    print("| run | A(판정) | B(참고) | A 요약 | A 옵션 | A Text | A 가장자리 | B Text | 해석 ms(중앙/최대) |")
    print("|---|---|---|---|---|---|---|---|---|")
    for item in summaries:
        print(f"| {item['run']} | {item['a']} | {item['b']} | {item['a_summary']} | {item['a_options']} | "
              f"{item['a_text']} | {item['a_edge']} | {item['b_text']} | {item['msMedian']}/{item['msMax']} |")
    write_json(SPIKE_HOME / "details.json", {"details": details, "summaries": summaries})


def main(argv: list[str]) -> int:
    """명령줄 진입점. 모듈 설명 참고."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["build", "taps", "selfcheck", "report"])
    parser.add_argument("--config", choices=CONFIGS, default="default")
    arguments = parser.parse_args(argv)
    if arguments.command == "build":
        run_build()
    elif arguments.command == "taps":
        run_taps(arguments.config)
    elif arguments.command == "selfcheck":
        run_selfcheck(arguments.config)
    else:
        run_report()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
