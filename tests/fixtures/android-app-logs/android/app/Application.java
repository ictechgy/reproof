package android.app;

import android.content.Context;
import android.os.Bundle;
import java.io.File;
import java.util.ArrayList;
import java.util.List;

public class Application extends Context {
    public interface ActivityLifecycleCallbacks {
        void onActivityCreated(Activity activity, Bundle state);
        void onActivityStarted(Activity activity);
        void onActivityResumed(Activity activity);
        void onActivityPaused(Activity activity);
        void onActivityStopped(Activity activity);
        void onActivitySaveInstanceState(Activity activity, Bundle state);
        void onActivityDestroyed(Activity activity);
    }

    private final List<ActivityLifecycleCallbacks> callbacks = new ArrayList<>();
    public Application(File filesDir, String packageName) { super(filesDir, packageName); }
    public void registerActivityLifecycleCallbacks(ActivityLifecycleCallbacks callback) { callbacks.add(callback); }
    public void created(Activity activity) { for (ActivityLifecycleCallbacks c : callbacks) c.onActivityCreated(activity, new Bundle()); }
    public void started(Activity activity) { for (ActivityLifecycleCallbacks c : callbacks) c.onActivityStarted(activity); }
    public void resumed(Activity activity) { for (ActivityLifecycleCallbacks c : callbacks) c.onActivityResumed(activity); }
    public void paused(Activity activity) { for (ActivityLifecycleCallbacks c : callbacks) c.onActivityPaused(activity); }
    public void stopped(Activity activity) { for (ActivityLifecycleCallbacks c : callbacks) c.onActivityStopped(activity); }
    public void saved(Activity activity) { for (ActivityLifecycleCallbacks c : callbacks) c.onActivitySaveInstanceState(activity, new Bundle()); }
    public void destroyed(Activity activity) { for (ActivityLifecycleCallbacks c : callbacks) c.onActivityDestroyed(activity); }
}
