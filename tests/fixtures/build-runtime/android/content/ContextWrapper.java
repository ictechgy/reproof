package android.content;

public class ContextWrapper extends Context {
    private Context baseContext;

    public ContextWrapper(Context baseContext) {
        this.baseContext = baseContext;
    }

    public Context getBaseContext() {
        return baseContext;
    }

    public void setBaseContext(Context baseContext) {
        this.baseContext = baseContext;
    }
}
