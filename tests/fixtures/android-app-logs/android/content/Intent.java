package android.content;

import java.util.HashMap;
import java.util.Map;

public class Intent {
    private final Map<String, Object> values = new HashMap<>();
    public Intent putExtra(String key, String value) { values.put(key, value); return this; }
    public Intent putExtra(String key, int value) { values.put(key, value); return this; }
    public String getStringExtra(String key) { return (String) values.get(key); }
    public int getIntExtra(String key, int fallback) {
        Object value = values.get(key);
        return value instanceof Integer ? (Integer) value : fallback;
    }
}
