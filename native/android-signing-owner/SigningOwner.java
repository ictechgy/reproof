package io.reproof.signing;

import com.android.apksig.ApkSigner;
import com.android.apksig.ApkVerifier;
import com.android.apksig.KeyConfig;
import com.android.apksig.apk.ApkUtils;
import com.android.apksig.internal.apk.AndroidBinXmlParser;
import com.android.apksig.util.DataSources;
import com.android.apksig.util.DataSink;
import com.android.apksig.util.DataSinks;

import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.lang.reflect.Field;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFilePermission;
import java.nio.file.attribute.PosixFilePermissions;
import java.security.Key;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/**
 * One fixed JVM owns Android signing. It never launches apksigner, aapt2, or
 * another child. All operation metadata and secrets arrive on inherited FDs.
 * A termination record is an fsynced exit intent; only subsequent acquisition
 * of the canonical flock proves that this OS process has exited.
 */
public final class SigningOwner {
    static {
        System.loadLibrary("reproof_signing_owner_fd");
    }
    private static final int MAX_CONFIG_BYTES = 64 * 1024;
    private static final int MAX_SECRET_BYTES = 2048;
    private static final long MAX_APK_BYTES = 64L * 1024 * 1024;
    private static final String ANDROID_NS = "http://schemas.android.com/apk/res/android";
    private static final Pattern ID = Pattern.compile("[a-z][a-z0-9_-]{0,63}");
    private static final Pattern DIGEST = Pattern.compile("[0-9a-f]{64}");
    private static final Pattern PACKAGE = Pattern.compile(
            "[A-Za-z][A-Za-z0-9_]*(?:\\.[A-Za-z][A-Za-z0-9_]*)+");
    private static final Pattern PERMISSION = PACKAGE;
    private static final Pattern ALIAS = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._-]{0,127}");
    private static final Set<String> MODES = Set.of("sign", "inspect", "liveness-probe");
    private static final Set<String> CONFIG_KEYS = Set.of(
            "schemaVersion", "mode", "operationId", "requestDigest", "contextDigest",
            "scopeDigest", "ownerDefinitionDigest", "inputPath", "outputPath",
            "inputDigest", "maxBytes", "packageName", "certificateSha256", "schemes",
            "usesPermissions", "declaredPermissions", "lockPath", "startPath",
            "terminationPath", "workPath", "keyAlias");

    private static FileInputStream lockHandle;
    private static FileDescriptor directoryDescriptor;
    private static FileOutputStream terminationStream;
    private static final AtomicBoolean parentLost = new AtomicBoolean(false);
    private static final CountDownLatch normalExit = new CountDownLatch(1);
    private static volatile Config config;
    private static volatile String stage = "configuration";

    private SigningOwner() {}

    private static native boolean validateFdPathNative(
            int descriptor, String path, boolean directory, boolean zeroSize,
            boolean requireLock);
    private static native boolean validatePrivateRegularFdNative(int descriptor);

    private static final class Rejected extends Exception {
        private static final long serialVersionUID = 1L;
        Rejected() { super(); }
        Rejected(Throwable cause) { super(cause); }
    }

    private static final class Config {
        final String mode;
        final String operationId;
        final String requestDigest;
        final String contextDigest;
        final String scopeDigest;
        final String ownerDefinitionDigest;
        final Path input;
        final Path output;
        final String inputDigest;
        final long maxBytes;
        final String packageName;
        final String certificateSha256;
        final Set<String> schemes;
        final List<String> usesPermissions;
        final List<String> declaredPermissions;
        final Path lockPath;
        final Path startPath;
        final Path terminationPath;
        final Path workPath;
        final String keyAlias;

        Config(Map<String, String> value) throws Rejected {
            if (!CONFIG_KEYS.equals(value.keySet()) || !"1".equals(value.get("schemaVersion"))) {
                throw new Rejected();
            }
            mode = required(value, "mode", MODES::contains);
            operationId = required(value, "operationId", text -> ID.matcher(text).matches());
            requestDigest = digest(value, "requestDigest");
            contextDigest = digest(value, "contextDigest");
            scopeDigest = digest(value, "scopeDigest");
            ownerDefinitionDigest = digest(value, "ownerDefinitionDigest");
            input = absolute(value, "inputPath");
            output = absolute(value, "outputPath");
            inputDigest = digest(value, "inputDigest");
            try {
                maxBytes = Long.parseLong(value.get("maxBytes"));
            } catch (NumberFormatException error) {
                throw new Rejected(error);
            }
            if (maxBytes < 1 || maxBytes > MAX_APK_BYTES) throw new Rejected();
            packageName = required(value, "packageName", text -> PACKAGE.matcher(text).matches());
            certificateSha256 = digest(value, "certificateSha256");
            schemes = parseSchemes(value.get("schemes"));
            usesPermissions = parsePermissions(value.get("usesPermissions"));
            declaredPermissions = parsePermissions(value.get("declaredPermissions"));
            lockPath = absolute(value, "lockPath");
            startPath = absolute(value, "startPath");
            terminationPath = absolute(value, "terminationPath");
            workPath = absolute(value, "workPath");
            String selectedAlias = value.get("keyAlias");
            if ("sign".equals(mode)) {
                keyAlias = required(value, "keyAlias", text -> ALIAS.matcher(text).matches());
            } else {
                if (selectedAlias == null || !selectedAlias.isEmpty()) throw new Rejected();
                keyAlias = "";
            }
            if (!input.getParent().equals(workPath) || !output.getParent().equals(workPath)
                    || !lockPath.getParent().equals(workPath)
                    || !startPath.getParent().equals(workPath)
                    || !terminationPath.getParent().equals(workPath)
                    || new HashSet<>(List.of(input, output, lockPath, startPath,
                            terminationPath, workPath)).size() != 6) {
                throw new Rejected();
            }
        }
    }

    @FunctionalInterface
    private interface TextCheck { boolean accepts(String value); }

    private static String required(Map<String, String> values, String key, TextCheck check)
            throws Rejected {
        String value = values.get(key);
        if (value == null || value.isEmpty() || !check.accepts(value)) throw new Rejected();
        return value;
    }

    private static String digest(Map<String, String> values, String key) throws Rejected {
        return required(values, key, text -> DIGEST.matcher(text).matches());
    }

    private static Path absolute(Map<String, String> values, String key) throws Rejected {
        String value = required(values, key, text -> text.length() <= 4096 && text.indexOf('\0') < 0);
        Path path = Path.of(value);
        if (!path.isAbsolute() || !path.equals(path.normalize())) throw new Rejected();
        return path;
    }

    private static Set<String> parseSchemes(String value) throws Rejected {
        if (value == null) throw new Rejected();
        Set<String> result = new TreeSet<>();
        if (!value.isEmpty()) result.addAll(Arrays.asList(value.split(",", -1)));
        if (!result.contains("v2") || !Set.of("v1", "v2", "v3").containsAll(result)
                || String.join(",", result).equals(value) == false) throw new Rejected();
        return Collections.unmodifiableSet(result);
    }

    private static List<String> parsePermissions(String value) throws Rejected {
        if (value == null) throw new Rejected();
        List<String> result = new ArrayList<>();
        if (!value.isEmpty()) result.addAll(Arrays.asList(value.split(",", -1)));
        if (result.size() > 256 || new HashSet<>(result).size() != result.size()) throw new Rejected();
        List<String> sorted = new ArrayList<>(result);
        Collections.sort(sorted);
        if (!sorted.equals(result)) throw new Rejected();
        for (String item : result) if (!PERMISSION.matcher(item).matches()) throw new Rejected();
        return Collections.unmodifiableList(result);
    }

    private static FileDescriptor descriptor(int value) throws Rejected {
        if (value < 3 || value > 1_000_000) throw new Rejected();
        try {
            FileDescriptor descriptor = new FileDescriptor();
            Field field = FileDescriptor.class.getDeclaredField("fd");
            field.setAccessible(true);
            field.setInt(descriptor, value);
            return descriptor;
        } catch (ReflectiveOperationException error) {
            throw new Rejected(error);
        }
    }

    private static Map<String, String> readConfig(FileDescriptor descriptor) throws Rejected {
        Map<String, String> result = new HashMap<>();
        byte[] raw = new byte[MAX_CONFIG_BYTES + 1];
        int total = 0;
        try (FileInputStream input = new FileInputStream(descriptor)) {
            while (total < raw.length) {
                int count = input.read(raw, total, raw.length - total);
                if (count == -1) break;
                if (count == 0) continue;
                total += count;
            }
            if (total > MAX_CONFIG_BYTES || total == 0 || raw[total - 1] != '\n') {
                throw new Rejected();
            }
            for (int index = 0; index < total; index++) {
                int value = raw[index] & 0xff;
                if (value != '\n' && (value < 0x20 || value > 0x7e)) throw new Rejected();
            }
            String text = new String(raw, 0, total, StandardCharsets.US_ASCII);
            Arrays.fill(raw, (byte) 0);
            String[] lines = text.split("\\n", -1);
            if (!lines[lines.length - 1].isEmpty()) throw new Rejected();
            for (int index = 0; index < lines.length - 1; index++) {
                String line = lines[index];
                int split = line.indexOf('=');
                if (split <= 0) throw new Rejected();
                String key = line.substring(0, split);
                String value = line.substring(split + 1);
                if (result.putIfAbsent(key, value) != null) throw new Rejected();
            }
        } catch (IOException error) {
            throw new Rejected(error);
        } finally {
            Arrays.fill(raw, (byte) 0);
        }
        return result;
    }

    private static Map<String, Object> unix(Path path, boolean noFollow) throws Rejected {
        try {
            @SuppressWarnings("unchecked")
            Map<String, Object> value = noFollow
                    ? Files.readAttributes(path, "unix:dev,ino,mode,nlink,uid,size",
                            LinkOption.NOFOLLOW_LINKS)
                    : Files.readAttributes(path, "unix:dev,ino,mode,nlink,uid,size");
            return value;
        } catch (IOException | UnsupportedOperationException error) {
            throw new Rejected(error);
        }
    }

    private static long number(Map<String, Object> value, String key) throws Rejected {
        Object selected = value.get(key);
        if (!(selected instanceof Number)) throw new Rejected();
        return ((Number) selected).longValue();
    }

    private static void validatePrivateInput(Path input, Path work) throws Rejected {
        Map<String, Object> file = unix(input, true);
        Map<String, Object> directory = unix(work, true);
        if (((int) number(file, "mode") & 0170000) != 0100000
                || ((int) number(file, "mode") & 0777) != 0600
                || number(file, "nlink") != 1
                || number(file, "uid") != number(directory, "uid")) throw new Rejected();
    }

    private static void validateFdPath(int fd, Path path, boolean directory,
                                       boolean zero, boolean requireLock)
            throws Rejected {
        if (!validateFdPathNative(fd, path.toString(), directory, zero, requireLock)) {
            throw new Rejected();
        }
    }

    private static String quote(String value) {
        return "\"" + value + "\"";
    }

    private static String array(Iterable<String> values) {
        StringBuilder result = new StringBuilder("[");
        boolean first = true;
        for (String value : values) {
            if (!first) result.append(',');
            result.append(quote(value));
            first = false;
        }
        return result.append(']').toString();
    }

    private static synchronized void writeRecord(FileOutputStream stream, String state)
            throws IOException {
        Config value = config;
        String record;
        if (value == null) {
            record = "{\"schemaVersion\":1,\"state\":\"configuration-rejected\"}";
        } else {
            Instant start = ProcessHandle.current().info().startInstant().orElse(Instant.EPOCH);
            record = "{\"contextDigest\":" + quote(value.contextDigest)
                    + ",\"operationId\":" + quote(value.operationId)
                    + ",\"ownerDefinitionDigest\":" + quote(value.ownerDefinitionDigest)
                    + ",\"ownerPid\":" + ProcessHandle.current().pid()
                    + ",\"ownerStartedAtMs\":" + start.toEpochMilli()
                    + ",\"recordMeaning\":"
                    + quote("started".equals(state) ? "owner-start" : "exit-intent")
                    + ",\"requestDigest\":" + quote(value.requestDigest)
                    + ",\"schemaVersion\":1,\"scopeDigest\":" + quote(value.scopeDigest)
                    + ",\"stage\":" + quote(stage)
                    + ",\"state\":" + quote(state) + "}";
        }
        byte[] raw = record.getBytes(StandardCharsets.US_ASCII);
        if (raw.length > 4096) throw new IOException();
        FileChannel channel = stream.getChannel();
        channel.truncate(0);
        channel.position(0);
        ByteBuffer buffer = ByteBuffer.wrap(raw);
        while (buffer.hasRemaining()) {
            if (channel.write(buffer) <= 0) throw new IOException();
        }
        channel.force(true);
        stream.getFD().sync();
        directoryDescriptor.sync();
    }

    private static void startLiveness(FileDescriptor liveness) {
        Thread watcher = new Thread(() -> {
            boolean acknowledged = false;
            try (FileInputStream stream = new FileInputStream(liveness)) {
                acknowledged = stream.read() == 1;
            } catch (IOException ignored) { }
            if (acknowledged) {
                normalExit.countDown();
                return;
            }
            if (parentLost.compareAndSet(false, true)) {
                stage = "parent-loss";
                try { writeRecord(terminationStream, "parent-loss-exit-intent"); }
                catch (IOException ignored) { }
                try { Thread.sleep(750); }
                catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
                Runtime.getRuntime().halt(70);
            }
        }, "repro-signing-parent-liveness");
        watcher.setDaemon(true);
        watcher.start();
    }

    private static String hex(byte[] digest) {
        StringBuilder result = new StringBuilder(64);
        for (byte item : digest) result.append(String.format("%02x", item & 0xff));
        return result.toString();
    }

    private static String sha256(byte[] value) throws Exception {
        return hex(MessageDigest.getInstance("SHA-256").digest(value));
    }

    private static String fileDigest(Path path, long maximum) throws Exception {
        long size = Files.size(path);
        if (size < 1 || size > maximum || Files.isSymbolicLink(path)) throw new Rejected();
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (FileInputStream input = new FileInputStream(path.toFile())) {
            byte[] buffer = new byte[1024 * 1024];
            int count;
            long total = 0;
            while ((count = input.read(buffer)) != -1) {
                total += count;
                if (total > maximum) throw new Rejected();
                digest.update(buffer, 0, count);
            }
            if (total != size) throw new Rejected();
        }
        return hex(digest.digest());
    }

    private static final class ManifestIdentity {
        final String packageName;
        final List<String> uses;
        final List<String> declares;

        ManifestIdentity(String packageName, List<String> uses, List<String> declares) {
            this.packageName = packageName;
            this.uses = uses;
            this.declares = declares;
        }
    }

    private static final class BoundedSink implements DataSink {
        private final DataSink target;
        private final long maximum;
        private long consumed;

        BoundedSink(DataSink target, long maximum) {
            this.target = target;
            this.maximum = maximum;
        }

        private void reserve(int count) throws IOException {
            if (count < 0 || consumed > maximum - count) {
                stage = "output-bound";
                throw new IOException("bounded output rejected");
            }
            consumed += count;
        }

        @Override
        public void consume(byte[] value, int offset, int count) throws IOException {
            reserve(count);
            target.consume(value, offset, count);
        }

        @Override
        public void consume(ByteBuffer value) throws IOException {
            reserve(value.remaining());
            target.consume(value);
        }
    }

    private static ManifestIdentity manifest(Path apk) throws Exception {
        ByteBuffer document;
        try (FileChannel channel = FileChannel.open(apk, StandardOpenOption.READ)) {
            document = ApkUtils.getAndroidManifest(DataSources.asDataSource(channel));
        }
        String packageName = ApkUtils.getPackageNameFromBinaryAndroidManifest(document.duplicate());
        TreeSet<String> uses = new TreeSet<>();
        TreeSet<String> declares = new TreeSet<>();
        AndroidBinXmlParser parser = new AndroidBinXmlParser(document.duplicate());
        while (parser.next() != AndroidBinXmlParser.EVENT_END_DOCUMENT) {
            if (parser.getEventType() != AndroidBinXmlParser.EVENT_START_ELEMENT) continue;
            String element = parser.getName();
            TreeSet<String> selected = Set.of("uses-permission", "uses-permission-sdk-23")
                    .contains(element) ? uses
                    : "permission".equals(element) ? declares : null;
            if (selected == null && element.startsWith("uses-permission")) throw new Rejected();
            if (selected == null) continue;
            String name = null;
            for (int index = 0; index < parser.getAttributeCount(); index++) {
                if ("name".equals(parser.getAttributeName(index))
                        && ANDROID_NS.equals(parser.getAttributeNamespace(index))
                        && parser.getAttributeValueType(index) == AndroidBinXmlParser.VALUE_TYPE_STRING) {
                    name = parser.getAttributeStringValue(index);
                }
            }
            if (name == null || !PERMISSION.matcher(name).matches() || !selected.add(name)
                    || uses.size() > 256 || declares.size() > 256) throw new Rejected();
        }
        return new ManifestIdentity(packageName,
                List.copyOf(uses), List.copyOf(declares));
    }

    private static void requireManifest(ManifestIdentity measured) throws Rejected {
        if (!config.packageName.equals(measured.packageName)
                || !config.usesPermissions.equals(measured.uses)
                || !config.declaredPermissions.equals(measured.declares)) throw new Rejected();
    }

    private static void requireInputStable(ManifestIdentity expected) throws Exception {
        if (!fileDigest(config.input, config.maxBytes).equals(config.inputDigest)) {
            throw new Rejected();
        }
        ManifestIdentity measured = manifest(config.input);
        requireManifest(measured);
        if (!expected.packageName.equals(measured.packageName)
                || !expected.uses.equals(measured.uses)
                || !expected.declares.equals(measured.declares)) throw new Rejected();
    }

    private static char[][] passwords(FileDescriptor descriptor) throws Rejected {
        byte[] raw = new byte[MAX_SECRET_BYTES + 1];
        int total = 0;
        try (FileInputStream input = new FileInputStream(descriptor)) {
            int count;
            while ((count = input.read(raw, total, raw.length - total)) != -1) {
                total += count;
                if (total > MAX_SECRET_BYTES || total == raw.length) throw new Rejected();
            }
        } catch (IOException error) {
            throw new Rejected(error);
        }
        String value = new String(raw, 0, total, StandardCharsets.UTF_8);
        Arrays.fill(raw, (byte) 0);
        String[] lines = value.split("\\n", -1);
        if (lines.length != 3 || !lines[2].isEmpty() || lines[0].isEmpty() || lines[1].isEmpty()
                || lines[0].length() > 1024 || lines[1].length() > 1024) throw new Rejected();
        return new char[][] {lines[0].toCharArray(), lines[1].toCharArray()};
    }

    private static List<X509Certificate> certificates(Certificate[] chain) throws Rejected {
        if (chain == null || chain.length < 1 || chain.length > 8) throw new Rejected();
        List<X509Certificate> result = new ArrayList<>();
        for (Certificate item : chain) {
            if (!(item instanceof X509Certificate)) throw new Rejected();
            result.add((X509Certificate) item);
        }
        return result;
    }

    private static void sign(FileDescriptor keyDescriptor, FileDescriptor passwordDescriptor)
            throws Exception {
        ManifestIdentity before = manifest(config.input);
        requireManifest(before);
        if (!fileDigest(config.input, config.maxBytes).equals(config.inputDigest)
                || Files.exists(config.output, LinkOption.NOFOLLOW_LINKS)) throw new Rejected();
        char[][] passwords = passwords(passwordDescriptor);
        try {
            KeyStore store = KeyStore.getInstance("PKCS12");
            try (FileInputStream input = new FileInputStream(keyDescriptor)) {
                store.load(input, passwords[0]);
            }
            Key key = store.getKey(config.keyAlias, passwords[1]);
            if (!(key instanceof PrivateKey)) throw new Rejected();
            List<X509Certificate> chain = certificates(store.getCertificateChain(config.keyAlias));
            if (!sha256(chain.get(0).getEncoded()).equals(config.certificateSha256)) throw new Rejected();
            ApkSigner.SignerConfig signer = new ApkSigner.SignerConfig.Builder(
                    config.keyAlias, new KeyConfig.Jca((PrivateKey) key), chain).build();
            Set<PosixFilePermission> privateFile = Set.of(
                    PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE);
            Files.createFile(config.output,
                    PosixFilePermissions.asFileAttribute(privateFile));
            try (RandomAccessFile output = new RandomAccessFile(config.output.toFile(), "rw")) {
                BoundedSink sink = new BoundedSink(DataSinks.asDataSink(output), config.maxBytes);
                ApkSigner owner = new ApkSigner.Builder(List.of(signer))
                        .setInputApk(config.input.toFile())
                        .setOutputApk(sink, DataSources.asDataSource(output))
                        .setV1SigningEnabled(config.schemes.contains("v1"))
                        .setV2SigningEnabled(config.schemes.contains("v2"))
                        .setV3SigningEnabled(config.schemes.contains("v3"))
                        .setV4SigningEnabled(false)
                        .setOtherSignersSignaturesPreserved(false)
                        .setCreatedBy("Reproof Android Signing Owner")
                        .build();
                owner.sign();
                output.getFD().sync();
            }
            Files.setPosixFilePermissions(config.output, privateFile);
        } finally {
            Arrays.fill(passwords[0], '\0');
            Arrays.fill(passwords[1], '\0');
        }
        inspect(config.output, before);
        requireInputStable(before);
    }

    private static void inspect(Path apk, ManifestIdentity expected) throws Exception {
        if (Files.isSymbolicLink(apk) || Files.size(apk) < 1 || Files.size(apk) > config.maxBytes) {
            throw new Rejected();
        }
        ApkVerifier.Result verified = new ApkVerifier.Builder(apk.toFile()).build().verify();
        List<X509Certificate> signers = verified.getSignerCertificates();
        Set<String> schemes = new TreeSet<>();
        if (verified.isVerifiedUsingV1Scheme()) schemes.add("v1");
        if (verified.isVerifiedUsingV2Scheme()) schemes.add("v2");
        if (verified.isVerifiedUsingV3Scheme()) schemes.add("v3");
        if (verified.isVerifiedUsingV31Scheme()) schemes.add("v3.1");
        if (verified.isVerifiedUsingV4Scheme()) schemes.add("v4");
        if (!verified.isVerified() || verified.containsErrors() || verified.isSourceStampVerified()
                || verified.getSourceStampInfo() != null
                || signers.size() != 1 || !schemes.equals(config.schemes)
                || !sha256(signers.get(0).getEncoded()).equals(config.certificateSha256)) {
            throw new Rejected();
        }
        ManifestIdentity measured = manifest(apk);
        requireManifest(measured);
        if (expected != null && (!expected.packageName.equals(measured.packageName)
                || !expected.uses.equals(measured.uses)
                || !expected.declares.equals(measured.declares))) throw new Rejected();
    }

    private static String result() throws Exception {
        ManifestIdentity measured = manifest(config.mode.equals("sign") ? config.output : config.input);
        Path artifact = config.mode.equals("sign") ? config.output : config.input;
        return "{\"certificateSha256\":" + quote(config.certificateSha256)
                + ",\"contextDigest\":" + quote(config.contextDigest)
                + ",\"declaredPermissions\":" + array(measured.declares)
                + ",\"inputDigest\":" + quote(config.inputDigest)
                + ",\"operationId\":" + quote(config.operationId)
                + ",\"outputDigest\":" + quote(fileDigest(artifact, config.maxBytes))
                + ",\"packageName\":" + quote(measured.packageName)
                + ",\"requestDigest\":" + quote(config.requestDigest)
                + ",\"schemaVersion\":1,\"schemes\":" + array(config.schemes)
                + ",\"scopeDigest\":" + quote(config.scopeDigest)
                + ",\"status\":\"succeeded\",\"usesPermissions\":" + array(measured.uses)
                + "}";
    }

    public static void main(String[] arguments) {
        int exit = 2;
        try {
            if (arguments.length != 8) throw new Rejected();
            int[] fds = new int[8];
            for (int index = 0; index < arguments.length; index++) {
                if (!arguments[index].matches("[0-9]{1,7}")) throw new Rejected();
                fds[index] = Integer.parseInt(arguments[index]);
            }
            Set<Integer> controlFds = new HashSet<>();
            for (int index = 0; index < 6; index++) {
                if (fds[index] < 3) throw new Rejected();
                controlFds.add(fds[index]);
            }
            if (controlFds.size() != 6) throw new Rejected();
            directoryDescriptor = descriptor(fds[5]);
            terminationStream = new FileOutputStream(descriptor(fds[4]));
            config = new Config(readConfig(descriptor(fds[0])));
            if ("sign".equals(config.mode)) {
                Set<Integer> allFds = new HashSet<>(controlFds);
                if (fds[6] < 3 || fds[7] < 3) throw new Rejected();
                allFds.add(fds[6]); allFds.add(fds[7]);
                if (allFds.size() != 8) throw new Rejected();
            } else if (fds[6] != 0 || fds[7] != 0) {
                throw new Rejected();
            }
            stage = "fd-lock";
            validateFdPath(fds[1], config.lockPath, false, true, true);
            stage = "fd-start";
            validateFdPath(fds[3], config.startPath, false, true, false);
            stage = "fd-termination";
            validateFdPath(fds[4], config.terminationPath, false, true, false);
            stage = "fd-directory";
            validateFdPath(fds[5], config.workPath, true, false, false);
            if ("sign".equals(config.mode)) {
                stage = "fd-key";
                if (!validatePrivateRegularFdNative(fds[6])) throw new Rejected();
            }
            stage = "input-validation";
            validatePrivateInput(config.input, config.workPath);
            lockHandle = new FileInputStream(descriptor(fds[1]));
            FileOutputStream startStream = new FileOutputStream(descriptor(fds[3]));
            stage = "started";
            writeRecord(startStream, "started");
            startLiveness(descriptor(fds[2]));
            if ("liveness-probe".equals(config.mode)) {
                stage = "liveness-probe";
                Thread.sleep(5000);
                throw new Rejected();
            } else if ("sign".equals(config.mode)) {
                stage = "signing";
                sign(descriptor(fds[6]), descriptor(fds[7]));
            } else {
                stage = "inspection";
                ManifestIdentity before = manifest(config.input);
                requireManifest(before);
                requireInputStable(before);
                inspect(config.input, before);
                requireInputStable(before);
            }
            if (parentLost.get()) throw new Rejected();
            String output = result();
            if (output.getBytes(StandardCharsets.US_ASCII).length > 64 * 1024) throw new Rejected();
            stage = "complete";
            writeRecord(terminationStream, "succeeded");
            System.out.println(output);
            System.out.flush();
            if (!normalExit.await(30, TimeUnit.SECONDS)) {
                stage = "ack-timeout";
                writeRecord(terminationStream, "ack-timeout-exit-intent");
                Runtime.getRuntime().halt(71);
            }
            exit = 0;
        } catch (Throwable error) {
            try {
                if (terminationStream != null) writeRecord(terminationStream, "failed");
            } catch (Throwable ignored) { }
        } finally {
            System.exit(exit);
        }
    }
}
