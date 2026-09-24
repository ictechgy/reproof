package io.reproof.instrumentation.gradle;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import java.util.zip.ZipOutputStream;
import org.gradle.api.DefaultTask;
import org.gradle.api.file.Directory;
import org.gradle.api.file.RegularFile;
import org.gradle.api.file.RegularFileProperty;
import org.gradle.api.provider.ListProperty;
import org.gradle.api.provider.Property;
import org.gradle.api.tasks.CacheableTask;
import org.gradle.api.tasks.Input;
import org.gradle.api.tasks.InputFiles;
import org.gradle.api.tasks.OutputFile;
import org.gradle.api.tasks.PathSensitive;
import org.gradle.api.tasks.PathSensitivity;
import org.gradle.api.tasks.TaskAction;

/** One cacheable, deterministic transform over the selected project's classes. */
@CacheableTask
public abstract class ReproInstrumentationTask extends DefaultTask {
    private static final int BUFFER_SIZE = 64 * 1024;

    @Input
    public abstract Property<String> getModule();

    @Input
    public abstract Property<String> getVariant();

    @Input
    public abstract Property<String> getActivity();

    @Input
    public abstract Property<String> getProfileDigest();

    /** Canonical entries encoded as decimal source line + '=' + site id. */
    @Input
    public abstract ListProperty<String> getSites();

    @InputFiles
    @PathSensitive(PathSensitivity.RELATIVE)
    public abstract ListProperty<RegularFile> getAllJars();

    @InputFiles
    @PathSensitive(PathSensitivity.RELATIVE)
    public abstract ListProperty<Directory> getAllDirectories();

    @OutputFile
    public abstract RegularFileProperty getOutputJar();

    @OutputFile
    public abstract RegularFileProperty getReport();

    /** Stable proof copy; AGP may replace the scoped transform output location internally. */
    @OutputFile
    public abstract RegularFileProperty getFinalJar();

    @TaskAction
    public final void transform() {
        String activity = requireText(getActivity().get(), "activity");
        String profileDigest = requireDigest(getProfileDigest().get());
        TreeMap<Integer, String> configuredSites = parseSites(getSites().get());
        String targetEntry = activity.replace('.', '/') + ".class";

        TreeMap<String, byte[]> entries = new TreeMap<>();
        int targetCount = 0;
        byte[] inputActivity = null;
        int[] directoryTargetCount = new int[1];
        byte[][] directoryTarget = new byte[1][];
        for (RegularFile regularFile : sortedFiles(getAllJars().get())) {
            Path jar = regularFile.getAsFile().toPath();
            requireRegularFile(jar, "Input class jar");
            try (ZipFile zip = new ZipFile(jar.toFile())) {
                Enumeration<? extends ZipEntry> enumeration = zip.entries();
                while (enumeration.hasMoreElements()) {
                    ZipEntry entry = enumeration.nextElement();
                    String name = safeEntryName(entry.getName());
                    byte[] bytes = entry.isDirectory() ? new byte[0] : readZipEntry(zip, entry);
                    if (name.equals(targetEntry)) {
                        targetCount++;
                        inputActivity = bytes;
                    }
                    addEntry(entries, name, bytes);
                }
            } catch (IOException error) {
                throw failure("Unable to read input class jar " + jar, error);
            }
        }
        for (Directory directory : sortedDirectories(getAllDirectories().get())) {
            Path root = directory.getAsFile().toPath();
            requireDirectory(root, "Input class directory");
            readDirectory(root, entries, targetEntry, new TargetBytes() {
                @Override
                public void found(byte[] bytes) {
                    directoryTargetCount[0]++;
                    directoryTarget[0] = bytes;
                }
            });
        }
        // The visitor callback above is intentionally isolated from mutable task state for the
        // file walker. Copy the values back after all directories have been visited.
        targetCount += directoryTargetCount[0];
        if (inputActivity == null) {
            inputActivity = directoryTarget[0];
        }
        require(targetCount == 1 && inputActivity != null,
                targetCount == 0 ? "Selected activity class is missing" : "Selected activity class is duplicated");

        ReproBytecodeTransformer.Result result = ReproBytecodeTransformer.transform(
                inputActivity, activity, configuredSites);
        require(result.matched().equals(configuredSites), "Transformer did not match all configured sites");
        entries.put(targetEntry, result.bytes());

        Path outputJar = getOutputJar().get().getAsFile().toPath();
        Path report = getReport().get().getAsFile().toPath();
        require(!outputJar.toAbsolutePath().normalize().equals(report.toAbsolutePath().normalize()),
                "Output jar and report must be different files");
        try {
            writeJar(outputJar, entries);
            Path finalJar = getFinalJar().get().getAsFile().toPath();
            if (!outputJar.toAbsolutePath().normalize().equals(finalJar.toAbsolutePath().normalize())) {
                copyFile(outputJar, finalJar);
            }
            String reportText = buildReport(profileDigest, activity, result.matched(),
                    sha256(inputActivity), sha256(result.bytes()), sha256(finalJar));
            writeText(report, reportText);
        } catch (IOException error) {
            throw failure("Unable to write deterministic instrumentation outputs", error);
        }
    }

    private static void readDirectory(Path root, TreeMap<String, byte[]> entries, String targetEntry,
            TargetBytes target) {
        try {
            Files.walkFileTree(root, new SimpleFileVisitor<>() {
                @Override
                public FileVisitResult preVisitDirectory(Path directory, BasicFileAttributes attributes) {
                    rejectSymlink(directory, "Input class directory");
                    return FileVisitResult.CONTINUE;
                }

                @Override
                public FileVisitResult visitFile(Path file, BasicFileAttributes attributes) {
                    rejectSymlink(file, "Input class file");
                    require(attributes.isRegularFile(), "Input class tree contains a non-file: " + file);
                    String name = safeEntryName(root.relativize(file).toString()
                            .replace(java.io.File.separatorChar, '/'));
                    try {
                        byte[] bytes = Files.readAllBytes(file);
                        if (name.equals(targetEntry)) {
                            target.found(bytes);
                        }
                        addEntry(entries, name, bytes);
                    } catch (IOException error) {
                        throw failure("Unable to read input class file " + file, error);
                    }
                    return FileVisitResult.CONTINUE;
                }
            });
        } catch (IOException error) {
            throw failure("Unable to walk input class directory " + root, error);
        }
    }

    private static void writeJar(Path destination, TreeMap<String, byte[]> entries) throws IOException {
        Path parent = destination.toAbsolutePath().normalize().getParent();
        require(parent != null, "Output jar has no parent directory");
        Files.createDirectories(parent);
        Path temporary = Files.createTempFile(parent, ".repro-classes-", ".tmp");
        boolean moved = false;
        try (OutputStream stream = Files.newOutputStream(temporary);
                ZipOutputStream zip = new ZipOutputStream(stream, StandardCharsets.UTF_8)) {
            for (Map.Entry<String, byte[]> entry : entries.entrySet()) {
                ZipEntry output = new ZipEntry(entry.getKey());
                output.setTime(0L);
                byte[] bytes = entry.getValue();
                if (entry.getKey().endsWith("/")) {
                    output.setMethod(ZipEntry.STORED);
                    output.setSize(0L);
                    output.setCompressedSize(0L);
                    output.setCrc(0L);
                }
                zip.putNextEntry(output);
                if (bytes.length > 0) {
                    zip.write(bytes);
                }
                zip.closeEntry();
            }
        }
        try {
            Files.move(temporary, destination, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            moved = true;
        } catch (AtomicMoveNotSupportedException ignored) {
            Files.move(temporary, destination, StandardCopyOption.REPLACE_EXISTING);
            moved = true;
        } finally {
            if (!moved) {
                Files.deleteIfExists(temporary);
            }
        }
    }

    private static void copyFile(Path source, Path destination) throws IOException {
        Path parent = destination.toAbsolutePath().normalize().getParent();
        require(parent != null, "Output jar has no parent directory");
        Files.createDirectories(parent);
        Path temporary = Files.createTempFile(parent, ".repro-classes-copy-", ".tmp");
        boolean moved = false;
        try {
            Files.copy(source, temporary, StandardCopyOption.REPLACE_EXISTING);
            try {
                Files.move(temporary, destination, StandardCopyOption.ATOMIC_MOVE,
                        StandardCopyOption.REPLACE_EXISTING);
            } catch (AtomicMoveNotSupportedException ignored) {
                Files.move(temporary, destination, StandardCopyOption.REPLACE_EXISTING);
            }
            moved = true;
        } finally {
            if (!moved) {
                Files.deleteIfExists(temporary);
            }
        }
    }

    private static String buildReport(String profileDigest, String activity, Map<Integer, String> sites,
            String inputClassSha256, String outputClassSha256, String outputJarSha256) {
        StringBuilder text = new StringBuilder(512);
        text.append('{').append("\"schemaVersion\":1");
        field(text, "kind", quote("android_asm_v1"));
        field(text, "appProfileDigest", quote(profileDigest));
        field(text, "activityClass", quote(activity));
        field(text, "instrumentedClasses", "1");
        text.append(",\"sites\":[");
        boolean first = true;
        for (Map.Entry<Integer, String> site : sites.entrySet()) {
            if (!first) {
                text.append(',');
            }
            first = false;
            text.append("{\"line\":").append(site.getKey())
                    .append(",\"id\":").append(quote(site.getValue())).append('}');
        }
        text.append(']');
        text.append(",\"lifecycle\":{\"onCreate\":true,\"onDestroy\":true}");
        field(text, "inputClassSha256", quote(inputClassSha256));
        field(text, "outputClassSha256", quote(outputClassSha256));
        field(text, "outputJarSha256", quote(outputJarSha256));
        text.append('}').append('\n');
        return text.toString();
    }

    private static void field(StringBuilder text, String name, String value) {
        text.append(',').append(quote(name)).append(':').append(value);
    }

    private static String quote(String value) {
        StringBuilder quoted = new StringBuilder(value.length() + 2).append('"');
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            switch (character) {
                case '"' -> quoted.append("\\\"");
                case '\\' -> quoted.append("\\\\");
                case '\n' -> quoted.append("\\n");
                case '\r' -> quoted.append("\\r");
                case '\t' -> quoted.append("\\t");
                default -> {
                    if (character < 0x20) {
                        quoted.append(String.format("\\u%04x", (int) character));
                    } else {
                        quoted.append(character);
                    }
                }
            }
        }
        return quoted.append('"').toString();
    }

    private static TreeMap<Integer, String> parseSites(List<String> encodedSites) {
        TreeMap<Integer, String> sites = new TreeMap<>();
        require(encodedSites != null, "ReproPlan.SITES is missing");
        for (String encoded : encodedSites) {
            require(encoded != null, "ReproPlan.SITES contains a null entry");
            int separator = encoded.indexOf('=');
            require(separator > 0 && separator < encoded.length() - 1, "Invalid encoded ReproPlan site");
            int line;
            try {
                line = Integer.parseInt(encoded.substring(0, separator));
            } catch (NumberFormatException error) {
                throw failure("Invalid encoded ReproPlan site line", error);
            }
            String id = encoded.substring(separator + 1);
            require(line > 0 && id.matches("s[0-9a-f]+"), "Invalid encoded ReproPlan site");
            require(sites.put(line, id) == null, "Duplicate configured site line " + line);
        }
        require(sites.size() == new java.util.HashSet<>(sites.values()).size(),
                "Configured site ids must be unique");
        return sites;
    }

    private static List<RegularFile> sortedFiles(List<RegularFile> files) {
        List<RegularFile> result = new ArrayList<>(files == null ? Collections.emptyList() : files);
        result.sort((left, right) -> left.getAsFile().toPath().toString()
                .compareTo(right.getAsFile().toPath().toString()));
        return result;
    }

    private static List<Directory> sortedDirectories(List<Directory> directories) {
        List<Directory> result = new ArrayList<>(directories == null ? Collections.emptyList() : directories);
        result.sort((left, right) -> left.getAsFile().toPath().toString()
                .compareTo(right.getAsFile().toPath().toString()));
        return result;
    }

    private static byte[] readZipEntry(ZipFile zip, ZipEntry entry) {
        try (InputStream stream = zip.getInputStream(entry)) {
            return stream.readAllBytes();
        } catch (IOException error) {
            throw failure("Unable to read zip entry " + entry.getName(), error);
        }
    }

    private static void addEntry(TreeMap<String, byte[]> entries, String name, byte[] bytes) {
        byte[] previous = entries.putIfAbsent(name, bytes);
        require(previous == null || java.util.Arrays.equals(previous, bytes),
                "Conflicting duplicate class entry " + name);
    }

    private static String safeEntryName(String raw) {
        require(raw != null && !raw.isEmpty() && !raw.startsWith("/") && !raw.startsWith("\\")
                && raw.indexOf('\u0000') < 0 && raw.indexOf('\\') < 0
                && !(raw.length() >= 2 && Character.isLetter(raw.charAt(0)) && raw.charAt(1) == ':'),
                "Invalid class entry path");
        boolean directory = raw.endsWith("/");
        String body = directory ? raw.substring(0, raw.length() - 1) : raw;
        require(!body.isEmpty(), "Invalid empty class entry path");
        for (String part : body.split("/", -1)) {
            require(!part.isEmpty() && !part.equals(".") && !part.equals(".."),
                    "Invalid class entry path");
        }
        return directory ? body + "/" : body;
    }

    private static void requireRegularFile(Path file, String description) {
        rejectSymlink(file, description);
        require(Files.isRegularFile(file, LinkOption.NOFOLLOW_LINKS), description + " is not a regular file: " + file);
    }

    private static void requireDirectory(Path directory, String description) {
        rejectSymlink(directory, description);
        require(Files.isDirectory(directory, LinkOption.NOFOLLOW_LINKS), description + " is invalid: " + directory);
    }

    private static void rejectSymlink(Path path, String description) {
        require(!Files.isSymbolicLink(path), description + " symlinks are not allowed: " + path);
    }

    private static String requireText(String value, String name) {
        require(value != null && !value.isEmpty(), name + " is missing");
        return value;
    }

    private static String requireDigest(String value) {
        require(value != null && value.matches("[0-9a-f]{64}"), "profile digest is invalid");
        return value;
    }

    private static String sha256(byte[] bytes) {
        try {
            return hex(MessageDigest.getInstance("SHA-256").digest(bytes));
        } catch (NoSuchAlgorithmException error) {
            throw failure("SHA-256 is unavailable", error);
        }
    }

    private static String sha256(Path file) throws IOException {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            try (InputStream stream = Files.newInputStream(file)) {
                byte[] buffer = new byte[BUFFER_SIZE];
                int count;
                while ((count = stream.read(buffer)) >= 0) {
                    if (count > 0) {
                        digest.update(buffer, 0, count);
                    }
                }
            }
            return hex(digest.digest());
        } catch (NoSuchAlgorithmException error) {
            throw failure("SHA-256 is unavailable", error);
        }
    }

    private static String hex(byte[] bytes) {
        StringBuilder result = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) {
            result.append(String.format("%02x", value & 0xff));
        }
        return result.toString();
    }

    private static void writeText(Path destination, String text) throws IOException {
        Path parent = destination.toAbsolutePath().normalize().getParent();
        require(parent != null, "Report has no parent directory");
        Files.createDirectories(parent);
        Path temporary = Files.createTempFile(parent, ".repro-report-", ".tmp");
        boolean moved = false;
        try {
            Files.writeString(temporary, text, StandardCharsets.UTF_8);
            try {
                Files.move(temporary, destination, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            } catch (AtomicMoveNotSupportedException ignored) {
                Files.move(temporary, destination, StandardCopyOption.REPLACE_EXISTING);
            }
            moved = true;
        } finally {
            if (!moved) {
                Files.deleteIfExists(temporary);
            }
        }
    }

    private static IllegalStateException failure(String message, Throwable cause) {
        return new IllegalStateException(message, cause);
    }

    private static void require(boolean condition, String message) {
        if (!condition) {
            throw new IllegalStateException(message);
        }
    }

    private interface TargetBytes {
        void found(byte[] bytes);
    }
}
