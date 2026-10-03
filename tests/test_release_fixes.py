"""Regression tests for the 1.3.1 pre-release bug hunt.

One small test per behavior fix; each fails on the code before the fix.
"""

import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from commander_gui import config, gui_settings, main
from commander_gui.fomod import FomodFile, _place_item
from commander_gui.game_backup import create_backup
from commander_gui.modlist import seed_new_mo2_profile
from commander_gui.profile_bundle import ImportedProfileBundle
from commander_gui.settings import CliProfile, cli_ok
from commander_gui.ui.utilities_page import _rewrite_mo2_ini_paths

LD_NOISE = (
    "ERROR: ld.so: object '/home/deck/.local/share/Steam/ubuntu12_32/"
    "gameoverlayrenderer.so' from LD_PRELOAD cannot be preloaded "
    "(wrong ELF class: ELFCLASS32): ignored."
)


# --- interrupted Move Game must never delete the only copy -----------------


def _move_marker(dest: Path, sources: list[Path] | None) -> dict:
    saved = {"move_dest": str(dest), "move_expected": ["anomaly", "gamma"]}
    if sources is not None:
        saved["move_sources"] = [str(s) for s in sources]
    return saved


def _make_dest(tmp_path: Path) -> Path:
    dest = tmp_path / "dest"
    for name in ("anomaly", "gamma"):
        (dest / name).mkdir(parents=True)
        (dest / name / "file").write_text("x")
    return dest


def test_interrupted_move_keeps_destination_when_originals_are_gone(tmp_path):
    dest = _make_dest(tmp_path)
    gone = [tmp_path / "src" / "anomaly", tmp_path / "src" / "gamma"]
    with (
        patch.object(main.QMessageBox, "question") as question,
        patch.object(main.QMessageBox, "information"),
    ):
        main._cleanup_interrupted_move(_move_marker(dest, gone))
    question.assert_not_called()
    assert (dest / "anomaly" / "file").exists()
    assert gui_settings.load_gui_settings()["move_dest"] == ""


def test_interrupted_move_from_old_marker_never_deletes(tmp_path):
    dest = _make_dest(tmp_path)
    with (
        patch.object(main.QMessageBox, "question") as question,
        patch.object(main.QMessageBox, "information"),
    ):
        main._cleanup_interrupted_move(_move_marker(dest, None))
    question.assert_not_called()
    assert (dest / "gamma").is_dir()


def test_interrupted_move_offers_cleanup_while_originals_exist(tmp_path):
    dest = _make_dest(tmp_path)
    sources = []
    for name in ("anomaly", "gamma"):
        src = tmp_path / "src" / name
        src.mkdir(parents=True)
        (src / "file").write_text("x")
        sources.append(src)
    with patch.object(
        main.QMessageBox, "question", return_value=main.QMessageBox.StandardButton.Yes
    ):
        main._cleanup_interrupted_move(_move_marker(dest, sources))
    assert not (dest / "anomaly").exists()
    assert (tmp_path / "src" / "anomaly" / "file").exists()


def test_ini_rewrite_accepts_new_path_containing_old_path(tmp_path):
    old = str(tmp_path / "Games" / "GAMMA")
    new = str(tmp_path / "Games" / "GAMMA-new" / "GAMMA")
    (tmp_path / "ModOrganizer.ini").write_text(f"gamePath={old}\n")
    ini = _rewrite_mo2_ini_paths(tmp_path, [(old, new)])
    assert ini.read_text() == f"gamePath={new}\n"


# --- Steam overlay loader noise is not a CLI failure -----------------------


def test_cli_ok_ignores_ld_preload_noise():
    assert cli_ok(0, "Install complete", LD_NOISE)
    assert not cli_ok(0, "Install complete", LD_NOISE + "\nError: real failure")


def test_query_environment_drops_overlay_preload():
    with patch.dict(os.environ, {"LD_PRELOAD": "/x/gameoverlayrenderer.so:/keep.so"}):
        assert config._query_environment()["LD_PRELOAD"] == "/keep.so"


# --- self-update relaunch must not inherit the old mount's cert path -------


def test_relaunch_drops_stale_ssl_cert_file(tmp_path):
    from commander_gui import self_update

    env = {
        "APPDIR": "/tmp/.mount_old",
        "APPIMAGE": "/apps/c.AppImage",
        "SSL_CERT_FILE": "/tmp/.mount_old/opt/_internal/certs.pem",
    }
    with (
        patch.dict(os.environ, env),
        patch.object(self_update.subprocess, "Popen") as popen,
    ):
        self_update.relaunch_commander(tmp_path / "new.AppImage")
    assert "SSL_CERT_FILE" not in popen.call_args.kwargs["env"]


# --- FOMOD <file> destination that renames the file ------------------------


def test_fomod_file_destination_can_rename(tmp_path):
    root, dest = tmp_path / "src", tmp_path / "out"
    (root / "opt").mkdir(parents=True)
    (root / "opt" / "foo_v2.ltx").write_text("v2")
    dest.mkdir()
    _place_item(
        FomodFile(source="opt/foo_v2.ltx", destination="gamedata\\configs\\foo.ltx"),
        root,
        dest,
    )
    assert (dest / "gamedata" / "configs" / "foo.ltx").read_text() == "v2"


def test_fomod_file_destination_folder_still_appends_name(tmp_path):
    root, dest = tmp_path / "src", tmp_path / "out"
    root.mkdir()
    (root / "foo.ltx").write_text("x")
    dest.mkdir()
    _place_item(FomodFile(source="foo.ltx", destination="gamedata\\configs\\"), root, dest)
    assert (dest / "gamedata" / "configs" / "foo.ltx").is_file()


# --- backups survive pre-1980 timestamps -----------------------------------


def test_backup_handles_pre_1980_mtime(tmp_path):
    anomaly = tmp_path / "anomaly"
    user_ltx = anomaly / "appdata" / "user.ltx"
    user_ltx.parent.mkdir(parents=True)
    user_ltx.write_text("bind jump kSPACE\n")
    os.utime(user_ltx, (0, 0))
    info = create_backup("p", anomaly, tmp_path / "gamma")
    assert info is not None
    with zipfile.ZipFile(info.path) as archive:
        assert "user.ltx" in archive.namelist()


# --- imported bundles can't point the MO2 profile outside profiles/ --------


@pytest.mark.parametrize("bad", ["../../.config/autostart", "a/b", "..", "-x", ""])
def test_bundle_rejects_unsafe_mo2_profile(bad):
    profile = CliProfile()
    original = profile.mo2_profile
    ImportedProfileBundle({"mo2_profile": bad}, None).apply_to(profile)
    assert profile.mo2_profile == original


def test_bundle_accepts_normal_mo2_profile():
    profile = CliProfile()
    ImportedProfileBundle({"mo2_profile": "My Run"}, None).apply_to(profile)
    assert profile.mo2_profile == "My Run"


def test_seed_profile_refuses_escaping_name(tmp_path):
    src = tmp_path / "profiles" / "G.A.M.M.A"
    src.mkdir(parents=True)
    (src / "modlist.txt").write_text("+A\n")
    assert not seed_new_mo2_profile(tmp_path, "../../escape")
    assert not (tmp_path.parent / "escape").exists()
    assert seed_new_mo2_profile(tmp_path, "Fresh")


# --- GAMMA Reset finish must not crash on the restore note -----------------


def test_install_page_never_writes_to_the_log_less_full_progress():
    """full_progress is ProgressArea(show_log=False), so .log is None: any
    .log call on it raised AttributeError after set_install_busy(True) and
    left the whole app locked as busy."""
    from commander_gui.ui import install_page

    source = Path(install_page.__file__).read_text()
    assert "ProgressArea(show_log=False)" in source
    assert "full_progress.log" not in source


# --- user categories don't leak between MO2 profiles -----------------------


def test_user_categories_are_tracked_per_mo2_profile():
    from unittest.mock import Mock

    from commander_gui.ui.mod_manager_page import ModManagerPage

    page = ModManagerPage.__new__(ModManagerPage)
    profile = Mock(profile_name="Main", mo2_profile="G.A.M.M.A")
    page.window = Mock()
    page.window.settings.active_profile = profile
    page.profile_combo = Mock()
    gui_settings.save_gui_settings(user_created_categories={"Main": ["Legacy"]})

    page.profile_combo.currentText.return_value = "G.A.M.M.A"
    assert page._tracked_user_categories() == ["Legacy"]
    page._add_tracked_user_category("Mine")

    page.profile_combo.currentText.return_value = "Other"
    assert page._tracked_user_categories() == []
    page.profile_combo.currentText.return_value = "G.A.M.M.A"
    assert page._tracked_user_categories() == ["Legacy", "Mine"]


# --- refused reinstall puts the mod back -----------------------------------


def test_refused_reinstall_restores_moved_aside_folder(tmp_path):
    from unittest.mock import Mock

    from commander_gui.ui.mod_manager_page import ModManagerPage

    page = ModManagerPage.__new__(ModManagerPage)
    page.window = Mock()
    page._install_active = False
    destination = tmp_path / "Mine"
    backup = tmp_path / ".Mine.reinstall-backup-x"
    backup.mkdir()
    (backup / "old.ltx").write_text("old")
    # _start_mod_install bailed out (MO2 started meanwhile): nothing active.
    with patch.object(page, "_start_mod_install"):
        page._on_reinstall_moved_aside(destination, backup, Path("/x.zip"), "Mine", "done")
    assert (destination / "old.ltx").read_text() == "old"
    assert page._reinstall_backup is None
    assert page._install_is_reinstall is False


# --- load-order restore keeps each duplicate line's own status -------------


def test_reorder_to_original_keeps_duplicate_status():
    from commander_gui.modlist import reorder_to_original

    original = ["+A", "+B", "+Dup"]
    lines = ["+Dup", "+B", "-Dup", "+A"]
    out = reorder_to_original(lines, original)
    assert out[0] == "+A" and out[1] == "+B"
    assert sorted(out[2:]) == ["+Dup", "-Dup"]


# --- update check doesn't nag about an older remote build ------------------


def test_update_available_compares_build_numbers():
    from commander_gui.updates import UpdateStatus

    assert UpdateStatus(installed="120", latest="121").update_available
    assert not UpdateStatus(installed="121", latest="120").update_available


# --- "Add to Steam" keeps the user's collections and play history ----------


def test_shortcut_update_preserves_user_owned_fields():
    from commander_gui.steam_shortcuts import _TYPE_MAP, add_or_update_shortcut

    first = add_or_update_shortcut([], "COMMANDER", "/a/c.AppImage", "/a")
    key, _t, fields = first[0]
    user_set = [
        (k, t, [("0", 1, "Favorites")]) if k == "tags" else (k, t, 7 if k == "LastPlayTime" else v)
        for k, t, v in fields
    ]
    again = add_or_update_shortcut(
        [(key, _TYPE_MAP, user_set)], "COMMANDER", "/a/c.AppImage", "/a", icon="/i.png"
    )
    values = {k: v for k, _t, v in again[0][2]}
    assert values["tags"] == [("0", 1, "Favorites")]
    assert values["LastPlayTime"] == 7
    assert values["icon"] == "/i.png"


# --- unreadable settings.json is never replaced ----------------------------


def test_unreadable_settings_are_not_reset(tmp_path):
    from commander_gui.settings import load_settings

    path = tmp_path / "settings.json"
    path.write_text('{"Profiles": [{"ProfileName": "Mine"}]}')
    with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
        settings = load_settings(path)
    assert settings.load_error
    assert not (tmp_path / "settings.json.corrupt").exists()
    with pytest.raises(OSError):
        settings.save(path)
    assert "Mine" in path.read_text()


# --- ASSISTANT: redaction, fatal blocks, usvfs noise, size caps ------------


def test_redaction_is_anchored_and_covers_wine_paths():
    from assistant import report

    with (
        patch.object(report.Path, "home", return_value=Path("/home/al")),
        patch("getpass.getuser", return_value="al"),
    ):
        text = report._redact(r"/home/alice/x /home/al/y Z:\home\al\z C:\users\AL\w")
    assert "/home/alice/x" in text
    assert "~/y" in text
    assert r"Z:\home\<user>\z" in text
    assert r"C:\users\<user>\w" in text


def test_xray_fatal_without_entries_does_not_hide_the_rest():
    from assistant.analyzers.xray import analyze_xray

    lines = ["FATAL ERROR", "", "some noise"] + ["filler"] * 20 + [
        "FATAL ERROR",
        "[error]Expression    : 0",
        "[error]Description   : later crash",
    ]
    titles = [f.title for f in analyze_xray("xray.log", "game", lines)]
    assert any("later crash" in t for t in titles)


def test_usvfs_problems_counted_in_one_finding():
    from assistant.analyzers.mo2 import analyze_usvfs

    findings = analyze_usvfs("usvfs.log", "mo2", ["hook error x"] * 500)
    problems = [f for f in findings if "hooking" in f.title]
    assert len(problems) == 1 and problems[0].count == 500


def test_assistant_reads_every_log_size_commander_dumps():
    from assistant import dump
    from commander_gui import log_dump

    assert dump.MAX_TEXT_BYTES >= log_dump.MAX_FILE_BYTES
