package android.content;

import java.util.HashMap;
import java.util.Map;

public class Intent {
    private final Map<String, String> strings = new HashMap<>();

    public Intent putExtra(String key, String value) {
        strings.put(key, value);
        return this;
    }

    public String getStringExtra(String key) {
        return strings.get(key);
    }
}
