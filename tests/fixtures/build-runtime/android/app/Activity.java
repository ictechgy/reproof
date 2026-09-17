package android.app;

import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;

public class Activity extends Context {
    private final ApplicationInfo applicationInfo = new ApplicationInfo();
    private Intent intent;

    public ApplicationInfo getApplicationInfo() {
        return applicationInfo;
    }

    public Intent getIntent() {
        return intent;
    }

    public void setIntent(Intent intent) {
        this.intent = intent;
    }
}
