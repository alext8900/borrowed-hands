"""The new-app template the agent copies to start a SwiftUI app from nothing (no Xcode on the Hermes host).

It ships inside the plugin directory (the catalog installs only `borrowed-hands/`), and
`templates/new_swiftui_app.py` instantiates it with stdlib only: validated name and bundle id, deterministic
bytes, no placeholder left, never overwrites. The xcodebuild check of instances runs on a Mac, not here.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from borrowed_hands import prompt, tools

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "borrowed-hands"
SCRIPT = PLUGIN_DIR / "templates" / "new_swiftui_app.py"
TEMPLATE = PLUGIN_DIR / "templates" / "swiftui-app"
PLACEHOLDER = re.compile(r"__[A-Z][A-Z_]*__")


def make(dest, name="HelloApp", bundle="com.example.helloapp", *extra):
    return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--name", name, "--bundle-id", bundle,
                           *extra, str(dest)], capture_output=True, text=True, timeout=60)


def tree(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for f in sorted(filenames):
            p = Path(dirpath) / f
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


# ---------------------------------------------------------------- shipped with the plugin
def test_template_and_script_live_in_the_plugin_dir():
    assert SCRIPT.is_file() and TEMPLATE.is_dir()
    for p in TEMPLATE.rglob("*"):
        assert not p.is_symlink(), p
        assert "__pycache__" not in p.parts
    names = {p.relative_to(TEMPLATE).as_posix() for p in TEMPLATE.rglob("*") if p.is_file()}
    assert names == {
        "gitignore",
        "__APP_NAME__.xcodeproj/project.pbxproj",
        "__APP_NAME__.xcodeproj/xcshareddata/xcschemes/__APP_NAME__.xcscheme",
        "__APP_NAME__/__APP_NAME__App.swift",
        "__APP_NAME__/ContentView.swift",
    }


ALLOWED_ISA = {"PBXFileReference", "PBXFileSystemSynchronizedRootGroup", "PBXFrameworksBuildPhase", "PBXGroup",
               "PBXNativeTarget", "PBXProject", "PBXResourcesBuildPhase", "PBXSourcesBuildPhase",
               "XCBuildConfiguration", "XCConfigurationList"}
ALLOWED_SCHEME_TAGS = {"Scheme", "BuildAction", "BuildActionEntries", "BuildActionEntry", "BuildableReference",
                       "TestAction", "LaunchAction", "BuildableProductRunnable", "ProfileAction", "AnalyzeAction",
                       "ArchiveAction"}


def structure_problems(pbx: str, scheme: str) -> list:
    """An allowlist for the template's project: only these object types, no build phase lists files, no
    build rules, dependencies, packages or base configurations, and only these scheme elements (so no
    Pre/PostActions, environment, arguments or test plans). Anything new must be added here on purpose."""
    import xml.etree.ElementTree as ET
    problems = []
    isa = set(re.findall(r"\bisa = (\w+);", pbx))
    problems += [f"isa {x}" for x in sorted(isa - ALLOWED_ISA)]
    for key in ("files", "buildRules", "dependencies", "packageProductDependencies"):
        for body in re.findall(rf"\b{key} = \(([^)]*)\);", pbx):
            if body.strip():
                problems.append(f"{key} not empty")
    for word in ("packageReferences", "baseConfigurationReference", "shellScript"):
        if word in pbx:
            problems.append(word)
    tags = {e.tag for e in ET.fromstring(scheme).iter()}
    problems += [f"scheme {t}" for t in sorted(tags - ALLOWED_SCHEME_TAGS)]
    return problems


def generated_project(tmp_path):
    assert make(tmp_path / "s").returncode == 0
    proj = tmp_path / "s" / "HelloApp.xcodeproj"
    return (proj / "project.pbxproj").read_text(), (proj / "xcshareddata/xcschemes/HelloApp.xcscheme").read_text()


def test_template_follows_the_runner_rules(tmp_path):
    pbx, scheme = generated_project(tmp_path)
    assert structure_problems(pbx, scheme) == []
    assert set(re.findall(r"\bisa = (\w+);", pbx)) == ALLOWED_ISA            # and all of them are really used
    assert "objectVersion = 77;" in pbx
    assert 'SUPPORTED_PLATFORMS = "iphoneos iphonesimulator macosx";' in pbx
    swift = "".join(p.read_text() for p in (TEMPLATE / "__APP_NAME__").glob("*.swift"))
    # macro-free, so it also builds on helpers that predate the macro fix
    assert "@State" not in swift and "#Preview" not in swift and "@Observable" not in swift


@pytest.mark.parametrize("edit", ["build-rule", "copy-files-phase", "phase-file", "dependency", "package",
                                  "base-config", "pre-action", "env-vars"])
def test_the_allowlist_catches_additions(tmp_path, edit):
    """Mutation check for the guard above: each edit is something a later template change could slip in."""
    pbx, scheme = generated_project(tmp_path)
    original = (pbx, scheme)
    if edit == "build-rule":
        pbx = pbx.replace("buildRules = (\n", "buildRules = (\n\t\t\t\tA1000000000000000000099 /* r */,\n", 1)
        pbx = pbx.replace("/* Begin PBXGroup",
                          "\t\tA1000000000000000000099 = {isa = PBXBuildRule; };\n/* Begin PBXGroup")
    if edit == "copy-files-phase":
        pbx = pbx.replace("/* Begin PBXGroup", "\t\tA1000000000000000000098 = {isa = PBXCopyFilesBuildPhase; };\n"
                                               "/* Begin PBXGroup")
    if edit == "phase-file":
        pbx = pbx.replace("files = (\n", "files = (\n\t\t\t\tA1000000000000000000097 /* x */,\n", 1)
    if edit == "dependency":
        pbx = pbx.replace("dependencies = (\n", "dependencies = (\n\t\t\t\tA1000000000000000000096 /* d */,\n", 1)
    if edit == "package":
        pbx = pbx.replace("targets = (", "packageReferences = (\n\t\t\t);\n\t\t\ttargets = (", 1)
    if edit == "base-config":
        base = "\t\t\tbaseConfigurationReference = A1000000000000000000095;"
        pbx = pbx.replace("isa = XCBuildConfiguration;", "isa = XCBuildConfiguration;\n" + base, 1)
    if edit == "pre-action":
        scheme = scheme.replace("<BuildAction\n", "<BuildAction\n", 1).replace(
            "<BuildActionEntries>", "<PreActions></PreActions>\n      <BuildActionEntries>", 1)
    if edit == "env-vars":
        scheme = scheme.replace("</LaunchAction>", "<EnvironmentVariables></EnvironmentVariables></LaunchAction>", 1)
    assert (pbx, scheme) != original, edit                                 # the edit really applied
    assert structure_problems(pbx, scheme) != [], edit


def test_script_is_stdlib_only_and_starts_no_processes():
    src = SCRIPT.read_text()
    imports = set(re.findall(r"^(?:from|import) ([a-zA-Z_]+)", src, re.M))
    assert imports <= {"__future__", "argparse", "hashlib", "json", "os", "re", "stat", "sys", "pathlib"}, imports
    for banned in ("subprocess", "os.system", "shutil", "urllib", "socket", "rmtree", "eval(", "exec("):
        assert banned not in src, banned


# ---------------------------------------------------------------- instantiation
def test_instantiates_a_complete_project_with_no_placeholder_left(tmp_path):
    dest = tmp_path / "repo"
    r = make(dest, "HelloApp", "com.example.hello-app")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["project"] == "HelloApp.xcodeproj" and out["scheme"] == "HelloApp"
    files = tree(dest)
    assert set(files) == {
        ".gitignore",
        "HelloApp.xcodeproj/project.pbxproj",
        "HelloApp.xcodeproj/xcshareddata/xcschemes/HelloApp.xcscheme",
        "HelloApp/HelloAppApp.swift",
        "HelloApp/ContentView.swift",
    }
    assert sorted(out["files"]) == sorted(files)
    for rel, data in files.items():
        assert not PLACEHOLDER.search(rel), rel
        assert not PLACEHOLDER.search(data.decode()), rel
    pbx = files["HelloApp.xcodeproj/project.pbxproj"].decode()
    assert 'PRODUCT_BUNDLE_IDENTIFIER = "com.example.hello-app";' in pbx
    assert "path = HelloApp;" in pbx and "path = HelloApp.app;" in pbx
    assert "struct HelloAppApp: App" in files["HelloApp/HelloAppApp.swift"].decode()


def test_pbxproj_is_self_consistent(tmp_path):
    assert make(tmp_path / "r").returncode == 0
    pbx = (tmp_path / "r/HelloApp.xcodeproj/project.pbxproj").read_text()
    assert pbx.count("{") == pbx.count("}") and pbx.count("(") == pbx.count(")")
    defined = set(re.findall(r"^\t\t([0-9A-F]{24}) (?:/\*.*?\*/ )?= \{", pbx, re.M))
    used = set(re.findall(r"\b([0-9A-F]{24})\b", pbx))
    assert used == defined and len(defined) >= 12
    root = re.search(r"rootObject = ([0-9A-F]{24})", pbx).group(1)
    assert root in defined
    target = re.search(r"([0-9A-F]{24}) /\* HelloApp \*/ = \{\n\t\t\tisa = PBXNativeTarget;", pbx).group(1)
    scheme = (tmp_path / "r/HelloApp.xcodeproj/xcshareddata/xcschemes/HelloApp.xcscheme").read_text()
    assert set(re.findall(r'BlueprintIdentifier = "([0-9A-F]{24})"', scheme)) == {target}
    assert 'ReferencedContainer = "container:HelloApp.xcodeproj"' in scheme
    assert 'BuildableName = "HelloApp.app"' in scheme and 'BlueprintName = "HelloApp"' in scheme


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS-only")
def test_pbxproj_parses_as_a_property_list(tmp_path):
    assert make(tmp_path / "r").returncode == 0
    r = subprocess.run(["plutil", "-lint", str(tmp_path / "r/HelloApp.xcodeproj/project.pbxproj"),
                        str(tmp_path / "r/HelloApp.xcodeproj/xcshareddata/xcschemes/HelloApp.xcscheme")],
                       capture_output=True, text=True)
    # plutil lints the old-style pbxproj; the scheme is XML but not a plist, so only the first must pass
    assert "project.pbxproj: OK" in r.stdout, r.stdout + r.stderr


def test_output_is_deterministic(tmp_path):
    assert make(tmp_path / "a").returncode == 0 and make(tmp_path / "b").returncode == 0
    assert tree(tmp_path / "a") == tree(tmp_path / "b")
    assert make(tmp_path / "c", "OtherApp", "org.example.other").returncode == 0
    c = tree(tmp_path / "c")
    assert c != tree(tmp_path / "a")
    digest = lambda t: hashlib.sha256(json.dumps({k: v.decode() for k, v in t.items()}, sort_keys=True).encode())
    assert digest(tree(tmp_path / "a")).hexdigest() == digest(tree(tmp_path / "b")).hexdigest()


def test_file_modes_do_not_depend_on_the_template_or_umask(tmp_path):
    assert make(tmp_path / "r").returncode == 0
    for p in (tmp_path / "r").rglob("*"):
        if p.is_file():
            assert p.stat().st_mode & 0o777 == 0o644, p


def test_existing_empty_repo_dir_is_fine_but_nothing_is_overwritten(tmp_path):
    dest = tmp_path / "repo"
    (dest / ".git").mkdir(parents=True)
    (dest / "LICENSE").write_text("MIT\n")
    assert make(dest).returncode == 0
    assert (dest / "LICENSE").read_text() == "MIT\n"
    before = tree(dest)
    r = make(dest)                                    # second run would overwrite: refused, nothing changed
    assert r.returncode != 0 and "already exists" in r.stderr
    assert tree(dest) == before

    dest2 = tmp_path / "repo2"
    dest2.mkdir()
    (dest2 / ".gitignore").write_text("mine\n")
    r = make(dest2)
    assert r.returncode != 0 and ".gitignore" in r.stderr
    assert sorted(p.name for p in dest2.iterdir()) == [".gitignore"]   # refused before writing anything


def test_dest_must_be_a_directory(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    assert make(f).returncode != 0
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "elsewhere")
    assert make(link).returncode != 0
    assert not (tmp_path / "elsewhere").exists()


# ---------------------------------------------------------------- confinement (Review round 1 F1)
GOLDEN = "804de0cfa66cb7644dacc3ec33b890df8aba58edf2a63c758ef99c4ae8029485"


def golden_digest(root: Path) -> str:
    h = hashlib.sha256()
    for rel, data in sorted(tree(root).items()):
        h.update(rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def test_output_bytes_match_the_golden_digest(tmp_path):
    """The bytes Xcode 27.2 built for both destinations (HelloApp, com.example.helloapp)."""
    assert make(tmp_path / "r").returncode == 0
    assert golden_digest(tmp_path / "r") == GOLDEN


def run_inproc(dest, hook=None, name="HelloApp", bundle="com.example.helloapp", umask=None):
    mod = _load_script()
    mod._hook = hook
    old = os.umask(umask) if umask is not None else None
    try:
        return mod.main(["--name", name, "--bundle-id", bundle, str(dest)])
    finally:
        if old is not None:
            os.umask(old)


def outside_dir(tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    return out


@pytest.mark.parametrize("linked", ["HelloApp", "HelloApp.xcodeproj", "HelloApp.xcodeproj/xcshareddata"])
def test_an_existing_symlinked_output_directory_is_refused(tmp_path, linked):
    out = outside_dir(tmp_path)
    dest = tmp_path / "repo"
    (dest / linked).parent.mkdir(parents=True, exist_ok=True)
    (dest / linked).symlink_to(out, target_is_directory=True)
    r = make(dest)
    assert r.returncode != 0 and "symlink" in r.stderr
    assert list(out.iterdir()) == []
    assert not (dest / ".gitignore").exists()                           # refused before writing anything


def test_a_symlink_anywhere_in_dest_is_refused(tmp_path):
    real = tmp_path / "real"
    (real / "repo").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    r = make(tmp_path / "alias" / "repo")
    assert r.returncode == 2 and "symlink" in r.stderr
    assert list((real / "repo").iterdir()) == []


def test_dest_replaced_by_a_symlink_after_the_checks_gets_nothing(tmp_path):
    out = outside_dir(tmp_path)
    dest = tmp_path / "repo"
    dest.mkdir()

    def swap(stage, rel):
        if stage == "checked":
            dest.rename(tmp_path / "moved")
            dest.symlink_to(out, target_is_directory=True)
    run_inproc(dest, swap)
    assert list(out.iterdir()) == []
    assert (tmp_path / "moved" / "HelloApp" / "ContentView.swift").is_file()   # the pinned directory got it


def test_a_created_directory_replaced_by_a_symlink_mid_run_gets_nothing(tmp_path):
    out = outside_dir(tmp_path)
    dest = tmp_path / "repo"
    dest.mkdir()
    swapped = []

    def swap(stage, rel):
        # after HelloApp/ exists and holds one file, move it away and point the name outside
        if stage == "wrote" and rel.startswith("HelloApp/") and not swapped:
            (dest / "HelloApp").rename(tmp_path / "moved-app")
            (dest / "HelloApp").symlink_to(out, target_is_directory=True)
            swapped.append(rel)
    run_inproc(dest, swap)
    assert swapped and list(out.iterdir()) == []
    assert sorted(p.name for p in (tmp_path / "moved-app").iterdir()) == ["ContentView.swift", "HelloAppApp.swift"]


INNER_DIRS = ["HelloApp.xcodeproj", "HelloApp.xcodeproj/xcshareddata",
              "HelloApp.xcodeproj/xcshareddata/xcschemes", "HelloApp"]


def collide(dest, at="HelloApp/HelloAppApp.swift"):
    """A hook that plants the file `at` just before the writer creates it."""
    def plant(stage, rel):
        if stage == "before" and rel == at:
            (dest / at).write_text("theirs\n")
    return plant


def chain(*hooks):
    def run(stage, rel):
        for h in hooks:
            h(stage, rel)
    return run


def test_a_file_that_appears_mid_run_stops_the_run_deletes_nothing_and_lists_what_was_written(tmp_path, capsys):
    dest = tmp_path / "repo"
    dest.mkdir()
    assert run_inproc(dest, collide(dest)) == 1
    assert (dest / "HelloApp" / "HelloAppApp.swift").read_text() == "theirs\n"
    written = [".gitignore", "HelloApp.xcodeproj/project.pbxproj",
               "HelloApp.xcodeproj/xcshareddata/xcschemes/HelloApp.xcscheme", "HelloApp/ContentView.swift"]
    for rel in written:
        assert (dest / rel).is_file(), rel                              # kept, not rolled back
    report = json.loads(capsys.readouterr().out)
    assert report["conflict"] == "HelloApp/HelloAppApp.swift" and report["written"] == written
    assert report["incomplete"] == []
    assert report["created_dirs"] == INNER_DIRS       # DEST existed already


def test_a_replacement_file_under_a_written_name_survives_a_late_collision(tmp_path):
    dest = tmp_path / "repo"
    dest.mkdir()
    keep = tmp_path / "keep"
    keep.write_text("must keep\n")

    def replace(stage, rel):
        if stage == "wrote" and rel == ".gitignore":
            (dest / ".gitignore").unlink()
            keep.rename(dest / ".gitignore")
    assert run_inproc(dest, chain(replace, collide(dest))) == 1
    assert (dest / ".gitignore").read_text() == "must keep\n"


def test_a_replacement_directory_under_a_created_name_survives_a_late_collision(tmp_path):
    dest = tmp_path / "repo"
    dest.mkdir()
    theirs = tmp_path / "theirs"
    theirs.mkdir()

    def replace(stage, rel):
        if stage == "before" and rel == "HelloApp.xcodeproj/project.pbxproj":
            (dest / "HelloApp.xcodeproj").rename(tmp_path / "moved")
            theirs.rename(dest / "HelloApp.xcodeproj")
            (tmp_path / "moved" / "project.pbxproj").write_text("planted\n")  # collides via the held descriptor
    assert run_inproc(dest, replace) == 1
    assert (dest / "HelloApp.xcodeproj").is_dir() and list((dest / "HelloApp.xcodeproj").iterdir()) == []


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("name", [
    "", "a", "hello", "1App", "_App", ".App", "..", "../Evil", "A/B", "A\\B", "My App", "My-App", "My.App",
    "App;rm", "App$(x)", "App`x`", "App|x", "App&x", "App>x", "App'x", 'App"x', "App\nx", "App\x00", "Äpp",
    "App*", "App?", "App~", "A" * 51,
    "SwiftUI", "swiftui", "Foundation", "App", "ContentView", "UIKit", "AppKit", "Swift",
])
def test_unsafe_or_reserved_names_are_refused(tmp_path, name):
    if "\x00" in name:                                  # argv cannot carry NUL; check the function directly
        mod = _load_script()
        with pytest.raises(ValueError):
            mod.check_name(name)
        return
    r = make(tmp_path / "out", name)
    assert r.returncode == 2, (name, r.stdout, r.stderr)
    assert not (tmp_path / "out").exists()
    assert r.stdout == ""


@pytest.mark.parametrize("name", ["Hi", "HelloApp", "Todo2", "A" * 50, "MyCoolApp"])
def test_good_names_are_accepted(tmp_path, name):
    assert make(tmp_path / "out", name).returncode == 0


@pytest.mark.parametrize("bundle", [
    "", "com", "com.", ".com.x", "com..x", "com.example.my app", "com/example", "com.example.$x", "com.example.x;y",
    "com.example.ü", "com.example.a_b", "com.example.-x", "com.example." + "a" * 200, "com.example.x\n",
])
def test_bad_bundle_ids_are_refused(tmp_path, bundle):
    r = make(tmp_path / "out", "HelloApp", bundle)
    assert r.returncode == 2, (bundle, r.stdout, r.stderr)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("bundle", ["com.example.helloapp", "io.github.some-user.Hello2", "dev.x"])
def test_good_bundle_ids_are_accepted(tmp_path, bundle):
    assert make(tmp_path / "out", "HelloApp", bundle).returncode == 0


# ---------------------------------------------------------------- grant hint for the owner
def test_repo_gives_the_exact_grant_command_for_the_owner(tmp_path):
    repo = "https://github.com/someone/hello-app.git"
    r = make(tmp_path / "r", "HelloApp", "com.example.helloapp", "--repo", repo)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["grant_command"] == (
        f"bh grant add xcode.build --workspace {out['workspace']} --repo https://github.com/someone/hello-app.git "
        "--project HelloApp.xcodeproj --scheme HelloApp --configuration Debug "
        "--destination 'platform=macOS,arch=arm64' --destination 'generic/platform=iOS Simulator'")
    assert out["destinations"] == ["platform=macOS,arch=arm64", "generic/platform=iOS Simulator"]
    # without --repo there is no command to paste and no workspace name to suggest
    out = json.loads(make(tmp_path / "s").stdout)
    assert "grant_command" not in out and out["workspace"] is None and "--repo" in out["grant_note"]


@pytest.mark.parametrize("repo", [
    "http://github.com/a/b.git", "https://user:pw@github.com/a/b.git", "https://github.com/a/b.git?x=1",
    "https://github.com/a/b.git#f", "https://github.com:8443/a/b.git", "https://github.com/a/$(id).git",
    "https://github.com/a/b c.git", "https://github.com/a/b;rm.git", "git@github.com:a/b.git", "https://github.com",
    "https://github.com/a/../b.git", "file:///etc/passwd", "https://github.com/a/b.git\n",
])
def test_bad_repo_urls_are_refused(tmp_path, repo):
    r = make(tmp_path / "r", "HelloApp", "com.example.helloapp", "--repo", repo)
    assert r.returncode == 2, repo
    assert not (tmp_path / "r").exists()


# ---------------------------------------------------------------- wording the agent sees
def test_prompt_names_the_new_project_steps_and_stays_short():
    t = prompt.TEXT
    assert len(t) <= 4000
    new = t[t.index("To start a new app"):]
    for needle in ["new_swiftui_app.py", "$HERMES_HOME/plugins/borrowed-hands/templates", "public",
                   "bh/hermes/<task>", "grant_command", "grant_note", "only the owner", "Mac"]:
        assert needle in new, needle
    # create, then push, then the owner grants, then build
    assert (new.index("new_swiftui_app.py") < new.index("push it to bh/hermes/<task>") < new.index("grant_command")
            < new.index("only the owner can grant") < new.index("Then build"))
    assert "not you" in new                                                   # the agent never grants itself
    relay = new[new.index("Relay"):new.index("Then build")]                   # both go to the user, not only one
    assert "grant_command and grant_note" in relay and "to the user" in relay


def test_build_tool_description_points_at_the_template():
    assert "new_swiftui_app.py" in tools.HANDS_XCODE_BUILD["description"]


def test_autonomous_doc_covers_new_projects_and_keeps_the_division_of_labour():
    doc = (ROOT / "docs" / "AUTONOMOUS.md").read_text()
    assert ("GitHub operations use Hermes's existing Git tooling and a repository-scoped credential. Borrowed Hands "
            "handles Mac discovery, locally authorized Xcode execution, and structured build results.") in doc
    sec = doc[doc.index("## Starting a new app"):]
    for needle in ["templates/new_swiftui_app.py", "--bundle-id", "grant_command", "bh grant add xcode.build",
                   "public", "bh/hermes/"]:
        assert needle in sec, needle


def _load_script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("new_swiftui_app", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- permissions (Review round 1 F2)
@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077])
def test_files_are_0644_and_directories_follow_the_umask(tmp_path, umask):
    """Files get fchmod on their own O_EXCL descriptor; directories are never chmodded (mkdir 0755 minus umask)."""
    assert run_inproc(tmp_path / "r", umask=umask) == 0
    for p in (tmp_path / "r").rglob("*"):
        want = 0o644 if p.is_file() else 0o755 & ~umask
        assert p.stat().st_mode & 0o777 == want, (oct(umask), p)
    assert (tmp_path / "r").stat().st_mode & 0o777 == 0o755 & ~umask


def test_a_umask_that_makes_directories_unwritable_stops_cleanly(tmp_path, capsys):
    assert run_inproc(tmp_path / "r", umask=0o277) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "stopped" and report["written"] == [] and "ermission" in report["reason"]
    assert report["created_dirs"] == [str(tmp_path / "r")]                # DEST itself, by absolute path


def swap_in_real_dir(tmp_path, target: Path, mode=0o700):
    """Rename the entry the writer just made aside and move a pre-existing private directory into its place."""
    theirs = tmp_path / "theirs"
    theirs.mkdir()
    (theirs / "confidential").write_text("x\n")
    theirs.chmod(mode)

    def swap():
        target.rename(tmp_path / "made-by-run")
        theirs.rename(target)
    return swap


@pytest.mark.parametrize("which", ["dest", "app"])
def test_a_directory_swapped_in_after_mkdir_keeps_its_mode(tmp_path, which):
    dest = tmp_path / "repo"
    if which == "app":
        dest.mkdir()
    target = dest if which == "dest" else dest / "HelloApp"
    swap = swap_in_real_dir(tmp_path, target)
    want = str(dest) if which == "dest" else "HelloApp"

    def hook(stage, rel):
        if stage == "mkdir" and rel == want:
            swap()
    run_inproc(dest, hook)
    assert target.stat().st_mode & 0o777 == 0o700
    assert (target / "confidential").read_text() == "x\n"


@pytest.mark.parametrize("which", ["dest", "app"])
def test_a_symlink_swapped_in_between_the_check_and_the_open_is_refused(tmp_path, which):
    """Only O_NOFOLLOW on the directory open stops this: the lstat check has already passed."""
    out = outside_dir(tmp_path)
    dest = tmp_path / "repo"
    if which == "app":
        dest.mkdir()
    target = dest if which == "dest" else dest / "HelloApp"
    want = str(dest) if which == "dest" else "HelloApp"
    seen = []

    def hook(stage, rel):
        if stage == "open" and rel == want and target.is_dir() and not target.is_symlink():
            target.rename(tmp_path / "made-by-run")
            target.symlink_to(out, target_is_directory=True)
            seen.append(rel)
    # dest: the run created DEST, so this is a stop with a report; app: .gitignore was already written
    assert run_inproc(dest, hook) == 1
    assert seen and list(out.iterdir()) == []


def test_a_written_file_swapped_for_a_symlink_never_changes_an_outside_mode(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("keep\n")
    secret.chmod(0o600)
    dest = tmp_path / "repo"
    dest.mkdir()

    def swap(stage, rel):
        # the moment after the file is created: any later chmod by name would follow this link
        if stage == "created" and rel == ".gitignore":
            (dest / ".gitignore").unlink()
            (dest / ".gitignore").symlink_to(secret)
    run_inproc(dest, swap)
    assert secret.stat().st_mode & 0o777 == 0o600
    assert secret.read_text() == "keep\n"


# ---------------------------------------------------------------- workspace names (Review round 1 F3)
WORKSPACE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")          # helper config.WORKSPACE_ID_RE


def workspace_for(tmp_path, repo, name="HelloApp", sub="r"):
    r = make(tmp_path / sub, name, "com.example.helloapp", "--repo", repo)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_workspace_is_per_repository_not_per_app_name(tmp_path):
    a = workspace_for(tmp_path, "https://github.com/someone/old-repo.git", sub="a")["workspace"]
    b = workspace_for(tmp_path, "https://github.com/someone/new-repo.git", sub="b")["workspace"]
    assert a != b and a.startswith("someone-old-repo-helloapp-") and b.startswith("someone-new-repo-helloapp-")
    assert workspace_for(tmp_path, "https://github.com/someone/old-repo.git", sub="c")["workspace"] == a


@pytest.mark.parametrize("pair", [
    ("https://github.com/a.b/c.git", "https://github.com/a-b/c.git"),       # same after sanitizing
    ("https://github.com/a/b.git", "https://gitlab.com/a/b.git"),           # same path, other host
    ("https://github.com/" + "o" * 60 + "/x.git", "https://github.com/" + "o" * 60 + "/y.git"),  # truncated alike
])
def test_lossy_or_truncated_repos_still_get_distinct_workspaces(tmp_path, pair):
    a = workspace_for(tmp_path, pair[0], sub="a")["workspace"]
    b = workspace_for(tmp_path, pair[1], sub="b")["workspace"]
    assert a != b
    assert WORKSPACE_RE.match(a) and WORKSPACE_RE.match(b)


def test_owner_is_told_to_check_existing_grants_first(tmp_path):
    out = workspace_for(tmp_path, "https://github.com/someone/hello-app.git")
    note = out["grant_note"]
    assert "bh grant list" in note and out["workspace"] in note and "--replace" in note
    assert "--confirm" in note and "--expires" in note


def test_printed_grant_adds_beside_an_existing_constrained_grant(tmp_path):
    """The real helper parser and config: the generated command must not replace a same-app grant elsewhere."""
    import shlex
    sys.path.insert(0, str(ROOT / "helper"))
    from borrowed_hands_helper import cli as hcli, config as hconfig
    old = hconfig.Workspace(id="helloapp", remote_url="https://github.com/someone/old-repo.git",
                            project="HelloApp.xcodeproj", schemes=("HelloApp",), configurations=("Debug",),
                            destinations=(hconfig.DESTINATIONS[0],), confirm=True, expires_at=4102444800000)
    cfg = hconfig.Config.empty("Mac").with_workspace(old)
    out = workspace_for(tmp_path, "https://github.com/someone/new-repo.git")
    argv = shlex.split(out["grant_command"])
    assert argv[0] == "bh"
    a = hcli._parser().parse_args(argv[1:])
    new = hconfig.Workspace(id=a.workspace, remote_url=a.repo, project=a.project, schemes=tuple(a.scheme),
                            configurations=tuple(a.configuration), destinations=tuple(a.destination),
                            confirm=a.confirm, expires_at=None)
    cfg = cfg.with_workspace(new)
    assert [w.id for w in cfg.workspaces] == ["helloapp", out["workspace"]]
    kept = cfg.workspace("helloapp")
    assert kept.confirm is True and kept.expires_at == 4102444800000
    assert kept.destinations == (hconfig.DESTINATIONS[0],)


def test_prompt_says_never_reuse_a_workspace_name():
    t = prompt.TEXT
    assert "existing workspace name" in t[t.index("To start a new app"):]
    assert len(t) <= 4000


def test_workspace_hash_keeps_path_case_like_the_helper_fingerprint(tmp_path):
    """The helper's repo fingerprint keeps path case (protocol/bhproto.py normalize_repo_url), so the hash must too:
    on a case-sensitive host Owner/Repo and owner/repo are different repositories."""
    upper = workspace_for(tmp_path, "https://git.example.com/Owner/Repo.git", sub="a")["workspace"]
    lower = workspace_for(tmp_path, "https://git.example.com/owner/repo.git", sub="b")["workspace"]
    assert upper != lower and upper.startswith("owner-repo-") and lower.startswith("owner-repo-")
    # spellings the helper treats as one repository get one name: host case and a .git suffix
    same = {workspace_for(tmp_path, u, sub=f"s{i}")["workspace"] for i, u in enumerate([
        "https://GIT.example.com/Owner/Repo.git", "https://git.example.com/Owner/Repo"])}
    assert same == {upper}
    from borrowed_hands import bhproto
    for url, ws in [("https://git.example.com/Owner/Repo.git", upper),
                    ("https://git.example.com/owner/repo.git", lower)]:
        key = bhproto.normalize_repo_url(url) + "\0" + "HelloApp.xcodeproj"
        assert ws.endswith("-" + hashlib.sha256(key.encode()).hexdigest()[:6])



# ---------------------------------------------------------------- one failure boundary (Review round 3)
def stop_report(capsys):
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    report = json.loads(captured.out)
    assert report["error"] == "stopped"
    return report


def test_new_dest_and_parent_are_in_created_dirs(tmp_path, capsys):
    dest = tmp_path / "new-parent" / "dest"
    assert run_inproc(dest, collide(dest)) == 1
    report = stop_report(capsys)
    assert report["created_dirs"] == [str(tmp_path / "new-parent"), str(dest)] + INNER_DIRS


def test_a_late_directory_refusal_after_output_still_reports(tmp_path, capsys):
    out = outside_dir(tmp_path)
    dest = tmp_path / "repo"
    dest.mkdir()

    def link(stage, rel):
        if stage == "wrote" and rel == ".gitignore":
            (dest / "HelloApp.xcodeproj").symlink_to(out, target_is_directory=True)
    assert run_inproc(dest, link) == 1
    report = stop_report(capsys)
    assert report["conflict"] == "HelloApp.xcodeproj/project.pbxproj" and "symlink" in report["reason"]
    assert report["written"] == [".gitignore"] and report["incomplete"] == [] and report["created_dirs"] == []
    assert list(out.iterdir()) == []


def test_an_fchmod_denial_reports_the_file_as_incomplete(tmp_path, capsys, monkeypatch):
    mod = _load_script()

    def deny(fd, mode):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(mod.os, "fchmod", deny)
    mod._hook = None
    dest = tmp_path / "repo"
    dest.mkdir()
    assert mod.main(["--name", "HelloApp", "--bundle-id", "com.example.helloapp", str(dest)]) == 1
    monkeypatch.undo()
    report = stop_report(capsys)
    assert report["conflict"] == ".gitignore" and report["incomplete"] == [".gitignore"] and report["written"] == []
    assert (dest / ".gitignore").exists()                               # kept, never cleaned up by name


def test_a_real_write_failure_reports_the_truncated_file_as_incomplete(tmp_path):
    resource = pytest.importorskip("resource")
    import signal

    def limit():
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))
    dest = tmp_path / "repo"
    try:
        r = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--name", "HelloApp", "--bundle-id",
                            "com.example.helloapp", str(dest)], capture_output=True, text=True, timeout=60,
                           preexec_fn=limit)
    except (OSError, subprocess.SubprocessError) as e:
        pytest.skip(f"cannot set RLIMIT_FSIZE here: {e}")
    if r.returncode == 0:
        pytest.skip("RLIMIT_FSIZE was not enforced here")
    assert r.returncode == 1 and "Traceback" not in r.stderr, r.stderr
    report = json.loads(r.stdout)
    assert report["written"] == [".gitignore"]
    assert report["incomplete"] == ["HelloApp.xcodeproj/project.pbxproj"]
    assert report["conflict"] == "HelloApp.xcodeproj/project.pbxproj"
    assert (dest / "HelloApp.xcodeproj/project.pbxproj").stat().st_size <= 1024   # truncated, kept, reported


def test_docs_say_the_report_is_history_not_authority_and_pin_dest_normalization(tmp_path):
    src = SCRIPT.read_text()
    doc = (ROOT / "docs" / "AUTONOMOUS.md").read_text()
    for text in (src, doc):
        flat = " ".join(text.split())
        assert "history" in flat and "not proof of what is at those paths now" in flat
        assert "must not be used as authority to delete" in flat
    assert "folds `..` away" in " ".join(src.split())
    # behaviour that sentence describes: link/.. is folded away before the walk, so it is never checked
    (tmp_path / "link").symlink_to(tmp_path / "nowhere")
    assert make(tmp_path / "link" / ".." / "dest").returncode == 0
    assert (tmp_path / "dest" / "HelloApp.xcodeproj").is_dir()



# ---------------------------------------------------------------- one outcome, delivered first (Review round 4)
# Each case runs in its own interpreter, so broken fds and shutdown flushes are real. The child loads the script,
# sets up the case, calls main() and exits with its return code.
CHILD = r"""
import errno, importlib.util, os, stat, sys
script, case, base = sys.argv[1:]
spec = importlib.util.spec_from_file_location("t", script)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
dest = os.path.join(base, "dest")
state = {}
real_close = os.close
COLLIDE = {"close-masks-stop", "stderr-full", "stderr-closed", "stderr-pipe", "stdout-closed", "stdout-pipe",
           "stdout-stream-closed", "rlimit-stderr-closed"}

def hook(stage, rel):
    if stage == "before" and rel == "HelloApp/HelloAppApp.swift" and case in COLLIDE:
        with open(os.path.join(dest, rel), "w") as f:
            f.write("keep\n")
    if stage == "wrote" and rel == ".gitignore" and case == "non-oserror":
        raise RuntimeError("not an OSError")
    if stage == "wrote" and (rel == "HelloApp/HelloAppApp.swift" or
                             (case == "close-masks-stop" and rel == "HelloApp/ContentView.swift")):
        state["fail_close"] = True
mod._hook = hook

if case in ("close-final-dir", "close-masks-stop"):
    def bad_close(fd):
        if state.get("fail_close") and stat.S_ISDIR(os.fstat(fd).st_mode):
            state["fail_close"] = False
            real_close(fd)
            raise OSError(errno.EIO, "injected directory close failure")
        real_close(fd)
    os.close = bad_close
if case == "rlimit-stderr-closed":
    import resource, signal
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))
if case in ("stderr-closed", "stdout-closed", "rlimit-stderr-closed", "success-stdout-closed"):
    real_close(1 if "stdout" in case else 2)
if case in ("stderr-pipe", "stdout-pipe", "success-stdout-pipe"):
    r, w = os.pipe()
    real_close(r)
    os.dup2(w, 1 if "stdout" in case else 2)
    real_close(w)
if case == "stdout-stream-closed":
    sys.stdout.close()
if case == "stderr-full":
    r, w = os.pipe()                    # a stderr pipe nobody reads, already full: any write to it blocks
    os.set_blocking(w, False)
    try:
        while True:
            os.write(w, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(w, True)
    os.dup2(w, 2)
sys.exit(mod.main(["--name", "HelloApp", "--bundle-id", "com.example.helloapp", dest]))
"""


def run_case(tmp_path, case):
    base = tmp_path / case
    base.mkdir()
    r = subprocess.run([sys.executable, "-I", "-B", "-c", CHILD, str(SCRIPT), case, str(base)],
                       capture_output=True, text=True, timeout=60)
    for noise in ("Traceback", "Exception ignored"):
        assert noise not in r.stderr, (case, r.stderr)
    return r, base / "dest"


ALL_FILES = [".gitignore", "HelloApp.xcodeproj/project.pbxproj",
             "HelloApp.xcodeproj/xcshareddata/xcschemes/HelloApp.xcscheme", "HelloApp/ContentView.swift",
             "HelloApp/HelloAppApp.swift"]


def test_a_directory_close_failure_after_success_is_a_stop_with_everything_written(tmp_path):
    r, _ = run_case(tmp_path, "close-final-dir")
    assert r.returncode == 1
    report = json.loads(r.stdout)
    assert report["error"] == "stopped" and "injected directory close failure" in report["reason"]
    assert report["written"] == ALL_FILES and report["incomplete"] == []


def test_a_close_failure_never_masks_the_primary_stop(tmp_path):
    r, dest = run_case(tmp_path, "close-masks-stop")
    assert r.returncode == 1
    report = json.loads(r.stdout)
    assert report["conflict"] == "HelloApp/HelloAppApp.swift" and report["reason"] == "appeared during the run"
    assert (dest / "HelloApp/HelloAppApp.swift").read_text() == "keep\n"


def test_any_exception_after_creation_is_a_stop(tmp_path):
    r, _ = run_case(tmp_path, "non-oserror")
    assert r.returncode == 1
    report = json.loads(r.stdout)
    assert report["written"] == [".gitignore"] and "not an OSError" in report["reason"]
    assert report["conflict"] == ".gitignore"                       # converted where it happened, in write()


@pytest.mark.parametrize("case", ["stderr-closed", "stderr-pipe", "rlimit-stderr-closed"])
def test_a_broken_stderr_never_suppresses_the_report(tmp_path, case):
    r, _ = run_case(tmp_path, case)
    assert r.returncode == 1, (case, r.returncode)
    report = json.loads(r.stdout)
    assert report["error"] == "stopped"
    if case == "rlimit-stderr-closed":
        assert report["incomplete"] == ["HelloApp.xcodeproj/project.pbxproj"]


@pytest.mark.parametrize("case", ["stdout-closed", "stdout-pipe", "stdout-stream-closed"])
def test_an_unusable_stdout_still_exits_1_quietly(tmp_path, case):
    r, dest = run_case(tmp_path, case)
    assert r.returncode == 1, (case, r.returncode, r.stderr)
    assert (dest / ".gitignore").is_file()                              # nothing deleted


@pytest.mark.parametrize("case", ["success-stdout-closed", "success-stdout-pipe"])
def test_a_success_report_that_cannot_be_delivered_does_not_exit_0(tmp_path, case):
    r, dest = run_case(tmp_path, case)
    assert r.returncode == 1, (case, r.returncode, r.stderr)
    assert all((dest / f).is_file() for f in ALL_FILES)


def test_docs_say_delivery_needs_a_usable_stdout():
    for text in (SCRIPT.read_text(), (ROOT / "docs" / "AUTONOMOUS.md").read_text()):
        flat = " ".join(text.split())
        assert "delivered when stdout is usable" in flat and "exit code is still correct" in flat


def test_help_works_on_this_python():
    """_NoColor overrides a private argparse hook whose signature varies by version."""
    r = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--bundle-id" in r.stdout and "Traceback" not in r.stderr


def test_the_report_reaches_stdout_before_any_diagnostic(tmp_path):
    """With a full, unread stderr the diagnostic blocks forever; the stop JSON must already be on stdout."""
    base = tmp_path / "stderr-full"
    base.mkdir()
    out = tmp_path / "stdout.txt"
    with open(out, "wb") as fh:
        proc = subprocess.Popen([sys.executable, "-I", "-B", "-c", CHILD, str(SCRIPT), "stderr-full", str(base)],
                                stdout=fh, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    report = json.loads(out.read_text())
    assert report["error"] == "stopped" and report["conflict"] == "HelloApp/HelloAppApp.swift"



# ---------------------------------------------------------------- r5: template inventory, absent streams, file fd
def copy_generator(tmp_path) -> Path:
    """A private copy of the script and its template, so a test can damage the template."""
    import shutil as sh
    root = tmp_path / "gen"
    sh.copytree(SCRIPT.parent, root, ignore=sh.ignore_patterns("__pycache__"))
    return root


@pytest.mark.parametrize("damage", ["root-unreadable", "app-unreadable", "missing-file", "extra-file"])
def test_a_damaged_template_is_refused_before_anything_is_created(tmp_path, damage):
    root = copy_generator(tmp_path)
    tpl = root / "swiftui-app"
    target = {"root-unreadable": tpl, "app-unreadable": tpl / "__APP_NAME__"}.get(damage)
    if damage == "missing-file":
        (tpl / "__APP_NAME__" / "ContentView.swift").unlink()
    if damage == "extra-file":
        (tpl / "__APP_NAME__" / "Extra.swift").write_text("struct Extra {}\n")
    if target is not None:
        target.chmod(0)
    try:
        dest = tmp_path / "out"
        r = subprocess.run([sys.executable, "-I", "-B", str(root / "new_swiftui_app.py"), "--name", "HelloApp",
                            "--bundle-id", "com.example.helloapp", str(dest)], capture_output=True, text=True)
    finally:
        if target is not None:
            target.chmod(0o755)
    assert r.returncode == 1 and r.stdout == "" and "Traceback" not in r.stderr, (r.stdout, r.stderr)
    # an unreadable directory is named as such (traversal error), not only caught by the inventory check
    assert ("template unreadable" if target is not None else "template incomplete or altered") in r.stderr
    assert not dest.exists()


def run_with_fd_closed_at_start(tmp_path, fd, *args, limit=False):
    def pre():
        if limit:
            import resource
            import signal
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (1024, 1024))
        os.close(fd)
    other = {1: "stderr", 2: "stdout"}[fd]
    r = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), *args], capture_output=True, text=True,
                       preexec_fn=pre, timeout=60)
    return r, getattr(r, other)


@pytest.mark.parametrize("limit", [False, True])
def test_stdout_closed_before_python_starts_exits_1_without_a_traceback(tmp_path, limit):
    dest = tmp_path / "dest"
    r, err = run_with_fd_closed_at_start(tmp_path, 1, "--name", "HelloApp", "--bundle-id", "com.example.helloapp",
                                         str(dest), limit=limit)
    assert r.returncode == 1 and "Traceback" not in err and "Exception ignored" not in err, err
    assert (dest / ".gitignore").is_file()


def test_stderr_closed_before_python_starts_keeps_exit_codes(tmp_path):
    r, out = run_with_fd_closed_at_start(tmp_path, 2, "--name", "bad", "--bundle-id", "com.example.x",
                                         str(tmp_path / "d"))
    assert r.returncode == 2 and out == ""                              # refusal, nothing on stdout
    dest = tmp_path / "ok"
    r, out = run_with_fd_closed_at_start(tmp_path, 2, "--name", "HelloApp", "--bundle-id", "com.example.helloapp",
                                         str(dest))
    assert r.returncode == 0 and json.loads(out)["scheme"] == "HelloApp"


def open_fds() -> set:
    out = set()
    for name in os.listdir("/dev/fd"):
        try:
            os.fstat(int(name))
            out.add(int(name))
        except OSError:
            pass
    return out


@pytest.mark.parametrize("raised", [RuntimeError, KeyboardInterrupt])
def test_the_new_file_descriptor_is_closed_whatever_interrupts_it(tmp_path, capsys, raised):
    dest = tmp_path / "repo"

    def boom(stage, rel):
        if stage == "created" and rel == ".gitignore":
            raise raised("interrupted right after the exclusive create")
    before = open_fds()
    try:
        rc = run_inproc(dest, boom)
    except KeyboardInterrupt:
        rc = None
    assert open_fds() == before
    if raised is RuntimeError:
        assert rc == 1 and json.loads(capsys.readouterr().out)["incomplete"] == [".gitignore"]


@pytest.mark.parametrize("primary", ["write", "fchmod"])
def test_a_file_close_error_never_replaces_the_first_failure(tmp_path, capsys, monkeypatch, primary):
    import errno
    import stat as st
    mod = _load_script()
    mod._hook = None
    real_close, real_write = os.close, os.write

    def bad_write(fd, data):
        real_write(fd, bytes(data[:7]))
        raise OSError(errno.ENOSPC, "PRIMARY write failure")

    def bad_fchmod(fd, mode):
        raise OSError(errno.EPERM, "PRIMARY fchmod failure")

    def bad_close(fd):
        regular = st.S_ISREG(os.fstat(fd).st_mode)
        real_close(fd)
        if regular:
            raise OSError(errno.EIO, "SECONDARY file close failure")
    if primary == "write":
        monkeypatch.setattr(mod.os, "write", bad_write)
    else:
        monkeypatch.setattr(mod.os, "fchmod", bad_fchmod)
    monkeypatch.setattr(mod.os, "close", bad_close)
    dest = tmp_path / "repo"
    dest.mkdir()
    rc = mod.main(["--name", "HelloApp", "--bundle-id", "com.example.helloapp", str(dest)])
    monkeypatch.undo()
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == f"PRIMARY {primary} failure" and report["incomplete"] == [".gitignore"]


def test_a_write_that_makes_no_progress_is_a_stop_not_a_spin(tmp_path, capsys, monkeypatch):
    mod = _load_script()
    mod._hook = None
    monkeypatch.setattr(mod.os, "write", lambda fd, data: 0)
    dest = tmp_path / "repo"
    dest.mkdir()
    rc = mod.main(["--name", "HelloApp", "--bundle-id", "com.example.helloapp", str(dest)])
    monkeypatch.undo()
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert "no progress" in report["reason"] and report["incomplete"] == [".gitignore"]


def test_a_file_close_error_after_a_complete_write_is_a_stop(tmp_path, capsys, monkeypatch):
    import errno
    import stat as st
    mod = _load_script()
    mod._hook = None
    real_close = os.close

    def bad_close(fd):
        regular = st.S_ISREG(os.fstat(fd).st_mode)
        real_close(fd)
        if regular:
            raise OSError(errno.EIO, "file close failure")
    monkeypatch.setattr(mod.os, "close", bad_close)
    dest = tmp_path / "repo"
    rc = mod.main(["--name", "HelloApp", "--bundle-id", "com.example.helloapp", str(dest)])
    monkeypatch.undo()
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == "file close failure" and report["incomplete"] == [".gitignore"]
    assert report["written"] == []


@pytest.mark.parametrize("late", ["refused", "exists"])
def test_a_refusal_after_the_files_exist_is_still_a_stop(tmp_path, capsys, monkeypatch, late):
    """Building the success report runs after write(); a Refused or FileExistsError there must not masquerade
    as an up-front refusal (exit 2 / 'refused:') once this run has created files."""
    mod = _load_script()
    mod._hook = None

    def fail(workspace):
        raise mod.Refused("late refusal") if late == "refused" else FileExistsError("late collision")
    monkeypatch.setattr(mod, "grant_note", fail)
    rc = mod.main(["--name", "HelloApp", "--bundle-id", "com.example.helloapp", str(tmp_path / "d")])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"] == "stopped" and report["written"] == ALL_FILES



# ---------------------------------------------------------------- Security review round 1 F1: same-repository grants
def test_two_apps_in_one_repository_get_different_workspaces(tmp_path):
    repo = "https://github.com/someone/apps.git"
    hello = workspace_for(tmp_path, repo, name="HelloApp", sub="h")["workspace"]
    todo = workspace_for(tmp_path, repo, name="TodoApp", sub="t")["workspace"]
    assert hello != todo and hello.startswith("someone-apps-helloapp-") and todo.startswith("someone-apps-todoapp-")
    assert WORKSPACE_RE.match(hello) and WORKSPACE_RE.match(todo)


def test_the_note_stops_the_owner_from_replacing_a_same_repository_grant(tmp_path):
    """Regenerating for the same repository and project prints the same workspace. Run as printed, the helper
    refuses and changes nothing; only --replace replaces the owner's grant (and would drop --confirm and --expires).
    The note says so to the owner, and says what a second grant under another name does not carry over."""
    import shlex
    sys.path.insert(0, str(ROOT / "helper"))
    from borrowed_hands_helper import cli as hcli, config as hconfig
    out = workspace_for(tmp_path, "https://github.com/someone/apps.git")
    ws = out["workspace"]
    existing = hconfig.Workspace(id=ws, remote_url="https://github.com/someone/apps.git",
                                 project="HelloApp.xcodeproj", schemes=("HelloApp",), configurations=("Debug",),
                                 destinations=(hconfig.DESTINATIONS[0],), confirm=True, expires_at=4102444800000)
    cfg = hconfig.Config.empty("Mac").with_workspace(existing)
    a = hcli._parser().parse_args(shlex.split(out["grant_command"])[1:])
    printed = hconfig.Workspace(
        id=a.workspace, remote_url=a.repo, project=a.project, schemes=tuple(a.scheme),
        configurations=tuple(a.configuration), destinations=tuple(a.destination), confirm=a.confirm,
        expires_at=None)
    assert a.replace is False
    with pytest.raises(hconfig.GrantExists):                                  # as printed: nothing changes
        cfg.with_workspace(printed)
    replaced = cfg.with_workspace(printed, replace_existing=True).workspace(ws)
    assert replaced.confirm is False and replaced.expires_at is None          # what --replace would drop
    flat = " ".join(out["grant_note"].split())
    for needle in ["the Mac's owner", "bh grant list", f"If {ws} is already listed",
                   "never changes an existing grant", "changes nothing", "add --replace only to replace it on purpose",
                   "Another --workspace name adds a second, separate grant",
                   "pass --confirm and --expires", "Only the owner can grant"]:
        assert needle in flat, needle


def test_docs_make_the_owner_check_the_control():
    doc = " ".join((ROOT / "docs" / "AUTONOMOUS.md").read_text().split())
    assert "never suggests an existing workspace name" not in doc
    assert "accidental reuse unlikely" in doc and "never changes an existing grant" in doc
    assert "adds a second, separate grant" in doc and "`--confirm` and `--expires`" in doc


# ---------------------------------------------------------------- Security review round 1 F2: a note that can be followed
def test_the_no_repo_note_gives_an_instruction_that_works(tmp_path):
    first = json.loads(make(tmp_path / "first").stdout)
    note = " ".join(first["grant_note"].split())
    assert "same --name and --bundle-id" in note and "--repo" in note and "new empty directory" in note
    assert "nothing in this one needs to change" in note
    # following it: same arguments plus --repo, in a new empty directory, works and matches the first output
    again = make(tmp_path / "again", "HelloApp", "com.example.helloapp", "--repo",
                 "https://github.com/someone/hello-app.git")
    assert again.returncode == 0 and golden_digest(tmp_path / "again") == golden_digest(tmp_path / "first")


def test_notes_and_docstring_say_project_is_relative_to_the_repository_root(tmp_path):
    out = workspace_for(tmp_path, "https://github.com/someone/hello-app.git")
    for text in (out["grant_note"], SCRIPT.read_text()):
        flat = " ".join(text.split())
        assert "only when DEST is the repository root" in flat


# ---------------------------------------------------------------- Security review round 1 F3: the server's host rule
NEVER_PUBLIC = ["127.0.0.1", "10.0.0.5", "localhost", "intranet", "169.254.169.254", "github.com.", "a-"]


@pytest.mark.parametrize("host", NEVER_PUBLIC)
def test_hosts_the_server_would_refuse_are_refused(tmp_path, host):
    r = make(tmp_path / "r", "HelloApp", "com.example.helloapp", "--repo", f"https://{host}/o/r")
    assert r.returncode == 2 and "public" in r.stderr
    assert not (tmp_path / "r").exists()


def test_host_rule_matches_refs_ascii_host():
    from borrowed_hands import refs
    mod = _load_script()
    hosts = NEVER_PUBLIC + ["github.com", "Git-Hub.Example.com", "gitlab.example.co.uk", "xn--bcher-kva.example",
                            "a.b", "1.example.com", "example.c0m", "example.123", "-a.com", "a..com",
                            "a" * 64 + ".com",
                            "x.y-", "example.xn--p1ai"]
    for host in hosts:
        try:
            refs.ascii_host(host)
            server = True
        except refs.RefsError:
            server = False
        ours = bool(mod.REPO_RE.match(f"https://{host}/o/r")) and mod.public_host(host)
        assert ours == server, (host, ours, server)


# ---------------------------------------------------------------- Security review round 1 F5: a missing script is a stop
def test_prompt_quotes_hermes_home_and_stops_if_the_script_is_missing():
    t = prompt.TEXT
    new = t[t.index("To start a new app"):]
    assert '`python3 "$HERMES_HOME/plugins/borrowed-hands/templates/new_swiftui_app.py"' in new
    assert "If that file is missing, stop and tell the user." in new
    assert len(t) <= 4000


def test_docs_pin_the_fallback_copy_to_the_installed_version():
    doc = " ".join((ROOT / "docs" / "AUTONOMOUS.md").read_text().split())
    assert "https://github.com/alext8900/borrowed-hands" in doc
    assert "at the commit of the installed plugin version" in doc


# ---------------------------------------------------------------- Security review round 1 F6: catalog rule 13 disclosure
DISCLOSURE = ("The system prompt section tells the agent to run the bundled, standard-library-only "
              "`templates/new_swiftui_app.py` with its own shell tool to write a new Xcode project into its working "
              "directory. The script starts no process, makes no network call, overwrites and deletes nothing, and "
              "prints a `bh grant add` command that only the Mac's owner can run.")


def test_readme_discloses_the_bundled_script():
    readme = " ".join((PLUGIN_DIR / "README.md").read_text().split())
    assert DISCLOSURE in readme
    disclosures = readme[readme.index("## Disclosures (rule 13)"):readme.index("What the separate Mac install does")]
    assert DISCLOSURE in disclosures
