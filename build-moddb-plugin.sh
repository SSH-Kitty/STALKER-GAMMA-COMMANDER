#!/usr/bin/env bash
#
# Build the ModDB browser plugin: COMMANDER-ModDB-Browser-Plugin-linux-x86_64.tar.xz
# plus its .sha512sum. Upload both to the same GitHub release as the
# AppImage; Settings > Plugins downloads them from there
# (commander_gui/plugins.py).
#
# The archive is one "moddb-browser" folder: the same pinned Python and Qt
# as the AppImage, but with QtWebEngine kept (the AppImage strips it), the
# commander_gui package, a launcher and plugin.json.
#
# Run ./build-appimage.sh first: this reuses its cached Python base, Qt
# wheels and the xcb helper libraries it bundled.
#
# Usage:  ./build-moddb-plugin.sh [output-dir]
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-$PROJECT_DIR}"
BUILD_DIR="${BUILD_DIR:-$PROJECT_DIR/.appimage-build}"
CACHE_DIR="$BUILD_DIR/cache"
PLUGIN_ROOT="$BUILD_DIR/plugin"
PLUGIN="$PLUGIN_ROOT/moddb-browser"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# The same Python and Qt pins as the AppImage, read from its own script so
# the two can never drift apart.
eval "$(sed -n '/^PY_SERIES=/,/^ARCH=/p; /^BASE_SHA256=/p' "$PROJECT_DIR/build-appimage.sh")"
BASE_APPIMAGE="python${PY_FULL}-${PY_ABI}-${PY_ABI}-${PY_PLATFORM}.AppImage"
[ -n "${QT_REQUIREMENTS:-}" ] && [ -n "${BASE_SHA256:-}" ] || die "couldn't read the pins from build-appimage.sh"

if [ "${ALLOW_DIRTY:-0}" != 1 ] && git -C "$PROJECT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  dirty="$(git -C "$PROJECT_DIR" status --porcelain -- commander_gui)"
  if [ -n "$dirty" ]; then
    printf '%s\n' "$dirty" | head -n 20 >&2
    die "uncommitted changes in commander_gui - commit them, or set ALLOW_DIRTY=1 for a test build"
  fi
fi

BASE_FILE="$CACHE_DIR/$BASE_APPIMAGE"
[ -s "$BASE_FILE" ] && [ "$(sha256sum "$BASE_FILE" | cut -d' ' -f1)" = "$BASE_SHA256" ] \
  || die "cached Python base missing or changed - run ./build-appimage.sh first"
APPIMAGE_BASE="$(find "$CACHE_DIR" -maxdepth 1 -type d -name 'base-*' | head -n 1)"
[ -n "$APPIMAGE_BASE" ] || die "no cached AppImage base - run ./build-appimage.sh first"

log "extracting the Python base"
rm -rf "$PLUGIN_ROOT"
mkdir -p "$PLUGIN_ROOT" "$OUT_DIR"
( cd "$PLUGIN_ROOT" && "$BASE_FILE" --appimage-extract >/dev/null && mv squashfs-root moddb-browser )

PY="$PLUGIN/opt/python$PY_SERIES/bin/python$PY_SERIES"
PYLIB="$PLUGIN/opt/python$PY_SERIES/lib/python$PY_SERIES"
SITE="$PYLIB/site-packages"
PS="$SITE/PySide6"
[ -x "$PY" ] || die "bundled interpreter not found at $PY"

log "installing the pinned Qt wheels (QtWebEngine included)"
QT_REQ_FILE="$PLUGIN_ROOT/qt-requirements.txt"
printf '%s\n' "$QT_REQUIREMENTS" > "$QT_REQ_FILE"
"$PY" -m pip install --no-cache-dir -q --no-index --find-links "$CACHE_DIR/wheels" \
  --require-hashes -r "$QT_REQ_FILE" || die "Qt wheels missing from the cache - run ./build-appimage.sh first"
rm -f "$QT_REQ_FILE"

# Tools and headers first; the Qt libraries are pruned further down, by what
# QtWebEngine and the browser actually link.
log "pruning developer tools and unused stdlib"
for tool in qmlls qmlformat assistant linguist lupdate lrelease designer uic rcc \
            qmllint qmlimportscanner qmlcachegen qmltyperegistrar qsb balsam \
            qmlprofiler qmlscene qmltestrunner svgtoqml qmldom \
            shiboken6 shiboken6-genpyi deploy_lib project_lib metaobjectdump.py; do
  rm -rf "${PS:?}/$tool"
done
find "$PS" -maxdepth 1 -name '*.pyi' -delete
rm -rf "$PS/Qt/metatypes" "$PS/Qt/modules" "$PS/Qt/typesystems" "$PS/Qt/qml"
rm -rf "$PLUGIN/opt/python$PY_SERIES/include" "$PYLIB/test" "$PYLIB/idlelib" \
       "$PYLIB/tkinter" "$PYLIB/lib2to3" "$PYLIB/ensurepip" "$PYLIB/turtledemo" \
       "$SITE/pip" "$SITE"/pip-*.dist-info "$PLUGIN/usr/share/tcltk"
rm -f "$PLUGIN"/AppRun "$PLUGIN"/*.desktop "$PLUGIN"/*.png "$PLUGIN/.DirIcon"

# The xcb/xkb helper libraries Qt needs, exactly as the AppImage bundles
# them (same glibc gate) - most distros lack libxcb-cursor.
for lib in "$APPIMAGE_BASE/opt/python$PY_SERIES/lib/python$PY_SERIES/site-packages/PySide6/Qt/lib"/lib{xcb,xkbcommon}*.so*; do
  [ -e "$lib" ] && cp -L "$lib" "$PS/Qt/lib/"
done

# The PySide6 wheels carry every Qt add-on (3D, Charts, Multimedia with its
# own FFmpeg, Designer...). Keep only the Python modules the browser imports,
# the plugin folders a desktop session can load, and whatever Qt library
# those link to (followed through readelf); the rest goes.
log "pruning Qt down to what the browser links"
KEEP_MODULES="QtCore QtGui QtWidgets QtNetwork QtPrintSupport QtWebChannel QtWebEngineCore QtWebEngineWidgets"
KEEP_PLUGINS="platforms platformthemes platforminputcontexts iconengines imageformats tls networkinformation
              xcbglintegrations egldeviceintegrations wayland-decoration-client
              wayland-graphics-integration-client wayland-shell-integration"
for module in "$PS"/Qt*.abi3.so; do
  name="$(basename "$module" .abi3.so)"
  case " $KEEP_MODULES " in *" $name "*) ;; *) rm -f "$module" ;; esac
done
for dir in "$PS/Qt/plugins"/*/; do
  case " $(echo $KEEP_PLUGINS) " in *" $(basename "$dir") "*) ;; *) rm -rf "$dir" ;; esac
done
"$PY" -s -P - "$PS" <<'PRUNE' || die "couldn't work out which Qt libraries are used"
import pathlib, subprocess, sys
ps = pathlib.Path(sys.argv[1])
lib = ps / "Qt" / "lib"
roots = [*ps.glob("*.so*"), *(ps / "Qt" / "plugins").rglob("*.so"), ps / "Qt" / "libexec" / "QtWebEngineProcess"]
keep, todo = set(), list(roots)
while todo:
    out = subprocess.run(["readelf", "-d", str(todo.pop())], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if "(NEEDED)" in line:
            name = line.split("[", 1)[1].rstrip("]")
            if (lib / name).is_file() and name not in keep:
                keep.add(name)
                todo.append(lib / name)
for path in lib.iterdir():
    if path.name not in keep:
        path.unlink()
PRUNE
# Translations: Qt's own aren't loaded; Chromium's only for COMMANDER's languages.
rm -f "$PS/Qt/translations"/*.qm
find "$PS/Qt/translations/qtwebengine_locales" -name '*.pak' \
  ! -regex '.*/\(en-US\|en-GB\|de\|es\|es-419\|fr\|pl\|pt-BR\|pt-PT\|ro\|ru\|tr\|uk\)\.pak' -delete
# Only the developer tools (never opened here) use this one.
rm -f "$PS/Qt/resources/qtwebengine_devtools_resources.pak"
rm -f "$PLUGIN"/usr/lib/lib{tcl,tk}8*.so

log "copying commander_gui"
APP="$PLUGIN/app"
mkdir -p "$APP"
cp -r "$PROJECT_DIR/commander_gui" "$APP/"
cp "$PROJECT_DIR/LICENSE" "$PLUGIN/"
cp -r "$PROJECT_DIR/licenses" "$PLUGIN/"
# The main app's artwork; the browser only shows its own icon.
find "$APP/commander_gui/assets" -mindepth 1 -maxdepth 1 ! -name moddb-browser.png -exec rm -rf {} +
find "$PLUGIN" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PLUGIN" -name '*.pyc' -delete 2>/dev/null || true

cat > "$PLUGIN/moddb-browser" <<LAUNCHER
#!/usr/bin/env bash
# Started by COMMANDER (Settings > Plugins); see commander_gui/plugins.py.
HERE="\$(dirname "\$(readlink -f "\$0")")"
export PYTHONNOUSERSITE=1
export PYTHONPATH="\$HERE/app"
if [ ! -f "\${SSL_CERT_FILE:-}" ] && [ -f "\$HERE/opt/_internal/certs.pem" ]; then
    export SSL_CERT_FILE="\$HERE/opt/_internal/certs.pem"
fi
exec "\$HERE/opt/python$PY_SERIES/bin/python$PY_SERIES" -s -P -m commander_gui.moddb_browser "\$@"
LAUNCHER
chmod +x "$PLUGIN/moddb-browser"

PROTOCOL="$(sed -n 's/^PROTOCOL = \([0-9]*\)$/\1/p' "$PROJECT_DIR/commander_gui/plugins.py")"
QT_VERSION="$(printf '%s\n' "$QT_REQUIREMENTS" | sed -n 's/^PySide6-Essentials==\([^ ]*\).*/\1/p')"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$PROJECT_DIR/commander_gui/__init__.py")"
[ -n "$PROTOCOL" ] && [ -n "$QT_VERSION" ] || die "couldn't read PROTOCOL or the Qt version"
printf '{"protocol": %s, "qt": "%s", "version": "%s", "exec": "moddb-browser"}\n' \
  "$PROTOCOL" "$QT_VERSION" "$VERSION" > "$PLUGIN/plugin.json"

# Imports only, no window: catches a pruned WebEngine piece before release.
env -u PYTHONPATH PYTHONPATH="$APP" PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen "$PY" -s -P -c \
  'import PySide6.QtWebEngineWidgets, PySide6.QtWebEngineCore
import commander_gui.moddb_browser' \
  || die "the plugin failed its import check"
[ -x "$PS/Qt/libexec/QtWebEngineProcess" ] || die "QtWebEngineProcess missing"

OUTPUT="$OUT_DIR/COMMANDER-ModDB-Browser-Plugin-linux-$ARCH.tar.xz"
log "packing $(basename "$OUTPUT")  (folder: $(du -sh "$PLUGIN" | cut -f1))"
rm -f "$OUTPUT"
tar -C "$PLUGIN_ROOT" --owner=0 --group=0 --numeric-owner --sort=name \
    -cf - moddb-browser | xz -T0 -9 > "$OUTPUT"
( cd "$(dirname "$OUTPUT")" && sha512sum "$(basename "$OUTPUT")" > "$(basename "$OUTPUT").sha512sum" )
log "done: $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
log "attach it and its .sha512sum to the release alongside the AppImage"
