import unittest

from reproof.core import ContractError
from reproof.kotlin_instrumenter import instrument_kotlin


SOURCE = '''package demo

import android.app.Activity
import android.os.Bundle
import android.widget.Button

class MainActivity : Activity() {
    override fun onCreate(state: Bundle?) {
        val save = Button(this).apply {
            id = R.id.save
            setOnClickListener {
                if (label.text.toString() == "Test") return@setOnClickListener
                println("R.id.not_a_real_handler")
            }
        }
        findViewById<Button>(R.id.cancel).setOnClickListener { view ->
            println(view)
        }
    }
}
'''


def instrumented(targets=("save", "cancel")):
    return instrument_kotlin(
        {"app/src/main/java/demo/MainActivity.kt": SOURCE},
        "demo.MainActivity",
        list(targets),
    )


class KotlinInstrumenterTests(unittest.TestCase):
    def test_instruments_activity_and_returns_site_metadata(self):
        result = instrumented()
        self.assertEqual(result["schemaVersion"], 1)
        self.assertEqual(result["activityPath"], "app/src/main/java/demo/MainActivity.kt")
        transformed = result["files"][result["activityPath"]]
        self.assertIn("ReproAuto.start(this@MainActivity)", transformed)
        self.assertIn("ReproAuto.stop(this@MainActivity)", transformed)
        self.assertEqual({site["target"] for site in result["sites"]}, {"save", "cancel"})
        self.assertTrue(all(site["kind"] == "tap" and site["id"].startswith("s") for site in result["sites"]))

    def test_comments_and_strings_do_not_create_handlers(self):
        source = SOURCE.replace(
            'println("R.id.not_a_real_handler")',
            '// setOnClickListener { R.id.save }\n                println("setOnClickListener R.id.save")',
        )
        result = instrument_kotlin({"Main.kt": source}, "demo.MainActivity", ["save", "cancel"])
        self.assertEqual(len(result["sites"]), 2)

    def test_labeled_listener_return_is_preserved_inside_try(self):
        result = instrumented(("save",))
        transformed = result["files"][result["activityPath"]]
        self.assertIn("return@setOnClickListener", transformed)
        self.assertIn("try {", transformed)
        self.assertIn("finally {", transformed)

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(ContractError):
            instrumented(("missing",))

    def test_duplicate_resource_handlers_are_rejected(self):
        source = SOURCE.replace(
            '        findViewById<Button>(R.id.cancel).setOnClickListener { view ->\n            println(view)\n        }',
            '        findViewById<Button>(R.id.save).setOnClickListener { view ->\n            println(view)\n        }',
        )
        with self.assertRaises(ContractError):
            instrument_kotlin({"Main.kt": source}, "demo.MainActivity", ["save"])

    def test_repeated_invocation_is_refused(self):
        result = instrumented()
        transformed = result["files"][result["activityPath"]]
        with self.assertRaises(ContractError):
            instrument_kotlin({result["activityPath"]: transformed}, "demo.MainActivity", ["save", "cancel"])


if __name__ == "__main__":
    unittest.main()
