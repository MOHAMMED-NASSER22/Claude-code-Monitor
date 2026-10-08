#!/usr/bin/env bash
# Build a signed TokenMaxxing.apk on this PC (Git Bash on Windows).
#   ./build.sh 1.0 [versionCode]
# Toolchain (JDK 17, Gradle 8.9, Android SDK 35) and the release key live in
# ~/.token-maxxing — back up android-release.jks and its password file: Android
# only installs an update over an APK signed with the same key.
set -euo pipefail
cd "$(dirname "$0")"
TM="$HOME/.token-maxxing"
T="$TM/toolchain"
export JAVA_HOME="$(ls -d "$T"/jdk-17* | head -1)"
export ANDROID_HOME="$T/sdk"
export PATH="$JAVA_HOME/bin:$T/gradle-8.9/bin:$PATH"

export TM_VERSION_NAME="${1:-0.0-dev}"
# Default versionCode from the version name: 1.2 -> 102.
IFS=. read -r MA MI _ <<< "$TM_VERSION_NAME"
export TM_VERSION_CODE="${2:-$(( ${MA//[!0-9]/} * 100 + ${MI//[!0-9]/0} ))}"

KS="$TM/android-release.jks"
PROPS="$TM/android-release-password.txt"
if [ ! -f "$KS" ]; then
  echo "Creating release key at $KS"
  PW="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 28)"
  keytool -genkeypair -storetype PKCS12 -keystore "$KS" -alias tokenmaxxing \
    -keyalg RSA -keysize 2048 -validity 10000 -storepass "$PW" -keypass "$PW" \
    -dname "CN=Token Maxxing" >/dev/null
  printf 'password: %s\n' "$PW" > "$PROPS"
fi
PW="$(sed -n 's/^password: //p' "$PROPS")"
export TM_KEYSTORE="$KS" TM_KEY_ALIAS=tokenmaxxing TM_KEYSTORE_PASSWORD="$PW" TM_KEY_PASSWORD="$PW"

gradle --no-daemon -q assembleRelease
mkdir -p dist
cp app/build/outputs/apk/release/app-release.apk dist/TokenMaxxing.apk
echo "Built dist/TokenMaxxing.apk ($TM_VERSION_NAME, code $TM_VERSION_CODE)"
