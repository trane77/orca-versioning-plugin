"""Self-check of the core (no OrcaSlicer needed): python test_orca_versioning.py"""
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import orca_versioning as ov


def make_3mf(settings: dict, stamp: str, pauses="<pauses/>", mesh="<mesh v='8'/>") -> bytes:
    buf = io.BytesIO()
    entries = {
        "Metadata/project_settings.config": json.dumps(settings),
        "Metadata/model_settings.config": "<config/>",
        "Metadata/custom_gcode_per_layer.xml": pauses,
        "Metadata/plate_1.png": b"\x89PNG" + stamp.encode(),  # ignored
        "3D/Objects/object_1.model": mesh,
    }
    with zipfile.ZipFile(buf, "w") as z:
        z.comment = stamp.encode()  # makes the 3MF bytes differ on every export, like real zip timestamps
        for name, data in entries.items():  # fixed date: same args → same bytes, whatever the clock says
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
    return buf.getvalue()


def version(repo, settings, stamp, dest="file", **kw):
    files = ov.files_from_3mf(make_3mf(settings, stamp, **kw))
    files["model.txt"] = b"object 'cube'\n"
    return ov.commit_version(repo, files, "Export cube.gcode", {"Destination": dest})


SANDBOX = None  # mimics OrcaSlicer: paths with denied keywords are blocked, permissions or not


def _sandbox(event, args):
    if SANDBOX and args and isinstance(args[0], (str, os.PathLike)):
        p = os.fspath(args[0])
        if p.startswith(SANDBOX) and re.search(ov.DENIED_WORDS, p[len(SANDBOX):], re.IGNORECASE):
            raise PermissionError(f"sandbox: {event} {p}")


sys.addaudithook(_sandbox)

with tempfile.TemporaryDirectory() as d:
    SANDBOX = d
    repo = ov.open_repo(Path(d) / "p.git")
    base = {"layer_height": "0.2", "infill_density": "15%", "wall_loops": ["2"]}

    a = version(repo, base, "t1")
    assert a and ov.current_branch(repo) == b"refs/heads/main"
    # same content, different 3MF timestamp and trailer (export, then send) → no new version
    assert version(repo, base, "t2", dest="printer:print_job") is None

    b = version(repo, {**base, "layer_height": "0.16"}, "t3")
    assert "1 settings changed: layer_height" in ov.history(repo)[0]["message"]
    d_ab = ov.diff(repo, a, b)
    assert "-layer_height = 0.2" in d_ab and "+layer_height = 0.16" in d_ab, d_ab
    assert 'wall_loops = ["2"]' not in d_ab  # unchanged keys don't show up

    # branch from a, commit there; main stays on b
    ov.branch_create(repo, "petg", a)
    ov.branch_switch(repo, "petg")
    c = version(repo, {**base, "infill_density": "30%"}, "t4")
    assert [h["sha"] for h in ov.history(repo)] == [c, a]
    assert [h["sha"] for h in ov.history(repo, "main")] == [b, a]
    assert ov.branches(repo) == ["main", "petg"]

    ov.tag_create(repo, "v1", b)
    assert ov.history(repo, "main")[0]["tags"] == ["v1"]
    try:
        ov.tag_create(repo, "v1", c)
        raise AssertionError("duplicate tag accepted")
    except ValueError:
        pass

    # edits that live only inside the 3MF (pause at a layer, geometry/painting) are versions too
    # branches aren't in the UI for now: reopening a repo puts HEAD back on main
    repo.close()
    repo = ov.open_repo(Path(d) / "p.git")
    assert ov.current_branch(repo) == b"refs/heads/main"
    p = version(repo, {**base, "layer_height": "0.16"}, "t5", pauses="<pause z='5'/>")
    assert p and "+<pause z='5'/>" in ov.diff(repo, b, p)
    g = version(repo, {**base, "layer_height": "0.16"}, "t6", pauses="<pause z='5'/>", mesh="<mesh v='9'/>")
    assert g and "3D/Objects/object_1.model" in ov.diff(repo, p, g)

    for bad in ("", "has space", "a..b", "x~1"):
        try:
            ov.branch_create(repo, bad)
            raise AssertionError(f"invalid name accepted: {bad!r}")
        except ValueError:
            pass

    repos = Path(d) / "repos"
    ov.open_repo(repos / "untitled.git")
    ov.adopt_untitled(repos, "/x/cube.3mf")
    assert (repos / f"{ov.slug_for('/x/cube.3mf')}.git").exists() and not (repos / "untitled.git").exists()

    out = ov.extract_3mf(repo, b, Path(d) / "cube@v1.3mf")
    assert out.read_bytes() == make_3mf({**base, "layer_height": "0.16"}, "t3")
    assert ov.slug_for("") == "untitled" and ov.slug_for("/x/cube.3mf").startswith("cube-")
    if os.name == "nt":  # same file spelled differently → same history
        assert ov.slug_for(r"C:\x\Cube.3mf") == ov.slug_for("c:/X/Cube.3mf")

    # a repo that was used and closed can be renamed (Windows locks open files)
    with ov.open_repo(repos / "untitled.git") as u:
        version(u, base, "t7")
    ov.archive_untitled(repos)
    assert not (repos / "untitled.git").exists()

    # project names with sandbox-denied words still get a usable repo
    s = ov.slug_for("/x/Config Holder.3mf")
    assert not re.search(ov.DENIED_WORDS, s, re.IGNORECASE), s
    with ov.open_repo(repos / f"{s}.git") as r:
        assert version(r, base, "t8")

    # a repo left half-made by the old init (dirs, no HEAD, no config) is completed and usable
    half = repos / "half.git"
    for sub in ("objects/pack", "refs/heads", "hooks"):
        (half / sub).mkdir(parents=True)
    with ov.open_repo(half) as r:
        assert version(r, base, "t9") and ov.current_branch(r) == b"refs/heads/main"

# preset check, with serialized values as OrcaSlicer reports them (see the G-code config block)
ok_pipeline, ok_plugins = '"Version on export"', '"orca_versioning;;Version on export"'
assert ov.preset_warning(ok_pipeline, ok_plugins) is None
assert ov.preset_warning('"Version on export";"Other"', '"x;;Other";"orca_versioning;;Version on export"') is None
assert "add" in ov.preset_warning("", "")                      # plugin not picked in the preset
assert "add" in ov.preset_warning(None, None)                  # keys missing altogether
assert "add" in ov.preset_warning('"Other"', ok_plugins)
assert "OrcaSlicer bug" in ov.preset_warning(ok_pipeline, "")  # picked, but the manifest is empty

print("ok")
