package io.reproof.runtimefixture;

import android.app.Activity;
import android.graphics.Insets;
import android.os.Build;
import android.os.Bundle;
import android.text.Editable;
import android.text.InputType;
import android.text.TextWatcher;
import android.util.AtomicFile;
import android.view.View;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.UUID;

/** An ordinary offline Views app, intentionally unrelated to the counter SDK. */
public final class MainActivity extends Activity {
    private final ArrayList<String> items = new ArrayList<>();
    private final String launchId = UUID.randomUUID().toString();
    private String screen = "list";
    private int selected = -1;
    private int validationErrors = 0;
    private int duplicateActivations = 0;
    private int disabledActivations = 0;
    private int revision = 0;
    private LinearLayout root;
    private EditText name;
    private EditText protectedEntry;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        items.add("Notebook");
        items.add("Cable");
        showList("launch");
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private LinearLayout beginScreen(String next) {
        screen = next;
        name = null;
        protectedEntry = null;
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(0xfffafafa);
        root.setPadding(dp(16), dp(16), dp(16), dp(16));
        if (Build.VERSION.SDK_INT >= 30) {
            root.setOnApplyWindowInsetsListener((view, insets) -> {
                Insets bars = insets.getInsets(WindowInsets.Type.systemBars());
                view.setPadding(dp(16) + bars.left, dp(16) + bars.top,
                        dp(16) + bars.right, dp(16) + bars.bottom);
                return insets;
            });
        }
        setContentView(root);
        return root;
    }

    private TextView label(int id, String text) {
        TextView view = new TextView(this);
        view.setId(id);
        view.setText(text);
        view.setTextColor(0xff202020);
        view.setTextSize(20);
        view.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_YES);
        root.addView(view, new LinearLayout.LayoutParams(-1, dp(40)));
        return view;
    }

    private Button button(int id, String text, Runnable action) {
        Button view = new Button(this);
        view.setId(id);
        view.setText(text);
        view.setAllCaps(false);
        view.setOnClickListener(ignored -> action.run());
        root.addView(view, new LinearLayout.LayoutParams(-1, dp(48)));
        return view;
    }

    private void showList(String event) {
        selected = -1;
        beginScreen("list");
        label(R.id.list_screen, "Inventory");
        button(R.id.new_item, "New item", this::showForm);
        button(R.id.row_notebook, items.get(0), () -> showDetail(0));
        button(R.id.row_cable, items.get(1), () -> showDetail(1));
        if (items.size() > 2) {
            button(R.id.saved_item, items.get(items.size() - 1),
                    () -> showDetail(items.size() - 1));
        }
        // Repeated row IDs are realistic, but unsafe for an unqualified locator.
        button(R.id.duplicate_open, "Open first (ambiguous)", () -> {
            duplicateActivations++;
            showDetail(0);
        });
        button(R.id.duplicate_open, "Open second (ambiguous)", () -> {
            duplicateActivations++;
            showDetail(1);
        });
        button(R.id.disabled_item, "Unavailable item", () -> {
            disabledActivations++;
            writeState("disabled_activation");
        }).setEnabled(false);
        writeState(event);
    }

    private void showForm() {
        beginScreen("form");
        label(R.id.form_screen, "New inventory item");
        name = new EditText(this);
        name.setId(R.id.item_name);
        name.setSingleLine(true);
        name.setHint("Item name (required)");
        name.setInputType(InputType.TYPE_CLASS_TEXT);
        // A real focusable EditText, without an IME covering test controls.
        name.setShowSoftInputOnFocus(false);
        root.addView(name, new LinearLayout.LayoutParams(-1, dp(56)));
        protectedEntry = new EditText(this);
        protectedEntry.setId(R.id.protected_entry);
        protectedEntry.setSingleLine(true);
        protectedEntry.setHint("Synthetic protected field");
        protectedEntry.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        protectedEntry.setShowSoftInputOnFocus(false);
        root.addView(protectedEntry, new LinearLayout.LayoutParams(-1, dp(56)));
        TextView error = label(R.id.validation_error, "");
        error.setVisibility(View.GONE);
        button(R.id.save_item, "Save item", () -> {
            String value = name.getText().toString().trim();
            if (value.isEmpty() || value.length() > 40) {
                validationErrors++;
                error.setText("Enter 1 to 40 characters");
                error.setVisibility(View.VISIBLE);
                writeState("invalid_submit");
                return;
            }
            items.add(value);
            showList("created");
        });
        button(R.id.cancel_form, "Cancel", () -> showList("cancel_form"));
        TextWatcher watcher = new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int start, int count, int after) { }
            @Override public void onTextChanged(CharSequence s, int start, int before, int count) {
                writeState("edited");
            }
            @Override public void afterTextChanged(Editable value) { }
        };
        name.addTextChangedListener(watcher);
        protectedEntry.addTextChangedListener(watcher);
        writeState("new_form");
    }

    private void showDetail(int index) {
        selected = index;
        beginScreen("detail");
        label(R.id.detail_screen, "Item details");
        label(R.id.detail_title, items.get(index));
        button(R.id.back_to_list, "Back to inventory", () -> showList("back_to_list"));
        writeState("opened");
    }

    private String visibleText(int id) {
        TextView view = findViewById(id);
        return view != null && view.getVisibility() == View.VISIBLE ? view.getText().toString() : "";
    }

    /**
     * Read-only test oracle, not an input channel or Reproof observation adapter.
     * Model and actual displayed widget values are separate. Native accessibility
     * snapshots and screenshots are still required by the host runtime test.
     * This contains synthetic fixture data only; protected text is never recorded.
     */
    private void writeState(String event) {
        AtomicFile file = new AtomicFile(new File(getFilesDir(), "fixture-state.json"));
        FileOutputStream output = null;
        try {
            JSONObject model = new JSONObject()
                    .put("items", new JSONArray(items))
                    .put("selected", selected)
                    .put("validationErrors", validationErrors)
                    .put("duplicateActivations", duplicateActivations)
                    .put("disabledActivations", disabledActivations);
            JSONArray displayedItems = new JSONArray();
            if (screen.equals("list")) {
                displayedItems.put(visibleText(R.id.row_notebook));
                displayedItems.put(visibleText(R.id.row_cable));
                if (items.size() > 2) displayedItems.put(visibleText(R.id.saved_item));
            }
            JSONObject displayed = new JSONObject()
                    .put("screen", screen)
                    .put("items", displayedItems)
                    .put("detailTitle", visibleText(R.id.detail_title))
                    .put("draft", name == null ? "" : name.getText().toString())
                    .put("validation", visibleText(R.id.validation_error))
                    .put("protectedLength", protectedEntry == null ? 0 : protectedEntry.length());
            JSONObject value = new JSONObject()
                    .put("schemaVersion", 1).put("launchId", launchId)
                    .put("revision", ++revision).put("event", event)
                    .put("model", model).put("displayed", displayed);
            output = file.startWrite();
            output.write(value.toString().getBytes(StandardCharsets.UTF_8));
            file.finishWrite(output);
        } catch (Exception error) {
            if (output != null) file.failWrite(output);
            throw new IllegalStateException("Fixture oracle could not be written", error);
        }
    }
}
