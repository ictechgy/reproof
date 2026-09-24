package io.reproof.sample

import android.app.Activity
import android.content.res.Configuration
import android.os.Bundle
import android.os.Build
import android.text.Editable
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.WindowInsets
import android.util.TypedValue
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import io.reproof.sdk.ReproRecorder
import org.json.JSONObject

/** A deliberately small app used by the host replay and driver integration tests. */
class MainActivity : Activity() {
    private var recorder: ReproRecorder? = null
    private lateinit var countView: TextView
    private lateinit var nameView: EditText
    private lateinit var statusView: TextView
    private lateinit var mainScreen: LinearLayout
    private lateinit var secondScreen: LinearLayout
    private var count = 0
    private var recording = false
    private var secondScreenVisible = false
    private var bottomScrollRecorded = false
    private var nameDirty = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE) actionBar?.hide()
        recording = intent.getStringExtra("repro_mode") == "record"
        val fixtureId = intent.getStringExtra("fixture_id") ?: "default"
        buildContent()
        if (recording) {
            recorder = ReproRecorder(
                context = this,
                fixtureId = fixtureId,
                startState = JSONObject()
                    .put("screen", "main")
                    .put("nodes", JSONObject().put("count", "0").put("name", "")),
            )
            if (savedInstanceState != null) recorder?.markIncomplete()
        } else {
            statusView.text = "Replay mode"
        }
    }

    private fun buildContent() {
        val landscape = resources.configuration.orientation == Configuration.ORIENTATION_LANDSCAPE
        val verticalPadding = dp(if (landscape) 8 else 20)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), verticalPadding, dp(20), verticalPadding)
        }
        if (Build.VERSION.SDK_INT >= 35) {
            // Android's enforced edge-to-edge layout otherwise places the first
            // editable field behind the platform ActionBar and clips Report.
            root.setOnApplyWindowInsetsListener { view, insets ->
                val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
                val actionBarSize = TypedValue()
                val barHeight = if (actionBar?.isShowing == true && theme.resolveAttribute(android.R.attr.actionBarSize, actionBarSize, true)) {
                    TypedValue.complexToDimensionPixelSize(actionBarSize.data, resources.displayMetrics)
                } else 0
                view.setPadding(dp(20) + bars.left, verticalPadding + bars.top + barHeight, dp(20) + bars.right, verticalPadding + bars.bottom)
                insets
            }
        }

        mainScreen = LinearLayout(this).apply {
            orientation = if (landscape) LinearLayout.HORIZONTAL else LinearLayout.VERTICAL
        }
        val controls = if (landscape) LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, 0, dp(12), 0)
        } else mainScreen
        if (landscape) mainScreen.addView(controls, LinearLayout.LayoutParams(0, -1, 1f))
        nameView = EditText(this).apply {
            id = R.id.name
            hint = "Name"
            contentDescription = "Name"
            inputType = android.text.InputType.TYPE_CLASS_TEXT
        }
        controls.addView(nameView, LinearLayout.LayoutParams(-1, dp(56)))

        countView = TextView(this).apply {
            id = R.id.count
            text = "0"
            textSize = 28f
            gravity = Gravity.CENTER_VERTICAL
            contentDescription = "Count"
        }
        controls.addView(countView, LinearLayout.LayoutParams(-1, dp(64)))

        val addButton = Button(this).apply {
            id = R.id.add
            text = "Add"
            setOnClickListener { addItem() }
        }
        controls.addView(addButton, LinearLayout.LayoutParams(-1, dp(52)))

        val list = ScrollView(this).apply {
            id = R.id.list
            isFillViewport = true
        }
        val listContent = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        listContent.addView(TextView(this).apply {
            text = "Items"
            textSize = 18f
            setPadding(0, dp(12), 0, dp(12))
        }, LinearLayout.LayoutParams(-1, dp(56)))
        repeat(12) { index ->
            listContent.addView(TextView(this).apply {
                text = "Item ${index + 1}"
                setPadding(0, dp(8), 0, dp(8))
            }, LinearLayout.LayoutParams(-1, dp(52)))
        }
        val bottomTarget = TextView(this).apply {
            id = R.id.bottom
            text = "Bottom"
            textSize = 18f
            gravity = Gravity.CENTER_VERTICAL
            contentDescription = "Bottom"
        }
        listContent.addView(bottomTarget, LinearLayout.LayoutParams(-1, dp(64)))
        list.addView(listContent)
        mainScreen.addView(list, if (landscape) LinearLayout.LayoutParams(0, -1, 1f) else LinearLayout.LayoutParams(-1, 0, 1f))
        root.addView(mainScreen, LinearLayout.LayoutParams(-1, 0, 1f))

        secondScreen = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            addView(TextView(this@MainActivity).apply {
                text = "Second screen"
                textSize = 24f
                gravity = Gravity.CENTER
            }, LinearLayout.LayoutParams(-1, 0, 1f))
        }
        root.addView(secondScreen, LinearLayout.LayoutParams(-1, 0, 1f))

        val bottom = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
        }
        val back = Button(this).apply {
            id = R.id.back
            text = "Back"
            setOnClickListener { returnToMain(fromSystemBack = false) }
        }
        val next = Button(this).apply {
            id = R.id.next
            text = "Next"
            setOnClickListener { showSecondScreen() }
        }
        bottom.addView(back, LinearLayout.LayoutParams(0, dp(52), 1f))
        bottom.addView(next, LinearLayout.LayoutParams(0, dp(52), 1f))
        root.addView(bottom)

        statusView = TextView(this).apply {
            text = if (recording) "Recording" else "Ready"
            setPadding(0, dp(8), 0, dp(8))
        }
        root.addView(statusView, LinearLayout.LayoutParams(-1, dp(44)))

        val report = Button(this).apply {
            id = R.id.report
            text = "Report"
            setOnClickListener { reportSession() }
        }
        root.addView(report, LinearLayout.LayoutParams(-1, dp(52)))
        setContentView(root)

        nameView.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = Unit
            override fun afterTextChanged(s: Editable?) {
                if (recording) nameDirty = true
            }
        })
        nameView.setOnFocusChangeListener { _, hasFocus ->
            if (!hasFocus) commitName()
        }
        list.setOnScrollChangeListener { _, _, scrollY, _, _ ->
            if (recording && !bottomScrollRecorded && scrollY > 0 && bottomTarget.getGlobalVisibleRect(android.graphics.Rect())) {
                commitName()
                if (recorder?.recordScrollTo("list", "forward", "bottom") == true) {
                    bottomScrollRecorded = true
                }
            }
        }
    }

    private fun addItem() {
        commitName()
        if (recording) recorder?.recordTap("add")
        count += CounterLogic.increment()
        countView.text = count.toString()
    }

    private fun commitName() {
        if (!recording || !nameDirty) return
        recorder?.recordReplace("name", nameView.text?.toString().orEmpty())
        nameDirty = false
    }

    private fun showSecondScreen() {
        commitName()
        if (recording) recorder?.recordTap("next")
        secondScreenVisible = true
        mainScreen.visibility = View.GONE
        secondScreen.visibility = View.VISIBLE
        statusView.text = "Second screen"
    }

    private fun returnToMain(fromSystemBack: Boolean) {
        commitName()
        if (recording) {
            if (fromSystemBack) recorder?.recordBack() else recorder?.recordTap("back")
        }
        secondScreenVisible = false
        secondScreen.visibility = View.GONE
        mainScreen.visibility = View.VISIBLE
        statusView.text = "Main screen"
    }

    private fun reportSession() {
        commitName()
        val activeRecorder = recorder ?: run {
            statusView.text = "Replay mode"
            return
        }
        if (!activeRecorder.freezeAndExport { file ->
                runOnUiThread { statusView.text = "Capture saved: ${file.name}" }
            }) {
            statusView.text = "Capture already saved"
        } else {
            statusView.text = "Saving capture"
        }
    }

    @Suppress("DEPRECATION")
    @Deprecated("Android dispatches back through OnBackInvokedDispatcher on newer releases")
    override fun onBackPressed() {
        if (secondScreenVisible) {
            returnToMain(fromSystemBack = true)
        } else {
            commitName()
            if (recording) recorder?.recordBack()
            statusView.text = "Main screen"
        }
    }

    override fun onDestroy() {
        // A report freezes the recorder. If the app is killed before reporting,
        // events.jsonl and session.json remain available for recovery.
        recorder?.close()
        super.onDestroy()
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()
}
