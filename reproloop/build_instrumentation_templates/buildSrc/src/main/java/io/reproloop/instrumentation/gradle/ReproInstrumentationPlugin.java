package io.reproloop.instrumentation.gradle;

import java.lang.reflect.Method;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import kotlin.jvm.functions.Function1;
import org.gradle.api.Action;
import org.gradle.api.GradleException;
import org.gradle.api.NamedDomainObjectContainer;
import org.gradle.api.Plugin;
import org.gradle.api.Project;
import org.gradle.api.file.Directory;
import org.gradle.api.file.RegularFile;
import org.gradle.api.file.RegularFileProperty;
import org.gradle.api.provider.ListProperty;
import org.gradle.api.tasks.TaskProvider;
import com.android.build.api.dsl.AndroidSourceSet;
import com.android.build.api.dsl.ApplicationExtension;
import com.android.build.api.variant.ApplicationAndroidComponentsExtension;
import com.android.build.api.variant.ApplicationVariant;
import com.android.build.api.variant.ScopedArtifacts;
import com.android.build.api.variant.ScopedArtifactsOperation;
import com.android.build.api.artifact.ScopedArtifact;

/** Applies the debug-only, plan-bound class transform to one Android application module. */
public final class ReproInstrumentationPlugin implements Plugin<Project> {
    @Override
    public void apply(Project project) {
        project.getPluginManager().withPlugin("com.android.application", ignored -> configure(project));
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private static void configure(Project project) {
        Plan plan = Plan.fromGenerated();
        require(plan.module.equals(project.getPath()),
                "ReproPlan.MODULE " + plan.module + " does not select project " + project.getPath());

        Path instrumentationRoot = project.getProjectDir().toPath().resolve("reproloop-instrumentation");
        Path runtimeRoot = instrumentationRoot.resolve("runtime");
        Path manifest = instrumentationRoot.resolve("AndroidManifest.xml");
        requireDirectory(runtimeRoot, "Instrumentation runtime source directory");
        requireFile(manifest, "Instrumentation debug manifest");

        ApplicationExtension android = project.getExtensions().getByType(ApplicationExtension.class);
        AndroidSourceSet debug = sourceSet(android, "debug");
        Path conventionalManifest = project.getProjectDir().toPath()
                .resolve("src/debug/AndroidManifest.xml").toAbsolutePath().normalize();
        Path configuredManifest = existingManifestPath(debug, conventionalManifest);
        require(configuredManifest != null && configuredManifest.equals(conventionalManifest),
                "Custom debug manifest paths are unsupported for bytecode instrumentation");
        debug.getJava().srcDir(runtimeRoot.toFile());
        debug.getManifest().srcFile(manifest.toFile());
        Path observationAssets = instrumentationRoot.resolve("assets");
        if (Files.exists(observationAssets, java.nio.file.LinkOption.NOFOLLOW_LINKS)) {
            requireDirectory(observationAssets, "Instrumentation observation assets");
            debug.getAssets().srcDir(observationAssets.toFile());
        }

        ApplicationAndroidComponentsExtension components = project.getExtensions()
                .getByType(ApplicationAndroidComponentsExtension.class);
        components.onVariants(components.selector().withName(plan.variant), new Action<ApplicationVariant>() {
            @Override
            public void execute(ApplicationVariant variant) {
                require(variant.getName().equals(plan.variant), "Android variant selection is not exact");
                require(variant.getDebuggable() && (plan.variant.equals("debug") || plan.variant.endsWith("Debug")),
                        "Repro instrumentation requires the selected debuggable Debug variant");
                registerTransform(project, variant, plan);
            }
        });
    }

    private static void registerTransform(Project project, ApplicationVariant variant, Plan plan) {
        String suffix = Character.toUpperCase(plan.variant.charAt(0)) + plan.variant.substring(1);
        TaskProvider<ReproInstrumentationTask> task = project.getTasks().register(
                "reproInstrument" + suffix, ReproInstrumentationTask.class, configured -> {
                    configured.getModule().set(plan.module);
                    configured.getVariant().set(plan.variant);
                    configured.getActivity().set(plan.activity);
                    configured.getProfileDigest().set(plan.profileDigest);
                    configured.getSites().set(plan.encodedSites);
                    configured.getOutputJar().set(project.getLayout().getBuildDirectory()
                            .file("reproloop/" + plan.variant + "/classes.jar"));
                    configured.getFinalJar().set(project.getLayout().getBuildDirectory()
                            .file("reproloop/" + plan.variant + "/classes.jar"));
                    configured.getReport().set(project.getLayout().getBuildDirectory()
                            .file("reproloop/" + plan.variant + "/report.json"));
                });
        ScopedArtifactsOperation<ReproInstrumentationTask> operation = variant.getArtifacts()
                .forScope(ScopedArtifacts.Scope.PROJECT).use(task);
        operation.toTransform(ScopedArtifact.CLASSES.INSTANCE,
                new Function1<ReproInstrumentationTask, ListProperty<RegularFile>>() {
                    @Override
                    public ListProperty<RegularFile> invoke(ReproInstrumentationTask value) {
                        return value.getAllJars();
                    }
                },
                new Function1<ReproInstrumentationTask, ListProperty<Directory>>() {
                    @Override
                    public ListProperty<Directory> invoke(ReproInstrumentationTask value) {
                        return value.getAllDirectories();
                    }
                },
                new Function1<ReproInstrumentationTask, RegularFileProperty>() {
                    @Override
                    public RegularFileProperty invoke(ReproInstrumentationTask value) {
                        return value.getOutputJar();
                    }
                });
    }

    private static AndroidSourceSet sourceSet(ApplicationExtension android, String name) {
        NamedDomainObjectContainer<? extends AndroidSourceSet> sourceSets = android.getSourceSets();
        return sourceSets.getByName(name);
    }

    private static Path existingManifestPath(AndroidSourceSet sourceSet, Path conventional) {
        Object manifest = sourceSet.getManifest();
        try {
            // AGP 8.13's concrete AndroidSourceFile exposes getSrcFile(), although the public
            // DSL interface only documents srcFile(Object). Treat any API drift as unsafe.
            Method method = manifest.getClass().getMethod("getSrcFile");
            Object value = method.invoke(manifest);
            if (value == null) {
                return conventional;
            }
            Path path = pathFrom(value);
            return path == null ? null : path.toAbsolutePath().normalize();
        } catch (ReflectiveOperationException ignored) {
            // A future AGP implementation without a readable source path must fail closed.
            return null;
        }
    }

    private static Path pathFrom(Object value) {
        if (value instanceof java.io.File file) {
            return file.toPath();
        }
        if (value instanceof Iterable<?> values) {
            Path found = null;
            for (Object item : values) {
                Path current = pathFrom(item);
                if (current != null) {
                    require(found == null || found.equals(current), "Debug manifest has multiple source paths");
                    found = current;
                }
            }
            return found;
        }
        return null;
    }

    private static void requireFile(Path path, String description) {
        require(!Files.isSymbolicLink(path) && Files.isRegularFile(path, java.nio.file.LinkOption.NOFOLLOW_LINKS),
                description + " is missing or invalid: " + path);
    }

    private static void requireDirectory(Path path, String description) {
        require(!Files.isSymbolicLink(path) && Files.isDirectory(path, java.nio.file.LinkOption.NOFOLLOW_LINKS),
                description + " is missing or invalid: " + path);
    }

    private static void require(boolean condition, String message) {
        if (!condition) {
            throw new GradleException(message);
        }
    }

    private record Plan(String module, String variant, String activity, String profileDigest,
            List<String> encodedSites) {
        private static Plan fromGenerated() {
            String module = ReproPlan.MODULE;
            String variant = ReproPlan.VARIANT;
            String activity = ReproPlan.ACTIVITY;
            String profileDigest = ReproPlan.PROFILE_DIGEST;
            require(module != null && module.startsWith(":") && !module.contains("..") && !module.contains("/"),
                    "ReproPlan.MODULE is invalid");
            require(variant != null && (variant.equals("debug") || variant.matches("[A-Za-z][A-Za-z0-9]*Debug")),
                    "ReproPlan.VARIANT must select a Debug variant");
            require(activity != null && activity.matches("[A-Za-z_$][A-Za-z0-9_$.]*"),
                    "ReproPlan.ACTIVITY is invalid");
            require(profileDigest != null && profileDigest.matches("[0-9a-f]{64}"),
                    "ReproPlan.PROFILE_DIGEST is invalid");
            require(ReproPlan.SITES != null, "ReproPlan.SITES is missing");
            TreeMap<Integer, String> sites = new TreeMap<>();
            for (Map.Entry<Integer, String> site : ReproPlan.SITES.entrySet()) {
                require(site.getKey() != null && site.getKey() > 0
                                && site.getValue() != null && site.getValue().matches("s[0-9a-f]+"),
                        "ReproPlan.SITES contains an invalid entry");
                require(sites.put(site.getKey(), site.getValue()) == null,
                        "Duplicate ReproPlan site line " + site.getKey());
            }
            require(sites.size() == new java.util.HashSet<>(sites.values()).size(),
                    "Duplicate ReproPlan site id");
            List<String> encodedSites = new ArrayList<>();
            for (Map.Entry<Integer, String> site : sites.entrySet()) {
                encodedSites.add(site.getKey() + "=" + site.getValue());
            }
            return new Plan(module, variant, activity, profileDigest, encodedSites);
        }

        private Plan {
            encodedSites = Collections.unmodifiableList(new ArrayList<>(encodedSites));
        }
    }
}
