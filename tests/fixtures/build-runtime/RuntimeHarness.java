import android.app.Activity;
import android.content.ContextWrapper;
import android.content.Intent;
import android.content.res.Resources;
import android.view.View;
import io.reproloop.autotrace.ReproAuto;
import io.reproloop.autotrace.ReproHooks;

public final class RuntimeHarness {
    private static final int BUTTON_ID = 7;

    public static void main(String[] args) {
        Activity recordActivity = new Activity();
        recordActivity.getApplicationInfo().flags = android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE;
        recordActivity.setIntent(new Intent().putExtra("repro_mode", "record"));
        Resources resources = new Resources();
        resources.addEntry(BUTTON_ID, "save");

        View view = new View(new ContextWrapper(recordActivity), resources, BUTTON_ID);
        final int[] calls = {0};
        final View[] callback = {null};
        View.OnClickListener delegate = clicked -> {
            calls[0]++;
            callback[0] = clicked;
        };
        ReproAuto.reset();
        ReproHooks.install(view, delegate, "sabc");
        view.performClick();
        check(calls[0] == 1 && callback[0] == view, "delegate once and callback identity");
        check(ReproAuto.beforeCalls == 1 && ReproAuto.afterCalls == 1 && ReproAuto.threwCalls == 0,
                "normal collector order");
        check("save".equals(ReproAuto.target) && "sabc".equals(ReproAuto.site), "resource target and site");

        final IllegalStateException expected = new IllegalStateException("product");
        ReproAuto.reset();
        View throwing = new View(recordActivity, resources, BUTTON_ID);
        ReproHooks.install(throwing, clicked -> { throw expected; }, "sabc");
        try {
            throwing.performClick();
            fail("product exception was swallowed");
        } catch (IllegalStateException actual) {
            check(actual == expected, "product exception identity");
        }
        check(ReproAuto.threwCalls == 1 && ReproAuto.afterCalls == 1, "throw cleanup");

        View cleared = new View(recordActivity, resources, BUTTON_ID);
        ReproHooks.install(cleared, null, "sabc");
        check(cleared.getOnClickListener() == null, "null listener clears");

        Activity nonrecord = new Activity();
        nonrecord.setIntent(new Intent().putExtra("repro_mode", "record"));
        View direct = new View(nonrecord, resources, BUTTON_ID);
        ReproAuto.reset();
        ReproHooks.install(direct, delegate, "sabc");
        check(direct.getOnClickListener() == delegate, "nonrecord listener remains direct");
        direct.performClick();
        check(ReproAuto.beforeCalls == 0, "nonrecord does not collect");

        ContextWrapper cycleA = new ContextWrapper(null);
        ContextWrapper cycleB = new ContextWrapper(cycleA);
        cycleA.setBaseContext(cycleB);
        View cycle = new View(cycleA, resources, BUTTON_ID);
        ReproAuto.reset();
        ReproHooks.install(cycle, delegate, "sabc");
        cycle.performClick();
        check(ReproAuto.beforeCalls == 0 && calls[0] == 3, "context cycle remains business safe");

        View missingResource = new View(recordActivity, new Resources(), BUTTON_ID);
        ReproAuto.reset();
        ReproHooks.install(missingResource, delegate, "sabc");
        missingResource.performClick();
        check(ReproAuto.beforeCalls == 0 && calls[0] == 4, "missing resource remains business safe");

        ReproAuto.reset();
        ReproAuto.throwBefore = true;
        ReproAuto.throwAfter = true;
        View collectorFailure = new View(recordActivity, resources, BUTTON_ID);
        ReproHooks.install(collectorFailure, delegate, "sabc");
        collectorFailure.performClick();
        check(calls[0] == 5, "collector failure does not change callback");
    }

    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    private static void fail(String message) {
        throw new AssertionError(message);
    }
}
