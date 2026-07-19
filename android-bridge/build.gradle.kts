// Root build script. Plugin versions are aligned with the locally cached
// toolchain (AGP 8.7.3 + Kotlin 2.0.21) to minimise fresh downloads.
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("org.jetbrains.kotlin.plugin.serialization") version "2.0.21" apply false
}
