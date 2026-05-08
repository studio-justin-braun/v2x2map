plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "org.opentrafficmap.receiver"
    compileSdk = 34

    defaultConfig {
        applicationId = "org.opentrafficmap.receiver"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }

    packaging {
        resources {
            excludes += setOf("META-INF/INDEX.LIST", "META-INF/io.netty.versions.properties")
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.recyclerview:recyclerview:1.3.2")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.7.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-ktx:2.7.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")

    // USB CDC/ACM serial driver
    implementation("com.github.mik3y:usb-serial-for-android:3.7.0")

    // OpenStreetMap (offline-capable, no API key)
    implementation("org.osmdroid:osmdroid-android:6.1.18")

    // Wrapping toolbar/button row (controls overflow gracefully on narrow screens)
    implementation("com.google.android.flexbox:flexbox:3.0.0")

    // Google Play Services Location for the "centre on me" button
    implementation("com.google.android.gms:play-services-location:21.0.1")

    // MQTT (Paho Java client, async I/O runs on its own thread)
    implementation("org.eclipse.paho:org.eclipse.paho.client.mqttv3:1.2.5")
}
