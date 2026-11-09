from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from borrowed_hands_helper import xcresult
from borrowed_hands_helper.xcresult import XcresultError, parse_build_results

FIX = Path(__file__).parent / "fixtures" / "x1-build-results.json"
EVIDENCE_PROJ = Path(__file__).parent / "fixtures" / "probe-proj"
ROOT = "/tmp/bh-capture/scratchpad/probes-xcode/proj"


def issue(msg="boom", url=None, typ="Swift Compiler Error"):
    d = {"className": "DVTTextDocumentLocation", "issueType": typ, "message": msg}
    if url is not None:
        d["sourceURL"] = url
    return d


def results(errors=(), warnings=(), status="failed"):
    return {"status": status, "errorCount": len(errors), "warningCount": len(warnings),
            "errors": list(errors), "warnings": list(warnings)}


def test_real_evidence():
    d = parse_build_results(json.loads(FIX.read_text()), source_root=ROOT)
    assert d.status == "failed"
    assert (d.error_count, d.warning_count) == (2, 0)
    e = d.diagnostics[0]
    assert e.severity == "error"
    assert e.message.startswith("Cannot convert value of type")
    assert e.file == "Sources/Greeting.swift"
    assert (e.line, e.column) == (1, 42)
    assert e.issue_type == "Swift Compiler Error"
    assert not d.truncated


def test_no_absolute_paths_leak_in_real_evidence():
    d = parse_build_results(json.loads(FIX.read_text()), source_root=ROOT)
    blob = json.dumps(d.to_dict())
    assert "/tmp/" not in blob and "bh-capture" not in blob and "/Users" not in blob


def test_zero_based_to_one_based():
    url = "file:///src/a.swift#StartingColumnNumber=0&StartingLineNumber=0"
    d = parse_build_results(results([issue(url=url)]), source_root="/src")
    assert (d.diagnostics[0].line, d.diagnostics[0].column) == (1, 1)


def test_missing_source_url_and_fragment():
    d = parse_build_results(results([issue()], [issue("w", "file:///src/b.swift", "Warning")]),
                            source_root="/src")
    assert (d.diagnostics[0].file, d.diagnostics[0].line, d.diagnostics[0].column) == (None, None, None)
    w = d.diagnostics[1]
    assert w.severity == "warning" and w.file == "b.swift" and w.line is None


def test_url_encoded_path():
    url = "file:///src/My%20App/Caf%C3%A9.swift#StartingLineNumber=4&StartingColumnNumber=2"
    d = parse_build_results(results([issue(url=url)]), source_root="/src")
    assert d.diagnostics[0].file == "My App/Café.swift"
    assert d.diagnostics[0].line == 5


def test_duplicate_fragment_keys_first_wins():
    url = "file:///src/a.swift#StartingLineNumber=1&StartingLineNumber=9&StartingColumnNumber=3"
    d = parse_build_results(results([issue(url=url)]), source_root="/src")
    assert d.diagnostics[0].line == 2


def test_garbage_fragment_values():
    url = "file:///src/a.swift#StartingLineNumber=abc&StartingColumnNumber=-5"
    d = parse_build_results(results([issue(url=url)]), source_root="/src")
    assert d.diagnostics[0].file == "a.swift"
    assert d.diagnostics[0].line is None and d.diagnostics[0].column is None


def test_path_outside_root_is_basename_only():
    url = "file:///Users/someone/Library/SDK/Foo.h#StartingLineNumber=0"
    d = parse_build_results(results([issue(url=url)]), source_root="/src")
    assert d.diagnostics[0].file == "Foo.h"


def test_root_prefix_must_be_path_boundary():
    url = "file:///src-evil/secret/a.swift#StartingLineNumber=0"
    d = parse_build_results(results([issue(url=url)]), source_root="/src")
    assert d.diagnostics[0].file == "a.swift"


def test_dotdot_cannot_escape_root():
    url = "file:///src/../etc/passwd#StartingLineNumber=0"
    d = parse_build_results(results([issue(url=url)]), source_root="/src")
    assert d.diagnostics[0].file == "passwd"


def test_no_source_root_gives_basename():
    url = "file:///Users/someone/proj/a.swift#StartingLineNumber=0"
    d = parse_build_results(results([issue(url=url)]))
    assert d.diagnostics[0].file == "a.swift"


def test_message_scrubbed_and_capped():
    msg = "No such file /src/Sources/x.h and /Users/bob/secret.txt " + "z" * 2000
    d = parse_build_results(results([issue(msg)]), source_root="/src", max_message_len=100)
    m = d.diagnostics[0].message
    assert len(m) <= 100 and "/Users/bob" not in m and "/src" not in m


def test_list_cap_keeps_errors_first_and_counts_true_totals():
    errs = [issue(f"e{i}") for i in range(5)]
    warns = [issue(f"w{i}", typ="Warning") for i in range(5)]
    d = parse_build_results(results(errs, warns), max_diagnostics=7)
    assert len(d.diagnostics) == 7 and d.truncated
    assert [x.severity for x in d.diagnostics] == ["error"] * 5 + ["warning"] * 2
    assert (d.error_count, d.warning_count) == (5, 5)


def test_counts_prefer_reported_totals():
    obj = results([issue()])
    obj["errorCount"] = 40
    d = parse_build_results(obj, max_diagnostics=10)
    assert d.error_count == 40 and len(d.diagnostics) == 1


def test_succeeded_and_sparse_input():
    d = parse_build_results({"status": "succeeded"})
    assert d.status == "succeeded" and d.diagnostics == [] and d.error_count == 0
    assert parse_build_results({}).status == "unknown"


@pytest.mark.parametrize("bad", [None, [], "x", 3])
def test_non_dict_rejected(bad):
    with pytest.raises(XcresultError):
        parse_build_results(bad)


def test_from_bundle_shells_argv_with_timeout(monkeypatch, tmp_path):
    seen = {}

    def fake(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, FIX.read_text(), "")
    monkeypatch.setattr(xcresult.subprocess, "run", fake)
    d = xcresult.from_bundle(tmp_path / "x.xcresult", source_root=ROOT)
    assert seen["argv"][:5] == ["xcrun", "xcresulttool", "get", "build-results", "--path"]
    assert seen["argv"][5] == str(tmp_path / "x.xcresult")
    assert seen["kw"]["timeout"] > 0 and not seen["kw"].get("shell")
    assert d.error_count == 2


def test_from_bundle_failure_modes(monkeypatch, tmp_path):
    monkeypatch.setattr(xcresult.subprocess, "run",
                        lambda a, **k: subprocess.CompletedProcess(a, 1, "", "no such bundle"))
    with pytest.raises(XcresultError):
        xcresult.from_bundle(tmp_path / "x")
    monkeypatch.setattr(xcresult.subprocess, "run",
                        lambda a, **k: subprocess.CompletedProcess(a, 0, "not json", ""))
    with pytest.raises(XcresultError):
        xcresult.from_bundle(tmp_path / "x")

    def boom(a, **k):
        raise subprocess.TimeoutExpired(a, 1)
    monkeypatch.setattr(xcresult.subprocess, "run", boom)
    with pytest.raises(XcresultError):
        xcresult.from_bundle(tmp_path / "x")


@pytest.mark.xcode
@pytest.mark.skipif(shutil.which("xcodebuild") is None, reason="xcodebuild missing")
def test_integration_real_build_line_numbers(tmp_path: Path):
    proj = tmp_path / "proj"
    shutil.copytree(EVIDENCE_PROJ, proj)
    swift = proj / "Sources" / "Greeting.swift"
    swift.write_text(
        "func greeting() -> String {\n"       # line 1
        "    let ok = \"fine\"\n"             # line 2
        "    let n: Int = \"oops\"\n"         # line 3  <- error
        "    return ok\n"
        "}\n"
    )
    bundle = tmp_path / "out.xcresult"
    r = subprocess.run(
        ["xcodebuild", "-project", str(proj / "Probe.xcodeproj"), "-scheme", "Probe",
         "-destination", "platform=macOS", "-derivedDataPath", str(tmp_path / "dd"),
         "-resultBundlePath", str(bundle), "CODE_SIGNING_ALLOWED=NO", "build"],
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home()),
             "PROBE_MARKER_DIR": str(tmp_path)},
        capture_output=True, text=True, timeout=600)
    assert r.returncode != 0
    d = xcresult.from_bundle(bundle, source_root=proj)
    assert d.status == "failed" and d.error_count >= 1
    errs = [x for x in d.diagnostics if x.severity == "error" and x.file == "Sources/Greeting.swift"]
    assert errs, d.diagnostics
    assert errs[0].line == 3
    assert errs[0].column is not None and errs[0].column >= 1
    assert str(tmp_path) not in json.dumps(d.to_dict())


def test_private_prefix_spellings_both_relativize():
    for url_root in ("/private/var/x/proj", "/var/x/proj"):
        url = f"file://{url_root}/Sources/a.swift#StartingLineNumber=0"
        d = parse_build_results(results([issue(url=url)]), source_root="/var/x/proj")
        assert d.diagnostics[0].file == "Sources/a.swift"


@pytest.mark.parametrize("leak", [
    "/private/var/folders/ab/cdef/T/x.h",
    "/var/folders/ab/cdef/T/x.h",
    "/Users/alex/Library/Keychains/login.keychain-db",
    "%2FUsers%2Falex%2Fsecret.txt",
    "/Volumes/Backup/stuff/a.h",
    "/tmp/agent-502/scratch/a.h",
    "/private/tmp/x/a.h",
])
def test_message_scrubs_system_paths(leak):
    d = parse_build_results(results([issue(f"cannot open {leak} now")]), source_root="/src")
    m = d.diagnostics[0].message
    for bad in ("alex", "folders", "/private", "/Volumes", "Keychains", "agent-502", "/tmp/"):
        assert bad not in m, (bad, m)
    assert m.startswith("cannot open")


def test_library_tail_dropped():
    d = parse_build_results(results([issue("x /Users/bob/Library/Application Support/Foo/y")]))
    assert "Support" not in d.diagnostics[0].message
    assert "bob" not in d.diagnostics[0].message


CTRL = "a\nb\rc\x1b[31md\u202ee\u200bf\x85g\u2028h"


def _clean(s):
    import unicodedata
    return all(unicodedata.category(c) not in ("Cc", "Cf", "Zl", "Zp") for c in s)


def test_controls_stripped_everywhere():
    obj = results([{"message": CTRL, "issueType": CTRL * 20,
                    "sourceURL": "file:///src/x%0a%0d%1b%5B31m%E2%80%AE.swift#StartingLineNumber=0"}])
    obj["status"] = CTRL * 30
    d = parse_build_results(obj, source_root="/src")
    x = d.diagnostics[0]
    for v in (x.message, x.file, x.issue_type, d.status):
        assert _clean(v), repr(v)
    assert len(x.issue_type) <= 80 and len(d.status) <= 40


def test_file_capped():
    url = "file:///src/" + "d/" * 300 + "a.swift"
    d = parse_build_results(results([issue(url=url)]), source_root="/src")
    assert len(d.diagnostics[0].file) <= 200


@pytest.mark.parametrize("key", ["errors", "warnings"])
@pytest.mark.parametrize("bad", ["x", 3, {"a": 1}, True])
def test_non_list_issue_collections_rejected(key, bad):
    with pytest.raises(XcresultError):
        parse_build_results({key: bad})


@pytest.mark.parametrize("bad", [True, False, "3", 2.5, -1])
def test_bad_counts_fall_back_to_listed(bad):
    d = parse_build_results({"errors": [issue()], "errorCount": bad, "warningCount": bad})
    assert d.error_count == 1 and d.warning_count == 0


# ---- review round 2 -----------------------------------------------------

def _msg(text, root="/src"):
    return parse_build_results(results([issue(text)]), source_root=root).diagnostics[0].message


@pytest.mark.parametrize("sneaky", [
    "/\u200bUsers/alex/Library/Keychains/x",
    "/Users\u200b/alex/.ssh/id_rsa",
    "/Us\x00ers/alex/.ssh/id_rsa",
    "/Us%00ers/alex/.ssh/id_rsa",
    "/%E2%80%8BUsers/alex/.ssh/id_rsa",
    "/users/alex/.ssh/id_rsa",
    "/USERS/ALEX/.ssh/id_rsa",
    "/Users/./alex/.ssh/id_rsa",
    "/./Users/alex/.ssh/id_rsa",
    "//Users//alex/.ssh/id_rsa",
    "/tmp/../Users/alex/.ssh/id_rsa",
    "%252FUsers%252Falex%252F.ssh%252Fid_rsa",
    "%25252FUsers%25252Falex%25252F.ssh",
    "/VAR/folders/zz/T/x.h",
    "/Private/Var/folders/zz/T/x.h",
    "/var/./folders/zz/T/x.h",
    "~/.ssh/id_rsa",
    "~/.aws/credentials",
    "file:///Users/./alex/.ssh/id_rsa",
])
def test_message_scrub_cannot_be_dodged(sneaky):
    m = _msg(f"open {sneaky} failed")
    for bad in ("alex", "ALEX", "id_rsa", ".aws", "folders", "Keychains", "Users", "/var", "/VAR"):
        assert bad not in m, (bad, m)
    assert m.startswith("open")


def test_plain_text_and_relative_paths_untouched():
    assert _msg("use of undeclared type 'Foo' in Sources/var/a.swift") == \
        "use of undeclared type 'Foo' in Sources/var/a.swift"
    assert "https://example.com/a" in _msg("see https://example.com/a for details")


@pytest.mark.parametrize("url", [
    "file:///Users/alex/.ssh/id_rsa#StartingLineNumber=0",
    "file:///src/../.ssh/id_rsa#StartingLineNumber=0",
    "file:///src/%2e%2e/.ssh/id_rsa#StartingLineNumber=0",
    "file:///src/%252e%252e/.ssh/id_rsa#StartingLineNumber=0",
    "file:///%E2%80%8BUsers/alex/.ssh/id_rsa#StartingLineNumber=0",
    "file:///src/x%00/../../Users/alex/.ssh/id_rsa",
])
def test_file_never_absolute_or_dotdot(url):
    f = parse_build_results(results([issue(url=url)]), source_root="/src").diagnostics[0].file
    assert f == "id_rsa"


def test_issue_type_and_status_are_scrubbed():
    obj = results([{"message": "m", "issueType": "/Users/alex/.ssh/id_rsa error",
                    "sourceURL": "file:///src/a.swift"}])
    obj["status"] = "~/.aws/credentials"
    d = parse_build_results(obj, source_root="/src")
    for v in (d.diagnostics[0].issue_type, d.status):
        assert "alex" not in v and ".aws" not in v and "id_rsa" not in v


def test_counts_and_positions_capped():
    obj = results([issue(url="file:///src/a.swift#StartingLineNumber=99999999999&StartingColumnNumber=5")])
    obj["errorCount"] = 10**15
    obj["warningCount"] = 2**70
    d = parse_build_results(obj, source_root="/src")
    assert d.error_count <= 10_000_000 and d.warning_count <= 10_000_000
    assert d.diagnostics[0].line is None and d.diagnostics[0].column == 6


# ---- round 3: scrubber is hygiene, not a boundary -----------------------

@pytest.mark.parametrize("text", [
    "file://localhost/Users/alex/.ssh/id_rsa",
    "file://host.example/private/var/folders/zz/T/x",
    "/System/Volumes/Data/Users/alex/.ssh/id_rsa",
    "/System/Volumes/Data/private/var/folders/zz/T/x",
    "../Users/alex/.ssh/id_rsa",
    "~alex/.ssh/id_rsa",
    "~root/.profile",
])
def test_more_machine_path_forms(text):
    m = _msg(f"bad {text} end")
    for leak in ("alex", "id_rsa", "folders", "root", ".profile", "Users"):
        assert leak not in m, (leak, m)


def test_sensitive_roots_with_real_fixture():
    obj = json.loads(FIX.read_text())
    secret = "/tmp/agent-502/-Users-jdoe-example-com-Development-project"
    obj["errors"][0]["message"] += f" at {secret}/4af636d7/scratchpad/DerivedData/x.swiftmodule"
    obj["errors"][1]["message"] += " " + secret.upper() + "/Build"
    d = parse_build_results(obj, source_root=ROOT, sensitive_roots=[secret])
    blob = json.dumps(d.to_dict())
    assert "agent-502" not in blob.lower() and "jdoe" not in blob.lower()
    assert d.diagnostics[0].file == "Sources/Greeting.swift"  # source root still stripped


def test_sensitive_root_equal_to_source_root_keeps_relative_paths():
    d = parse_build_results(results([issue("err in /src/Sources/a.swift")]),
                            source_root="/src", sensitive_roots=["/src"])
    assert d.diagnostics[0].message == "err in Sources/a.swift"


def test_sensitive_root_redacted_before_decoding():
    d = parse_build_results(results([issue("/Volumes/Builder%20Disk/job-1/x.o")]),
                            sensitive_roots=["/volumes/builder%20disk/JOB-1"])
    assert "job-1" not in d.diagnostics[0].message.lower()


@pytest.mark.parametrize("field_", ["message", "issueType"])
def test_unstable_or_leftover_encoding_redacts_whole_field(field_):
    deep = "%" + "25" * 9 + "41"  # needs more than the round cap to settle
    obj = results([{"message": "m", "issueType": "t", field_: "keep? " + deep}])
    d = parse_build_results(obj)
    got = d.diagnostics[0].message if field_ == "message" else d.diagnostics[0].issue_type
    assert got == "<redacted>"


def test_file_unstable_encoding_redacted():
    url = "file:///src/" + "%" + "25" * 9 + "41.swift"
    assert parse_build_results(results([issue(url=url)]), source_root="/src").diagnostics[0].file == "<redacted>"


@pytest.mark.parametrize("path,expected", [
    ("/src/Sources/a.swift", "Sources/a.swift"),
    ("/src/a/../b.swift", "b.swift"),
    ("/src\\Sources\\a.swift", "<unknown>"),            # backslash is not allowlisted
    ("/src/Sources\uff0fa.swift", "Sources/a.swift"),     # NFKC maps fullwidth solidus to /
    ("/src/Sources\u2215a.swift", "<unknown>"),
    ("/src/Sources\u2044a.swift", "<unknown>"),
    ("/src/Sources\u29f8a.swift", "<unknown>"),
    ("/src/Sources\uff3ca.swift", "<unknown>"),
    ("/src/..／.ssh／id_rsa", "id_rsa"),
    ("/src/x%2f..%2f..%2fetc/passwd", "passwd"),
    ("/src/", "src"),
    ("/", "<unknown>"),
    ("/src/%2e%2e", "<unknown>"),
])
def test_file_field_rules(path, expected):
    url = "file://" + path.replace("\\", "%5C") + "#StartingLineNumber=0"
    if path.endswith("%2e%2e") or "%2f" in path:
        url = "file://" + path
    got = parse_build_results(results([issue(url=url)]), source_root="/src").diagnostics[0].file
    assert got == expected, (path, got)


def test_file_not_decoded_again_after_decision():
    # a literal "%2e%2e" in a file NAME (double-encoded) must end as a literal, never as ".."
    url = "file:///src/Sources/%252e%252e.swift"
    got = parse_build_results(results([issue(url=url)]), source_root="/src").diagnostics[0].file
    assert got == "Sources/...swift"


def test_root_strip_only_at_span_start():
    # root text in the middle of another path must not be spliced out
    m = _msg("copy /other/src/Sources/a.swift to /src/Sources/b.swift")
    assert m == "copy /other/src/Sources/a.swift to Sources/b.swift"


# ---- round 4: delimiter semantics, flag-glued roots, ordering -----------

@pytest.mark.parametrize("text", [
    "/Volumes/Macintosh HD/Users/alex/proj/a.h",
    "/Users/alex/Library/Application Support/Foo Bar/x.db",
    "/Users/alex/Library/Developer/Xcode/iOS DeviceSupport/17.0/Symbols",
    "/Users/alex/Library/MobileDevice/Provisioning Profiles/abc.mobileprovision",
    "~/Library/Application Support/Foo/x",
    "/private/var/folders/zz/My Cache/T/x",
    "/System/Volumes/Data/Applications/Some App.app/x",
])
def test_paths_with_spaces_redacted_to_delimiter(text):
    m = _msg(f"cannot read '{text}' here")
    for leak in ("Macintosh", "Support", "DeviceSupport", "Provisioning", "Bar", "Cache", "Some App",
                 "alex", "Symbols", "abc.mobileprovision"):
        assert leak not in m, (leak, m)
    assert m == "cannot read '<path>' here", m


@pytest.mark.parametrize("tail,expected", [
    ("(see /Users/a b/c.h) later", "(see <path>) later"),
    ("[/Users/a b/c.h] later", "[<path>] later"),
    ("/Users/a b/c.h, next", "<path>, next"),
    ("/Users/a b/c.h (line 3)", "<path> (line 3)"),
    ("\"/Users/a b/c.h\" end", "\"<path>\" end"),
    ("/Users/a b/c.h and more text", "<path>"),
])
def test_delimiters(tail, expected):
    assert _msg(tail) == expected


def test_regression_tcc_db_with_and_without_sensitive_roots():
    msg = "open /Users/jdoe/Library/Application Support/com.apple.TCC/TCC.db failed"
    for sens in (None, ["/Users/jdoe"]):
        m = parse_build_results(results([issue(msg)]), sensitive_roots=sens).diagnostics[0].message
        assert "Support" not in m and "TCC" not in m and "jdoe" not in m, m


@pytest.mark.parametrize("text", [
    "-L/private/var/folders/zz/T/x",
    "-I/Users/x/include",
    "-isystem/Users/x/inc",
    "-iquote/Users/x/inc",
    "-Xlinker/Users/x/inc",
    "-o/Volumes/Disk One/out",
    "-F/Users/x/DerivedData/Build",
    "-include/Users/x/pre.h",
    "path=/Users/x/inc",
    "(/Users/x/inc)",
    "[/Users/x/inc",
    "a,/Users/x/inc",
])
def test_flag_glued_and_punctuation_preceded_roots(text):
    m = _msg(f"clang {text} -c a.c")
    assert "Users" not in m and "folders" not in m and "Disk" not in m and "x/inc" not in m, m


def test_flag_glue_with_sensitive_root():
    dd = "/opt/builder/DerivedData"
    m = parse_build_results(results([issue(f"ld -F{dd}/Build/Products x")]),
                            sensitive_roots=[dd]).diagnostics[0].message
    assert "builder" not in m and "-F<path>" in m


@pytest.mark.parametrize("keep", [
    "Sources/Users/List.swift", "App/Volumes/Mixer.swift", "lib/private/Impl.swift",
    "x-/Users/y", "Sources/tmp/a.swift", "a/etc/b",
])
def test_word_preceded_roots_untouched(keep):
    assert _msg(f"error in {keep}") == f"error in {keep}"


def test_sensitive_pass_runs_after_decode_and_normalisation():
    home = "/Users/Shared/.bh-builder-home"
    for text in (f"{home}%2FLibrary%2Fx", f"{home}%252FLibrary", "/opt/ci//job-42/DerivedData/Build/x.o",
                 "/opt/ci/./job-42/DerivedData/x", "/opt/ci/x/../job-42/DerivedData/x"):
        m = parse_build_results(results([issue(f"at {text} end")]),
                                sensitive_roots=[home, "/opt/ci//job-42/DerivedData"]).diagnostics[0].message
        assert "builder" not in m and "job-42" not in m and "DerivedData" not in m, (text, m)


def test_sensitive_root_given_non_normalised():
    m = parse_build_results(results([issue("at /opt/ci/job-42/x")]),
                            sensitive_roots=["/opt/ci/./a/../job-42"]).diagnostics[0].message
    assert "job-42" not in m


def test_file_host_strip_cannot_eat_placeholder():
    m = parse_build_results(results([issue("see file://localhost/opt/ci/x' and <path>/y")]),
                            sensitive_roots=["/opt/ci"]).diagnostics[0].message
    assert m == "see <path>' and <path>/y"
