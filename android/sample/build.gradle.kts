import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import org.jetbrains.kotlin.gradle.tasks.KotlinCompile

plugins {
    id("com.android.application")
}

apply(plugin = "org.jetbrains.kotlin.android")

android {
    namespace = "io.reproof.sample"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.reproof.sample"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1"
    }

    buildFeatures.buildConfig = true
    flavorDimensions += "behavior"
    productFlavors {
        create("buggy") {
            dimension = "behavior"
            buildConfigField("boolean", "BUGGY", "true")
        }
        create("fixed") {
            dimension = "behavior"
            buildConfigField("boolean", "BUGGY", "false")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

tasks.withType<KotlinCompile>().configureEach {
    compilerOptions.jvmTarget.set(JvmTarget.JVM_17)
}

dependencies {
    implementation(project(":sdk"))
    testImplementation("junit:junit:4.13.2")
}
