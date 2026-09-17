package android.view;

public class View {
    public interface OnClickListener {
        void onClick(View view);
    }

    private OnClickListener listener;

    public void setOnClickListener(OnClickListener listener) {
        this.listener = listener;
    }

    public void performClick() {
        if (listener != null) {
            listener.onClick(this);
        }
    }
}
