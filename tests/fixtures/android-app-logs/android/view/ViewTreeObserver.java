package android.view;

import java.util.ArrayList;
import java.util.List;

public class ViewTreeObserver {
    public interface OnGlobalLayoutListener { void onGlobalLayout(); }
    private final List<OnGlobalLayoutListener> listeners = new ArrayList<>();
    public void addOnGlobalLayoutListener(OnGlobalLayoutListener listener) { listeners.add(listener); }
    public void removeOnGlobalLayoutListener(OnGlobalLayoutListener listener) { listeners.remove(listener); }
    public void dispatch() { for (OnGlobalLayoutListener listener : new ArrayList<>(listeners)) listener.onGlobalLayout(); }
}
