"""COMMANDER must never run a Wine that did not build the prefix it touches.

Pinned by a real incident. The dependency status probe ran system
``winetricks list-installed`` with ``WINEPREFIX`` set to the Proton prefix;
winetricks runs ``wine cmd /c "echo init"`` before any command, system wine
performed its implicit prefix update, and 97 of Proton's DLLs in system32 -
``ntdll.dll`` included - were overwritten with another build's. Every Proton
process then faulted on its first thread, each fault started ``winedbg``,
which faulted, and the machine froze from memory exhaustion in about two
minutes. The probe ran on every Dashboard refresh, so nothing the user could
do would repair it. A full reinstall keeps the prefix and changed nothing.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from commander_gui.launcher import (
    RUNNER_CRASH_LOOP_MARKER,
    RUNNER_CRASH_LOOP_THRESHOLD,
    LaunchError,
    Runner,
    ensure_runner_prefix,
    prefix_foreign_dlls,
    read_log_tail,
    runner_crash_loop,
)
from commander_gui.winetricks import (
    WINETRICKS_VERBS,
    check_winetricks_full_status,
    check_winetricks_status,
    winetricks_install_command,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Bigger than the placeholder threshold, so the guard treats it as a real DLL.
_REAL_DLL = b"MZ" + b"\x90" * 20_000


def _fake_proton(root: Path, name: str = "GE-Proton11-6") -> Path:
    """A Proton build dir with the two ntdll copies the guard compares against.

    Payload bytes are derived from ``name``, so two different builds (as two
    calls with different names) never collide - a test comparing "this
    build's ntdll" against "a different build's ntdll" would otherwise pass
    for the wrong reason.
    """
    build = root / name
    tag = name.encode()
    for arch, payload in (("x86_64-windows", b"P64" + tag), ("i386-windows", b"P32" + tag)):
        lib = build / "files" / "lib" / "wine" / arch
        lib.mkdir(parents=True)
        (lib / "ntdll.dll").write_bytes(_REAL_DLL + payload)
        (lib / "msvcrt.dll").write_bytes(_REAL_DLL + b"MSVCRT" + payload)
    (build / "files" / "bin").mkdir(parents=True)
    (build / "files" / "bin" / "wine").write_text("#!/bin/sh\n")
    (build / "files" / "bin" / "wineserver").write_text("#!/bin/sh\n")
    (build / "toolmanifest.vdf").write_text("")
    return build


def _umu_runner(build: Path, prefix: Path) -> Runner:
    return Runner(
        "umu", build.name, ["/usr/bin/umu-run"],
        {"PROTONPATH": str(build), "WINEPREFIX": str(prefix)},
    )


# =====================================================================
# The root cause: status is a file read, never a Wine process
# =====================================================================
class WinetricksStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_reads_the_log_and_spawns_nothing(self):
        """The regression test for the incident itself."""
        (self.tmp / "winetricks.log").write_text("d3dx9\nvcrun2022\nquartz\n")
        with patch.object(subprocess, "run", side_effect=AssertionError("a process was started")):
            with patch.object(subprocess, "Popen", side_effect=AssertionError("a process was started")):
                status = check_winetricks_status(str(self.tmp))
        self.assertEqual(
            {verb for verb, ok in status.items() if ok}, {"d3dx9", "vcrun2022", "quartz"}
        )
        self.assertEqual(set(status), set(WINETRICKS_VERBS))

    def test_missing_log_means_nothing_installed(self):
        with patch.object(subprocess, "run", side_effect=AssertionError):
            status = check_winetricks_status(str(self.tmp))
        self.assertFalse(any(status.values()))

    def test_missing_prefix_is_not_an_error(self):
        status = check_winetricks_status(str(self.tmp / "nope"))
        self.assertFalse(any(status.values()))
        self.assertFalse(any(check_winetricks_status("").values()))

    def test_full_status_spawns_nothing_either(self):
        with patch.object(subprocess, "run", side_effect=AssertionError):
            status = check_winetricks_full_status(str(self.tmp))
        for tool in ("wine", "protontricks", "umu"):
            self.assertIn(tool, status)


# =====================================================================
# Installing verbs goes through the runner's own Wine
# =====================================================================
class WinetricksInstallCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.build = _fake_proton(self.tmp)

    def test_umu_runner_uses_umu_winetricks_mode(self):
        runner = _umu_runner(self.build, self.tmp / "pfx")
        with patch("commander_gui.winetricks.umu_binary", return_value="/usr/bin/umu-run"):
            command, env = winetricks_install_command(runner)
        self.assertEqual(command[:2], ["/usr/bin/umu-run", "winetricks"])
        self.assertEqual(tuple(command[2:]), WINETRICKS_VERBS)
        self.assertEqual(env["PROTONPATH"], str(self.build))
        self.assertEqual(env["WINEPREFIX"], str(self.tmp / "pfx"))
        # Never the bare system script for a Proton prefix.
        self.assertNotIn("winetricks", Path(command[0]).name)

    def test_umu_runner_without_umu_declines(self):
        runner = _umu_runner(self.build, self.tmp / "pfx")
        with patch("commander_gui.winetricks.umu_binary", return_value=""):
            command, env = winetricks_install_command(runner)
        self.assertEqual((command, env), ([], {}))

    def test_steam_proton_runner_pins_wine_to_the_build(self):
        runner = Runner(
            "proton", "Steam Proton", [str(self.build / "proton"), "run"],
            {"STEAM_COMPAT_DATA_PATH": str(self.tmp / "compat")},
        )
        with patch("commander_gui.winetricks.winetricks_binary", return_value="/usr/bin/winetricks"):
            command, env = winetricks_install_command(runner)
        self.assertEqual(command[:2], ["/usr/bin/winetricks", "-q"])
        self.assertEqual(env["WINE"], str(self.build / "files" / "bin" / "wine"))
        self.assertEqual(env["WINESERVER"], str(self.build / "files" / "bin" / "wineserver"))
        self.assertEqual(env["WINEPREFIX"], str(self.tmp / "compat" / "pfx"))

    def test_plain_wine_runner_pins_wine_to_that_binary(self):
        runner = Runner("wine", "Wine", ["/opt/wine/bin/wine"], {"WINEPREFIX": "/p"})
        with patch("commander_gui.winetricks.winetricks_binary", return_value="/usr/bin/winetricks"):
            command, env = winetricks_install_command(runner)
        self.assertEqual(command[:2], ["/usr/bin/winetricks", "-q"])
        self.assertEqual(env["WINE"], "/opt/wine/bin/wine")
        self.assertEqual(env["WINEPREFIX"], "/p")


# =====================================================================
# The guard: refuse to launch into a corrupted prefix
# =====================================================================
class PrefixGuardTests(unittest.TestCase):
    """The launch guard: catches *system* Wine, never a different Proton.

    A real incident forced the distinction this class exists to pin. The
    first version of ``prefix_foreign_dlls`` compared the prefix's ntdll
    against the *selected runner's own* copy - so switching from one
    installed GE-Proton build to another, in the same prefix, made a
    perfectly healthy launch refuse to start: the prefix still carried the
    previous build's ntdll, which is exactly what Proton itself would sync
    back on the next launch, not a sign of corruption. The fix compares
    against Wine builds actually installed on the *host* instead - system
    wine, a Lutris runner, anything that is not some flavour of Proton -
    which is the only thing that can genuinely leave a prefix unable to
    start anything.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.build = _fake_proton(self.tmp)
        # A second, legitimate Proton build - the "switched runner" case.
        self.other_build = _fake_proton(self.tmp, name="GE-Proton11-5")
        self.prefix = self.tmp / "prefix"
        self.system32 = self.prefix / "drive_c" / "windows" / "system32"
        self.syswow64 = self.prefix / "drive_c" / "windows" / "syswow64"
        self.system32.mkdir(parents=True)
        self.syswow64.mkdir(parents=True)
        self.runner = _umu_runner(self.build, self.prefix)
        # A fake "system wine" the corruption tests below write bytes that
        # match, so "foreign" means what it means on a real machine: bytes
        # identical to a Wine actually installed on the host.
        self.host = self.tmp / "usr" / "lib" / "wine"
        for arch, payload in (("x86_64-windows", b"HOST64"), ("i386-windows", b"HOST32")):
            (self.host / arch).mkdir(parents=True)
            (self.host / arch / "ntdll.dll").write_bytes(_REAL_DLL + payload)
        self.host_patch = patch(
            "commander_gui.launcher.host_wine_lib_dirs", return_value=[self.host]
        )
        self.host_patch.start()
        self.addCleanup(self.host_patch.stop)

    def test_empty_prefix_is_fine(self):
        self.assertEqual(prefix_foreign_dlls(self.prefix, self.runner), [])

    def test_placeholder_is_fine(self):
        (self.system32 / "ntdll.dll").write_bytes(b"\x00" * 117)
        self.assertEqual(prefix_foreign_dlls(self.prefix, self.runner), [])

    def test_the_runners_own_copy_is_fine(self):
        lib = self.build / "files" / "lib" / "wine"
        (self.system32 / "ntdll.dll").write_bytes((lib / "x86_64-windows/ntdll.dll").read_bytes())
        (self.syswow64 / "ntdll.dll").write_bytes((lib / "i386-windows/ntdll.dll").read_bytes())
        self.assertEqual(prefix_foreign_dlls(self.prefix, self.runner), [])

    def test_a_different_installed_proton_build_is_never_flagged(self):
        """The regression test for the real incident.

        The prefix carries GE-Proton11-5's ntdll (a real, previously-used
        build) while the *selected* runner is GE-Proton11-6. That is a
        completely ordinary runner switch, not corruption, and must launch.
        """
        proton5_ntdll = self.other_build / "files/lib/wine/x86_64-windows/ntdll.dll"
        proton5_ntdll32 = self.other_build / "files/lib/wine/i386-windows/ntdll.dll"
        self.system32.joinpath("ntdll.dll").write_bytes(proton5_ntdll.read_bytes())
        self.syswow64.joinpath("ntdll.dll").write_bytes(proton5_ntdll32.read_bytes())
        self.assertEqual(prefix_foreign_dlls(self.prefix, self.runner), [])

    def test_a_foreign_ntdll_is_caught_on_both_sides(self):
        (self.system32 / "ntdll.dll").write_bytes(_REAL_DLL + b"HOST64")
        (self.syswow64 / "ntdll.dll").write_bytes(_REAL_DLL + b"HOST32")
        self.assertEqual(
            prefix_foreign_dlls(self.prefix, self.runner),
            ["system32/ntdll.dll", "syswow64/ntdll.dll"],
        )

    def test_plain_wine_runner_is_never_judged(self):
        (self.system32 / "ntdll.dll").write_bytes(_REAL_DLL + b"HOST64")
        runner = Runner("wine", "Wine", ["/usr/bin/wine"], {"WINEPREFIX": str(self.prefix)})
        self.assertEqual(prefix_foreign_dlls(self.prefix, runner), [])

    def test_ensure_runner_prefix_refuses_and_names_the_fix(self):
        (self.system32 / "ntdll.dll").write_bytes(_REAL_DLL + b"HOST64")
        with self.assertRaises(LaunchError) as caught:
            ensure_runner_prefix(self.runner)
        message = str(caught.exception)
        self.assertIn("ntdll.dll", message)
        self.assertIn("Repair Prefix", message)
        # And nothing was written: the guard runs before the marker file.
        self.assertFalse((self.prefix / ".commander-runner").exists())

    def test_ensure_runner_prefix_permits_a_runner_switch(self):
        """The same scenario as the incident, through the real entry point."""
        proton5_ntdll = self.other_build / "files/lib/wine/x86_64-windows/ntdll.dll"
        self.system32.joinpath("ntdll.dll").write_bytes(proton5_ntdll.read_bytes())
        ensure_runner_prefix(self.runner)  # does not raise

    def test_guard_can_be_bypassed_for_diagnosis_only(self):
        (self.system32 / "ntdll.dll").write_bytes(_REAL_DLL + b"HOST64")
        with patch.dict(os.environ, {"COMMANDER_SKIP_PREFIX_GUARD": "1"}):
            ensure_runner_prefix(self.runner)  # does not raise


# =====================================================================
# The breaker: recognise a crash loop from the log
# =====================================================================
class CrashLoopTests(unittest.TestCase):
    def test_threshold(self):
        line = f"wine: Unhandled page fault on read access to 0000000000000018 at address 00006FFFFEBBDA29 (thread 0058), {RUNNER_CRASH_LOOP_MARKER}\n"
        self.assertFalse(runner_crash_loop(line * (RUNNER_CRASH_LOOP_THRESHOLD - 1)))
        self.assertTrue(runner_crash_loop(line * RUNNER_CRASH_LOOP_THRESHOLD))

    def test_a_healthy_launch_is_not_a_loop(self):
        healthy = (
            "INFO: umu-launcher version 1.4.3\n"
            "Proton: /home/x/gamma/ModOrganizer.exe\n"
            "Proton: Executable is a unix path, launching with 'umu.exe'.\n"
            "ntsync: up and running.\n"
        ) * 50
        self.assertFalse(runner_crash_loop(healthy))

    def test_one_genuine_crash_is_not_a_loop(self):
        self.assertFalse(runner_crash_loop(f"something {RUNNER_CRASH_LOOP_MARKER}\n"))

    def test_tail_read_is_bounded(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"x" * (5 * 1024 * 1024) + b"THE-END")
            path = handle.name
        self.addCleanup(os.unlink, path)
        with patch.object(Path, "read_text", side_effect=AssertionError("read the whole file")):
            tail = read_log_tail(path)
        self.assertLessEqual(len(tail), 64 * 1024)
        self.assertTrue(tail.endswith("THE-END"))
        self.assertEqual(read_log_tail("/no/such/file"), "")


class LogRotationTests(unittest.TestCase):
    """A real incident: the breaker blocked every future launch, forever.

    ``_rotate_log`` used to roll the log over only past 1MB. The breaker
    added alongside it kills a crashing launch after a few dozen lines - far
    short of a megabyte - so a prefix that crash-loops never produces enough
    output to trigger that rotation. The *next* launch attempt then appended
    onto the same file, and its very first poll read a tail still full of
    the *previous* attempt's "starting debugger..." lines and aborted before
    writing a line of its own - the user could not get a new attempt to even
    try, no matter how many times they clicked Launch. Rotation must never
    be conditional on how much the previous attempt happened to write.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / "launcher.log"

    def test_a_tiny_leftover_log_still_rotates(self):
        """The regression test for the incident itself."""
        from commander_gui.launcher import _rotate_log

        marker = RUNNER_CRASH_LOOP_MARKER
        self.log.write_text(f"a few lines\n{marker}\n" * RUNNER_CRASH_LOOP_THRESHOLD)
        self.assertLess(self.log.stat().st_size, 1024)  # nowhere near 1MB
        _rotate_log(self.log)
        self.assertFalse(self.log.exists())
        self.assertTrue((self.tmp / "launcher.log.1").exists())

    def test_a_new_launch_never_inherits_the_previous_ones_crash(self):
        """Through the real entry point: launch_detached() itself."""
        from commander_gui.launcher import launch_detached

        marker = RUNNER_CRASH_LOOP_MARKER
        self.log.write_text(f"{marker}\n" * (RUNNER_CRASH_LOOP_THRESHOLD + 5))
        process = launch_detached(["true"], {}, str(self.tmp), log_path=self.log)
        process.wait(timeout=5)
        # The new attempt's own log starts empty - none of the old marker
        # text is present for the very next poll to trip over.
        self.assertNotIn(marker, self.log.read_text())
        self.assertFalse(runner_crash_loop(read_log_tail(self.log)))

    def test_rotation_is_a_no_op_when_there_is_nothing_to_roll(self):
        from commander_gui.launcher import _rotate_log

        _rotate_log(self.log)  # no launcher.log exists yet
        self.assertFalse(self.log.exists())
        self.assertFalse((self.tmp / "launcher.log.1").exists())


# =====================================================================
# The repair: surgical, keyed on another Wine's byte identity
# =====================================================================
class PrefixRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.build = _fake_proton(self.tmp)
        # A second, "system" wine whose builtins are what contaminated the prefix.
        self.host = self.tmp / "usr" / "lib" / "wine"
        for arch, payload in (("x86_64-windows", b"H64"), ("i386-windows", b"H32")):
            (self.host / arch).mkdir(parents=True)
            (self.host / arch / "ntdll.dll").write_bytes(_REAL_DLL + b"HOST" + payload)
            (self.host / arch / "msvcrt.dll").write_bytes(_REAL_DLL + b"HOSTMSVCRT" + payload)
            (self.host / arch / "hostonly.dll").write_bytes(_REAL_DLL + b"HOSTONLY" + payload)
        self.prefix = self.tmp / "prefix"
        self.system32 = self.prefix / "drive_c" / "windows" / "system32"
        self.syswow64 = self.prefix / "drive_c" / "windows" / "syswow64"
        self.system32.mkdir(parents=True)
        self.syswow64.mkdir(parents=True)
        self.runner = _umu_runner(self.build, self.prefix)
        self.host_patch = patch(
            "commander_gui.repair.host_wine_lib_dirs", return_value=[self.host]
        )
        self.host_patch.start()
        self.addCleanup(self.host_patch.stop)

    def _contaminate(self):
        import shutil

        for arch, folder in (("x86_64-windows", self.system32), ("i386-windows", self.syswow64)):
            for name in ("ntdll.dll", "msvcrt.dll", "hostonly.dll"):
                shutil.copyfile(self.host / arch / name, folder / name)
        # Things that must survive untouched:
        (self.system32 / "d3d11.dll").write_bytes(_REAL_DLL + b"DXVK")        # differs from Proton's, but not a host builtin
        (self.system32 / "msvcp140.dll").write_bytes(_REAL_DLL + b"MICROSOFT")  # a verb's genuine runtime
        (self.system32 / "kernelbase.dll").write_bytes(b"\x00" * 117)         # a placeholder
        (self.prefix / "winetricks.log").write_text("vcrun2022\n")
        (self.prefix / "system.reg").write_text("WINE REGISTRY Version 2\n")

    def test_finds_exactly_the_host_wines_files(self):
        from commander_gui.repair import foreign_prefix_dlls

        self._contaminate()
        found = sorted(str(item.relative_to(self.prefix / "drive_c" / "windows")) for item, _r in foreign_prefix_dlls(self.prefix, self.runner))
        self.assertEqual(found, [
            "system32/hostonly.dll", "system32/msvcrt.dll", "system32/ntdll.dll",
            "syswow64/hostonly.dll", "syswow64/msvcrt.dll", "syswow64/ntdll.dll",
        ])

    def test_repair_restores_the_runners_copies_and_touches_nothing_else(self):
        from commander_gui.repair import repair_prefix_foreign_dlls

        self._contaminate()
        repaired = repair_prefix_foreign_dlls(self.prefix, self.runner)
        self.assertEqual(len(repaired), 6)
        # Restored to the runner's bytes.
        lib = self.build / "files" / "lib" / "wine"
        self.assertEqual((self.system32 / "ntdll.dll").read_bytes(), (lib / "x86_64-windows/ntdll.dll").read_bytes())
        self.assertEqual((self.syswow64 / "ntdll.dll").read_bytes(), (lib / "i386-windows/ntdll.dll").read_bytes())
        self.assertEqual((self.system32 / "msvcrt.dll").read_bytes(), (lib / "x86_64-windows/msvcrt.dll").read_bytes())
        # A host-only DLL the runner does not ship is removed, not replaced.
        self.assertFalse((self.system32 / "hostonly.dll").exists())
        # Untouched: DXVK, a Microsoft runtime, a placeholder, the log, the registry.
        self.assertEqual((self.system32 / "d3d11.dll").read_bytes(), _REAL_DLL + b"DXVK")
        self.assertEqual((self.system32 / "msvcp140.dll").read_bytes(), _REAL_DLL + b"MICROSOFT")
        self.assertEqual((self.system32 / "kernelbase.dll").stat().st_size, 117)
        self.assertEqual((self.prefix / "winetricks.log").read_text(), "vcrun2022\n")
        self.assertEqual((self.prefix / "system.reg").read_text(), "WINE REGISTRY Version 2\n")
        # No temp files left behind.
        self.assertEqual([p.name for p in self.system32.glob(".*commander-repair")], [])
        # And the guard is satisfied afterwards.
        self.assertEqual(prefix_foreign_dlls(self.prefix, self.runner), [])

    def test_repair_is_a_no_op_on_a_healthy_prefix(self):
        from commander_gui.repair import repair_prefix_foreign_dlls

        lib = self.build / "files" / "lib" / "wine"
        (self.system32 / "ntdll.dll").write_bytes((lib / "x86_64-windows/ntdll.dll").read_bytes())
        self.assertEqual(repair_prefix_foreign_dlls(self.prefix, self.runner), [])


# =====================================================================
# Architecture guard
# =====================================================================
class NoStrayWineEnvTests(unittest.TestCase):
    def test_wineprefix_is_only_set_for_a_runner(self):
        """Only winetricks.py and launcher.py may build a Wine environment.

        Every other file that mentions WINEPREFIX may only *read* it from a
        resolved runner's env. A stray ``{"WINEPREFIX": ...}`` anywhere else
        is the shape of the bug this whole file exists for.
        """
        offenders = []
        for folder in ("commander_gui", "Steamdeck"):
            for path in (REPO_ROOT / folder).rglob("*.py"):
                if path.name in ("winetricks.py", "launcher.py"):
                    continue
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if '"WINEPREFIX":' in line and not line.lstrip().startswith("#"):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
        self.assertEqual(offenders, [], f"Wine env built outside the runner layer: {offenders}")


if __name__ == "__main__":
    unittest.main()
