pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "reproof-android"
include(":sdk", ":sample", ":driver", ":live")

// Offline, repository-owned app used only by native runtime acceptance CI.
include(":runtime-fixture")
