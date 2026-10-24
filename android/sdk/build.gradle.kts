import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import org.jetbrains.kotlin.gradle.tasks.KotlinCompile

plugins {
    id("com.android.library")
}

apply(plugin = "org.jetbrains.kotlin.android")

android {
    namespace = "io.reproof.sdk"
    compileSdk = 35

    defaultConfig {
        minSdk = 26
        consumerProguardFiles("consumer-rules.pro")
    }

    // Compile the real source-vendored runtime only in the test variant, never in the SDK AAR.
    sourceSets.getByName("test").java.srcDir("../../reproof/instrumentation_templates/android/debug/java")

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

tasks.withType<KotlinCompile>().configureEach {
    compilerOptions.jvmTarget.set(JvmTarget.JVM_17)
}

dependencies {
    testImplementation("junit:junit:4.13.2")
}
