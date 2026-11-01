"""전용 Simulator 관리 테스트. 실제 simctl은 호출하지 않고 FakeToolRunner를 쓴다."""
import json
import unittest
from pathlib import Path

from reproof.simple.config import SimulatorSettings
from reproof.simple.errors import SimpleError, ToolFailure
from reproof.simple.simulator import (
    Runtime, SimulatorManager, launch_locale_settings, list_runtimes, pick_runtime,
)
from tests.simple_support import FakeToolRunner

RUNTIME_26 = "com.apple.CoreSimulator.SimRuntime.iOS-26-0"
RUNTIME_27 = "com.apple.CoreSimulator.SimRuntime.iOS-27-0"
TYPE_16 = "com.apple.CoreSimulator.SimDeviceType.iPhone-16"
TYPE_17 = "com.apple.CoreSimulator.SimDeviceType.iPhone-17"
SETTINGS = SimulatorSettings(device_type="iPhone 16", runtime="latest")


def runtimes_json():
    """iOS 26.0·27.0, 사용 불가 iOS 28.0, watchOS가 섞인 runtimes 출력을 만든다."""
    def entry(identifier, name, version, available):
        return {"identifier": identifier, "name": name, "version": version, "isAvailable": available}
    return json.dumps({"runtimes": [
        entry(RUNTIME_26, "iOS 26.0", "26.0", True),
        entry(RUNTIME_27, "iOS 27.0", "27.0", True),
        entry("com.apple.CoreSimulator.SimRuntime.iOS-28-0", "iOS 28.0", "28.0", False),
        entry("com.apple.CoreSimulator.SimRuntime.watchOS-12-0", "watchOS 12.0", "12.0", True),
    ]})


def devicetypes_json():
    """iPhone 16·17과 iPad 기기 종류 출력을 만든다."""
    return json.dumps({"devicetypes": [
        {"identifier": TYPE_16, "name": "iPhone 16"},
        {"identifier": TYPE_17, "name": "iPhone 17"},
        {"identifier": "com.apple.CoreSimulator.SimDeviceType.iPad-Pro", "name": "iPad Pro"},
    ]})


def device(udid, name, type_id=TYPE_16, state="Shutdown", available=True):
    """devices 출력의 기기 항목 하나를 만든다."""
    return {"udid": udid, "name": name, "state": state, "isAvailable": available,
            "deviceTypeIdentifier": type_id}


def devices_json(by_runtime):
    """런타임 식별자별 기기 목록을 simctl 형식으로 직렬화한다."""
    return json.dumps({"devices": by_runtime})


class SimulatorTests(unittest.TestCase):
    """SimulatorManager와 보조 함수 검증."""

    def base_responses(self, devices):
        """조회 응답 세 개를 묶는다."""
        return [
            (["xcrun", "simctl", "list", "runtimes", "--json"], runtimes_json()),
            (["xcrun", "simctl", "list", "devicetypes", "--json"], devicetypes_json()),
            (["xcrun", "simctl", "list", "devices", "--json"], devices_json(devices)),
        ]

    def mutating(self, fake):
        """create·boot·erase 등 상태를 바꾸는 호출만 고른다."""
        return [c["argv"] for c in fake.calls if c["argv"][2] not in ("list",)]

    def test_ensure_device_reuses_owned_device_and_creates_when_missing(self):
        fake = FakeToolRunner(self.base_responses({RUNTIME_27: [device("UDID-A", "reproof-shop")]}))
        self.assertEqual(SimulatorManager(fake).ensure_device("shop", SETTINGS), "UDID-A")
        self.assertEqual(self.mutating(fake), [])
        # 이름은 같지만 device type이 다르면 새로 만들고 기존 기기는 건드리지 않는다.
        fake = FakeToolRunner(self.base_responses({RUNTIME_27: [device("UDID-A", "reproof-shop", TYPE_17)]})
                              + [(["xcrun", "simctl", "create"], "UDID-NEW\n")])
        self.assertEqual(SimulatorManager(fake).ensure_device("shop", SETTINGS), "UDID-NEW")
        self.assertEqual(self.mutating(fake), [["xcrun", "simctl", "create", "reproof-shop", TYPE_16, RUNTIME_27]])

    def test_operations_refuse_device_without_prefix(self):
        fake = FakeToolRunner(self.base_responses({RUNTIME_27: [device("UDID-U", "iPhone 16")]}))
        manager = SimulatorManager(fake)
        for operation in (manager.reset, manager.apply_presentation,
                          lambda udid: manager.install_app(udid, Path("/x/A.app")),
                          lambda udid: manager.uninstall_app(udid, "io.a.b")):
            with self.assertRaises(SimpleError) as caught:
                operation("UDID-U")
            self.assertEqual(caught.exception.code, "simulator_not_owned")
        with self.assertRaises(SimpleError) as caught:
            manager.reset("UDID-UNKNOWN")
        self.assertEqual(caught.exception.code, "simulator_not_owned")
        self.assertEqual(self.mutating(fake), [])

    def test_reset_order_is_shutdown_erase_boot_bootstatus(self):
        fake = FakeToolRunner(self.base_responses({RUNTIME_27: [device("U", "reproof-shop", state="Booted")]})
                              + [(["xcrun", "simctl"], "")])
        SimulatorManager(fake).reset("U")
        self.assertEqual(self.mutating(fake), [
            ["xcrun", "simctl", "shutdown", "U"], ["xcrun", "simctl", "erase", "U"],
            ["xcrun", "simctl", "boot", "U"], ["xcrun", "simctl", "bootstatus", "U", "-b"]])

    def test_reset_skips_shutdown_for_shutdown_device(self):
        fake = FakeToolRunner(self.base_responses({RUNTIME_27: [device("U", "reproof-shop")]})
                              + [(["xcrun", "simctl"], "")])
        SimulatorManager(fake).reset("U")
        self.assertEqual([c[2] for c in self.mutating(fake)], ["erase", "boot", "bootstatus"])

    def test_reset_propagates_shutdown_failure_with_empty_tail_for_booted_device(self):
        base = self.base_responses({RUNTIME_27: [device("U", "reproof-shop", state="Booted")]})
        fake = FakeToolRunner(base + [(["xcrun", "simctl", "shutdown"],
                                       ToolFailure("xcrun", log_path=None, output_tail=""))])
        with self.assertRaises(ToolFailure):
            SimulatorManager(fake).reset("U")
        self.assertFalse(any("erase" in c["argv"] for c in fake.calls))

    def test_ensure_device_ignores_unavailable_device(self):
        fake = FakeToolRunner(self.base_responses(
            {RUNTIME_27: [device("OLD", "reproof-shop", available=False)]})
            + [(["xcrun", "simctl", "create"], "UDID-NEW\n")])
        self.assertEqual(SimulatorManager(fake).ensure_device("shop", SETTINGS), "UDID-NEW")

    def test_unexpected_json_raises_simulator_output_invalid(self):
        bad_runtimes = json.dumps({"runtimes": [{"identifier": "x.SimRuntime.iOS-27-0"}]})
        with self.assertRaises(SimpleError) as caught:
            list_runtimes(FakeToolRunner([(["xcrun"], bad_runtimes)]))
        self.assertEqual(caught.exception.code, "simulator_output_invalid")
        bad_types = json.dumps({"devicetypes": [{"identifier": "x"}]})
        fake = FakeToolRunner([(["xcrun", "simctl", "list", "runtimes"], runtimes_json()),
                               (["xcrun", "simctl", "list", "devicetypes"], bad_types)])
        with self.assertRaises(SimpleError) as caught:
            SimulatorManager(fake).ensure_device("shop", SETTINGS)
        self.assertEqual(caught.exception.code, "simulator_output_invalid")

    def test_reset_ignores_only_already_shutdown_failure(self):
        base = self.base_responses({RUNTIME_27: [device("U", "reproof-shop", state="Booted")]})
        already = ToolFailure("xcrun", log_path=None,
                              output_tail="Unable to shutdown device in current state: Shutdown")
        fake = FakeToolRunner(base + [(["xcrun", "simctl", "shutdown"], already),
                                      (["xcrun", "simctl"], "")])
        SimulatorManager(fake).reset("U")
        other = ToolFailure("xcrun", log_path=None, output_tail="Invalid device: U")
        fake = FakeToolRunner(base + [(["xcrun", "simctl", "shutdown"], other)])
        with self.assertRaises(ToolFailure):
            SimulatorManager(fake).reset("U")
        self.assertFalse(any("erase" in c["argv"] for c in fake.calls))

    def test_apply_presentation_install_and_uninstall_commands(self):
        fake = FakeToolRunner(self.base_responses({RUNTIME_27: [device("U", "reproof-shop")]})
                              + [(["xcrun", "simctl"], "")])
        manager = SimulatorManager(fake)
        manager.apply_presentation("U")
        manager.install_app("U", Path("/tmp/회사 앱/My App.app"))
        manager.uninstall_app("U", "io.a.b")
        self.assertEqual(self.mutating(fake), [
            ["xcrun", "simctl", "status_bar", "U", "override", "--time", "9:41", "--batteryState",
             "charged", "--batteryLevel", "100", "--cellularBars", "4", "--wifiBars", "3"],
            ["xcrun", "simctl", "ui", "U", "appearance", "light"],
            ["xcrun", "simctl", "ui", "U", "content_size", "large"],
            ["xcrun", "simctl", "install", "U", "/tmp/회사 앱/My App.app"],
            ["xcrun", "simctl", "uninstall", "U", "io.a.b"]])

    def test_pick_runtime_latest_and_by_name_and_missing(self):
        runtimes = list_runtimes(FakeToolRunner(self.base_responses({})))
        self.assertEqual([r.name for r in runtimes], ["iOS 26.0", "iOS 27.0", "iOS 28.0"])
        self.assertEqual(pick_runtime(runtimes, "latest").name, "iOS 27.0")
        self.assertEqual(pick_runtime(runtimes, "iOS 26.0").identifier, RUNTIME_26)
        with self.assertRaises(SimpleError) as caught:
            pick_runtime(runtimes, "iOS 99.0")
        self.assertEqual(caught.exception.code, "ios_runtime_not_found")
        self.assertIn("iOS 27.0", caught.exception.cause)
        with self.assertRaises(SimpleError) as caught:
            pick_runtime((), "latest")
        self.assertEqual(caught.exception.code, "ios_runtime_missing")
        self.assertIn("xcodebuild -downloadPlatform iOS", caught.exception.remedy)

    def test_unknown_device_type_lists_available_iphones(self):
        fake = FakeToolRunner(self.base_responses({}))
        settings = SimulatorSettings(device_type="iPhone 99", runtime="latest")
        with self.assertRaises(SimpleError) as caught:
            SimulatorManager(fake).ensure_device("shop", settings)
        self.assertEqual(caught.exception.code, "device_type_not_found")
        self.assertIn("iPhone 17", caught.exception.cause)
        self.assertNotIn("iPad", caught.exception.cause)

    def test_launch_locale_settings_from_config(self):
        self.assertEqual(launch_locale_settings(SETTINGS),
                         (["-AppleLanguages", "(ko)", "-AppleLocale", "ko_KR"], {"TZ": "Asia/Seoul"}))
        custom = SimulatorSettings("iPhone 16", "latest", locale="ja_JP", language="ja", timezone="Asia/Tokyo")
        self.assertEqual(launch_locale_settings(custom),
                         (["-AppleLanguages", "(ja)", "-AppleLocale", "ja_JP"], {"TZ": "Asia/Tokyo"}))

    def test_runtime_dataclass_is_frozen(self):
        with self.assertRaises(Exception):
            Runtime("a", "b", "1", True).name = "c"


if __name__ == "__main__":
    unittest.main()
