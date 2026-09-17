import io.reproloop.instrumentation.gradle.ReproBytecodeTransformer;
import java.io.IOException;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.tree.AbstractInsnNode;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.LineNumberNode;
import org.objectweb.asm.tree.MethodInsnNode;

public final class Harness {
    private static final String ACTIVITY = "io.reproloop.plain.MainActivity";
    private static final String ACTIVITY_INTERNAL = "io/reproloop/plain/MainActivity";
    private static final String SET_ON_CLICK = "(Landroid/view/View$OnClickListener;)V";

    private Harness() {
    }

    public static void main(String[] args) throws Exception {
        Path classes = Path.of(args[0]);
        byte[] original = Files.readAllBytes(classes.resolve(ACTIVITY_INTERNAL + ".class"));
        List<Integer> lines = clickLines(original);
        check(lines.size() == 4, "fixture should contain four listener sites: " + lines);

        Map<Integer, String> sites = new TreeMap<>();
        sites.put(lines.get(0), "s000001");
        sites.put(lines.get(1), "s000002");
        sites.put(lines.get(2), "s000003");
        ReproBytecodeTransformer.Result transformed = ReproBytecodeTransformer.transform(original, ACTIVITY, sites);
        check(transformed.matched().equals(sites), "all expected sites must match");
        check(countHookCalls(transformed.bytes(), "install") == 3, "exactly three installs");
        check(countCalls(transformed.bytes(), "setOnClickListener") == 2,
                "only the unrelated listeners remain as setter calls");

        runActivity(classes, transformed.bytes());

        Map<Integer, String> incomplete = new TreeMap<>(sites);
        incomplete.put(lines.get(1) + 100, "s000004");
        expectFailure(() -> ReproBytecodeTransformer.transform(original, ACTIVITY, incomplete),
                "missing site coverage must fail");
        expectFailure(() -> ReproBytecodeTransformer.transform(transformed.bytes(), ACTIVITY, sites),
                "repeated instrumentation must fail");

        byte[] noDestroy = Files.readAllBytes(classes.resolve("io/reproloop/plain/NoDestroyActivity.class"));
        ReproBytecodeTransformer.Result synthesized = ReproBytecodeTransformer.transform(
                noDestroy, "io.reproloop.plain.NoDestroyActivity", Map.of());
        ClassNode noDestroyNode = new ClassNode();
        new ClassReader(synthesized.bytes()).accept(noDestroyNode, 0);
        boolean foundDestroy = false;
        boolean foundStop = false;
        for (var method : noDestroyNode.methods) {
            if (method.name.equals("onDestroy") && method.desc.equals("()V")) {
                foundDestroy = true;
                check((method.access & Opcodes.ACC_PROTECTED) != 0, "synthesized destroy is protected");
                for (AbstractInsnNode instruction = method.instructions.getFirst(); instruction != null;
                        instruction = instruction.getNext()) {
                    if (instruction instanceof MethodInsnNode call
                            && call.owner.equals("io/reproloop/autotrace/ReproHooks")
                            && call.name.equals("stop")) {
                        foundStop = true;
                    }
                }
            }
        }
        check(foundDestroy && foundStop, "missing onDestroy receives a stop hook");
        byte[] inherited = Files.readAllBytes(classes.resolve("io/reproloop/plain/InheritedDestroyActivity.class"));
        expectFailure(() -> ReproBytecodeTransformer.transform(inherited,
                "io.reproloop.plain.InheritedDestroyActivity", Map.of()),
                "unknown inherited lifecycle must be rejected before emitting an illegal override");
        System.out.println("bytecode fixture passed");
    }

    private static void runActivity(Path classes, byte[] transformed) throws Exception {
        io.reproloop.autotrace.ReproHooks.reset();
        ClassLoader loader = new TargetLoader(Harness.class.getClassLoader(), ACTIVITY, transformed);
        Class<?> type = Class.forName(ACTIVITY, true, loader);
        Object activity = type.getConstructor().newInstance();
        type.getMethod("onCreate", android.os.Bundle.class).invoke(activity, new android.os.Bundle());
        check(io.reproloop.autotrace.ReproHooks.starts == 1, "onCreate starts recording once");
        check(io.reproloop.autotrace.ReproHooks.installs == 3, "only configured sites are installed");

        click(type, activity, "normal");
        check(intField(type, activity, "normalCalls") == 1, "normal callback remains callable");
        check(io.reproloop.autotrace.ReproHooks.before == 1
                && io.reproloop.autotrace.ReproHooks.after == 1, "normal before/after hooks run");
        check("s000001".equals(io.reproloop.autotrace.ReproHooks.lastSite), "site id reaches runtime");

        click(type, activity, "labeledReturn");
        check(intField(type, activity, "labeledCalls") == 0, "labeled return still exits callback");
        check(io.reproloop.autotrace.ReproHooks.before == 2
                && io.reproloop.autotrace.ReproHooks.after == 2, "labeled callback remains wrapped");

        try {
            click(type, activity, "throwing");
            throw new AssertionError("throwing callback must propagate");
        } catch (InvocationTargetException error) {
            check(error.getCause() instanceof IllegalStateException, "callback exception is preserved");
        }
        check(intField(type, activity, "throwCalls") == 0, "throwing fixture does not mutate before throw");
        check(io.reproloop.autotrace.ReproHooks.thrown == 1
                && io.reproloop.autotrace.ReproHooks.after == 3, "throw path records and rethrows");

        click(type, activity, "unrelated");
        check(intField(type, activity, "normalCalls") == 101, "unmapped listener remains untouched");
        check(io.reproloop.autotrace.ReproHooks.installs == 3, "unmapped listener did not get a hook");
        type.getMethod("unrelatedListener").invoke(activity);
        click(type, activity, "unrelated");
        check(intField(type, activity, "normalCalls") == 111, "unmapped method remains untouched");
        check(io.reproloop.autotrace.ReproHooks.installs == 3, "unmapped method did not get a hook");

        type.getMethod("onDestroy").invoke(activity);
        check(io.reproloop.autotrace.ReproHooks.stops == 1, "existing onDestroy receives stop hook");
        check(intField(type, activity, "destroyCalls") == 1, "existing onDestroy body remains callable");
    }

    private static void click(Class<?> type, Object activity, String field) throws Exception {
        Field value = type.getField(field);
        value.get(activity).getClass().getMethod("performClick").invoke(value.get(activity));
    }

    private static int intField(Class<?> type, Object activity, String field) throws Exception {
        return type.getField(field).getInt(activity);
    }

    private static List<Integer> clickLines(byte[] bytes) {
        ClassNode node = new ClassNode();
        new ClassReader(bytes).accept(node, 0);
        List<Integer> result = new ArrayList<>();
        for (var method : node.methods) {
            if (!method.name.equals("onCreate")) {
                continue;
            }
            int line = -1;
            for (AbstractInsnNode instruction = method.instructions.getFirst(); instruction != null;
                    instruction = instruction.getNext()) {
                if (instruction instanceof LineNumberNode lineNumber) {
                    line = lineNumber.line;
                } else if (instruction instanceof MethodInsnNode call
                        && call.getOpcode() == Opcodes.INVOKEVIRTUAL
                        && call.name.equals("setOnClickListener") && call.desc.equals(SET_ON_CLICK)) {
                    result.add(line);
                }
            }
        }
        result.sort(Integer::compareTo);
        return result;
    }

    private static int countHookCalls(byte[] bytes, String name) {
        ClassNode node = new ClassNode();
        new ClassReader(bytes).accept(node, 0);
        int count = 0;
        for (var method : node.methods) {
            for (AbstractInsnNode instruction = method.instructions.getFirst(); instruction != null;
                    instruction = instruction.getNext()) {
                if (instruction instanceof MethodInsnNode call
                        && call.owner.equals("io/reproloop/autotrace/ReproHooks") && call.name.equals(name)) {
                    count++;
                }
            }
        }
        return count;
    }

    private static int countCalls(byte[] bytes, String name) {
        ClassNode node = new ClassNode();
        new ClassReader(bytes).accept(node, 0);
        int count = 0;
        for (var method : node.methods) {
            for (AbstractInsnNode instruction = method.instructions.getFirst(); instruction != null;
                    instruction = instruction.getNext()) {
                if (instruction instanceof MethodInsnNode call && call.name.equals(name)) {
                    count++;
                }
            }
        }
        return count;
    }

    private static void expectFailure(ThrowingRunnable action, String message) throws Exception {
        try {
            action.run();
        } catch (RuntimeException expected) {
            return;
        }
        throw new AssertionError(message);
    }

    private static void check(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
    }

    @FunctionalInterface
    private interface ThrowingRunnable {
        void run() throws Exception;
    }

    private static final class TargetLoader extends ClassLoader {
        private final String target;
        private final byte[] bytes;

        TargetLoader(ClassLoader parent, String target, byte[] bytes) {
            super(parent);
            this.target = target;
            this.bytes = bytes;
        }

        @Override
        protected Class<?> loadClass(String name, boolean resolve) throws ClassNotFoundException {
            if (name.equals(target)) {
                Class<?> found = findLoadedClass(name);
                if (found == null) {
                    found = defineClass(name, bytes, 0, bytes.length);
                }
                if (resolve) {
                    resolveClass(found);
                }
                return found;
            }
            return super.loadClass(name, resolve);
        }
    }
}
