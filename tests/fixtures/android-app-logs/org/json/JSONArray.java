package org.json;

import java.util.ArrayList;
import java.util.List;

public class JSONArray {
    private final List<Object> values = new ArrayList<>();
    public JSONArray put(Object value) { values.add(value); return this; }
    public String toString() {
        StringBuilder result = new StringBuilder("[");
        for (int i = 0; i < values.size(); i++) {
            if (i > 0) result.append(',');
            Object value = values.get(i);
            result.append(value == null || value == JSONObject.NULL ? "null" : value.toString());
        }
        return result.append(']').toString();
    }
}
