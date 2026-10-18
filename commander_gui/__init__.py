"""Stalker GAMMA GUI."""

#: The one version number for COMMANDER and its Deck Mode (which imports it
#: from here rather than keeping its own). Machine-readable: the AppImage
#: file name, the AUR pkgver and the update checker's comparison use it.
__version__ = "1.4.0-unstable2"
#: What the interface shows after "COMMANDER", desktop and Deck Mode alike:
#: "1.4.0" for a release, "1.4.0 Unstable 2" for "1.4.0-unstable2".
__version_label__ = __version__.replace("-unstable", " Unstable ").strip()
