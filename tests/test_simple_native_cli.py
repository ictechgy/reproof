"""`reproof app init → build → inspect`를 실제 Xcode·Simulator로 끝까지 검증하는 native 테스트.

MixedShop을 임시 폴더로 복사해(init이 reproof.toml을 쓰므로) 실행하고, 전용 Simulator `reproof-mixedshop`만 쓴다.
작업 공간(REPROOF_HOME)도 임시 폴더라 사용자의 ~/.reproof는 건드리지 않는다.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reproof.simple.cli import main
from reproof.simple.config import CONFIG_FILE_NAME, load_config
from reproof.simple.hierarchy import iter_elements, parse_hierarchy
from reproof.simple.process import HostToolRunner
from reproof.simple.simulator import OWNED_PREFIX

MIXEDSHOP = Path(__file__).resolve().parents[1] / "examples" / "simple-ios" / "MixedShop"


def run_cli(*argv: str) -> tuple[int, str, str]:
    """main을 같은 프로세스에서 실행하고 (종료 코드, stdout, stderr)를 돌려준다."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class SimpleNativeCliTests(unittest.TestCase):
    """MixedShop 복사본으로 init, build, inspect를 차례로 실행한다."""

    def setUp(self) -> None:
        """공백·한글이 든 임시 폴더에 MixedShop을 복사하고 REPROOF_HOME을 임시 작업 공간으로 돌린다."""
        tmp = tempfile.TemporaryDirectory(prefix="reproof CLI 검증 ")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.app = self.tmp / "MixedShop"
        shutil.copytree(MIXEDSHOP, self.app)
        environment = mock.patch.dict(os.environ, {"REPROOF_HOME": str(self.tmp / "home")})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(self._shutdown_owned_simulator)

    def _shutdown_owned_simulator(self) -> None:
        """테스트가 켠 reproof-mixedshop만 끈다. 이름이 정확히 일치하는 기기만 대상으로 한다."""
        runner = HostToolRunner()
        listing = json.loads(runner.run(["xcrun", "simctl", "list", "devices", "--json"], cwd=Path.cwd(), timeout=60))
        for group in listing["devices"].values():
            for item in group:
                if item["name"] == f"{OWNED_PREFIX}mixedshop" and item["state"] != "Shutdown":
                    runner.run(["xcrun", "simctl", "shutdown", item["udid"]], cwd=Path.cwd(), timeout=120)

    def add_button_center(self, hierarchy_file: Path) -> tuple[float, float]:
        """저장된 계층 JSON에서 '담기' 버튼의 중심 좌표를 구한다."""
        document = json.loads(hierarchy_file.read_text(encoding="utf-8"))
        root = parse_hierarchy(document["root"])
        add = next(e for e in iter_elements(root) if e.label == "담기" and e.element_type == "button")
        return add.frame.center()

    def test_init_build_inspect_end_to_end(self) -> None:
        """init은 질문 없이 설정을 쓰고, build는 빌드 ID를 보여 주며, inspect는 계층을 저장하고 '담기'를 weak로 되찾는다."""
        code, out, err = run_cli("init", "--path", str(self.app), "--non-interactive")
        self.assertEqual((code, err), (0, ""), out)
        config = load_config(self.app / CONFIG_FILE_NAME)
        self.assertEqual((config.scheme, config.bundle_id), ("MixedShop", "io.reproof.example.mixedshop"))
        self.assertIn("질문 0개", out)
        config_path = str(self.app / CONFIG_FILE_NAME)
        code, out, err = run_cli("build", "--config", config_path)
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("빌드 ID:", out)
        hierarchy_file = self.tmp / "계층 결과.json"
        code, out, err = run_cli("inspect", "--config", config_path, "--output", str(hierarchy_file))
        self.assertEqual((code, err), (0, ""), out)
        x, y = self.add_button_center(hierarchy_file)
        code, out, err = run_cli("inspect", "--config", config_path, "--output", str(hierarchy_file),
                                 "--point", f"{x},{y}")
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("weak", out)
        self.assertIn("type-label-anchor", out)


if __name__ == "__main__":
    unittest.main()
