package android.view;

import android.content.Context;

public class View {
    public static final int VISIBLE = 0;
    private final Context context;
    private final ViewTreeObserver observer = new ViewTreeObserver();
    private int visibility = VISIBLE;
    private boolean shown = true;
    public View(Context context) { this.context = context; }
    public Context getContext() { return context; }
    public int getVisibility() { return visibility; }
    public void setVisibility(int value) { visibility = value; }
    public boolean isShown() { return shown; }
    public void setShown(boolean value) { shown = value; }
    public ViewTreeObserver getViewTreeObserver() { return observer; }
}
