package io.reproof.autotrace;

import android.app.Activity;

public final class ReproAuto {
    public static int beforeCalls;
    public static int threwCalls;
    public static int afterCalls;
    public static String target;
    public static String site;
    public static boolean throwBefore;
    public static boolean throwThrew;
    public static boolean throwAfter;

    private ReproAuto() {}

    public static void reset() {
        beforeCalls = 0;
        threwCalls = 0;
        afterCalls = 0;
        target = null;
        site = null;
        throwBefore = false;
        throwThrew = false;
        throwAfter = false;
    }

    public static void start(Activity activity) {}
    public static void stop(Activity activity) {}

    public static long beforeTap(Activity activity, String target, String siteId) {
        beforeCalls++;
        ReproAuto.target = target;
        ReproAuto.site = siteId;
        if (throwBefore) throw new IllegalStateException("collector before failure");
        return 41L;
    }

    public static void threw(long token) {
        threwCalls++;
        if (throwThrew) throw new IllegalStateException("collector threw failure");
    }

    public static void afterTap(long token) {
        afterCalls++;
        if (throwAfter) throw new IllegalStateException("collector after failure");
    }
}
