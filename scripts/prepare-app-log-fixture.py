#!/usr/bin/env python3
"""Create a synthetic Android app with panels, another Activity, and recreation."""
import argparse
import importlib.util
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from reproof.android_profile import validate_app_profile
from reproof.storage import read_json,write_json


def prepare(output):
    module_spec=importlib.util.spec_from_file_location('plain_fixture',ROOT/'scripts/prepare-instrumentation-fixture.py')
    module=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(module)
    module.prepare(output);output=Path(output);source=output/'source'
    (source/'sample/src/main/java/io/reproof/sample/MainActivity.kt').write_text('''package io.reproof.plain

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

class MainActivity : Activity() {
    private var quantity = 0
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(40, 180, 40, 40)
        }
        val main = LinearLayout(this).apply {
            id = R.id.main_panel
            orientation = LinearLayout.VERTICAL
        }
        val details = LinearLayout(this).apply {
            id = R.id.details_panel
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
        }
        val name = EditText(this).apply {
            id = R.id.name
            hint = "Name"
            inputType = InputType.TYPE_CLASS_TEXT
        }
        val count = TextView(this).apply {
            id = R.id.count
            text = "0"
            textSize = 28f
        }
        val add = Button(this).apply {
            id = R.id.add
            text = "Add"
            setOnClickListener {
                quantity += CounterLogic.increment()
                count.text = quantity.toString()
            }
        }
        val next = Button(this).apply {
            id = R.id.next
            text = "Next panel"
            setOnClickListener {
                main.visibility = View.GONE
                details.visibility = View.VISIBLE
            }
        }
        val back = Button(this).apply {
            id = R.id.back
            text = "Back to main"
            setOnClickListener {
                details.visibility = View.GONE
                main.visibility = View.VISIBLE
            }
        }
        val recreate = Button(this).apply {
            id = R.id.recreate
            text = "Recreate Activity"
            setOnClickListener {
                this@MainActivity.recreate()
            }
        }
        val openActivity = Button(this).apply {
            id = R.id.open_activity
            text = "Open another Activity"
            setOnClickListener {
                startActivity(Intent(this@MainActivity, OtherActivity::class.java))
            }
        }
        main.addView(name, LinearLayout.LayoutParams(-1, 140))
        main.addView(count, LinearLayout.LayoutParams(-1, 120))
        main.addView(add, LinearLayout.LayoutParams(-1, 140))
        main.addView(next, LinearLayout.LayoutParams(-1, 140))
        details.addView(TextView(this).apply { text = "Details panel"; textSize = 26f }, LinearLayout.LayoutParams(-1, 140))
        details.addView(back, LinearLayout.LayoutParams(-1, 140))
        root.addView(main)
        root.addView(details)
        root.addView(recreate, LinearLayout.LayoutParams(-1, 140))
        root.addView(openActivity, LinearLayout.LayoutParams(-1, 140))
        setContentView(root)
    }
}

class OtherActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(TextView(this).apply { text = "Another Activity — use system Back"; textSize = 24f; setPadding(40,240,40,40) })
    }
}
''')
    ids=['name','count','add','next','back','recreate','open_activity','main_panel','details_panel']
    (source/'sample/src/main/res/values/ids.xml').write_text('<resources>\n'+''.join(f'  <item type="id" name="{name}" />\n' for name in ids)+'</resources>\n')
    manifest=source/'sample/src/main/AndroidManifest.xml'
    text=manifest.read_text();assert '</application>' in text
    manifest.write_text(text.replace('</application>','    <activity android:name=".OtherActivity" android:exported="false" />\n    </application>'))
    profile=read_json(output/'app-profile.json');profile['targets']['tap']=['add','next','back','recreate','open_activity']
    profile['screenTargets']={'main_panel':'main','details_panel':'details'}
    write_json(output/'app-profile.json',validate_app_profile(profile).data)
    write_json(output/'fixture.json',dict(synthetic=True,manualSdkCalls=0,reportView=False,
        cases=['click','panel visibility','second Activity','Activity recreation','background and return']))
    return {'source':str(source),'appProfile':str(output/'app-profile.json')}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    print(prepare(parser.parse_args().output))
