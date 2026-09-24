package io.reproloop.instrumentation.gradle;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Pattern;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.Type;
import org.objectweb.asm.tree.AbstractInsnNode;
import org.objectweb.asm.tree.ClassNode;
import org.objectweb.asm.tree.InsnList;
import org.objectweb.asm.tree.InsnNode;
import org.objectweb.asm.tree.LabelNode;
import org.objectweb.asm.tree.LineNumberNode;
import org.objectweb.asm.tree.MethodInsnNode;
import org.objectweb.asm.tree.MethodNode;
import org.objectweb.asm.tree.VarInsnNode;

/** A deliberately narrow ASM transform for one selected Android activity. */
public final class ReproBytecodeTransformer {
    public static final String HOOK_OWNER = "io/reproloop/autotrace/ReproHooks";
    public static final String INSTALL_DESCRIPTOR =
            "(Landroid/view/View;Landroid/view/View$OnClickListener;Ljava/lang/String;)V";
    private static final String SET_ON_CLICK_DESCRIPTOR =
            "(Landroid/view/View$OnClickListener;)V";
    private static final String START_DESCRIPTOR = "(Landroid/app/Activity;)V";
    private static final Pattern SITE_ID = Pattern.compile("s[0-9a-f]+[0-9a-f]*");

    private ReproBytecodeTransformer() {
    }

    public static Result transform(byte[] input, String activityClass, Map<Integer, String> configuredSites) {
        require(input != null && input.length > 0, "Selected activity class is empty");
        String expectedInternal = requireActivityName(activityClass);
        TreeMap<Integer, String> sites = validateSites(configuredSites);

        ClassReader reader;
        try {
            reader = new ClassReader(input);
        } catch (RuntimeException error) {
            throw new IllegalArgumentException("Selected activity class is not valid bytecode", error);
        }
        require(expectedInternal.equals(reader.getClassName()),
                "Selected activity class name does not match ReproPlan.ACTIVITY");

        ClassNode classNode = new ClassNode();
        reader.accept(classNode, 0);
        require(classNode.methods != null, "Selected activity has no methods");
        rejectExistingHooks(classNode);

        TreeMap<Integer, String> matched = new TreeMap<>();
        for (MethodNode method : classNode.methods) {
            int currentLine = -1;
            AbstractInsnNode instruction = method.instructions.getFirst();
            while (instruction != null) {
                AbstractInsnNode next = instruction.getNext();
                if (instruction instanceof LineNumberNode lineNumber) {
                    currentLine = lineNumber.line;
                    instruction = next;
                    continue;
                }
                if (!(instruction instanceof MethodInsnNode call)
                        || call.getOpcode() != Opcodes.INVOKEVIRTUAL
                        || !call.name.equals("setOnClickListener")
                        || !call.desc.equals(SET_ON_CLICK_DESCRIPTOR)) {
                    instruction = next;
                    continue;
                }
                String siteId = sites.get(currentLine);
                if (siteId == null) {
                    instruction = next;
                    continue;
                }
                require(!matched.containsKey(currentLine),
                        "More than one setOnClickListener call matches site line " + currentLine);
                InsnList replacement = new InsnList();
                replacement.add(new org.objectweb.asm.tree.LdcInsnNode(siteId));
                replacement.add(new MethodInsnNode(
                        Opcodes.INVOKESTATIC, HOOK_OWNER, "install", INSTALL_DESCRIPTOR, false));
                method.instructions.insertBefore(call, replacement);
                method.instructions.remove(call);
                matched.put(currentLine, siteId);
                instruction = next;
            }
        }
        require(matched.size() == sites.size(), missingCoverage(sites, matched));

        MethodNode onCreate = concreteLifecycle(classNode, "onCreate", "(Landroid/os/Bundle;)V");
        int returnCount = 0;
        for (AbstractInsnNode instruction = onCreate.instructions.getFirst(); instruction != null;
                instruction = instruction.getNext()) {
            if (instruction.getOpcode() == Opcodes.RETURN) {
                returnCount++;
                InsnList start = new InsnList();
                start.add(new VarInsnNode(Opcodes.ALOAD, 0));
                start.add(new MethodInsnNode(Opcodes.INVOKESTATIC, HOOK_OWNER, "start", START_DESCRIPTOR, false));
                onCreate.instructions.insertBefore(instruction, start);
            }
        }
        require(returnCount > 0, "onCreate(Bundle):void has no normal return");

        MethodNode onDestroy = findMethod(classNode, "onDestroy", "()V");
        if (onDestroy == null) {
            require("android/app/Activity".equals(classNode.superName),
                    "Inherited onDestroy requires superclass analysis before instrumentation");
            onDestroy = new MethodNode(Opcodes.ACC_PROTECTED, "onDestroy", "()V", null, null);
            InsnList body = onDestroy.instructions;
            body.add(new VarInsnNode(Opcodes.ALOAD, 0));
            body.add(new MethodInsnNode(Opcodes.INVOKESTATIC, HOOK_OWNER, "stop", START_DESCRIPTOR, false));
            body.add(new VarInsnNode(Opcodes.ALOAD, 0));
            body.add(new MethodInsnNode(Opcodes.INVOKESPECIAL, classNode.superName, "onDestroy", "()V", false));
            body.add(new InsnNode(Opcodes.RETURN));
            classNode.methods.add(onDestroy);
        } else {
            requireConcrete(onDestroy, "onDestroy():void");
            InsnList stop = new InsnList();
            stop.add(new VarInsnNode(Opcodes.ALOAD, 0));
            stop.add(new MethodInsnNode(Opcodes.INVOKESTATIC, HOOK_OWNER, "stop", START_DESCRIPTOR, false));
            AbstractInsnNode first = firstExecutable(onDestroy.instructions);
            require(first != null, "onDestroy():void has no body");
            onDestroy.instructions.insertBefore(first, stop);
        }

        ClassWriter writer = new ClassWriter(reader, ClassWriter.COMPUTE_MAXS);
        classNode.accept(writer);
        return new Result(writer.toByteArray(), matched);
    }

    private static void rejectExistingHooks(ClassNode classNode) {
        for (MethodNode method : classNode.methods) {
            for (AbstractInsnNode instruction = method.instructions.getFirst(); instruction != null;
                    instruction = instruction.getNext()) {
                if (instruction instanceof MethodInsnNode call && call.owner.equals(HOOK_OWNER)) {
                    throw new IllegalArgumentException("Selected activity already contains ReproHooks calls");
                }
            }
        }
    }

    private static MethodNode concreteLifecycle(ClassNode classNode, String name, String descriptor) {
        MethodNode method = findMethod(classNode, name, descriptor);
        require(method != null, "Selected activity must declare " + name + descriptor);
        requireConcrete(method, name + descriptor);
        return method;
    }

    private static void requireConcrete(MethodNode method, String description) {
        require((method.access & (Opcodes.ACC_ABSTRACT | Opcodes.ACC_NATIVE | Opcodes.ACC_STATIC)) == 0,
                description + " must be concrete and non-static");
    }

    private static MethodNode findMethod(ClassNode classNode, String name, String descriptor) {
        for (MethodNode method : classNode.methods) {
            if (method.name.equals(name) && method.desc.equals(descriptor)) {
                return method;
            }
        }
        return null;
    }

    private static AbstractInsnNode firstExecutable(InsnList instructions) {
        for (AbstractInsnNode instruction = instructions.getFirst(); instruction != null;
                instruction = instruction.getNext()) {
            int type = instruction.getType();
            if (type != AbstractInsnNode.LABEL && type != AbstractInsnNode.LINE
                    && type != AbstractInsnNode.FRAME) {
                return instruction;
            }
        }
        return null;
    }

    private static String missingCoverage(Map<Integer, String> sites, Map<Integer, String> matched) {
        TreeMap<Integer, String> missing = new TreeMap<>(sites);
        missing.keySet().removeAll(matched.keySet());
        return "Configured site lines have incomplete bytecode coverage: " + missing.keySet();
    }

    private static TreeMap<Integer, String> validateSites(Map<Integer, String> configuredSites) {
        require(configuredSites != null, "ReproPlan.SITES is missing");
        TreeMap<Integer, String> sites = new TreeMap<>();
        for (Map.Entry<Integer, String> entry : configuredSites.entrySet()) {
            Integer line = entry.getKey();
            String id = entry.getValue();
            require(line != null && line > 0, "Configured site line is invalid");
            require(id != null && SITE_ID.matcher(id).matches(), "Configured site id is invalid");
            require(sites.put(line, id) == null, "Duplicate configured site line " + line);
        }
        require(new java.util.HashSet<>(sites.values()).size() == sites.size(),
                "Configured site ids must be unique");
        return sites;
    }

    private static String requireActivityName(String activityClass) {
        require(activityClass != null && activityClass.matches("[A-Za-z_$][A-Za-z0-9_$.]*"),
                "ReproPlan.ACTIVITY is invalid");
        return activityClass.replace('.', '/');
    }

    private static void require(boolean condition, String message) {
        if (!condition) {
            throw new IllegalArgumentException(message);
        }
    }

    public static final class Result {
        private final byte[] bytes;
        private final Map<Integer, String> matched;

        private Result(byte[] bytes, Map<Integer, String> matched) {
            this.bytes = bytes;
            this.matched = Collections.unmodifiableMap(new LinkedHashMap<>(matched));
        }

        public byte[] bytes() {
            return bytes;
        }

        public Map<Integer, String> matched() {
            return matched;
        }
    }
}
