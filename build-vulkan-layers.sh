#!/usr/bin/env bash
#
# Build the MangoHud and vkBasalt Vulkan layers COMMANDER bundles
# (cli/usr/lib/mangohud, cli/usr/lib/vkbasalt). Only the 64-bit Vulkan
# layers: Anomaly runs on Vulkan through DXVK.
#
# Built in an AlmaLinux 9 container, so they need glibc 2.34 at most, the
# AppImage's floor (see build-appimage.sh). The distros' own builds and
# MangoHud's release need far newer ones. Run it after bumping a version
# below, then commit the two libraries.
#
# vkBasalt gets packaging/vkbasalt-live-reload.patch: it rebuilds its effects
# when VKBASALT_CONFIG_FILE changes, so Settings > Graphics shows in a
# running game. MangoHud gets packaging/mangohud-reload-lock.patch: it
# reloads its config under the lock its HUD is drawn under, so a change in
# a running game can't crash it (std::bad_function_call). Both get a
# *-state-file.patch: they write 1 (on) or 0 (off) to the file in
# COMMANDER_MANGOHUD_STATE / COMMANDER_VKBASALT_STATE whenever they go on
# or off, for COMMANDER's pop-up.
#
# Usage:  ./build-vulkan-layers.sh
#
set -euo pipefail

export MANGOHUD_VERSION=v0.8.4
export VKBASALT_VERSION=v0.3.2.10

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$PROJECT_DIR/cli/usr/lib"

docker run --rm -v "$OUT:/out" -v "$PROJECT_DIR/packaging:/patches:ro" -e MANGOHUD_VERSION -e VKBASALT_VERSION \
  -e OWNER="$(id -u):$(id -g)" docker.io/library/almalinux:9 bash -euxc '
dnf -y -q install dnf-plugins-core epel-release >/dev/null
dnf config-manager --set-enabled crb
dnf -y -q install gcc-c++ libstdc++-static git python3-pip glslang libX11-devel libXext-devel \
  wayland-devel libxkbcommon-devel vulkan-headers spirv-headers-devel >/dev/null
pip3 install -q meson ninja mako
cd /tmp
git clone -q --depth 1 -b "$MANGOHUD_VERSION" https://github.com/flightlessmango/MangoHud.git
( cd MangoHud && git apply /patches/mangohud-reload-lock.patch && git apply /patches/mangohud-state-file.patch && meson setup build --buildtype=release -Dstrip=true \
    -Dwith_xnvctrl=disabled -Dwith_dbus=disabled -Dmangoapp=false -Dmangohudctl=false \
    -Dmangoplot=disabled -Dtests=disabled -Dinclude_doc=false -Duse_system_spdlog=disabled \
  && ninja -C build src/libMangoHud.so )
git clone -q --depth 1 -b "$VKBASALT_VERSION" https://github.com/DadSchoorse/vkBasalt.git
( cd vkBasalt && git apply /patches/vkbasalt-live-reload.patch && git apply /patches/vkbasalt-state-file.patch && meson setup build --buildtype=release -Dstrip=true && ninja -C build )
mkdir -p /out/mangohud /out/vkbasalt
install -m644 MangoHud/build/src/libMangoHud.so /out/mangohud/
install -m644 vkBasalt/build/src/libvkbasalt.so /out/vkbasalt/
chown -R "$OWNER" /out/mangohud /out/vkbasalt
'
for lib in "$OUT"/mangohud/libMangoHud.so "$OUT"/vkbasalt/libvkbasalt.so; do
  printf '%s  glibc %s\n' "$lib" \
    "$(objdump -T "$lib" | grep -oE 'GLIBC_[0-9.]+' | sed 's/GLIBC_//' | sort -uV | tail -1)"
done
