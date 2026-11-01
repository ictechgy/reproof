"""Spike S1(버림용, 테스트 없음): 앱을 `reproof app inspect`와 같은 순서로 띄우고, 탭으로 화면을 옮겨 가며 계층 JSON을 저장한다.

한 번의 실행(run)은 inspect와 같은 함수를 같은 순서로 쓴다: prepare_build(완성 빌드 재사용) → prepare_simulator
(전용 Simulator 초기화·표시 고정·설치) → build_runner → RunnerSession → launch_app(로케일 인자, 3초 대기).
그 뒤 앱별 단계 목록대로 요소를 label·identifier로 찾아 중심을 탭하고, 화면마다 계층 문서를 저장한다.
run마다 Simulator를 초기화하므로 run-a와 run-b는 서로 독립된 새 설치·새 실행이다.

저장 위치: ~/.reproof/spikes/locator-coverage/dumps/<앱>/<변형>/<run>/<화면>.json (+ meta.json). 커밋하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from reproof.simple.cli import launch_app, prepare_simulator  # noqa: E402
from reproof.simple.config import ProjectConfig, SimulatorSettings, load_config  # noqa: E402
from reproof.simple.hierarchy import Element, iter_elements, parse_hierarchy  # noqa: E402
from reproof.simple.pipeline import prepare_build  # noqa: E402
from reproof.simple.process import HostToolRunner  # noqa: E402
from reproof.simple.runner_client import RunnerClient, RunnerSession, build_runner, free_loopback_port  # noqa: E402

SPIKE_HOME = Path.home() / ".reproof" / "spikes" / "locator-coverage"
WIKIPEDIA_SOURCE = Path.home() / ".reproof" / "sources" / "wikipedia-ios"
# 명령 하나의 응답 대기(초). 화면이 idle이 되지 않는 앱(Wikipedia 홈)은 XCUITest가 탭 앞뒤로 60초씩 기다린다.
# 실행기 기본값(30초)으로는 응답 전에 끊겨 다음 명령이 밀리므로 spike에서만 늘린다(결과 문서의 발견 항목 참고).
COMMAND_TIMEOUT = 300.0
# 탭 뒤 계층을 읽기 전에 화면 전환 애니메이션이 끝나기를 기다리는 시간(초).
SETTLE_SECONDS = 1.5
# 다음 화면 표시를 기다리는 최대 시간(초)과 확인 간격(초).
WAIT_TIMEOUT = 90.0
WAIT_INTERVAL = 1.0


def mixedshop_config() -> ProjectConfig:
    """MixedShop 설정(native 테스트와 같은 값). 사본 폴더에 reproof.toml을 쓰지 않으려고 코드로 만든다."""
    return ProjectConfig(name="mixedshop", container="MixedShop.xcodeproj", generator="xcodegen",
                         generator_spec="project.yml", scheme="MixedShop", app_target="MixedShop",
                         bundle_id="io.reproof.example.mixedshop", test_scheme="MixedShopTests",
                         simulator=SimulatorSettings("iPhone 16", "latest"))


def button(label: str) -> dict:
    """label로 버튼을 찾는 조건."""
    return {"type": "button", "label": label}


def by_id(identifier: str, element_type: str | None = None) -> dict:
    """identifier(와 종류)로 찾는 조건."""
    return {"identifier": identifier, **({"type": element_type} if element_type else {})}


# 화면 이동 단계. ("tap", 조건, 다음 화면 표시 조건) 또는 ("dump", 화면 이름).
MIXEDSHOP_STEPS = [
    ("wait", button("담기")), ("dump", "cart"),
    ("tap", button("옵션"), {"type": "switch", "label": "선물 포장"}), ("dump", "options"),
    ("tap", button("요약"), {"type": "staticText", "label": "주문 요약"}), ("dump", "summary"),
]
NEXT = by_id("App Onboarding Next Button", "button")
WIKIPEDIA_STEPS = [
    ("wait", by_id("App Onboarding Intro View")), ("dump", "onboarding-intro"),
    ("tap", NEXT, by_id("App Onboarding Data Privacy View")), ("dump", "onboarding-privacy"),
    ("tap", NEXT, by_id("App Onboarding Languages View")), ("dump", "onboarding-languages"),
    ("tap", NEXT, by_id("App Onboarding Personalization Intro View")), ("dump", "onboarding-personalization"),
    ("tap", NEXT, by_id("Interests View")), ("dump", "onboarding-interests"),
    ("tap", by_id("App Onboarding Skip Button", "button"), by_id("profile-button", "button")), ("dump", "home"),
    ("tap", by_id("profile-button", "button"), by_id("Profile View")), ("dump", "profile"),
    ("tap", button("설정"), by_id("설정", "navigationBar")), ("dump", "settings"),
    ("tap", button("검색"), by_id("검색", "navigationBar")), ("dump", "settings-search"),
    ("tap", by_id("BackButton", "button"), button("문서의 저장 및 동기화")),
    ("tap", button("문서의 저장 및 동기화"), {"type": "navigationBar", "identifier": "문서의 저장 및 동기화"}),
    ("dump", "settings-storage"),
]


def matches(element: Element, condition: dict) -> bool:
    """element가 조건(type·label·identifier 중 주어진 것)을 모두 만족하는지 돌려준다."""
    values = {"type": element.element_type, "label": element.label, "identifier": element.identifier}
    return all(values[key] == expected for key, expected in condition.items())


def find_unique(root: Element, condition: dict) -> Element:
    """조건에 맞는 요소가 정확히 하나면 돌려준다. 0개·여러 개면 RuntimeError(엉뚱한 화면으로 가지 않으려는 것)."""
    found = [element for element in iter_elements(root) if matches(element, condition)]
    if len(found) != 1:
        raise RuntimeError(f"이동 조건 {condition}에 맞는 요소가 {len(found)}개입니다(1개여야 함).")
    return found[0]


def wait_for(client: RunnerClient, condition: dict) -> float:
    """조건에 맞는 요소가 계층에 나타날 때까지 기다리고 걸린 초를 돌려준다. 시간이 지나면 RuntimeError."""
    started = time.monotonic()
    while time.monotonic() - started < WAIT_TIMEOUT:
        if any(matches(element, condition) for element in iter_elements(client.hierarchy())):
            return time.monotonic() - started
        time.sleep(WAIT_INTERVAL)
    raise RuntimeError(f"{WAIT_TIMEOUT:.0f}초 안에 {condition}이(가) 나타나지 않았습니다.")


def dump(client: RunnerClient, path: Path) -> int:
    """전환이 끝나도록 잠시 기다린 뒤 실행기 계층 문서를 그대로 저장하고 요소 수를 돌려준다."""
    time.sleep(SETTLE_SECONDS)
    document = client.command("hierarchy")
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    return sum(1 for _ in iter_elements(parse_hierarchy(document["root"])))


def run_steps(client: RunnerClient, steps: list, output: Path) -> list[dict]:
    """단계 목록을 실행하고 단계별 기록(종류, 대상, 걸린 초, 요소 수)을 돌려준다."""
    records = []
    for step in steps:
        started = time.monotonic()
        record = {"step": step[0], "target": step[1]}
        if step[0] == "dump":
            record["elements"] = dump(client, output / f"{step[1]}.json")
        elif step[0] == "wait":
            wait_for(client, step[1])
        else:
            client.tap_point(*find_unique(client.hierarchy(), step[1]).frame.center())
            record["waited_for_next"] = wait_for(client, step[2])
        record["seconds"] = round(time.monotonic() - started, 1)
        print(f"  {record}", flush=True)
        records.append(record)
    return records


def collect_run(config: ProjectConfig, prepared, runner: HostToolRunner, steps: list, output: Path) -> dict:
    """새 초기화·설치·실행으로 run 하나를 수집하고 meta 사전을 돌려준다."""
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    udid = prepare_simulator(config, prepared, runner)
    products = build_runner(Path.home() / ".reproof" / "runner", runner)
    session = RunnerSession(products, udid=udid, target_bundle=config.bundle_id, port=free_loopback_port(),
                            log_path=output / "session.log")
    with session as client:
        client._timeout = COMMAND_TIMEOUT  # spike 전용: 위 COMMAND_TIMEOUT 설명 참고.
        launch_app(client, config)
        records = run_steps(client, steps, output)
    return {"build_id": prepared.build_id, "app": str(prepared.products.app_path), "udid": udid,
            "seconds": round(time.monotonic() - started, 1), "steps": records}


def app_setup(app: str, variant: str, spike_home: Path):
    """(설정, 소스 루트, 작업 공간 루트, 단계)를 돌려준다. MixedShop은 edits.py가 만든 변형 사본을 쓴다."""
    if app == "mixedshop":
        source = spike_home / "sources" / variant / "MixedShop"
        if not source.is_dir():
            raise RuntimeError(f"{source}가 없습니다. 먼저 `python3 spikes/locator-coverage/edits.py {variant}`를 실행하세요.")
        return mixedshop_config(), source, spike_home / "workspace", MIXEDSHOP_STEPS
    config_path = WIKIPEDIA_SOURCE / "reproof.toml"
    return load_config(config_path), WIKIPEDIA_SOURCE, Path.home() / ".reproof", WIKIPEDIA_STEPS


def main(argv: list[str]) -> int:
    """명령줄: collect.py {mixedshop,wikipedia} [--variant 이름] [--runs run-a run-b]."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("app", choices=["mixedshop", "wikipedia"])
    parser.add_argument("--variant", default="baseline")
    parser.add_argument("--runs", nargs="+", default=["run-a", "run-b"])
    parser.add_argument("--spike-home", type=Path, default=SPIKE_HOME)
    arguments = parser.parse_args(argv)
    spike_home = arguments.spike_home.expanduser()
    config, source, workspace, steps = app_setup(arguments.app, arguments.variant, spike_home)
    runner = HostToolRunner()
    prepared = prepare_build(source, config, workspace_root=workspace, runner=runner)
    for run in arguments.runs:
        output = spike_home / "dumps" / arguments.app / arguments.variant / run
        print(f"{arguments.app}/{arguments.variant}/{run} 수집 시작", flush=True)
        meta = collect_run(config, prepared, runner, steps, output)
        (output / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{arguments.app}/{arguments.variant}/{run} 완료: {meta['seconds']}초", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
