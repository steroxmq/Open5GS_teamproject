#!/usr/bin/env bash
# Install an isolated, pinned iperf3 for the Compose lab measurements.
set -euo pipefail

VERSION="3.21"
SHA256="656e4405ebd620121de7ceca3eaf43a88f79ea1b857d041a6a0b1314801acdd8"
PREFIX="$HOME/.local/open5gs-lab/iperf3-$VERSION"
URL="https://github.com/esnet/iperf/releases/download/$VERSION/iperf-$VERSION.tar.gz"

if [ "$(id -u)" -eq 0 ]; then
    printf 'Spustite ako bezny pouzivatel: bash scripts/setup-lab-iperf3.sh\n' >&2
    exit 1
fi
for tool in cc make curl tar sha256sum install; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Chyba %s. Nainstalujte build-essential curl ca-certificates.\n' "$tool" >&2
        exit 1
    fi
done

BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/open5gs-iperf3-$VERSION-XXXXXX")"
cleanup() {
    code=$?
    trap - EXIT
    if [ "$code" -eq 0 ]; then
        rm -rf -- "$BUILD_DIR"
    else
        printf 'Instalacia zlyhala. Docasne subory a logy: %s\n' "$BUILD_DIR" >&2
        for log in "$BUILD_DIR/configure.log" "$BUILD_DIR/build.log"; do
            if [ -f "$log" ]; then tail -n 20 "$log" >&2; fi
        done
    fi
    exit "$code"
}
trap cleanup EXIT

printf 'Stahujem oficialny iperf3 %s...\n' "$VERSION"
curl --fail --location --retry 2 --connect-timeout 15 --max-time 180 \
    "$URL" -o "$BUILD_DIR/source.tar.gz"
printf '%s  %s\n' "$SHA256" "$BUILD_DIR/source.tar.gz" | sha256sum --check -
tar -xzf "$BUILD_DIR/source.tar.gz" -C "$BUILD_DIR"
cd "$BUILD_DIR/iperf-$VERSION"
printf 'Konfiguracia...\n'
./configure --disable-shared --without-openssl >"$BUILD_DIR/configure.log" 2>&1
printf 'Kompilacia (make -j1)...\n'
make -j1 >"$BUILD_DIR/build.log" 2>&1
./src/iperf3 --version
install -D -m 755 ./src/iperf3 "$PREFIX/bin/iperf3"
printf '\nHOTOVO: %s/bin/iperf3\n' "$PREFIX"
