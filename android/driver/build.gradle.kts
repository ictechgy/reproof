import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import org.jetbrains.kotlin.gradle.tasks.KotlinCompile

plugins {
    id("com.android.application")
}

apply(plugin = "org.jetbrains.kotlin.android")

android {
    namespace = "io.reproof.driver"
    compileSdk = 35

    sourceSets.getByName("main").java.srcDir("../native-common/src/main/java")

    defaultConfig {
        applicationId = "io.reproof.driver"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

tasks.withType<KotlinCompile>().configureEach {
    compilerOptions.jvmTarget.set(JvmTarget.JVM_17)
}
