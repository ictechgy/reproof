package io.reproof.sdk;

import android.content.Context;
import org.json.JSONObject;
import java.util.Collections;

/** Compile-only check for the original explicit Java constructor. Never executes Android code. */
final class ReproLegacyConstructorCompileCheck {
    static ReproRecorder create(Context context, JSONObject state) {
        return new ReproRecorder(context, "default", 1, state,
                Collections.singleton("name"), Collections.singleton(""),
                600000L, 20971520L, Collections.singleton("add"),
                Collections.singletonMap("list", Collections.singleton("bottom")), "back", "report");
    }
}
