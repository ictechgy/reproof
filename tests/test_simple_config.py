"""reproof.toml 설정 파일 읽기·쓰기 검증."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from reproof.simple.config import (
    config_digest,
    load_config,
    render_config,
    resolve_launch_environment,
)
from reproof.simple.errors import SimpleError
from tests.simple_support import sample_config


class SimpleConfigTests(unittest.TestCase):
    """설정 파일의 왕복, 검증, 환경 변수 해석을 확인한다."""

    def setUp(self):
        """임시 디렉터리와 설정 파일 경로를 만든다."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "reproof.toml"

    def assert_invalid(self, text, expected_word):
        """text를 읽으면 config_invalid가 나고 안내문에 expected_word가 있는지 확인한다."""
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaises(SimpleError) as caught:
            load_config(self.path)
        self.assertEqual(caught.exception.code, "config_invalid")
        self.assertIn(expected_word, caught.exception.user_message())

    def test_round_trip_render_then_load_is_identical(self):
        """쓴 설정을 다시 읽으면 같다."""
        config = sample_config()
        self.path.write_text(render_config(config), encoding="utf-8")
        self.assertEqual(load_config(self.path), config)

    def test_round_trip_without_optional_values(self):
        """선택 값이 없어도 왕복이 같다."""
        config = replace(sample_config(), generator="none", generator_spec=None,
                         test_scheme=None, launch_environment=(), launch_arguments=())
        self.path.write_text(render_config(config), encoding="utf-8")
        self.assertEqual(load_config(self.path), config)

    def test_unknown_key_is_rejected_with_key_name(self):
        """모르는 표는 키 이름과 함께 거절한다."""
        self.assert_invalid(render_config(sample_config()) + "\n[extra]\nfoo = 1\n", "extra")

    def test_unknown_key_inside_section_is_rejected(self):
        """표 안의 모르는 키도 거절한다."""
        text = render_config(sample_config()).replace("[build]", "[build]\nbogus = 1")
        self.assert_invalid(text, "bogus")

    def test_name_must_be_lowercase_slug(self):
        """이름은 소문자 slug여야 한다."""
        text = render_config(sample_config()).replace('name = "mixedshop"', 'name = "My App"')
        self.assert_invalid(text, "name")

    def test_name_with_trailing_newline_is_rejected(self):
        """끝에 개행이 붙은 이름은 거절한다($ 앵커 우회 방지)."""
        text = render_config(sample_config()).replace('name = "mixedshop"', 'name = "abc\\n"')
        self.assert_invalid(text, "name")

    def test_generator_must_be_known_value(self):
        """generator는 none|xcodegen|tuist만 허용한다."""
        text = render_config(sample_config()).replace('generator = "xcodegen"', 'generator = "cocoapods"')
        self.assert_invalid(text, "generator")

    def test_generator_none_forbids_spec(self):
        """generator가 none이면 generator_spec을 줄 수 없다."""
        text = render_config(sample_config()).replace('generator = "xcodegen"', 'generator = "none"')
        self.assert_invalid(text, "generator_spec")

    def test_wrong_type_names_key(self):
        """타입이 틀리면 키 이름을 알려 준다."""
        text = render_config(sample_config()).replace("timeout_seconds = 3600", 'timeout_seconds = "x"')
        self.assert_invalid(text, "timeout_seconds")

    def test_container_kind_follows_extension(self):
        """container 확장자로 workspace/project를 정한다."""
        self.assertEqual(sample_config().container_kind, "project")
        self.assertEqual(replace(sample_config(), container="A.xcworkspace").container_kind, "workspace")

    def test_env_reference_is_resolved_and_missing_reference_fails(self):
        """env: 참조를 풀고, 없으면 변수 이름을 알려 준다."""
        config = replace(sample_config(), launch_environment=(
            ("API_TOKEN", "env:REPROOF_API_TOKEN"), ("MODE", "qa")))
        self.assertEqual(resolve_launch_environment(config, {"REPROOF_API_TOKEN": "t"}),
                         {"API_TOKEN": "t", "MODE": "qa"})
        with self.assertRaises(SimpleError) as caught:
            resolve_launch_environment(config, {})
        self.assertIn("REPROOF_API_TOKEN", caught.exception.user_message())

    def test_digest_changes_when_scheme_changes(self):
        """scheme이 바뀌면 digest가 바뀌고 64자리 hex다."""
        digest = config_digest(sample_config())
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotEqual(digest, config_digest(replace(sample_config(), scheme="Other")))


if __name__ == "__main__":
    unittest.main()
