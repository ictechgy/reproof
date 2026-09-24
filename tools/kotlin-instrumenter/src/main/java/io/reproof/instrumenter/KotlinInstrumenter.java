package io.reproloop.instrumenter;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.stream.Collectors;

import org.jetbrains.kotlin.cli.jvm.compiler.EnvironmentConfigFiles;
import org.jetbrains.kotlin.cli.jvm.compiler.KotlinCoreEnvironment;
import org.jetbrains.kotlin.com.intellij.openapi.util.Disposer;
import org.jetbrains.kotlin.com.intellij.psi.PsiElement;
import org.jetbrains.kotlin.com.intellij.psi.PsiErrorElement;
import org.jetbrains.kotlin.com.intellij.psi.PsiRecursiveElementVisitor;
import org.jetbrains.kotlin.com.intellij.psi.tree.IElementType;
import org.jetbrains.kotlin.config.CommonConfigurationKeys;
import org.jetbrains.kotlin.config.CompilerConfiguration;
import org.jetbrains.kotlin.lexer.KtTokens;
import org.jetbrains.kotlin.psi.KtBinaryExpression;
import org.jetbrains.kotlin.psi.KtBlockExpression;
import org.jetbrains.kotlin.psi.KtCallExpression;
import org.jetbrains.kotlin.psi.KtClassOrObject;
import org.jetbrains.kotlin.psi.KtClassBody;
import org.jetbrains.kotlin.psi.KtDeclaration;
import org.jetbrains.kotlin.psi.KtDotQualifiedExpression;
import org.jetbrains.kotlin.psi.KtExpression;
import org.jetbrains.kotlin.psi.KtFile;
import org.jetbrains.kotlin.psi.KtFunctionLiteral;
import org.jetbrains.kotlin.psi.KtLambdaArgument;
import org.jetbrains.kotlin.psi.KtLambdaExpression;
import org.jetbrains.kotlin.psi.KtNameReferenceExpression;
import org.jetbrains.kotlin.psi.KtNamedFunction;
import org.jetbrains.kotlin.psi.KtProperty;
import org.jetbrains.kotlin.psi.KtPsiFactory;
import org.jetbrains.kotlin.psi.KtReturnExpression;
import org.jetbrains.kotlin.psi.KtScriptInitializer;
import org.jetbrains.kotlin.psi.KtSimpleNameExpression;
import org.jetbrains.kotlin.psi.KtTreeVisitorVoid;

/** Conservative Kotlin PSI based source instrumentation. */
public final class KotlinInstrumenter {
    private static final String REPRO_IMPORT = "io.reproloop.autotrace.ReproAuto";
    private static final int MAX_FILES = 128;
    private static final long MAX_FILE_BYTES = 4L * 1024L * 1024L;
    private static final long MAX_TOTAL_BYTES = 16L * 1024L * 1024L;
    private static final String AMBIGUOUS = "\u0000ambiguous";

    private KotlinInstrumenter() {}

    public static void main(String[] args) {
        System.setProperty("java.awt.headless", "true");
        try {
            if (args.length != 3) {
                throw new ContractException("expected source root, activity FQCN, and tap target list");
            }
            if (args[1].equals("--gradle") && args[2].equals("v1")) {
                System.out.print(configureGradle(Paths.get(args[0])));
                return;
            }
            List<String> targets = parseTargets(args[2]);
            Map<String, String> sources = readSources(Paths.get(args[0]));
            String activity = normalizeActivity(args[1]);
            Result result = new InstrumenterImpl(sources, activity, targets).run();
            System.out.print(result.toJson());
        } catch (ContractException e) {
            System.err.println("ContractError: " + e.getMessage());
            System.exit(3);
        } catch (Throwable e) {
            // PSI/compiler failures can include source excerpts.  Keep the process boundary opaque.
            System.err.println("Kotlin PSI instrumentation failed");
            System.exit(4);
        }
    }

    private static String configureGradle(Path root) throws IOException, ContractException {
        var disposable = Disposer.newDisposable("reproloop-gradle-integration");
        try {
            CompilerConfiguration configuration = new CompilerConfiguration();
            configuration.put(CommonConfigurationKeys.MODULE_NAME, "reproloop-gradle-integration");
            KotlinCoreEnvironment environment = KotlinCoreEnvironment.createForProduction(
                    disposable, configuration, EnvironmentConfigFiles.JVM_CONFIG_FILES);
            KtPsiFactory factory = new KtPsiFactory(environment.getProject(), false);
            StringBuilder result = new StringBuilder("{\"schemaVersion\":1,\"files\":{");
            for (String name : List.of("settings.gradle.kts", "module.gradle.kts")) {
                Path path = root.resolve(name);
                if (!Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS) || Files.size(path) > MAX_FILE_BYTES)
                    throw new ContractException("invalid Gradle input");
                String source = Files.readString(path, StandardCharsets.UTF_8);
                if (source.contains("reproloop-build-logic") || source.contains("io.reproloop.instrumentation"))
                    throw new ContractException("reserved instrumentation build integration");
                String block = name.startsWith("settings") ? "pluginManagement" : "plugins";
                String fragment = name.startsWith("settings")
                        ? "\n    includeBuild(\"reproloop-build-logic\")\n"
                        : "\n    id(\"io.reproloop.instrumentation\")\n";
                KtFile file = factory.createFile(name, source);
                if (InstrumenterImpl.hasPsiError(file) || file.getScript() == null
                        || !file.getPackageFqName().isRoot())
                    throw new ContractException("unsupported Gradle script syntax");
                List<KtExpression> statements = file.getScript().getBlockExpression().getStatements();
                KtCallExpression selected = null;
                for (KtExpression statement : statements) {
                    KtExpression expression = statement instanceof KtScriptInitializer initializer
                            ? initializer.getBody() : statement;
                    if (expression instanceof KtCallExpression call && call.getCalleeExpression() != null
                            && block.equals(call.getCalleeExpression().getText())) {
                        if (selected != null || call.getLambdaArguments().size() != 1
                                || call.getValueArgumentList() != null || !call.getTypeArguments().isEmpty())
                            throw new ContractException("ambiguous Gradle integration block");
                        selected = call;
                    }
                }
                int offset;
                if (selected == null) {
                    offset = statements.isEmpty() ? source.length() : statements.get(0).getTextRange().getStartOffset();
                    fragment = block + " {" + fragment + "}\n";
                    if (offset > 0 && source.charAt(offset - 1) != '\n') fragment = "\n" + fragment;
                } else {
                    var lambda = selected.getLambdaArguments().get(0).getLambdaExpression();
                    if (lambda == null || lambda.getFunctionLiteral().getRBrace() == null)
                        throw new ContractException("missing Gradle block boundary");
                    offset = lambda.getFunctionLiteral().getRBrace().getTextRange().getStartOffset();
                }
                String modified = source.substring(0, offset) + fragment + source.substring(offset);
                if (InstrumenterImpl.hasPsiError(factory.createFile(name, modified)))
                    throw new ContractException("invalid generated Gradle script");
                if (result.charAt(result.length() - 1) != '{') result.append(',');
                result.append(jsonString(name)).append(':').append(jsonString(modified));
            }
            return result.append("}}").toString();
        } finally {
            Disposer.dispose(disposable);
        }
    }

    private static String normalizeActivity(String value) throws ContractException {
        if (value == null || value.length() == 0 || value.length() > 240
                || !value.matches("(?:[A-Za-z_][A-Za-z0-9_]*\\.)*[A-Za-z_][A-Za-z0-9_]*")) {
            throw new ContractException("invalid activity name");
        }
        return value;
    }

    private static List<String> parseTargets(String value) throws ContractException {
        if (value == null || value.isEmpty()) throw new ContractException("tap target list is empty");
        String[] pieces = value.split(",", -1);
        if (pieces.length > 64) throw new ContractException("too many tap targets");
        LinkedHashSet<String> result = new LinkedHashSet<>();
        for (String piece : pieces) {
            if (!piece.matches("[A-Za-z_][A-Za-z0-9_]{0,127}")) throw new ContractException("invalid tap target");
            if (!result.add(piece)) throw new ContractException("duplicate tap target");
        }
        return new ArrayList<>(result);
    }

    private static Map<String, String> readSources(Path root) throws IOException, ContractException {
        if (!Files.isDirectory(root, LinkOption.NOFOLLOW_LINKS) || Files.isSymbolicLink(root)) {
            throw new ContractException("source root is invalid");
        }
        List<Path> paths;
        try (var stream = Files.walk(root)) {
            paths = stream.filter(path -> Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS))
                    .filter(path -> path.getFileName().toString().endsWith(".kt"))
                    .sorted().collect(Collectors.toList());
        }
        if (paths.isEmpty() || paths.size() > MAX_FILES) throw new ContractException("invalid Kotlin source file set");
        TreeMap<String, String> sources = new TreeMap<>();
        long total = 0;
        for (Path path : paths) {
            if (Files.isSymbolicLink(path)) throw new ContractException("linked source is not allowed");
            String name = root.relativize(path).toString().replace(path.getFileSystem().getSeparator(), "/");
            validateRelativePath(name);
            long bytes = Files.size(path);
            total += bytes;
            if (bytes > MAX_FILE_BYTES || total > MAX_TOTAL_BYTES) throw new ContractException("Kotlin source exceeds input limit");
            sources.put(name, Files.readString(path, StandardCharsets.UTF_8));
        }
        return sources;
    }

    private static void validateRelativePath(String value) throws ContractException {
        if (value.isEmpty() || value.startsWith("/") || value.contains("\\") || value.contains("..")) {
            throw new ContractException("unsafe source path");
        }
        for (String part : value.split("/", -1)) {
            if (part.isEmpty() || part.equals(".") || part.startsWith(".")) throw new ContractException("unsafe source path");
        }
    }

    private static final class InstrumenterImpl {
        private final Map<String, String> sources;
        private final String requestedActivity;
        private final List<String> targets;
        private final Set<String> targetSet;
        private final List<SourceFile> parsed = new ArrayList<>();
        private final Set<String> usedNames = new HashSet<>();
        private final List<Site> sites = new ArrayList<>();
        private final List<Edit> edits = new ArrayList<>();
        private KotlinCoreEnvironment environment;
        private KtPsiFactory factory;
        private SourceFile activityFile;
        private KtClassOrObject activityClass;
        private String activitySimpleName;
        private KtNamedFunction onCreate;

        InstrumenterImpl(Map<String, String> sources, String requestedActivity, List<String> targets) {
            this.sources = sources;
            this.requestedActivity = requestedActivity;
            this.targets = targets;
            this.targetSet = new LinkedHashSet<>(targets);
        }

        Result run() throws ContractException {
            var disposable = Disposer.newDisposable("reproloop-kotlin-instrumenter");
            try {
                CompilerConfiguration configuration = new CompilerConfiguration();
                configuration.put(CommonConfigurationKeys.MODULE_NAME, "reproloop-instrumenter");
                environment = KotlinCoreEnvironment.createForProduction(
                        disposable, configuration, EnvironmentConfigFiles.JVM_CONFIG_FILES);
                factory = new KtPsiFactory(environment.getProject(), false);
                parseAll();
                rejectExistingInstrumentation();
                locateActivity();
                planLifecycle();
                planTaps();
                validateCoverage();
                addImportEdit();
                String transformed = applyEdits(activityFile.source, edits);
                validateTransformed(transformed);
                return new Result(activityFile.path, transformed, sites);
            } catch (LocalContractException e) {
                throw new ContractException(e.getMessage());
            } finally {
                Disposer.dispose(disposable);
            }
        }

        private void parseAll() throws ContractException {
            for (Map.Entry<String, String> entry : sources.entrySet()) {
                KtFile file = factory.createFile(entry.getKey(), entry.getValue());
                if (hasPsiError(file)) throw new ContractException("Kotlin source contains unsupported syntax");
                SourceFile sourceFile = new SourceFile(entry.getKey(), entry.getValue(), file);
                parsed.add(sourceFile);
                collectUsedNames(file);
            }
        }

        private void collectUsedNames(KtFile file) {
            file.accept(new KtTreeVisitorVoid() {
                @Override public void visitSimpleNameExpression(KtSimpleNameExpression expression) {
                    if (expression.getReferencedName() != null) usedNames.add(expression.getReferencedName());
                    super.visitSimpleNameExpression(expression);
                }
            });
        }

        private void rejectExistingInstrumentation() throws ContractException {
            for (SourceFile sourceFile : parsed) {
                final boolean[] found = {false};
                sourceFile.file.accept(new KtTreeVisitorVoid() {
                    @Override public void visitSimpleNameExpression(KtSimpleNameExpression expression) {
                        if ("ReproAuto".equals(expression.getReferencedName())) found[0] = true;
                        super.visitSimpleNameExpression(expression);
                    }
                });
                if (found[0]) throw new ContractException("source already contains ReproAuto instrumentation");
            }
        }

        private void locateActivity() throws ContractException {
            List<ActivityMatch> matches = new ArrayList<>();
            for (SourceFile sourceFile : parsed) {
                sourceFile.file.accept(new KtTreeVisitorVoid() {
                    @Override public void visitClassOrObject(KtClassOrObject declaration) {
                        if (requestedActivity.equals(classFqName(sourceFile.file, declaration))) {
                            matches.add(new ActivityMatch(sourceFile, declaration));
                        }
                        super.visitClassOrObject(declaration);
                    }
                });
            }
            if (matches.size() != 1) throw new ContractException("activity is missing or ambiguous");
            activityFile = matches.get(0).file;
            activityClass = matches.get(0).declaration;
            activitySimpleName = activityClass.getName();
            if (activitySimpleName == null || !activitySimpleName.matches("[A-Za-z_][A-Za-z0-9_]*")) {
                throw new ContractException("activity name cannot be used as a receiver label");
            }
            if (!looksLikeActivity(activityClass)) throw new ContractException("selected class is not an Android Activity");
        }

        private static String classFqName(KtFile file, KtClassOrObject declaration) {
            List<String> names = new ArrayList<>();
            KtClassOrObject current = declaration;
            while (current != null) {
                if (current.getName() == null) return "";
                names.add(current.getName());
                PsiElement parent = current.getParent();
                while (parent != null && !(parent instanceof KtClassOrObject) && !(parent instanceof KtFile)) {
                    parent = parent.getParent();
                }
                current = parent instanceof KtClassOrObject ? (KtClassOrObject) parent : null;
            }
            Collections.reverse(names);
            String nested = String.join(".", names);
            String packageName = file.getPackageName();
            return packageName == null || packageName.isEmpty() ? nested : packageName + "." + nested;
        }

        private static boolean looksLikeActivity(KtClassOrObject declaration) {
            if (declaration.getSuperTypeListEntries().isEmpty()) return false;
            for (var entry : declaration.getSuperTypeListEntries()) {
                String text = entry.getText();
                if (text == null) continue;
                int generic = text.indexOf('<');
                if (generic >= 0) text = text.substring(0, generic);
                int paren = text.indexOf('(');
                if (paren >= 0) text = text.substring(0, paren);
                int dot = text.lastIndexOf('.');
                String simple = dot >= 0 ? text.substring(dot + 1) : text;
                if (simple.matches("(?:Activity|AppCompatActivity|FragmentActivity|ComponentActivity|[A-Za-z_][A-Za-z0-9_]*Activity)")) return true;
            }
            return false;
        }

        private void planLifecycle() throws ContractException {
            List<KtNamedFunction> creates = directFunctions("onCreate");
            if (creates.size() != 1) throw new ContractException("Activity.onCreate is missing or overloaded");
            onCreate = creates.get(0);
            if (!isOverride(onCreate) || onCreate.getValueParameters().size() != 1
                    || !isBundleParameter(onCreate.getValueParameters().get(0))) {
                throw new ContractException("Activity.onCreate has an unsupported signature");
            }
            KtBlockExpression createBody = onCreate.getBodyBlockExpression();
            if (createBody == null || hasMethodExit(createBody, "onCreate")) {
                throw new ContractException("Activity.onCreate has an unsupported lifecycle body");
            }
            int createBrace = createBody.getRBrace().getTextRange().getStartOffset();
            edits.add(new Edit(createBrace, createBrace,
                    lifecycleAtEnd("ReproAuto.start(this@" + activitySimpleName + ")", activityFile.source,
                            createBrace, bodyIndent(createBody))));

            List<KtNamedFunction> destroys = directFunctions("onDestroy");
            if (destroys.size() > 1) throw new ContractException("Activity.onDestroy is overloaded");
            if (destroys.size() == 1) {
                KtNamedFunction destroy = destroys.get(0);
                if (!isOverride(destroy) || !destroy.getValueParameters().isEmpty()) {
                    throw new ContractException("Activity.onDestroy has an unsupported signature");
                }
                KtBlockExpression destroyBody = destroy.getBodyBlockExpression();
                if (destroyBody == null) throw new ContractException("Activity.onDestroy has an unsupported lifecycle body");
                int bodyStart = firstNonWhitespace(activityFile.source,
                        destroyBody.getLBrace().getTextRange().getEndOffset(),
                        destroyBody.getRBrace().getTextRange().getStartOffset());
                edits.add(new Edit(bodyStart, bodyStart,
                        "ReproAuto.stop(this@" + activitySimpleName + ")\n" + indentationAt(activityFile.source, bodyStart)));
            } else {
                KtClassBody body = activityClass.getBody();
                if (body == null || body.getRBrace() == null) throw new ContractException("Activity body is unsupported");
                int brace = body.getRBrace().getTextRange().getStartOffset();
                String memberIndent = memberIndent(body, activityFile.source, brace);
                String classIndent = indentationAtLine(activityFile.source, brace);
                String method = "override fun onDestroy() {\n" + memberIndent + "    ReproAuto.stop(this@" +
                        activitySimpleName + ")\n" + memberIndent + "    super.onDestroy()\n" + memberIndent + "}";
                edits.add(new Edit(brace, brace, insertBeforeClosingBrace(activityFile.source, brace, method,
                        memberIndent, classIndent)));
            }
        }

        private static boolean isOverride(KtNamedFunction function) {
            return function.getModifierList() != null && function.getModifierList().hasModifier(KtTokens.OVERRIDE_KEYWORD);
        }

        private static boolean isBundleParameter(org.jetbrains.kotlin.psi.KtParameter parameter) {
            if (parameter.getTypeReference() == null) return false;
            String type = parameter.getTypeReference().getText();
            int generic = type.indexOf('<');
            if (generic >= 0) type = type.substring(0, generic);
            int question = type.indexOf('?');
            if (question >= 0) type = type.substring(0, question);
            int dot = type.lastIndexOf('.');
            return "Bundle".equals(dot >= 0 ? type.substring(dot + 1) : type);
        }

        private List<KtNamedFunction> directFunctions(String name) {
            List<KtNamedFunction> result = new ArrayList<>();
            KtClassBody body = activityClass.getBody();
            if (body == null) return result;
            for (KtDeclaration declaration : body.getDeclarations()) {
                if (declaration instanceof KtNamedFunction && name.equals(((KtNamedFunction) declaration).getName())) {
                    result.add((KtNamedFunction) declaration);
                }
            }
            return result;
        }

        private boolean hasMethodExit(KtBlockExpression body, String functionName) {
            final boolean[] found = {false};
            body.accept(new KtTreeVisitorVoid() {
                @Override public void visitReturnExpression(KtReturnExpression expression) {
                    String label = expression.getLabelName();
                    if (label == null || functionName.equals(label)) found[0] = true;
                    super.visitReturnExpression(expression);
                }

                @Override public void visitNamedFunction(KtNamedFunction function) {
                    // Local named functions have their own return target.  Lambdas are traversed
                    // normally so an unlabeled non-local return at any depth is rejected.
                }
            });
            return found[0];
        }

        private void planTaps() throws ContractException {
            TapDiscovery discovery = new TapDiscovery();
            activityClass.accept(discovery);
            for (String target : targets) {
                List<RawSite> candidates = discovery.byTarget.getOrDefault(target, List.of());
                if (candidates.size() != 1) {
                    if (candidates.isEmpty()) throw new ContractException("configured tap target is absent");
                    throw new ContractException("configured tap target has duplicate handlers");
                }
                RawSite raw = candidates.get(0);
                String siteId = siteId(activityFile.path, raw.line, target);
                String tokenName = uniqueName("__reproTapToken");
                String errorName = uniqueName("__reproTapError");
                usedNames.add(tokenName);
                usedNames.add(errorName);
                KtFunctionLiteral literal = raw.lambda.getFunctionLiteral();
                int left = literal.getArrow() == null
                        ? literal.getLBrace().getTextRange().getEndOffset()
                        : literal.getArrow().getTextRange().getEndOffset();
                int right = literal.getRBrace().getTextRange().getStartOffset();
                int bodyStart = firstNonWhitespace(activityFile.source, left, right);
                String bodyIndent = indentationAt(activityFile.source, bodyStart);
                String closeIndent = indentationAtLine(activityFile.source, right);
                String prefix = "val " + tokenName + " = ReproAuto.beforeTap(this@" + activitySimpleName +
                        ", \"" + target + "\", \"" + siteId + "\")\n" + bodyIndent + "try {\n" + bodyIndent;
                String suffix = catchFinally(errorName, tokenName, activityFile.source, right, closeIndent);
                if (bodyStart == right) edits.add(new Edit(right, right, prefix + suffix));
                else {
                    edits.add(new Edit(bodyStart, bodyStart, prefix));
                    edits.add(new Edit(right, right, suffix));
                }
                sites.add(new Site(siteId, activityFile.path, raw.line, target));
            }
            sites.sort(Comparator.comparing(site -> site.id));
        }

        private void validateCoverage() throws ContractException {
            Set<String> actual = sites.stream().map(site -> site.target).collect(Collectors.toSet());
            if (!actual.equals(targetSet)) throw new ContractException("tap target coverage is incomplete");
        }

        private void addImportEdit() {
            KtFile file = activityFile.file;
            int offset;
            String importText;
            if (file.getImportList() != null && !file.getImportList().getImports().isEmpty()) {
                var imports = file.getImportList().getImports();
                offset = imports.get(imports.size() - 1).getTextRange().getEndOffset();
                importText = "\nimport " + REPRO_IMPORT;
            } else if (file.getPackageDirective() != null) {
                offset = file.getPackageDirective().getTextRange().getEndOffset();
                importText = "\nimport " + REPRO_IMPORT;
            } else {
                offset = 0;
                importText = "import " + REPRO_IMPORT + "\n";
            }
            edits.add(new Edit(offset, offset, importText));
        }

        private void validateTransformed(String transformed) throws ContractException {
            KtFile transformedFile = factory.createFile(activityFile.path, transformed);
            if (hasPsiError(transformedFile)) throw new ContractException("generated Kotlin source is invalid");
        }

        private final class TapDiscovery extends KtTreeVisitorVoid {
            private final IdentityHashMap<KtLambdaExpression, String> applyTargets = new IdentityHashMap<>();
            private final Map<KtNamedFunction, Map<String, String>> aliases = new IdentityHashMap<>();
            private final Map<String, String> classAliases = new HashMap<>();
            private final Map<String, List<RawSite>> byTarget = new LinkedHashMap<>();

            @Override public void visitClassOrObject(KtClassOrObject declaration) {
                if (declaration != activityClass) return;
                super.visitClassOrObject(declaration);
            }

            @Override public void visitCallExpression(KtCallExpression expression) {
                String callee = calleeName(expression);
                if ("apply".equals(callee)) planApply(expression);
                if ("setOnClickListener".equals(callee)) discoverListener(expression);
                super.visitCallExpression(expression);
            }

            @Override public void visitProperty(KtProperty property) {
                if (!property.isVar() && property.getInitializer() != null && !property.hasDelegate()) {
                    String binding = bindingFromExpression(property.getInitializer());
                    if (binding != null) {
                        KtNamedFunction owner = enclosingFunction(property);
                        Map<String, String> map = owner == null ? classAliases :
                                aliases.computeIfAbsent(owner, ignored -> new HashMap<>());
                        String name = property.getName();
                        if (name != null) {
                            String old = map.get(name);
                            if (old != null && !old.equals(binding)) map.put(name, AMBIGUOUS);
                            else map.put(name, binding);
                        }
                    }
                }
                super.visitProperty(property);
            }

            private void planApply(KtCallExpression apply) {
                ApplyInfo info = applyInfo(apply);
                if (info == null) return;
                String target = idAssignment(info.lambda);
                applyTargets.put(info.lambda, target == null ? AMBIGUOUS : target);
            }

            private String bindingFromExpression(KtExpression expression) {
                if (expression instanceof KtCallExpression) return findViewTarget((KtCallExpression) expression);
                if (expression instanceof KtDotQualifiedExpression) {
                    KtDotQualifiedExpression qualified = (KtDotQualifiedExpression) expression;
                    if (!(qualified.getSelectorExpression() instanceof KtCallExpression)) return null;
                    KtCallExpression selector = (KtCallExpression) qualified.getSelectorExpression();
                    if (!"apply".equals(calleeName(selector))) return null;
                    ApplyInfo info = applyInfo(selector);
                    if (info == null) return null;
                    String target = applyTargets.get(info.lambda);
                    if (target == null) {
                        String direct = idAssignment(info.lambda);
                        target = direct == null ? AMBIGUOUS : direct;
                    }
                    return target == null || AMBIGUOUS.equals(target) ? null : target;
                }
                return null;
            }

            private void discoverListener(KtCallExpression call) {
                Binding binding = listenerBinding(call);
                if (binding == null || binding.target == null || AMBIGUOUS.equals(binding.target)
                        || !targetSet.contains(binding.target)) return;
                if (!validListenerShape(call)) throw new LocalContractException("unsupported setOnClickListener overload");
                KtLambdaArgument argument = call.getLambdaArguments().get(0);
                KtLambdaExpression lambda = argument.getLambdaExpression();
                if (lambda == null || lambda.getFunctionLiteral().getBodyExpression() == null) {
                    throw new LocalContractException("listener lambda body is unsupported");
                }
                int line = lineAt(activityFile.source, call.getTextRange().getStartOffset());
                byTarget.computeIfAbsent(binding.target, ignored -> new ArrayList<>())
                        .add(new RawSite(binding.target, lambda, line));
            }

            private Binding listenerBinding(KtCallExpression call) {
                PsiElement parent = call.getParent();
                if (parent instanceof KtDotQualifiedExpression
                        && ((KtDotQualifiedExpression) parent).getSelectorExpression() == call) {
                    KtExpression receiver = ((KtDotQualifiedExpression) parent).getReceiverExpression();
                    if (receiver instanceof KtCallExpression) {
                        KtCallExpression find = (KtCallExpression) receiver;
                        String target = findViewResource(find);
                        if (target != null) {
                            if (!validFindViewShape(find)) throw new LocalContractException("unsupported findViewById overload");
                            return new Binding(target);
                        }
                    }
                    if (receiver instanceof KtNameReferenceExpression) {
                        String target = alias(((KtNameReferenceExpression) receiver).getReferencedName(), enclosingFunction(call));
                        if (target != null) return new Binding(target);
                    }
                    return null;
                }
                String target = enclosingApplyTarget(call);
                return target == null ? null : new Binding(target);
            }

            private String alias(String name, KtNamedFunction owner) {
                if (owner != null) {
                    Map<String, String> local = aliases.get(owner);
                    if (local != null && local.containsKey(name)) return local.get(name);
                }
                return classAliases.get(name);
            }

            private String enclosingApplyTarget(KtCallExpression call) {
                PsiElement current = call.getParent();
                while (current != null && current != activityClass) {
                    if (current instanceof KtLambdaExpression) {
                        String target = applyTargets.get(current);
                        if (target != null) return target;
                    }
                    if (current instanceof KtClassOrObject) return null;
                    current = current.getParent();
                }
                return null;
            }

            private String idAssignment(KtLambdaExpression lambda) {
                final List<String> ids = new ArrayList<>();
                lambda.accept(new KtTreeVisitorVoid() {
                    @Override public void visitLambdaExpression(KtLambdaExpression expression) {
                        if (expression != lambda) return;
                        super.visitLambdaExpression(expression);
                    }
                    @Override public void visitBinaryExpression(KtBinaryExpression expression) {
                        IElementType token = expression.getOperationToken();
                        if (KtTokens.EQ.equals(token) && expression.getLeft() instanceof KtNameReferenceExpression
                                && "id".equals(((KtNameReferenceExpression) expression.getLeft()).getReferencedName())) {
                            String resource = resourceId(expression.getRight());
                            ids.add(resource == null ? AMBIGUOUS : resource);
                        }
                        super.visitBinaryExpression(expression);
                    }
                });
                if (ids.size() != 1 || AMBIGUOUS.equals(ids.get(0))) return null;
                return ids.get(0);
            }
        }

        private ApplyInfo applyInfo(KtCallExpression apply) {
            if (!"apply".equals(calleeName(apply)) || apply.getValueArguments().size() != 1
                    || !(apply.getValueArguments().get(0) instanceof KtLambdaArgument)
                    || apply.getLambdaArguments().size() != 1) return null;
            PsiElement parent = apply.getParent();
            if (!(parent instanceof KtDotQualifiedExpression)
                    || ((KtDotQualifiedExpression) parent).getSelectorExpression() != apply) return null;
            KtExpression receiver = ((KtDotQualifiedExpression) parent).getReceiverExpression();
            if (!(receiver instanceof KtCallExpression) || !"Button".equals(calleeName((KtCallExpression) receiver))) return null;
            KtLambdaExpression lambda = apply.getLambdaArguments().get(0).getLambdaExpression();
            return lambda == null ? null : new ApplyInfo(apply, lambda);
        }

        private static String findViewTarget(KtCallExpression call) {
            String target = findViewResource(call);
            return target != null && validFindViewShape(call) ? target : null;
        }

        private static String findViewResource(KtCallExpression call) {
            if (!"findViewById".equals(calleeName(call)) || call.getValueArguments().isEmpty()) return null;
            return resourceId(call.getValueArguments().get(0).getArgumentExpression());
        }

        private static boolean validFindViewShape(KtCallExpression call) {
            return "findViewById".equals(calleeName(call)) && call.getValueArguments().size() == 1
                    && call.getLambdaArguments().isEmpty() && call.getTypeArguments().size() == 1
                    && call.getTypeArguments().get(0).getTypeReference() != null
                    && !call.getTypeArguments().get(0).getText().contains("*");
        }

        private static boolean validListenerShape(KtCallExpression call) {
            return call.getValueArguments().size() == 1
                    && call.getValueArguments().get(0) instanceof KtLambdaArgument
                    && call.getTypeArguments().isEmpty()
                    && call.getLambdaArguments().size() == 1
                    && call.getLambdaArguments().get(0).getLambdaExpression() != null;
        }

        private static String calleeName(KtCallExpression call) {
            return call.getCalleeExpression() instanceof KtNameReferenceExpression
                    ? ((KtNameReferenceExpression) call.getCalleeExpression()).getReferencedName() : null;
        }

        /** Extract exactly R.id.name from the PSI tree. */
        private static String resourceId(KtExpression expression) {
            if (!(expression instanceof KtDotQualifiedExpression)) return null;
            KtDotQualifiedExpression outer = (KtDotQualifiedExpression) expression;
            if (!(outer.getSelectorExpression() instanceof KtNameReferenceExpression)
                    || !(outer.getReceiverExpression() instanceof KtDotQualifiedExpression)) return null;
            KtDotQualifiedExpression middle = (KtDotQualifiedExpression) outer.getReceiverExpression();
            if (!(middle.getReceiverExpression() instanceof KtNameReferenceExpression)
                    || !(middle.getSelectorExpression() instanceof KtNameReferenceExpression)) return null;
            if (!"R".equals(((KtNameReferenceExpression) middle.getReceiverExpression()).getReferencedName())
                    || !"id".equals(((KtNameReferenceExpression) middle.getSelectorExpression()).getReferencedName())) return null;
            String name = ((KtNameReferenceExpression) outer.getSelectorExpression()).getReferencedName();
            return name != null && name.matches("[A-Za-z_][A-Za-z0-9_]{0,127}") ? name : null;
        }

        private static KtNamedFunction enclosingFunction(PsiElement element) {
            PsiElement current = element.getParent();
            while (current != null) {
                if (current instanceof KtNamedFunction) return (KtNamedFunction) current;
                current = current.getParent();
            }
            return null;
        }

        private static boolean hasPsiError(KtFile file) {
            final boolean[] found = {false};
            file.accept(new PsiRecursiveElementVisitor() {
                @Override public void visitErrorElement(PsiErrorElement element) { found[0] = true; }
            });
            return found[0];
        }

        private static String applyEdits(String source, List<Edit> planned) throws ContractException {
            List<Edit> ordered = new ArrayList<>(planned);
            ordered.sort((a, b) -> {
                int byStart = Integer.compare(b.start, a.start);
                return byStart != 0 ? byStart : Integer.compare(b.end, a.end);
            });
            int previousStart = source.length() + 1;
            for (Edit edit : ordered) {
                if (edit.start < 0 || edit.end < edit.start || edit.end > source.length() || edit.start > previousStart) {
                    throw new ContractException("overlapping Kotlin instrumentation edits");
                }
                previousStart = edit.start;
            }
            StringBuilder result = new StringBuilder(source);
            for (Edit edit : ordered) result.insert(edit.start, edit.text);
            return result.toString();
        }

        private String uniqueName(String base) {
            if (!usedNames.contains(base)) return base;
            int suffix = 2;
            while (usedNames.contains(base + suffix)) suffix++;
            return base + suffix;
        }
    }

    private static final class SourceFile {
        final String path;
        final String source;
        final KtFile file;
        SourceFile(String path, String source, KtFile file) {
            this.path = path;
            this.source = source;
            this.file = file;
        }
    }

    private static final class ActivityMatch {
        final SourceFile file;
        final KtClassOrObject declaration;
        ActivityMatch(SourceFile file, KtClassOrObject declaration) {
            this.file = file;
            this.declaration = declaration;
        }
    }

    private static final class ApplyInfo {
        final KtCallExpression call;
        final KtLambdaExpression lambda;
        ApplyInfo(KtCallExpression call, KtLambdaExpression lambda) {
            this.call = call;
            this.lambda = lambda;
        }
    }

    private static final class Binding {
        final String target;
        Binding(String target) { this.target = target; }
    }

    private static final class RawSite {
        final String target;
        final KtLambdaExpression lambda;
        final int line;
        RawSite(String target, KtLambdaExpression lambda, int line) {
            this.target = target;
            this.lambda = lambda;
            this.line = line;
        }
    }

    private static final class Site {
        final String id;
        final String path;
        final int line;
        final String target;
        Site(String id, String path, int line, String target) {
            this.id = id;
            this.path = path;
            this.line = line;
            this.target = target;
        }
    }

    private static final class Edit {
        final int start;
        final int end;
        final String text;
        Edit(int start, int end, String text) {
            this.start = start;
            this.end = end;
            this.text = text;
        }
    }

    private static final class ContractException extends Exception {
        ContractException(String message) { super(message); }
    }

    private static final class LocalContractException extends RuntimeException {
        LocalContractException(String message) { super(message); }
    }

    private static final class Result {
        final String activityPath;
        final String transformed;
        final List<Site> sites;
        Result(String activityPath, String transformed, List<Site> sites) {
            this.activityPath = activityPath;
            this.transformed = transformed;
            this.sites = sites;
        }

        String toJson() {
            StringBuilder json = new StringBuilder();
            json.append("{\"schemaVersion\":1,\"activityPath\":").append(jsonString(activityPath));
            json.append(",\"files\":{").append(jsonString(activityPath)).append(":")
                    .append(jsonString(transformed)).append("},\"sites\":[");
            for (int i = 0; i < sites.size(); i++) {
                if (i > 0) json.append(',');
                Site site = sites.get(i);
                json.append("{\"id\":").append(jsonString(site.id))
                        .append(",\"path\":").append(jsonString(site.path))
                        .append(",\"line\":").append(site.line)
                        .append(",\"target\":").append(jsonString(site.target))
                        .append(",\"kind\":\"tap\"}");
            }
            return json.append("]}").toString();
        }
    }

    private static String siteId(String path, int line, String target) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] bytes = digest.digest((path + "\u0000" + line + "\u0000" + target)
                    .getBytes(StandardCharsets.UTF_8));
            StringBuilder result = new StringBuilder("s");
            for (int i = 0; i < 8; i++) result.append(String.format("%02x", bytes[i]));
            return result.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable");
        }
    }

    private static int lineAt(String source, int offset) {
        int line = 1;
        for (int i = 0; i < offset && i < source.length(); i++) if (source.charAt(i) == '\n') line++;
        return line;
    }

    private static int firstNonWhitespace(String source, int start, int end) {
        int offset = start;
        while (offset < end && Character.isWhitespace(source.charAt(offset))) offset++;
        return offset;
    }

    private static String indentationAt(String source, int offset) {
        int lineStart = offset;
        while (lineStart > 0 && source.charAt(lineStart - 1) != '\n') lineStart--;
        int cursor = lineStart;
        while (cursor < offset && (source.charAt(cursor) == ' ' || source.charAt(cursor) == '\t')) cursor++;
        return source.substring(lineStart, cursor);
    }

    private static String indentationAtLine(String source, int offset) {
        return indentationAt(source, offset);
    }

    private static String bodyIndent(KtBlockExpression body) {
        String source = body.getContainingKtFile().getText();
        int brace = body.getRBrace().getTextRange().getStartOffset();
        return indentationAtLine(source, brace) + "    ";
    }

    private static String lifecycleAtEnd(String call, String source, int brace, String bodyIndent) {
        int lineStart = brace;
        while (lineStart > 0 && source.charAt(lineStart - 1) != '\n') lineStart--;
        String prefix = source.substring(lineStart, brace);
        String closeIndent = indentationAtLine(source, brace);
        if (prefix.trim().isEmpty()) {
            String extraIndent = bodyIndent.startsWith(prefix) ? bodyIndent.substring(prefix.length()) : bodyIndent;
            return extraIndent + call + "\n" + closeIndent;
        }
        return "\n" + bodyIndent + call + "\n" + closeIndent;
    }

    private static String catchFinally(String errorName, String tokenName, String source, int brace, String closeIndent) {
        int lineStart = brace;
        while (lineStart > 0 && source.charAt(lineStart - 1) != '\n') lineStart--;
        String prefix = source.substring(lineStart, brace);
        String first = prefix.trim().isEmpty() ? "" : "\n" + closeIndent;
        String inner = closeIndent + "    ";
        return first + "} catch (" + errorName + ": Throwable) {\n" + inner +
                "ReproAuto.threw(" + tokenName + ")\n" + inner + "throw " + errorName +
                "\n" + closeIndent + "} finally {\n" + inner + "ReproAuto.afterTap(" + tokenName +
                ")\n" + closeIndent + "}\n" + closeIndent;
    }

    private static String memberIndent(KtClassBody body, String source, int brace) {
        for (KtDeclaration declaration : body.getDeclarations()) {
            return indentationAtLine(source, declaration.getTextRange().getStartOffset());
        }
        return indentationAtLine(source, brace) + "    ";
    }

    private static String insertBeforeClosingBrace(String source, int brace, String content,
                                                    String memberIndent, String closingIndent) {
        int lineStart = brace;
        while (lineStart > 0 && source.charAt(lineStart - 1) != '\n') lineStart--;
        String prefix = source.substring(lineStart, brace);
        String first;
        if (prefix.isEmpty()) first = memberIndent + content;
        else if (prefix.trim().isEmpty()) first = content;
        else first = "\n" + memberIndent + content;
        return first + "\n" + closingIndent;
    }

    private static String jsonString(String value) {
        StringBuilder result = new StringBuilder(value.length() + 16).append('"');
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> result.append("\\\"");
                case '\\' -> result.append("\\\\");
                case '\b' -> result.append("\\b");
                case '\f' -> result.append("\\f");
                case '\n' -> result.append("\\n");
                case '\r' -> result.append("\\r");
                case '\t' -> result.append("\\t");
                default -> {
                    if (c < 0x20) result.append(String.format("\\u%04x", (int) c));
                    else result.append(c);
                }
            }
        }
        return result.append('"').toString();
    }
}
