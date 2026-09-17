package org.json;

import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;

public class JSONObject {
    public static final Object NULL = new Object() { public String toString() { return "null"; } };
    private final Map<String, Object> values = new LinkedHashMap<>();
    public JSONObject() {}
    public JSONObject(String source) {
        String body = source.trim();
        if (body.length() < 2 || body.charAt(0) != '{' || body.charAt(body.length() - 1) != '}') throw new IllegalArgumentException();
        body = body.substring(1, body.length() - 1).trim();
        if (body.isEmpty()) return;
        for (String pair : body.split(",")) {
            String[] parts = pair.split(":", 2);
            if (parts.length != 2) throw new IllegalArgumentException();
            String key = unquote(parts[0].trim());
            values.put(key, unquote(parts[1].trim()));
        }
    }
    public int length() { return values.size(); }
    public Iterator<String> keys() { return values.keySet().iterator(); }
    public Object opt(String key) { return values.get(key); }
    public JSONObject put(String key, Object value) { values.put(key, value); return this; }
    public String toString() {
        StringBuilder result = new StringBuilder("{");
        boolean first = true;
        for (Map.Entry<String, Object> entry : values.entrySet()) {
            if (!first) result.append(',');
            first = false;
            result.append(quote(entry.getKey())).append(':').append(value(entry.getValue()));
        }
        return result.append('}').toString();
    }
    private static String unquote(String value) { return value.length() >= 2 && value.charAt(0) == '"' ? value.substring(1, value.length() - 1) : value; }
    private static String quote(String value) { return "\"" + value.replace("\"", "\\\"") + "\""; }
    private static String value(Object value) { return value == null || value == NULL ? "null" : value instanceof String ? quote((String) value) : value.toString(); }
}
