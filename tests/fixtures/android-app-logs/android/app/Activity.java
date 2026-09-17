package android.app;

import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.view.View;
import android.content.res.Resources;
import java.io.File;
import java.util.HashMap;
import java.util.Map;

public class Activity extends Context {
    private final ApplicationInfo applicationInfo = new ApplicationInfo();
    private final Resources resources = new Resources();
    private final Map<Integer, View> views = new HashMap<>();
    private Application application;
    private Intent intent;
    private boolean changingConfigurations;
    public Activity(File filesDir, String packageName) { super(filesDir, packageName); }
    public ApplicationInfo getApplicationInfo() { return applicationInfo; }
    public Intent getIntent() { return intent; }
    public void setIntent(Intent intent) { this.intent = intent; }
    public Application getApplication() { return application; }
    public void setApplication(Application application) { this.application = application; }
    public boolean isChangingConfigurations() { return changingConfigurations; }
    public void setChangingConfigurations(boolean value) { changingConfigurations = value; }
    public Resources getResources() { return resources; }
    public void addView(int id, View view, String name) { views.put(id, view); resources.addIdentifier(name, id); }
    public void removeView(int id) { views.remove(id); }
    public <T extends View> T findViewById(int id) { return (T) views.get(id); }
}
