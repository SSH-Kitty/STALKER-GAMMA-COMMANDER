"""Modded exes: install, keep across a GAMMA update, restore the original."""

import tempfile
import unittest
import zipfile
from pathlib import Path

from commander_gui import modded_exes as exes


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "commander-exes" not in p.parts
    }


def _release(tmp: Path, version: str) -> Path:
    path = tmp / f"exes-{version}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name in ("AnomalyDX11.exe", "AnomalyDX11AVX.exe", "AnomalyDX11.pdb"):
            zf.writestr(f"bin/{name}", version)
        zf.writestr(f"db/mods/{exes.GAMEDATA_DB}", version)
    return path


def _gamma_anomaly(root: Path) -> None:
    """What GAMMA's installer leaves: its exes, placeholder and loose gamedata."""
    _write(root / "bin/AnomalyDX11.exe", "gamma")
    _write(root / "bin/AnomalyDX11AVX.exe", "gamma")
    _write(root / "bin/xrCore.dll", "engine")
    _write(root / "db/mods/000_shader_placeholder.db0", "gamma")
    _write(root / "gamedata/configs/axr_options.ltx", "player")
    _write(root / "gamedata/configs/cache_dbg.ltx", "")
    _write(root / "gamedata/configs/unlocalizers/a.ltx", "gamma")
    _write(root / "gamedata/scripts/class_registrator_modded_exes.script", "gamma")
    _write(root / "gamedata/shaders/a.ps", "gamma")
    _write(root / "gamedata/levels/l01/level.ltx", "gamma")
    _write(root / "gamedata/textures/t.dds", "gamma")


class ModdedExesTest(unittest.TestCase):
    def test_install_cleans_up_and_restore_puts_everything_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            anomaly = tmp / "anomaly"
            _gamma_anomaly(anomaly)
            before = _tree(anomaly)

            exes.install(_release(tmp, "2026.9.12"), anomaly, "2026.9.12")

            self.assertEqual(
                _tree(anomaly),
                {
                    "bin/AnomalyDX11.exe": "2026.9.12",
                    "bin/AnomalyDX11AVX.exe": "2026.9.12",
                    "bin/AnomalyDX11.pdb": "2026.9.12",
                    "bin/xrCore.dll": "engine",
                    f"db/mods/{exes.GAMEDATA_DB}": "2026.9.12",
                    "gamedata/configs/axr_options.ltx": "player",
                    "gamedata/configs/cache_dbg.ltx": "",
                    "gamedata/levels/l01/level.ltx": "gamma",
                    "gamedata/textures/t.dds": "gamma",
                },
            )
            self.assertEqual(exes.load_state(anomaly)["previous"], None)

            exes.install(_release(tmp, "2026.10.11"), anomaly, "2026.10.11")
            state = exes.load_state(anomaly)
            self.assertEqual((state["version"], state["previous"]), ("2026.10.11", "2026.9.12"))

            exes.restore_original(anomaly)
            self.assertEqual(_tree(anomaly), before)
            self.assertFalse((anomaly / "commander-exes").exists())

    def test_gamma_update_with_kept_exes_is_cleaned_up_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            anomaly = tmp / "anomaly"
            _gamma_anomaly(anomaly)
            exes.install(_release(tmp, "2026.10.11"), anomaly, "2026.10.11")
            installed = _tree(anomaly)
            # A GAMMA update copies its loose gamedata and placeholder back.
            _write(anomaly / "gamedata/scripts/class_registrator_modded_exes.script", "gamma")
            _write(anomaly / "db/mods/000_shader_placeholder.db0", "gamma")

            exes.after_gamma_update(anomaly, kept_exes=True)
            self.assertEqual(_tree(anomaly), installed)

            exes.after_gamma_update(anomaly, kept_exes=False)
            self.assertIsNone(exes.load_state(anomaly))
            self.assertFalse((anomaly / "db/mods" / exes.GAMEDATA_DB).exists())

    def test_refuses_a_zip_that_is_not_modded_exes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            anomaly = tmp / "anomaly"
            _gamma_anomaly(anomaly)
            before = _tree(anomaly)
            bad = tmp / "bad.zip"
            with zipfile.ZipFile(bad, "w") as zf:
                zf.writestr("gamedata/scripts/x.script", "x")
            with self.assertRaises(exes.ExesError):
                exes.install(bad, anomaly, "file:bad.zip")
            self.assertEqual(_tree(anomaly), before)

    def test_refuses_while_coop_is_on_but_not_when_switched_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            anomaly = tmp / "anomaly"
            _gamma_anomaly(anomaly)
            _write(anomaly / "commander-coop/state.json", '{"active": true, "files": {}}')
            with self.assertRaises(exes.ExesError):
                exes.install(_release(tmp, "2026.10.11"), anomaly, "2026.10.11")
            _write(anomaly / "commander-coop/state.json", '{"active": false, "files": {}}')
            exes.install(_release(tmp, "2026.10.11"), anomaly, "2026.10.11")
            self.assertEqual(exes.load_state(anomaly)["version"], "2026.10.11")

    def test_coop_round_trip_keeps_the_new_exes(self):
        import hashlib
        import json

        from commander_gui import coop

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            anomaly = tmp / "anomaly"
            _gamma_anomaly(anomaly)
            # Co-op installed for plain Anomaly and switched off: its engine
            # waits under commander-coop/files.
            _write(anomaly / "commander-coop/files/bin/AnomalyDX11AVX.exe", "xrrazom")
            digest = hashlib.sha256(b"xrrazom").hexdigest()
            _write(anomaly / "commander-coop/state.json", json.dumps({
                "active": False, "kind": coop.KIND_ANOMALY,
                "files": {"bin/AnomalyDX11AVX.exe": digest}, "originals": [],
            }))

            exes.install(_release(tmp, "2026.10.11"), anomaly, "2026.10.11")
            coop.switch_to_coop(anomaly, "", "")
            self.assertEqual((anomaly / "bin/AnomalyDX11AVX.exe").read_text(), "xrrazom")
            coop.switch_to_singleplayer(anomaly, "", "")

            self.assertEqual((anomaly / "bin/AnomalyDX11AVX.exe").read_text(), "2026.10.11")
            self.assertEqual((anomaly / "bin/AnomalyDX11.exe").read_text(), "2026.10.11")


if __name__ == "__main__":
    unittest.main()
