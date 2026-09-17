package android.content.res;

import java.util.HashMap;
import java.util.Map;

public class Resources {
    private final Map<String, Integer> ids = new HashMap<>();
    public void addIdentifier(String name, int id) { ids.put(name, id); }
    public int getIdentifier(String name, String type, String packageName) { return ids.getOrDefault(name, 0); }
}
