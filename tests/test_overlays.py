import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commander_gui import gui_settings, launch_options, overlays


class OverlayTests(unittest.TestCase):
    """MangoHud and vkBasalt: config from Settings > Graphics, the bundled
    layers where none is installed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for name, value in (("XDG_CONFIG_HOME", self.tmp / "config"), ("XDG_DATA_HOME", self.tmp / "data"), ("XDG_DATA_DIRS", "")):
            stub = patch.dict(os.environ, {name: str(value)})
            stub.start()
            self.addCleanup(stub.stop)
        overlays._install_bundled.cache_clear()
        self.addCleanup(overlays._install_bundled.cache_clear)
        launch_options._install_frame_gen.cache_clear()
        self.addCleanup(launch_options._install_frame_gen.cache_clear)

    def test_bundled_layers_replace_missing_ones(self):
        gui_settings.save_gui_settings(mangohud_preset="fps", vkbasalt_preset="bleak", vkbasalt_sharpness="off", vkbasalt_aa="smaa")
        with patch("shutil.which", return_value=None):
            env = {}
            command = launch_options.apply(["game"], env, "mangohud ENABLE_VKBASALT=1 %command%")
        self.assertEqual(command, ["game"])
        self.assertEqual(env["COMMANDER_MANGOHUD"], "1")
        self.assertEqual(env["COMMANDER_VKBASALT"], "1")
        self.assertNotIn("ENABLE_VKBASALT", env)
        # Where the layers say on or off, for the pop-up; the last game's state gone.
        for name in ("mangohud", "vkbasalt"):
            state = Path(env[f"COMMANDER_{name.upper()}_STATE"])
            self.assertEqual(state, overlays.state_file(name))
            self.assertFalse(state.exists())
        config = Path(env["MANGOHUD_CONFIGFILE"]).read_text()
        self.assertIn("legacy_layout=0\n", config)
        self.assertIn("\nfps\n", config)
        self.assertNotIn("cpu_stats", config)
        vkbasalt = Path(env["VKBASALT_CONFIG_FILE"]).read_text()
        self.assertIn("effects = smaa:lut\n", vkbasalt)
        self.assertTrue(Path(vkbasalt.split('lutFile = "')[1].split('"')[0]).is_file())
        manifest = json.loads((self.tmp / "data/vulkan/implicit_layer.d/commander-mangohud.json").read_text())
        self.assertEqual(manifest["layer"]["enable_environment"], {"COMMANDER_MANGOHUD": "1"})
        self.assertIn("disable_environment", manifest["layer"])  # the loader skips an implicit layer without one
        self.assertTrue(Path(manifest["layer"]["library_path"]).is_file())

    def test_vkbasalt_and_mangohud_go_under_frame_gen(self):
        """Implicit, vkBasalt would sit between the game and frame generation
        and MangoHud count only the game's frames. With frame generation they
        load as explicit layers, after it in VK_INSTANCE_LAYERS; vkBasalt
        above MangoHud, so it never colours the HUD."""
        env = {"ENABLE_VKBASALT": "1", "MAKO_CONFIG": "x", "VK_INSTANCE_LAYERS": "VK_LAYER_mine"}
        with patch.object(overlays, "mangohud_installed", return_value=True):
            prefix = overlays.resolve(env, [overlays.MANGOHUD])
        self.assertEqual(prefix, [])
        self.assertEqual(env["VK_INSTANCE_LAYERS"],
                         "VK_LAYER_mine:VK_LAYER_COMMANDER_vkbasalt_explicit:VK_LAYER_COMMANDER_mangohud_explicit")
        for name in ("COMMANDER_VKBASALT", "ENABLE_VKBASALT", "COMMANDER_MANGOHUD"):
            self.assertNotIn(name, env)
        for name in ("vkbasalt", "mangohud"):
            manifest = json.loads((self.tmp / f"data/vulkan/explicit_layer.d/commander-{name}.json").read_text())
            self.assertEqual(manifest["layer"]["name"], f"VK_LAYER_COMMANDER_{name}_explicit")
            self.assertNotIn("enable_environment", manifest["layer"])
            self.assertTrue(Path(manifest["layer"]["library_path"]).is_file())

    def test_frame_gen_goes_first_with_vkbasalt_and_mangohud_under_it(self):
        """Frame generation as an explicit layer, nearest the game (implicit, Mesa's
        device_select crashes it); vkBasalt and MangoHud under it, so they
        see the generated frames. Loaded with frame generation off too, to be
        switched on live; multipliers stop at 5x."""
        dll = self.tmp / "Lossless.dll"
        dll.write_bytes(b"MZ")
        for text, enabled in (("LSFGVK_ENV=1 LSFGVK_MULTIPLIER=8 ENABLE_VKBASALT=1 mangohud %command%", "true"),
                              ("ENABLE_VKBASALT=1 mangohud %command%", "false")):
            env = {}
            with patch.object(launch_options, "lossless_dll", return_value=dll), \
                    patch.object(launch_options, "frame_gen_ready", return_value=True), \
                    patch("commander_gui.config.settings_dir", return_value=self.tmp), \
                    patch.object(overlays, "mangohud_installed", return_value=True):
                command = launch_options.apply(["game"], env, text)
            self.assertEqual(command, ["game"])
            self.assertEqual(env["VK_INSTANCE_LAYERS"], ":".join((
                launch_options.FRAME_GEN_LAYER, "VK_LAYER_COMMANDER_vkbasalt_explicit", "VK_LAYER_COMMANDER_mangohud_explicit")))
            for name in ("DISABLE_LSFG", "DISABLE_LSFGVK", "DISABLE_GAMESCOPE_WSI"):
                self.assertEqual(env[name], "1")
            config = Path(env["MAKO_CONFIG"]).read_text()
            self.assertIn(f"frame_generation_enabled = {enabled}\n", config)
            self.assertIn(f'dll = "{dll}"', config)
        self.assertIn("multiplier = 2\n", config)
        manifest = json.loads((self.tmp / "data/vulkan/explicit_layer.d/commander-mako.json").read_text())
        self.assertEqual(manifest["layer"]["name"], launch_options.FRAME_GEN_LAYER)
        self.assertNotIn("enable_environment", manifest["layer"])
        self.assertTrue(Path(manifest["layer"]["library_path"]).is_file())
        with patch("commander_gui.config.settings_dir", return_value=self.tmp):
            self.assertIn("multiplier = 5\n", launch_options._frame_gen_config(dll, "LSFGVK_ENV=1 LSFGVK_MULTIPLIER=8").read_text())

    def test_coop_shortcut_keeps_vkbasalt_and_mangohud_under_frame_gen(self):
        """Co-op runs through Steam's shortcut, its launch options merged
        from COMMANDER's base: the base, set up again, used to replace the
        layer list with frame generation alone (twice over), so vkBasalt
        and MangoHud never loaded."""
        dll = self.tmp / "Lossless.dll"
        dll.write_bytes(b"MZ")
        text = "LSFGVK_ENV=1 ENABLE_VKBASALT=1 mangohud %command%"
        with patch.object(launch_options, "lossless_dll", return_value=dll), \
                patch.object(launch_options, "frame_gen_ready", return_value=True), \
                patch("commander_gui.config.settings_dir", return_value=self.tmp), \
                patch.object(overlays, "mangohud_installed", return_value=True):
            base = launch_options.for_steam("", "STEAM_COMPAT_X=1 %command% -p GAMMA")
            steam = launch_options.for_steam(text, base)
            # Synced again: the shortcut's own text is the next base.
            again = launch_options.for_steam(text, steam)
            self.assertTrue(launch_options.extends(steam, base))
        for options in (steam, again):
            env: dict[str, str] = {}
            launch_options.apply(["%command%"], env, options, resolve=False)
            self.assertEqual(env["VK_INSTANCE_LAYERS"], ":".join((
                launch_options.FRAME_GEN_LAYER, "VK_LAYER_COMMANDER_vkbasalt_explicit", "VK_LAYER_COMMANDER_mangohud_explicit")))
            self.assertEqual(env["STEAM_COMPAT_X"], "1")

    def test_settings_changes_rewrite_the_live_config(self):
        gui_settings.save_gui_settings(mangohud_preset="custom", mangohud_items=["fps", "time"],
                                       mangohud_layout="bar", mangohud_background_transparency=100)
        config = overlays.write_mangohud_config().read_text()
        # Ordered as shown, with background_alpha the opposite of transparency.
        self.assertLess(config.index("\ntime\n"), config.index("\nfps\n"))
        self.assertIn("background_alpha=0.0\n", config)
        self.assertIn("\nhorizontal\n", config)
        # MangoHud shows the ms next to FPS unless told not to.
        self.assertIn("\nframetime=0\n", config)
        gui_settings.save_gui_settings(mangohud_items=["fps", "frametime"])
        self.assertNotIn("frametime=0", overlays.write_mangohud_config().read_text())
        self.assertNotIn("fps_limit", overlays.write_mangohud_config().read_text())
        gui_settings.save_gui_settings(mangohud_items=["fps", "lows"], mangohud_start_hidden=True,
                                       mangohud_colours=True)
        config = overlays.write_mangohud_config().read_text()
        for line in ("fps_metrics=0.01,0.001", "no_display", "fps_color_change", "core_load_change"):
            self.assertIn(f"\n{line}\n", config)
        gui_settings.save_gui_settings(mangohud_start_hidden="yes", mangohud_colours=0)  # hand-typed junk
        config = overlays.write_mangohud_config().read_text()
        self.assertNotIn("no_display", config)
        self.assertNotIn("color_change", config)
        # Full leaves out the core list and the NVIDIA-lagging throttling check.
        gui_settings.save_gui_settings(mangohud_preset="full")
        config = overlays.write_mangohud_config().read_text()
        self.assertIn("\nio_read\n", config)
        self.assertNotIn("core_load\n", config)
        self.assertNotIn("throttling_status", config)
        gui_settings.save_gui_settings(mangohud_preset="custom", mangohud_fps_limit=144)
        self.assertIn("\nfps_limit=144\n", overlays.write_mangohud_config().read_text())
        gui_settings.save_gui_settings(mangohud_fps_limit=143)  # hand-typed junk
        self.assertNotIn("fps_limit", overlays.write_mangohud_config().read_text())

    def test_a_bar_stays_on_the_left(self):
        # MangoHud draws a bar across the whole screen, so right can't work.
        gui_settings.save_gui_settings(mangohud_layout="bar", mangohud_position="bottom-right")
        self.assertIn("position=bottom-left\n", overlays.write_mangohud_config().read_text())
        gui_settings.save_gui_settings(mangohud_layout="box")
        self.assertIn("position=bottom-right\n", overlays.write_mangohud_config().read_text())

    def test_a_right_corner_switches_the_bar_to_a_box(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.settings import CliSettings
        from commander_gui.ui.settings_page import SettingsPage

        QApplication.instance() or QApplication([])

        class FakeWindow:
            settings = CliSettings()

            def switch_language(self):
                pass

            def apply_theme(self):
                pass

        gui_settings.save_gui_settings(mangohud_layout="bar", mangohud_position="top-left")
        with patch("commander_gui.ui.settings_page.probe_discord", return_value=False):
            page = SettingsPage(FakeWindow())
        page._render_graphics()
        page._graphics._mangohud_position.setCurrentIndex(page._graphics._mangohud_position.findData("top-right"))
        self.assertEqual(page._graphics._mangohud_layout.currentData(), "box")
        saved = overlays.settings()
        self.assertEqual((saved["mangohud_layout"], saved["mangohud_position"]), ("box", "top-right"))
        # And back: a bar moves the HUD to the left corner.
        page._graphics._mangohud_layout.setCurrentIndex(page._graphics._mangohud_layout.findData("bar"))
        self.assertEqual(page._graphics._mangohud_position.currentData(), "top-left")

    def test_bundled_mangohud_over_an_installed_one(self):
        # Only the bundled one is patched to reload its config safely: stock
        # MangoHud 0.8.4 crashed the game when Full was picked mid-game.
        env = {}
        with patch.object(overlays, "mangohud_installed", return_value=True):
            prefix = overlays.resolve(env, [overlays.MANGOHUD, "game"])
        self.assertEqual(prefix, ["game"])
        self.assertEqual(env["COMMANDER_MANGOHUD"], "1")

    def test_graphics_overlay_window_and_key(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui import graphics_window

        QApplication.instance() or QApplication([])
        self.assertEqual(graphics_window.key_setting(), "shift+grave")
        gui_settings.save_gui_settings(graphics_overlay_key="off")
        graphics_window.start_key()
        self.assertIsNone(graphics_window._grab)  # Off grabs nothing
        window = graphics_window.GraphicsWindow()
        window.toggle()
        self.assertTrue(window.isVisible())
        window.toggle()
        self.assertFalse(window.isVisible())
        # MangoHud, vkBasalt and Frame generation tabs; the last passes the
        # launch options text it changes back to COMMANDER.
        self.assertEqual(len(window.tabs.buttons()), 3)
        # A new click outside (in the game) closes it; one inside, or a
        # button held from a drag that started inside, doesn't.
        from PySide6.QtCore import QPointF

        window.toggle()
        inside = QPointF(window.frameGeometry().center())
        outside = QPointF(window.frameGeometry().right() + 50, 10)
        window.on_pointer(inside, False)
        window.on_pointer(inside, True)
        window.on_pointer(outside, True)
        self.assertTrue(window.isVisible())
        window.on_pointer(outside, False)
        window.on_pointer(outside, True)
        self.assertFalse(window.isVisible())
        # Switched to fullscreen in the game: it minimized when the panel
        # took focus, so it's brought back and the panel stops taking focus.
        from unittest import mock

        from PySide6.QtCore import Qt
        from PySide6.QtGui import QWindow

        window.toggle()
        window._game = QWindow()
        with mock.patch.object(graphics_window, "_minimized", return_value=True), \
                mock.patch.object(graphics_window, "_activate") as activate, \
                mock.patch.object(graphics_window.QTimer, "singleShot"):
            window._check_minimized(0)
        activate.assert_called_once()
        self.assertFalse(window.isVisible())
        self.assertTrue(window._fullscreen)
        window.toggle(focus=True)
        self.assertTrue(window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        window.toggle()
        sent = []
        window.options_changed.connect(sent.append)
        window.set_options("LSFGVK_ENV=1 %command%")
        window.frame_gen._set("LSFGVK_MULTIPLIER", "3")
        self.assertIn("LSFGVK_MULTIPLIER=3", sent[-1])
        # The key hint names the chosen key and leaves by itself.
        gui_settings.save_gui_settings(graphics_overlay_key="kp_multiply")
        hint = graphics_window._KeyHint()
        self.assertIn("Numpad *", hint.label.text())
        hint.show_hint()
        self.assertTrue(hint.isVisible())
        self.assertEqual(hint._timer.interval(), 10_000)
        self.assertTrue(hint._timer.isActive())

    def test_graphics_overlay_widens_for_a_big_font(self):
        from PySide6.QtWidgets import QApplication

        from commander_gui.themes import build_stylesheet
        from commander_gui.ui import graphics_window

        app = QApplication.instance() or QApplication([])
        app.setStyleSheet(build_stylesheet("gamma", font_size=18, font_family="DejaVu Sans"))
        try:
            window = graphics_window.GraphicsWindow()
            window.toggle()
            app.processEvents()
            # The Performance overlay card shows whole: no sideways scrolling.
            self.assertEqual(window.pages.widget(0).horizontalScrollBar().maximum(), 0)
            window.hide()
        finally:
            app.setStyleSheet("")


    def test_overlay_takes_focus_unless_the_game_is_fullscreen(self):
        from commander_gui.ui import graphics_window

        ltx = self.tmp / "anomaly" / "appdata" / "user.ltx"
        ltx.parent.mkdir(parents=True)
        for mode, fullscreen in (("fullscreen", True), ("borderless", False), ("windowed", False)):
            ltx.write_text(f"rs_render_static off\nrs_screenmode {mode}\n")
            self.assertEqual(graphics_window.game_fullscreen(self.tmp / "anomaly"), fullscreen, mode)
        self.assertFalse(graphics_window.game_fullscreen(self.tmp / "missing"))
        # Before launch, fullscreen becomes borderless; the rest stays.
        ltx.write_text("rs_screenmode fullscreen\nvid_mode 2560x1440\n")
        graphics_window.use_borderless(self.tmp / "anomaly")
        self.assertEqual(ltx.read_text(), "rs_screenmode borderless\nvid_mode 2560x1440\n")

    def test_dll_label_shows_folder_and_version(self):
        dll = self.tmp / "Lossless.dll"
        dll.write_bytes(b"MZ" + "FileVersion".encode("utf-16-le") + b"\0\0" + "3.2.2.0".encode("utf-16-le") + b"\0\0")
        self.assertEqual(launch_options.dll_label(dll), f"{self.tmp.name}/Lossless.dll 3.2.2")
        dll.write_bytes(b"MZ no version here")
        self.assertEqual(launch_options.dll_label(dll), f"{self.tmp.name}/Lossless.dll")

    def test_typed_frame_gen_values_never_break_the_config(self):
        # Hand-typed launch options: junk falls back, numbers are clamped.
        with patch("commander_gui.config.settings_dir", return_value=self.tmp):
            path = launch_options._frame_gen_config(self.tmp / "x.dll", "LSFGVK_ENV=1 LSFGVK_MULTIPLIER=abc LSFGVK_FLOW_SCALE=nan")
            self.assertIn("multiplier = 2\nflow_scale = 1.0", path.read_text())
            path = launch_options._frame_gen_config(self.tmp / "x.dll", "LSFGVK_ENV=1 LSFGVK_MULTIPLIER=99 LSFGVK_FLOW_SCALE=0.01")
            self.assertIn("multiplier = 5\nflow_scale = 0.25", path.read_text())

    def test_beta_dll_gives_way_to_lossless_dll(self):
        """lsfg-vk.dll (the lsfg-vk beta branch's) can't run: a Lossless.dll
        next to it is used instead, else the card says how to get one."""
        from PySide6.QtWidgets import QApplication

        from commander_gui.ui import frame_gen_card

        QApplication.instance() or QApplication([])
        folder = self.tmp / "ls"
        folder.mkdir()
        for name in ("Lossless.dll", "lsfg-vk.dll"):
            (folder / name).write_bytes(b"MZ")
        gui_settings.save_gui_settings(lossless_dll=str(folder / "lsfg-vk.dll"))
        text = ["LSFGVK_ENV=1 %command%"]
        with patch("commander_gui.launcher._steam_library_paths", return_value=[]), \
                patch.object(launch_options, "native_dll", return_value=True), \
                patch.object(frame_gen_card, "game_has_frame_gen", return_value=None):
            self.assertEqual(launch_options.lossless_dll(), folder / "Lossless.dll")
            card = frame_gen_card.FrameGenCard(lambda: text[0], lambda value: text.__setitem__(0, value))
            self.assertTrue(card.sync())
            (folder / "Lossless.dll").unlink()
            self.assertFalse(card.sync())
            self.assertIn("Properties > Betas > None", card._missing.text())
            on, off = (button for button, _value in card._rows[0][0])
            self.assertTrue(off.isEnabled())  # left on: it can still go off
            self.assertFalse(any(button.isEnabled() for button, _value in card._rows[1][0]))
            # Off: the settings show no pick, so they don't look live.
            text[0] = launch_options.frame_gen(text[0], False)
            card.sync()
            self.assertFalse(any(button.isChecked() for button, _value in card._rows[1][0]))

    def test_mouse_grab_set_in_the_prefix(self):
        # Off, so the overlay gets clicks over a fullscreen game; only the
        # X11 Driver section's GrabPointer changes.
        from commander_gui import launcher

        pfx = self.tmp / "prefix" / "pfx"
        pfx.mkdir(parents=True)
        reg = pfx / "user.reg"
        reg.write_text('WINE REGISTRY Version 2\n\n[Software\\\\Wine\\\\X11 Driver] 1\n"KeyboardLayoutList"="x"\n\n[Other] 2\n"a"="b"\n')
        runner = launcher.Runner(kind="proton", label="t", env={"STEAM_COMPAT_DATA_PATH": str(self.tmp / "prefix")})
        launcher.set_mouse_grab(runner, False)
        self.assertIn('"KeyboardLayoutList"="x"\n"GrabPointer"="N"\n\n[Other] 2', reg.read_text())
        launcher.set_mouse_grab(runner, True)
        self.assertEqual(reg.read_text().count("GrabPointer"), 1)
        self.assertIn('"GrabPointer"="Y"', reg.read_text())
        reg.write_text("WINE REGISTRY Version 2\n")
        launcher.set_mouse_grab(runner, False)
        self.assertIn('[Software\\\\Wine\\\\X11 Driver]', reg.read_text())
        self.assertIn('"GrabPointer"="N"', reg.read_text())

    def test_installed_ones_are_used_and_own_config_kept(self):
        gui_settings.save_gui_settings(mangohud_preset="own")
        layer = self.tmp / "data/vulkan/implicit_layer.d/vkBasalt.json"
        layer.parent.mkdir(parents=True)
        layer.write_text("{}")
        # vkBasalt: the installed one only without a bundled one.
        with patch("shutil.which", return_value="/usr/bin/mangohud"), patch.object(overlays, "_install_bundled", return_value=False):
            env = {}
            command = launch_options.apply(["game"], env, "mangohud ENABLE_VKBASALT=1 %command%")
        self.assertEqual(command, ["mangohud", "game"])
        self.assertNotIn("MANGOHUD_CONFIGFILE", env)
        self.assertEqual(env["ENABLE_VKBASALT"], "1")
        self.assertNotIn("COMMANDER_VKBASALT", env)

    def test_vkbasalt_and_native_wayland_turn_each_other_off(self):
        text = launch_options.toggle("PROTON_ENABLE_WAYLAND=1 %command%", overlays.VKBASALT, True)
        self.assertEqual(text, "ENABLE_VKBASALT=1 %command%")
        text = launch_options.toggle(text, launch_options.NATIVE_WAYLAND, True)
        self.assertEqual(text, "PROTON_ENABLE_WAYLAND=1 %command%")

    def test_debug_mode_goes_after_command(self):
        text = launch_options.toggle("PROTON_LOG=1", "-dbg", True)
        self.assertEqual(text, "PROTON_LOG=1 %command% -dbg")
        text = launch_options.toggle(text, "mangohud", True)
        command = launch_options.apply(["mo2", "run", "-e", "Anomaly (DX11)"], {}, text, resolve=False, game_mode=False)
        self.assertEqual(command, ["mangohud", "mo2", "run", "--arguments=-dbg", "-e", "Anomaly (DX11)"])
        self.assertEqual(launch_options.toggle(text, "-dbg", False), "mangohud PROTON_LOG=1 %command%")

    def test_a_look_sets_the_sliders(self):
        gui_settings.save_gui_settings(vkbasalt_preset="warm", vkbasalt_warmth=-40)
        self.assertEqual(overlays.settings()["vkbasalt_warmth"], overlays.VKBASALT_LOOKS["warm"][3])
        self.assertEqual(overlays.settings()["vkbasalt_custom"][3], -40)  # kept for Custom look
        gui_settings.save_gui_settings(vkbasalt_preset="custom")
        self.assertEqual(overlays.settings()["vkbasalt_warmth"], -40)

    def test_luts_stay_in_range(self):
        extremes = [(50, 50, -50, 50, -50), (-50, -50, 50, -50, 50), *overlays.VKBASALT_LOOKS.values()]
        for values in extremes:
            data = dict(zip((f"vkbasalt_{name}" for name in overlays.VKBASALT_COLOURS), values))
            rows = overlays.lut(data, 5).splitlines()
            self.assertEqual(rows[0], "LUT_3D_SIZE 5")
            self.assertEqual(len(rows), 1 + 5 ** 3)
            self.assertTrue(all(0 <= float(v) <= 1 for row in rows[1:] for v in row.split()))


    def test_looks_stay_gentle(self):
        # GAMMA is dark already: no look crushes the shadows or tints grey hard.
        for name, values in overlays.VKBASALT_LOOKS.items():
            data = dict(zip((f"vkbasalt_{key}" for key in overlays.VKBASALT_COLOURS), values))
            for grey in (0.05, 0.2, 0.5, 0.8):
                r, g, b = overlays._adjust(data, grey, grey, grey)
                luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
                with self.subTest(look=name, grey=grey):
                    self.assertLess(abs(luma / grey - 1), 0.25)
                    self.assertLess((max(r, g, b) - min(r, g, b)) / grey, 0.16)

    def test_umbriel_rule_and_socket_move(self):
        import socket
        import threading
        import tomllib

        from commander_gui.ui import graphics_window

        rule = tomllib.loads(graphics_window.UMBRIEL_RULE)["window_rule"][0]
        self.assertRegex(graphics_window.TITLE, rule["match"]["title"])
        self.assertEqual(rule["default_floating_size_px"]["width"], graphics_window._WIDTH)
        # Umbriel's socket: the windows list, then the move for the panel only.
        path = str(self.tmp / "umbriel.sock")
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.listen()
        self.addCleanup(server.close)
        got = []
        replies = [{"ok": [{"id": 3, "title": "Firefox"}, {"id": 7, "title": graphics_window.TITLE}]}, {"ok": None}]

        def serve():
            for reply in replies:
                conn, _ = server.accept()
                with conn:
                    got.append(json.loads(conn.makefile("rb").readline()))
                    conn.sendall(json.dumps(reply).encode() + b"\n")

        thread = threading.Thread(target=serve)
        thread.start()
        with patch.dict(os.environ, {"UMBRIEL_SOCKET": path}):
            self.assertTrue(graphics_window.on_umbriel())
            graphics_window._umbriel_dock()
        thread.join(2)
        self.assertEqual(got, [{"cmd": "windows"}, {"cmd": "msg", "arg": "window-move-to:top_left/0/0/7"}])
        # No Umbriel running: nothing raised.
        with patch.dict(os.environ, {"UMBRIEL_SOCKET": str(self.tmp / "gone.sock")}):
            graphics_window._umbriel_dock()

if __name__ == "__main__":
    unittest.main()
