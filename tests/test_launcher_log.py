from assistant.analyzers.launcher_log import analyze_launcher


def test_unimplemented_wine_function_flags_old_proton():
    lines = [
        "gamemodeauto: dlopen failed - libgamemode.so: cannot open shared object file: No such file or directory",
        "wine: Call from 00006FFFFF3D4894 to unimplemented function win32u.NtUserInitAnsiOem, aborting",
        "wine: Call from 00006FFFFFB9D977 to unimplemented function shell32.dll.SHGetFolderPathW, aborting",
    ]
    findings = analyze_launcher("launcher.log", "launcher.log", lines)
    old = [
        f
        for f in findings
        if f.title == "The selected Proton build is too old for this prefix."
    ]
    assert len(old) == 2
    assert "win32u.NtUserInitAnsiOem" in old[0].detail


def test_a_pressure_vessel_error_is_not_called_harmless():
    from assistant.findings import Severity

    lines = ["pressure-vessel-wrap[1234]: E: Failed to run bwrap: Permission denied"]
    findings = analyze_launcher("launcher.log", "launcher.log", lines)
    assert [f.severity for f in findings] == [Severity.ERROR]
    assert "bwrap" in findings[0].title


def test_long_launcher_errors_with_one_start_stay_separate():
    from assistant.findings import collapse_findings

    start = "x" * 130
    lines = [f"ERROR: {start} first", f"ERROR: {start} second"]
    findings = collapse_findings(analyze_launcher("launcher.log", "launcher.log", lines))
    assert len(findings) == 2


def test_vcredist_hex_exit_codes_are_read():
    from assistant.analyzers.winetricks_vcredist import analyze_vcredist

    findings = analyze_vcredist("dd_vcredist_x64.log", "prefix", ["Exit code: 0x643"])
    assert findings, "0x643 (1603) must be reported"


def test_an_httpclient_timeout_is_a_download_failure_not_a_cancel():
    from assistant.analyzers.cli_log import _classify

    trace = (
        "system.threading.tasks.taskcanceledexception: the request was canceled due to "
        "the configured httpclient.timeout of 100 seconds elapsing."
    )
    assert _classify("error downloading from moddb", trace) == "download"


def test_no_active_profile_matches_the_current_cli_wording():
    from assistant.analyzers.cli_log import analyze_cli

    line = "No active profile, create one with 'config create' or select one with 'config use'"
    assert analyze_cli("cli.log", "commander", [line])


def test_merged_checksum_failures_list_every_file():
    from assistant.analyzers.cli_log import analyze_cli
    from assistant.findings import collapse_findings

    lines = ["mods/a.7z | CORRUPT", "mods/b.7z | CORRUPT"]
    findings = [f for f in collapse_findings(analyze_cli("cli.log", "commander", lines)) if f.count == 2]
    assert len(findings) == 1
    assert "a.7z" in findings[0].technical and "b.7z" in findings[0].technical


def test_a_flood_of_engine_errors_stays_bounded():
    from assistant.analyzers.xray import analyze_xray

    lines = [f"! [Error] broken texture {n}" for n in range(2000)] + ["! [Error] broken texture 0"] * 3000
    findings = analyze_xray("xray.log", "game", lines)
    assert len(findings) == 501  # 500 distinct + one "more entries" line
    assert findings[0].count == 3001
    assert "1500" in findings[-1].title


def test_pathological_lines_are_handled_in_linear_time():
    import time

    from assistant.analyzers.cli_log import _EXCEPTION_TYPE_RE
    from assistant.report import _redact
    from commander_gui.integrity import is_gamma_overlay_corrupt_line

    started = time.perf_counter()
    assert _EXCEPTION_TYPE_RE.search("a" * 80_000) is None
    assert _EXCEPTION_TYPE_RE.search("x System.IO.IOException: y").group(1) == "System.IO.IOException"
    _redact("://" + "a:" * 40_000)
    assert "[REDACTED]" in _redact("https://user:secret@example.com/x")
    assert not is_gamma_overlay_corrupt_line("a" + " " * 40_000 + "b")
    # Each case took seconds before; together they must be near-instant.
    assert time.perf_counter() - started < 1.0
