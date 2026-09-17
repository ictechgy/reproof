package android.view;

import android.content.Context;
import android.content.res.Resources;

public class View {
    public interface OnClickListener {
        void onClick(View view);
    }

    private final Context context;
    private final Resources resources;
    private final int id;
    private OnClickListener listener;

    public View(Context context, Resources resources, int id) {
        this.context = context;
        this.resources = resources;
        this.id = id;
    }

    public Context getContext() {
        return context;
    }

    public Resources getResources() {
        return resources;
    }

    public int getId() {
        return id;
    }

    public void setOnClickListener(OnClickListener listener) {
        this.listener = listener;
    }

    public OnClickListener getOnClickListener() {
        return listener;
    }

    public void performClick() {
        if (listener != null) listener.onClick(this);
    }
}
