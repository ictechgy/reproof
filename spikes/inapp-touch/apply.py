"""Spike S2(버림용, 테스트 없음): MixedShop 작업 사본에 TouchProbe를 Debug 전용으로 넣는다.

examples/simple-ios/MixedShop은 절대 고치지 않는다. 사본은 ~/.reproof/spikes/s2/source/MixedShop에 매번 지우고 새로
만든다. 사본의 project.yml에서 앱 타깃(MixedShop) sources에 `TouchProbe` 폴더를 더하고, Release 구성에서는
`EXCLUDED_SOURCE_FILE_NAMES`로 두 파일을 뺀다(파일 안에도 `#if DEBUG`가 있다). 바꿀 문자열이 정확히 한 번 나오지
않으면 바로 멈춘다(조용히 프로브가 빠진 빌드를 재지 않으려는 것).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MIXEDSHOP = REPO / "examples" / "simple-ios" / "MixedShop"
SPIKE_HOME = Path.home() / ".reproof" / "spikes" / "s2"
PROBE_FILES = ("TouchProbe.swift", "TouchProbeBootstrap.m")
PROBE_FOLDER = "TouchProbe"

# 앱 타깃 sources 줄(테스트 타깃은 다른 줄이라 한 번만 나온다).
APP_SOURCES = "    sources: [MixedShop]\n"
# 앱 타깃 base 설정의 마지막 줄. 바로 뒤에 Release 구성 설정을 붙인다(테스트 타깃은 YES라 겹치지 않는다).
APP_SETTINGS_TAIL = "        GENERATE_INFOPLIST_FILE: NO\n"
RELEASE_EXCLUSION = (
    "      configs:\n"
    "        Release:\n"
    f"          EXCLUDED_SOURCE_FILE_NAMES: \"{' '.join(PROBE_FILES)}\"\n"
)


def replace_once(path: Path, old: str, new: str) -> None:
    """path에서 old가 정확히 한 번 나오면 new로 바꾼다. 아니면 RuntimeError(원인과 확인할 곳)."""
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}에서 바꿀 문자열이 {count}번 나옵니다(1번이어야 함): {old!r}. "
                           "examples/simple-ios/MixedShop/project.yml이 바뀌었는지 확인하세요.")
    path.write_text(text.replace(old, new), encoding="utf-8")


def copy_source(destination: Path) -> None:
    """MixedShop을 destination으로 새로 복사한다(있으면 지우고). 생성된 .xcodeproj는 가져오지 않는다."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(MIXEDSHOP, destination, ignore=shutil.ignore_patterns("*.xcodeproj", ".build", "DerivedData"))


def install_probe(copy: Path) -> None:
    """프로브 파일을 사본의 TouchProbe/ 폴더에 넣고 project.yml을 고친다."""
    folder = copy / PROBE_FOLDER
    folder.mkdir()
    for name in PROBE_FILES:
        shutil.copyfile(HERE / name, folder / name)
    project = copy / "project.yml"
    replace_once(project, APP_SOURCES, f"    sources: [MixedShop, {PROBE_FOLDER}]\n")
    replace_once(project, APP_SETTINGS_TAIL, APP_SETTINGS_TAIL + RELEASE_EXCLUSION)


def main(argv: list[str]) -> int:
    """명령줄: apply.py [--spike-home 경로]. 만든 사본 경로를 출력한다."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spike-home", type=Path, default=SPIKE_HOME)
    arguments = parser.parse_args(argv)
    copy = arguments.spike_home.expanduser() / "source" / "MixedShop"
    copy_source(copy)
    install_probe(copy)
    print(f"TouchProbe를 넣은 사본: {copy}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
