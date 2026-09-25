plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.example.mototracker"
    compileSdk = 37

    defaultConfig {
        // Deliberately the spike's id: this installs over it, so neither phone has
        // to be paired again. The name and icon below are what anyone actually sees.
        applicationId = "com.example.mototracker"
        minSdk = 31
        targetSdk = 37
        versionCode = 108
        versionName = "1.0.8"
        // On-device tests, for what only a real Android can show: ./gradlew connectedDebugAndroidTest
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release { isMinifyEnabled = false }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlin {
        compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
}

dependencies {
    // The detector is plain Kotlin with no Android in it, so it can be replayed
    // over the whole archive on this desktop in a second — see DetectorTest.
    testImplementation(libs.kotlin.test)
    testImplementation(libs.junit)
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.test.junit)
    androidTestImplementation(libs.androidx.test.core)

    implementation(libs.core.ktx)
    implementation(libs.lifecycle.runtime)
    implementation(libs.lifecycle.compose)
    implementation(libs.activity.compose)

    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.graphics)
    implementation(libs.compose.material3)

    implementation(libs.okhttp)
    implementation(libs.kotlinx.coroutines)
    implementation(libs.play.location)
}
