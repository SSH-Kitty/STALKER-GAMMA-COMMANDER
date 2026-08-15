#!/usr/bin/env bash
#
# Build a portable AppImage of STALKER GAMMA Commander.
#
# The AppImage is built on top of a *manylinux_2_28* Python from
# python-appimage rather than the host's Python. Building against the host
# would pin the result to the host's glibc (on a rolling distro that is far
# newer than anything users run), so the AppImage would refuse to start
# almost everywhere. manylinux_2_28 matches the ABI target of the official
# PySide6 wheels and yields a binary that runs on glibc 2.28+
# (Ubuntu 20.04+, Debian 11+, Fedora 29+, RHEL 8+).
#
# Usage:  ./build-appimage.sh [output-dir]
#
set -euo pipefail

# ----------------------------------------------------------------- settings
PY_SERIES=3.12
PY_FULL=3.12.13
PY_ABI=cp312
PY_PLATFORM=manylinux_2_28_x86_64
QT_PACKAGE="PySide6-Essentials>=6.6"   # app only needs QtCore/QtGui/QtWidgets
APP_NAME="STALKER-GAMMA-COMMANDER"
APP_TITLE="STALKER GAMMA Commander"
APP_ID="stalker-gamma-commander"
ARCH=x86_64

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-$PROJECT_DIR}"
BUILD_DIR="${BUILD_DIR:-$PROJECT_DIR/.appimage-build}"
CACHE_DIR="$BUILD_DIR/cache"
APPDIR="$BUILD_DIR/AppDir"

BASE_APPIMAGE="python${PY_FULL}-${PY_ABI}-${PY_ABI}-${PY_PLATFORM}.AppImage"
BASE_URL="https://github.com/niess/python-appimage/releases/download/python${PY_SERIES}/${BASE_APPIMAGE}"
TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"

# xcb/xkb helper libraries. Qt hard-requires these but stock Ubuntu/Debian
# often ship without them, which is the classic "could not load the Qt
# platform plugin xcb" failure. They are dropped into PySide6/Qt/lib, which
# is already on the RUNPATH of libqxcb.so ($ORIGIN/../../lib) and
# libQt6XcbQpa.so.6 ($ORIGIN) - so no LD_LIBRARY_PATH is needed and the
# bundled stalker-gamma CLI subprocess keeps a pristine environment.
# Deliberately NOT bundled: libc/libstdc++/libgcc (ABI-coupled) and
# libGL/libEGL/libX11/libxcb.so.1 (graphics-driver-coupled).
BUNDLED_SYS_LIBS=(
  libxcb-cursor.so.0 libxcb-icccm.so.4 libxcb-image.so.0
  libxcb-keysyms.so.1 libxcb-render-util.so.0 libxcb-util.so.1
  libxkbcommon.so.0 libxkbcommon-x11.so.0
)
SKIPPED_LIBS=()

# The AppImage's ABI floor. Nothing bundled from the build host may exceed it.
# 2.34 is imposed by the Qt 6.11 wheels and by the vendored stalker-gamma CLI
# itself, so it cannot be lowered without rebuilding those.
MAX_GLIBC=2.34

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -d "$PROJECT_DIR/stalker_gamma_gui" ] || die "run this from the project root"
[ -x "$PROJECT_DIR/cli/usr/bin/stalker-gamma" ] || die "cli/usr/bin/stalker-gamma missing or not executable"

mkdir -p "$CACHE_DIR" "$OUT_DIR"
rm -rf "$APPDIR"

# --------------------------------------------------------------- fetch tools
fetch() { # url dest
  [ -s "$2" ] && { log "cached: $(basename "$2")"; return; }
  log "downloading $(basename "$2")"
  curl -fL --retry 3 --connect-timeout 30 -o "$2" "$1"
  chmod +x "$2"
}
fetch "$TOOL_URL" "$CACHE_DIR/appimagetool"
fetch "$BASE_URL" "$CACHE_DIR/$BASE_APPIMAGE"

# --------------------------------------------------- base python -> AppDir
log "extracting manylinux Python base"
( cd "$BUILD_DIR" && rm -rf squashfs-root \
  && "$CACHE_DIR/$BASE_APPIMAGE" --appimage-extract >/dev/null \
  && mv squashfs-root "$APPDIR" )

PY="$APPDIR/opt/python$PY_SERIES/bin/python$PY_SERIES"
PYLIB="$APPDIR/opt/python$PY_SERIES/lib/python$PY_SERIES"
SITE="$PYLIB/site-packages"
[ -x "$PY" ] || die "bundled interpreter not found at $PY"

log "installing $QT_PACKAGE"
"$PY" -m pip install --no-cache-dir -q --upgrade pip
"$PY" -m pip install --no-cache-dir -q "$QT_PACKAGE"

# ------------------------------------------------------------------- prune
# Everything removed here is unreachable for a QtWidgets-only application.
log "pruning unused Qt modules and stdlib"
PS="$SITE/PySide6"
QTLIB="$PS/Qt/lib"

# Python bindings for Qt modules the app never imports.
# Uses find's own -name filters rather than piping to xargs: the project path
# may contain spaces, which xargs would split into broken (silently ignored)
# arguments, leaving the bindings in place.
find "$PS" -maxdepth 1 -name '*.abi3.so' \
     ! -name 'QtCore.abi3.so' ! -name 'QtGui.abi3.so' ! -name 'QtWidgets.abi3.so' \
     -delete
find "$PS" -maxdepth 1 -name '*.pyi' -delete

# Qt developer tools shipped inside the wheel.
for tool in qmlls qmlformat assistant linguist lupdate lrelease designer uic rcc \
            qmllint qmlimportscanner qmlcachegen qmltyperegistrar qsb balsam \
            qmlprofiler qmlscene qmltestrunner svgtoqml qmldom \
            shiboken6 shiboken6-genpyi deploy_lib project_lib metaobjectdump.py; do
  rm -rf "$PS/$tool"
done

# Qt feature sets with no path from QtWidgets.
rm -rf "$PS/Qt/qml" "$PS/Qt/translations" "$PS/Qt/libexec" \
       "$PS/Qt/metatypes" "$PS/Qt/modules" "$PS/Qt/typesystems"
find "$QTLIB" -maxdepth 1 -name 'libQt6*' \
  \( -name '*Quick*'  -o -name '*Qml*'    -o -name '*Designer*' -o -name '*Test*' \
  -o -name '*Sql*'    -o -name '*Help*'   -o -name '*Charts*'   -o -name '*Multimedia*' \
  -o -name '*WebSockets*' -o -name '*UiTools*' -o -name '*Concurrent*' \
  -o -name '*Labs*'   -o -name '*Lottie*' \) -delete
for plug in qmltooling sqldrivers designer assetimporters multimedia renderers \
            help qmllint scenegraph texttospeech webview position sensors; do
  rm -rf "$PS/Qt/plugins/$plug"
done

# Stdlib a bundled GUI app cannot use, plus pip now that installing is done.
rm -rf "$PYLIB/test" "$PYLIB/idlelib" "$PYLIB/tkinter" "$PYLIB/lib2to3" \
       "$PYLIB/ensurepip" "$PYLIB/turtledemo" "$SITE/pip" "$SITE"/pip-*.dist-info
rm -rf "$APPDIR/usr/share/tcltk"
find "$APPDIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$APPDIR" -name '*.pyc' -delete 2>/dev/null || true

# ------------------------------------------------- bundle xcb helper libs
# Locate a shared library on the build host.
# Note: awk deliberately does NOT 'exit' on first match - that would close the
# pipe, kill ldconfig with SIGPIPE, and trip 'set -o pipefail'.
find_syslib() {
  local name="$1" ldc path
  for ldc in ldconfig /sbin/ldconfig /usr/sbin/ldconfig; do
    command -v "$ldc" >/dev/null 2>&1 || continue
    path="$("$ldc" -p 2>/dev/null | awk -v n="$name" '$1==n && !seen {print $NF; seen=1}')" || true
    if [ -n "$path" ] && [ -e "$path" ]; then printf '%s\n' "$path"; return 0; fi
  done
  for dir in /usr/lib/x86_64-linux-gnu /lib/x86_64-linux-gnu /usr/lib64 /lib64 /usr/lib /lib; do
    if [ -e "$dir/$name" ]; then printf '%s\n' "$dir/$name"; return 0; fi
  done
  return 1
}

# Highest glibc symbol version an ELF file references.
max_glibc_of() {
  objdump -T "$1" 2>/dev/null | grep -oE 'GLIBC_[0-9]+\.[0-9]+' \
    | sed 's/GLIBC_//' | sort -uV | tail -1
}

log "bundling xcb/xkb helper libraries (ABI gate: glibc <= $MAX_GLIBC)"
for lib in "${BUNDLED_SYS_LIBS[@]}"; do
  if ! src="$(find_syslib "$lib")"; then
    echo "    ! $lib absent on build host - not bundled" >&2
    SKIPPED_LIBS+=("$lib (absent)")
    continue
  fi
  need="$(max_glibc_of "$src")"
  # A library copied from the build host must not raise the AppImage's ABI
  # floor. Rolling distros build these against a very new glibc, which would
  # silently make the AppImage refuse to start on the distros we target.
  if [ -n "$need" ] && [ "$(printf '%s\n%s\n' "$MAX_GLIBC" "$need" | sort -V | tail -1)" != "$MAX_GLIBC" ]; then
    echo "    ! $lib needs glibc $need > $MAX_GLIBC - not bundled" >&2
    SKIPPED_LIBS+=("$lib (needs glibc $need)")
    continue
  fi
  cp -L "$src" "$QTLIB/$lib"
  echo "    + $lib (glibc ${need:-none})"
done
if [ ${#SKIPPED_LIBS[@]} -gt 0 ]; then
  log "NOT bundled - users must have these from their distro:"
  printf '      %s\n' "${SKIPPED_LIBS[@]}"
fi

# --------------------------------------------------------------- payload
# Laid out so config.py's project_root() (parents[1] of the package) lands on
# opt/$APP_ID, making cli/usr/bin/stalker-gamma resolve with no code changes.
log "copying application payload"
PAYLOAD="$APPDIR/opt/$APP_ID"
mkdir -p "$PAYLOAD"
cp -r "$PROJECT_DIR/stalker_gamma_gui" "$PAYLOAD/"
cp -r "$PROJECT_DIR/cli" "$PAYLOAD/"
[ -f "$PROJECT_DIR/README.md" ] && cp "$PROJECT_DIR/README.md" "$PAYLOAD/"
[ -f "$PROJECT_DIR/LICENSE" ] && cp "$PROJECT_DIR/LICENSE" "$PAYLOAD/"
find "$PAYLOAD/stalker_gamma_gui" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------- AppRun / desktop / icon
log "writing AppRun, desktop entry and icons"
cat > "$APPDIR/AppRun" <<APPRUN
#!/usr/bin/env bash
# APPDIR is exported by the AppImage runtime; derive it when running extracted.
if [ -z "\${APPDIR:-}" ]; then
    APPDIR="\$(dirname "\$(readlink -f "\$0")")"
fi
export APPDIR

# Bundled CA bundle, so urllib (modlist / modpack fetches) can verify TLS.
if [ -z "\${SSL_CERT_FILE:-}" ] && [ -f "\$APPDIR/opt/_internal/certs.pem" ]; then
    export SSL_CERT_FILE="\$APPDIR/opt/_internal/certs.pem"
fi

# Pre-flight: Qt's xcb platform plugin needs libxcb-cursor.so.0, which several
# distros (notably Ubuntu 22.04 / Debian 12) do not install by default. It
# cannot be bundled here without raising the AppImage's glibc floor, so warn
# with something actionable instead of leaving Qt to abort cryptically.
if [ -z "\${QT_QPA_PLATFORM:-}" ]; then
    _found_xcb_cursor=""
    for _d in /usr/lib/x86_64-linux-gnu /lib/x86_64-linux-gnu /usr/lib64 /lib64 /usr/lib /lib; do
        [ -e "\$_d/libxcb-cursor.so.0" ] && { _found_xcb_cursor=1; break; }
    done
    if [ -z "\$_found_xcb_cursor" ]; then
        cat >&2 <<'MISSING'
STALKER GAMMA Commander: libxcb-cursor.so.0 was not found.
Qt needs it for the X11 backend. Install it with one of:
    Debian/Ubuntu : sudo apt install libxcb-cursor0
    Fedora/RHEL   : sudo dnf install xcb-util-cursor
    Arch          : sudo pacman -S xcb-util-cursor
    openSUSE      : sudo zypper install libxcb-cursor0
MISSING
    fi
    unset _d _found_xcb_cursor
fi

# Point the interpreter at the bundled payload and nothing else.
export PYTHONNOUSERSITE=1
export PYTHONPATH="\$APPDIR/opt/$APP_ID"

# Flags matter here:
#   -s  ignore the user's ~/.local site-packages
#   -P  do NOT prepend the current directory to sys.path, so a stray
#       'stalker_gamma_gui' next to wherever the user launched us cannot
#       shadow the bundled copy
# -E is deliberately NOT used: it would discard the PYTHONPATH set above.
#
# Also deliberately no LD_LIBRARY_PATH: Qt resolves its libraries through
# RUNPATH, so the bundled stalker-gamma CLI we spawn inherits a clean
# environment and finds its own libcurl-impersonate/libgit2 correctly.
exec "\$APPDIR/opt/python$PY_SERIES/bin/python$PY_SERIES" -s -P \\
     -m stalker_gamma_gui "\$@"
APPRUN
chmod +x "$APPDIR/AppRun"

rm -f "$APPDIR"/python*.desktop "$APPDIR"/python*.png "$APPDIR/.DirIcon"
cat > "$APPDIR/$APP_ID.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$APP_TITLE
GenericName=STALKER GAMMA Mod Manager
Comment=Install, update and launch the S.T.A.L.K.E.R. Anomaly + GAMMA mod pack
Exec=AppRun %U
Icon=$APP_ID
Terminal=false
Categories=Game;
Keywords=stalker;anomaly;gamma;mods;modorganizer;
DESKTOP

ICON_SRC="$PROJECT_DIR/cli/stalker-gamma.png"
[ -f "$ICON_SRC" ] || die "icon not found at $ICON_SRC"
cp "$ICON_SRC" "$APPDIR/$APP_ID.png"
icon_size="$(identify -format '%wx%h' "$ICON_SRC" 2>/dev/null || echo 256x256)"
install -Dm644 "$ICON_SRC" "$APPDIR/usr/share/icons/hicolor/${icon_size}/apps/$APP_ID.png"
ln -sf "$APP_ID.png" "$APPDIR/.DirIcon"
install -Dm644 "$APPDIR/$APP_ID.desktop" "$APPDIR/usr/share/applications/$APP_ID.desktop"

# ------------------------------------------------------------------ package
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$PROJECT_DIR/stalker_gamma_gui/__init__.py")"
VERSION="${VERSION:-0.0.0}"
OUTPUT="$OUT_DIR/${APP_NAME}-${VERSION}-${ARCH}.AppImage"
log "packaging $(basename "$OUTPUT")  (AppDir: $(du -sh "$APPDIR" | cut -f1))"
rm -f "$OUTPUT"
env APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" VERSION="$VERSION" \
    "$CACHE_DIR/appimagetool" --comp zstd --no-appstream "$APPDIR" "$OUTPUT" \
  || { log "zstd failed, retrying with default compression"
       env APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" VERSION="$VERSION" \
           "$CACHE_DIR/appimagetool" --no-appstream "$APPDIR" "$OUTPUT"; }

chmod +x "$OUTPUT"
log "done: $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
