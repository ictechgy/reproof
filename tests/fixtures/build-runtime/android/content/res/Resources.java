package android.content.res;

import java.util.HashMap;
import java.util.Map;

public class Resources {
    private final Map<Integer, String> names = new HashMap<>();

    public void addEntry(int id, String name) {
        names.put(id, name);
    }

    public String getResourceEntryName(int id) {
        String name = names.get(id);
        if (name == null) throw new NotFoundException();
        return name;
    }

    public static class NotFoundException extends RuntimeException {
    }
}
