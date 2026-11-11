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
    assert "1 setting changed: layer_height" in ov.history(repo)[0]["message"]
    assert ov.history(repo)[0]["summary"] == "1 setting changed: layer_height"
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

    # spaces become dashes (git names can't have them); other invalid names get a readable error
    ov.tag_create(repo, "  good  PETG ", a)
    assert ov.tags_by_sha(repo)[a] == ["good-PETG"]
    try:
        ov.tag_create(repo, "a..b", a)
        raise AssertionError("invalid tag accepted")
    except ValueError as e:
        assert "invalid tag name 'a..b'" in str(e)

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

# ---- structured views (Versions tab)
assert ov.trend("60", "70") == "up" and ov.trend('["0.2"]', '["0.16"]') == "down"
assert ov.trend("15%", "20%") == "up" and ov.trend('["1", "2"]', '["2", "3"]') == "up"
assert ov.trend('["1", "2"]', '["2", "1"]') == "changed" and ov.trend("PLA", "PETG") == "changed"
assert ov.trend(None, "1") == "added" and ov.trend("1", None) == "removed"
assert ov.display('["60"]') == "60" and ov.display('["PLA", "PETG"]') == "PLA, PETG" and ov.display("15%") == "15%"

# multi-line values (custom G-code) survive: read from the 3MF's JSON, not from settings.ini
gcode = "G28\nG1 Z5 ; lift\nM104 S200"
files = ov.files_from_3mf(make_3mf({"layer_height": "0.2", "machine_start_gcode": gcode}, "t"))
assert ov.settings_of(files)["machine_start_gcode"] == gcode
assert ov.parse_settings(files["settings.ini"])["machine_start_gcode"] == "G28"  # why settings_of exists

# legacy summaries drop the plugin's internal keys
pm = ov.parse_message("Export a.gcode — 3 settings changed: different_settings_to_system, wall_loops, plugins\n\n"
                      "Destination: file\nOutput: C:\\x\\a.gcode\n")
assert pm == {"title": "Export a.gcode", "summary": "1 setting changed: wall_loops",
              "trailers": {"Destination": "file", "Output": "C:\\x\\a.gcode"}}, pm


def project(settings, layer_z="5", pos="(110.0, 110.0, 11.0)", first_layer=146.9):
    f = ov.files_from_3mf(make_3mf(settings, "t"))
    f["model.txt"] = (
        "object 'Cubo' parts=1 facets=12\n"
        "  volume 'Cubo' type=ModelVolumeType.ModelPart extruder=1 facets=12 size=(22.0, 22.0, 22.0) "
        "offset=(0.0, 0.0, 0.0) rot=(0.0, -0.0, 0.0) scale=(1.0, 1.0, 1.0)\n"
        f"  instance offset={pos} rot=(0.0, 0.0, 1.5707963) scale=(0.5, 0.5, 0.5) "
        "mirror=(1.0, 1.0, 1.0) printable=True\n").encode()
    f["model_settings.config"] = (
        '<config><object id="2"><metadata key="name" value="Cubo"/><metadata key="extruder" value="1"/>'
        '<metadata key="wall_loops" value="4"/><part id="1"><metadata key="name" value="Cubo"/>'
        '<metadata key="matrix" value="1 0 0 0"/></part></object>'
        '<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value="Main"/>'
        '<model_instance><metadata key="object_id" value="2"/></model_instance></plate></config>').encode()
    f["plate_1.json"] = json.dumps({"bed_type": "hot_plate", "is_seq_print": False,
                                    "first_layer_time": first_layer}).encode()
    f["custom_gcode_per_layer.xml"] = (
        f'<custom_gcodes_per_layer><plate><plate_info id="1"/><layer top_z="{layer_z}" type="1" '
        'extruder="1" color="" extra="" gcode="M601"/></plate></custom_gcodes_per_layer>').encode()
    return f


base_settings = {"print_settings_id": "0.20mm Standard", "printer_settings_id": "K1C", "filament_settings_id": ["PLA"],
                 "wall_loops": "2", "layer_height": "0.2", "plugins": ["orca_versioning;;Version on export"],
                 "different_settings_to_system": ["wall_loops", "", ""]}
view = {s["id"]: s for s in ov.version_view(project(base_settings))}
rows = lambda s: {r["label"]: r["value"] for r in s["rows"]}
assert rows(view["presets"]) == {"Process": "0.20mm Standard", "Printer": "K1C", "Filament": "PLA"}
assert view["modified"]["rows"] == [{"key": "wall_loops", "label": "Wall loops", "value": "2", "group": "Process"}]
cubo = rows(view["object:Cubo"])
assert cubo["Size"] == "11 × 11 × 11 mm" and cubo["Position"] == "110 × 110 × 11 mm", cubo
assert cubo["Rotation"] == "0 × 0 × 90°" and cubo["Scale"] == "50 × 50 × 50 %" and cubo["Wall loops"] == "4", cubo
assert "Matrix" not in str(cubo) and "Name" not in cubo
assert rows(view["plate:1"]) == {"Name": "Main", "Objects": "Cubo", "Bed type": "Hot plate",
                                 "Print sequence": "By layer", "First layer time": "2m 27s"}
assert rows(view["layers"]) == {"Z 5 mm": "Pause"}
all_keys = {r["key"] for r in view["settings"]["rows"]}
assert "layer_height" in all_keys and "plugins" not in all_keys  # internal keys only in advanced mode
assert "geometry" not in view
adv = {s["id"]: s for s in ov.version_view(project(base_settings), advanced=True)}
assert "plugins" in {r["key"] for r in adv["settings"]["rows"]} and adv["geometry"]["rows"]

newer = {**base_settings, "wall_loops": "3", "layer_height": "0.16", "filament_settings_id": ["PETG"]}
cmp = {s["id"]: s for s in ov.compare_view(project(base_settings),
                                             project(newer, layer_z="7.5", pos="(100.0, 110.0, 11.0)", first_layer=120))}
pick = lambda sid: {r["label"]: (r["old"], r["new"], r["trend"]) for r in cmp[sid]["rows"]}
assert pick("settings") == {"Filament settings id": ("PLA", "PETG", "changed"),
                            "Layer height": ("0.2", "0.16", "down"), "Wall loops": ("2", "3", "up")}, pick("settings")
assert pick("presets") == {"Filament": ("PLA", "PETG", "changed")}
assert pick("object:Cubo") == {"Position": ("110 × 110 × 11 mm", "100 × 110 × 11 mm", "changed")}
assert pick("plate:1") == {"First layer time": ("2m 27s", "2m 0s", "down")}  # compared in seconds
assert pick("layers") == {"Z 7.5 mm": (None, "Pause", "added"), "Z 5 mm": ("Pause", None, "removed")}
assert "modified" not in cmp and "geometry" not in cmp  # meshes are the same
assert ov.compare_view(project(base_settings), project(base_settings)) == []

# ---- two plates: each has its own history (Cubo on plate 1, Cono on plate 2, as OrcaSlicer writes them)
def two_plates(settings, cono_x=374, cono_mesh="<mesh v='cono'/>", cubo_loops="4", stamp="t"):
    model = (
        '<?xml version="1.0" encoding="UTF-8"?><model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"><resources>'
        '<object id="2" type="model"><components><component p:path="/3D/Objects/Cubo_1.model" objectid="1"/></components></object>'
        '<object id="4" type="model"><components><component p:path="/3D/Objects/Cono_2.model" objectid="3"/></components></object>'
        f'</resources><build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 110 110 11"/>'
        f'<item objectid="4" transform="1 0 0 0 1 0 0 0 1 {cono_x} 110 11"/></build></model>')
    plate = lambda n, oid: (f'<plate><metadata key="plater_id" value="{n}"/><metadata key="plater_name" value="{"Cones" if n == 2 else ""}"/>'
                            f'<model_instance><metadata key="object_id" value="{oid}"/><metadata key="instance_id" value="0"/>'
                            '</model_instance></plate>')
    entries = {
        "Metadata/project_settings.config": json.dumps(settings),
        "Metadata/model_settings.config":
            f'<config><object id="2"><metadata key="name" value="Cubo"/><metadata key="wall_loops" value="{cubo_loops}"/></object>'
            '<object id="4"><metadata key="name" value="Cono"/></object>' + plate(1, 2) + plate(2, 4) + '</config>',
        "Metadata/plate_1.json": '{"bed_type":"hot_plate"}',
        "Metadata/plate_2.json": '{"bed_type":"cool_plate"}',
        "Metadata/filament_sequence.json": '{"plate_1":{"sequence":[]},"plate_2":{"sequence":[1]}}',
        "Metadata/custom_gcode_per_layer.xml":
            '<custom_gcodes_per_layer><plate><plate_info id="1"/><layer top_z="5" type="1"/></plate>'
            '<plate><plate_info id="2"/><layer top_z="9" type="0" extruder="1" color="#FF0000"/></plate></custom_gcodes_per_layer>',
        "Metadata/layer_heights_profile.txt": "object_id=1|0|0.2\nobject_id=2|0|0.1\n",
        "3D/3dmodel.model": model,
        "3D/Objects/Cubo_1.model": "<mesh v='cubo'/>",
        "3D/Objects/Cono_2.model": cono_mesh,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.comment = stamp.encode()
        for name, data in entries.items():
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
    f = ov.files_from_3mf(buf.getvalue())
    f["model.txt"] = (
        "object 'Cubo' parts=1 facets=12\n  volume 'Cubo' size=(22.0, 22.0, 22.0)\n"
        "  instance offset=(110.0, 110.0, 11.0) rot=(0.0, 0.0, 0.0) scale=(1.0, 1.0, 1.0) printable=True\n"
        "object 'Cono' parts=1 facets=330\n  volume 'Cono' size=(22.0, 22.0, 22.0)\n"
        f"  instance offset=({cono_x}.0, 110.0, 11.0) rot=(0.0, 0.0, 0.0) scale=(1.0, 1.0, 1.0) printable=True\n").encode()
    return f


s2 = {"layer_height": "0.2", "wall_loops": "2"}
p1, p2 = ov.plate_files(two_plates(s2), 1), ov.plate_files(two_plates(s2), 2)
assert "Cubo" in p1["model.txt"].decode() and "Cono" not in p1["model.txt"].decode()
assert "Cono" in p2["model.txt"].decode() and "Cubo" not in p2["model.txt"].decode()
assert "plate_2.json" not in p1 and "plate_1.json" not in p2 and p1["settings.ini"] == p2["settings.ini"]
assert "Cono_2" not in p1["geometry.sha1"].decode() and "Cubo_1" in p1["geometry.sha1"].decode()
assert json.loads(p2["filament_sequence.json"]) == {"plate_2": {"sequence": [1]}}
assert b'top_z="9"' in p2["custom_gcode_per_layer.xml"] and b'top_z="5"' not in p2["custom_gcode_per_layer.xml"]
assert p1["layer_heights_profile.txt"] == b"object_id=1|0|0.2\n"
assert p1["project.3mf"] == p2["project.3mf"]  # extracting restores the whole project
v2 = {s["id"]: s for s in ov.version_view(p2)}
assert "object:Cono" in v2 and "object:Cubo" not in v2 and "plate:2" in v2 and "plate:1" not in v2, list(v2)
assert rows(v2["plate:2"])["Objects"] == "Cono" and rows(v2["plate:2"])["Name"] == "Cones"
assert rows(v2["layers"]) == {"Z 9 mm": "Color change — extruder 1 #FF0000"}

with tempfile.TemporaryDirectory() as d:
    with ov.open_repo(Path(d) / "two.git") as r:
        ov.commit_version(r, {"settings.ini": b"old = 1\n"}, "Export old.gcode", {})  # pre-plates version, on main
        commit = lambda plate, **kw: ov.commit_version(r, ov.plate_files(two_plates(s2, **kw), plate), "Export",
                                                       {"Plate": str(plate)}, ref=ov.plate_ref(plate))
        c1, c2 = commit(1), commit(2)
        assert c1 and c2
        # Cono moved, re-meshed: a plate 2 version, not a plate 1 one
        assert commit(1, cono_x=300, cono_mesh="<mesh v='cono2'/>", stamp="t2") is None
        c2b = commit(2, cono_x=300, cono_mesh="<mesh v='cono2'/>", stamp="t2")
        assert c2b and "3D/Objects/Cono_2.model" in ov.diff(r, c2, c2b)
        # Cubo's own setting: a plate 1 version only
        assert commit(2, cono_x=300, cono_mesh="<mesh v='cono2'/>", cubo_loops="5") is None
        assert commit(1, cono_x=300, cono_mesh="<mesh v='cono2'/>", cubo_loops="5")
        # a global setting: each plate gets it on its next export
        s2 = {**s2, "layer_height": "0.16"}
        assert commit(2, cono_x=300, cono_mesh="<mesh v='cono2'/>", cubo_loops="5")
        assert len(ov.history(r, "plate-1")) == 2 and len(ov.history(r, "plate-2")) == 3
        lines = ov.plate_histories(r, current=3)  # plate 3 just created: listed, no versions yet
        assert [(x["id"], x["count"], x["current"]) for x in lines] == [
            ("plate-1", 2, False), ("plate-2", 3, False), ("plate-3", 0, True), ("main", 1, False)], lines
        assert lines[1]["name"] == "Cones"

# preset check, with serialized values as OrcaSlicer reports them (see the G-code config block)
ok_pipeline, ok_plugins = '"Version on export"', '"orca_versioning;;Version on export"'
assert ov.preset_warning(ok_pipeline, ok_plugins) is None
assert ov.preset_warning('"Version on export";"Other"', '"x;;Other";"orca_versioning;;Version on export"') is None
assert "add" in ov.preset_warning("", "")                      # plugin not picked in the preset
assert "add" in ov.preset_warning(None, None)                  # keys missing altogether
assert "add" in ov.preset_warning('"Other"', ok_plugins)
assert "OrcaSlicer bug" in ov.preset_warning(ok_pipeline, "")  # picked, but the manifest is empty

print("ok")
