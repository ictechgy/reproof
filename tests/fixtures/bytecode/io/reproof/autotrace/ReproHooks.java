package io.reproof.autotrace;

import android.app.Activity;
import android.view.View;

public final class ReproHooks {
    public static int installs;
    public static int starts;
    public static int stops;
    public static int before;
    public static int after;
    public static int thrown;
    public static String lastSite;

    private ReproHooks() {
    }

    public static void reset() {
        installs = 0;
        starts = 0;
        stops = 0;
        before = 0;
        after = 0;
        thrown = 0;
        lastSite = null;
    }

    public static void install(View view, View.OnClickListener delegate, String siteId) {
        installs++;
        lastSite = siteId;
        view.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View clicked) {
                lastSite = siteId;
                before++;
                try {
                    delegate.onClick(clicked);
                } catch (Throwable error) {
                    thrown++;
                    throw error;
                } finally {
                    after++;
                }
            }
        });
    }

    public static void start(Activity activity) {
        starts++;
    }

    public static void stop(Activity activity) {
        stops++;
    }
}
