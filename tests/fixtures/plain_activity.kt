package io.reproof.plain

import android.app.Activity
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
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
            setPadding(40, 240, 40, 40)
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
            gravity = Gravity.CENTER_VERTICAL
        }
        val add = Button(this).apply {
            id = R.id.add
            text = "Add"
            setOnClickListener {
                if (name.text.toString() == "Test") return@setOnClickListener
                quantity += CounterLogic.increment()
                count.text = quantity.toString()
            }
        }
        val crash = Button(this).apply {
            id = R.id.crash
            text = "Throw test exception"
            setOnClickListener {
                throw IllegalStateException("Expected fixture exception")
            }
        }
        root.addView(name, LinearLayout.LayoutParams(-1, 160))
        root.addView(count, LinearLayout.LayoutParams(-1, 180))
        root.addView(add, LinearLayout.LayoutParams(-1, 160))
        root.addView(crash, LinearLayout.LayoutParams(-1, 160))
        setContentView(root)
    }
}
