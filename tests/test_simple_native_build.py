"""간편 모드 빌드 파이프라인을 실제 Xcode로 검증하는 native 테스트(Xcode 없으면 skip하지 않고 실패한다)."""
from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

from reproof.simple.config import ProjectConfig, SimulatorSettings
from reproof.simple.pipeline import prepare_build
from reproof.simple.process import HostToolRunner

MIXEDSHOP = Path(__file__).resolve().parents[1] / "examples" / "simple-ios" / "MixedShop"


class SimpleNativeBuildTests(unittest.TestCase):
    """XcodeGen·SwiftUI+UIKit·로컬 패키지 앱이 prepare_build로 실제 빌드되는지 확인한다."""

    def test_pipeline_builds_mixedshop_with_xcodegen_and_local_package(self) -> None:
        """MixedShop을 임시 작업 공간에 빌드하고 .app의 bundle id를 확인한다. 왜: 첫 실제 xcodebuild 증거다."""
        config = ProjectConfig(name="mixedshop", container="MixedShop.xcodeproj", generator="xcodegen",
                               generator_spec="project.yml", scheme="MixedShop", app_target="MixedShop",
                               bundle_id="io.reproof.example.mixedshop", test_scheme="MixedShopTests",
                               simulator=SimulatorSettings("iPhone 16", "latest"))
        with tempfile.TemporaryDirectory() as tmp:
            prepared = prepare_build(MIXEDSHOP, config, workspace_root=Path(tmp), runner=HostToolRunner(),
                                     progress=lambda _: None)
            info = prepared.products.app_path / "Info.plist"
            self.assertTrue(info.exists())
            self.assertEqual(plistlib.loads(info.read_bytes())["CFBundleIdentifier"],
                             "io.reproof.example.mixedshop")


if __name__ == "__main__":
    unittest.main()
