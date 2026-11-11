# /// script
# requires-python = ">=3.12"
# dependencies = ["dulwich>=0.22"]
#
# [tool.orcaslicer.plugin]
# name = "Orca Versioning"
# description = "Git-style history of every G-code export / print send: settings and model diffs, tags, 3MF restore."
# author = ""
# version = "0.1.0"
# ///
"""A version is committed on every G-code export and every send to the printer.

The triggers run on worker threads, but export_3mf_copy() only works on the UI thread. The worker
queues the request and wakes a hidden dock panel ("relay"). The panel echoes the message back, and its
on_message runs on the UI thread, which takes the snapshot and makes the commit.
"""
import ast
import difflib
import hashlib
import io
import json
import math
import os
import re
import sys
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from dulwich.config import ConfigFile
from dulwich.objects import Blob, Commit, Tree
from dulwich.refs import check_ref_format
from dulwich.repo import Repo

try:
    import orca
except ImportError:  # outside OrcaSlicer (tests): the core functions don't need it
    orca = None

DEFAULT_BRANCH = b"refs/heads/main"
BINARY = "project.3mf"  # for restoring; it gets new zip timestamps on every export, so it never decides "changed"
AUTHOR = b"Orca Versioning <orca@localhost>"
SKIP = (".png", ".gcode", ".md5", "/_rels/")
DENIED_WORDS = r"conf|secret|cert"  # the sandbox's denied path keywords (config, certs, ... included)


# ---------------------------------------------------------------- core (no orca dependency)

def files_from_3mf(data: bytes) -> dict:
    """Turn a project 3MF into the files of a commit.

    - settings.ini: project_settings.config (all resolved settings) as sorted `key = value` lines
    - every other text Metadata/* entry as-is (overrides, layer heights, pauses/color changes…)
    - geometry.sha1: hash of the 3D/* entries and binary metadata (meshes, painting, transforms)
    - project.3mf: the 3MF itself
    """
    files, hashes = {BINARY: data}, []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in sorted(z.namelist()):
            if name.endswith("/") or any(x in name for x in SKIP):
                continue
            raw = z.read(name)
            if name == "Metadata/project_settings.config":
                settings = json.loads(raw)
                lines = (f"{k} = {v if isinstance(v, str) else json.dumps(v)}" for k, v in sorted(settings.items()))
                files["settings.ini"] = ("\n".join(lines) + "\n").encode()
                continue
            try:
                raw.decode("utf-8")
                is_text = name.startswith("Metadata/") and name.count("/") == 1
            except UnicodeDecodeError:
                is_text = False
            if is_text:
                files[name.split("/", 1)[1]] = raw
            else:
                hashes.append(f"{hashlib.sha1(raw).hexdigest()}  {name}")
    files["geometry.sha1"] = ("\n".join(hashes) + "\n").encode()
    return files


NS_3MF = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02",
          "p": "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"}


def _meta(el) -> dict:
    return {m.get("key"): m.get("value", "") for m in el.findall("metadata")}


def plate_files(files: dict, plate: int) -> dict:
    """The part of a snapshot that belongs to one plate: what that plate's history versions.

    Kept: the global settings, the plate's objects (model.txt, model_settings.config, their meshes in
    geometry.sha1, layer height profiles), plate_<n>.json, its filament_sequence.json entry and its
    pauses/color changes. Dropped: everything about the other plates, so editing them doesn't
    create a version here. project.3mf stays whole: extracting a version restores the full project.

    Objects are linked through model_settings.config: <plate plater_id> → model_instance object_id
    → <object id> (same id as in 3D/3dmodel.model, whose components point at 3D/Objects/*.model).
    """
    try:
        root = ET.fromstring(files["model_settings.config"])
    except (KeyError, ET.ParseError):
        return dict(files)
    plates = root.findall("plate")
    mine = next((p for i, p in enumerate(plates, 1) if _meta(p).get("plater_id", str(i)) == str(plate)), None)
    if mine is None:
        return dict(files)
    keep = {}  # object id → instance indexes on this plate
    for inst in mine.findall("model_instance"):
        m = _meta(inst)
        keep.setdefault(m.get("object_id"), set()).add(int(m.get("instance_id", "0") or 0))
    objects = root.findall("object")
    order = [(o.get("id"), _meta(o).get("name", "")) for o in objects]  # same order as model.txt
    for o in objects:
        if o.get("id") not in keep:
            root.remove(o)
    for p in plates:
        if p is not mine:
            root.remove(p)
    for asm in root.findall("assemble"):
        for item in asm.findall("assemble_item"):
            if item.get("object_id") not in keep:
                asm.remove(item)
    out = dict(files)
    out["model_settings.config"] = ET.tostring(root, encoding="utf-8")

    if "model.txt" in files:
        out["model.txt"] = _plate_model_txt(files["model.txt"].decode(errors="replace"), order, keep).encode()
    if "geometry.sha1" in files:
        out["geometry.sha1"] = _plate_geometry(files, keep).encode()

    for name in list(out):
        m = re.fullmatch(r"plate_(\d+)\.json", name)
        if m and int(m.group(1)) != plate:
            del out[name]
    if "filament_sequence.json" in files:
        try:
            seq = json.loads(files["filament_sequence.json"])
            seq = {k: v for k, v in seq.items() if k == f"plate_{plate}"}
            out["filament_sequence.json"] = json.dumps(seq, separators=(",", ":")).encode()
        except (ValueError, AttributeError):
            pass
    if "custom_gcode_per_layer.xml" in files:
        try:
            gc = ET.fromstring(files["custom_gcode_per_layer.xml"])
            for p in gc.findall("plate"):
                info = p.find("plate_info")
                if (info.get("id", "1") if info is not None else "1") != str(plate):
                    gc.remove(p)
            out["custom_gcode_per_layer.xml"] = ET.tostring(gc, encoding="utf-8")
        except ET.ParseError:
            pass
    if "layer_heights_profile.txt" in files:  # "object_id=<1-based object index>|z|h|…" per line
        ids = [oid for oid, _ in order]
        lines = [line for line in files["layer_heights_profile.txt"].decode(errors="replace").splitlines()
                 if (m := re.match(r"object_id=(\d+)\|", line)) is None
                 or (0 < int(m.group(1)) <= len(ids) and ids[int(m.group(1)) - 1] in keep)]
        out["layer_heights_profile.txt"] = ("\n".join(lines) + "\n").encode()
    return out


def _plate_model_txt(text: str, order: list, keep: dict) -> str:
    blocks = []  # [object line, volume lines…, instance lines…]
    for line in text.splitlines():
        if line.startswith("object "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    names = [o.get("name") for o in parse_model_txt(text)]
    by_name = {}
    for oid, name in order:
        by_name.setdefault(name, oid)
    out = []
    for i, block in enumerate(blocks):
        # model.txt and model_settings.config list objects in the same order; by name if they disagree
        oid = order[i][0] if len(blocks) == len(order) else by_name.get(names[i])
        if oid not in keep:
            continue
        n = -1
        for line in block:
            if line.lstrip().startswith("instance "):
                n += 1
                if n not in keep[oid]:
                    continue
            out.append(line)
    return "\n".join(out) + "\n"


def _plate_geometry(files: dict, keep: dict) -> str:
    """geometry.sha1 with only the plate's meshes; 3D/3dmodel.model hashed on the plate's part only."""
    paths, model_hash = None, None
    try:
        with zipfile.ZipFile(io.BytesIO(files[BINARY])) as z:
            model = ET.fromstring(z.read("3D/3dmodel.model"))
        paths, parts = set(), []
        for obj in model.iterfind("m:resources/m:object", NS_3MF):
            if obj.get("id") in keep:
                parts.append(ET.tostring(obj))
                paths |= {c.get(f"{{{NS_3MF['p']}}}path", "").lstrip("/")
                          for c in obj.iterfind("m:components/m:component", NS_3MF)}
        for item in model.iterfind("m:build/m:item", NS_3MF):
            if item.get("objectid") in keep:
                parts.append(ET.tostring(item))
        model_hash = hashlib.sha1(b"\n".join(parts)).hexdigest()
    except (KeyError, ET.ParseError, zipfile.BadZipFile):
        pass
    lines = []
    for line in files["geometry.sha1"].decode().splitlines():
        h, sep, name = line.partition("  ")
        if not sep:
            continue
        if name == "3D/3dmodel.model" and model_hash:
            line = f"{model_hash}  {name}"
        elif name.startswith("3D/Objects/") and paths is not None and name not in paths:
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def plate_ref(plate: int) -> bytes:
    return f"refs/heads/plate-{plate}".encode()


def slug_for(project_path: str) -> str:
    if not project_path:
        return "untitled"
    # Windows: "C:\a\b.3mf" and "c:/a/b.3mf" are the same file and must map to the same repo
    key = os.path.normcase(os.path.normpath(project_path))
    # OrcaSlicer's sandbox blocks paths containing these words, so they can't be in the repo name
    stem = re.sub(DENIED_WORDS, "_", Path(project_path).stem, flags=re.IGNORECASE)
    return f"{stem}-{hashlib.sha1(key.encode()).hexdigest()[:8]}"


class _NoConfigRepo(Repo):
    """A bare repo without the `config` file.

    OrcaSlicer's sandbox blocks any path containing "config" (even with permissions granted), so
    the repo config lives in memory only. git itself still reads the repo fine without it.
    """

    def get_config(self):
        cf = ConfigFile()
        cf.set((b"core",), b"repositoryformatversion", b"0")
        cf.set((b"core",), b"bare", True)
        return cf


def open_repo(path: Path) -> Repo:
    if not (path / "HEAD").exists():  # new, or left half-made by an earlier failed init
        for d in ("objects/info", "objects/pack", "refs/heads", "refs/tags"):
            (path / d).mkdir(parents=True, exist_ok=True)
        (path / "HEAD").write_bytes(b"ref: " + DEFAULT_BRANCH + b"\n")
    repo = _NoConfigRepo(str(path))
    # Branches aren't in the UI for now: every version goes on main
    if current_branch(repo) != DEFAULT_BRANCH:
        repo.refs.set_symbolic_ref(b"HEAD", DEFAULT_BRANCH)
    return repo


def current_branch(repo: Repo) -> bytes:
    target = repo.refs.read_ref(b"HEAD")
    return target[len(b"ref: "):] if target and target.startswith(b"ref: ") else DEFAULT_BRANCH


def head_sha(repo: Repo):
    return repo.refs[current_branch(repo)] if current_branch(repo) in repo.refs else None


def read_files(repo: Repo, sha) -> dict:
    if sha is None:
        return {}
    tree = repo[repo[sha].tree]
    return {name.decode(): repo[blob_id].data for name, _mode, blob_id in tree.items()}


def parse_settings(data: bytes) -> dict:
    out = {}
    for line in data.decode(errors="replace").splitlines():
        key, sep, value = line.partition(" = ")
        if sep:
            out[key] = value
    return out


def settings_of(files: dict) -> dict:
    """All resolved settings of a version as {key: text}, lists as JSON (like settings.ini).

    Read from the 3MF's JSON when there: settings.ini can't hold multi-line values (custom G-code)
    on one line, so parsing it back loses their continuation lines.
    """
    data = files.get(BINARY)
    if data:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                raw = json.loads(z.read("Metadata/project_settings.config"))
            return {k: v if isinstance(v, str) else json.dumps(v) for k, v in raw.items()}
        except (KeyError, ValueError, zipfile.BadZipFile):
            pass
    return parse_settings(files.get("settings.ini", b""))


def changed_keys(old: dict, new: dict) -> list:
    a, b = settings_of(old), settings_of(new)
    return sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))


def commit_version(repo: Repo, files: dict, subject: str, trailers: dict, ref: bytes = None):
    """Commit `files` on `ref` (default: the current branch). Returns the sha, or None if nothing changed.

    Only the text files decide whether something changed. The 3MF is rewritten with new timestamps
    on every export, so comparing it would create a version on every export.
    """
    ref = ref or current_branch(repo)
    parent = repo.refs[ref] if ref in repo.refs else None
    old = read_files(repo, parent)
    same = lambda n: old.get(n, b"") == files.get(n, b"")
    if parent is not None and all(same(n) for n in old.keys() | files.keys() if n != BINARY):
        return None

    keys = [k for k in changed_keys(old, files) if k not in INTERNAL_KEYS]
    if parent is not None and keys:
        subject += (f" — {len(keys)} setting{'s' if len(keys) != 1 else ''} changed: " + ", ".join(keys[:8])
                    + ("…" if len(keys) > 8 else ""))
    message = subject + "\n\n" + "".join(f"{k}: {v}\n" for k, v in trailers.items())

    tree = Tree()
    for name, data in sorted(files.items()):
        blob = Blob.from_string(data)
        repo.object_store.add_object(blob)
        tree.add(name.encode(), 0o100644, blob.id)
    repo.object_store.add_object(tree)

    c = Commit()
    c.tree = tree.id
    c.parents = [parent] if parent else []
    c.author = c.committer = AUTHOR
    c.author_time = c.commit_time = int(time.time())
    c.author_timezone = c.commit_timezone = -time.timezone
    c.encoding = b"UTF-8"
    c.message = message.encode()
    repo.object_store.add_object(c)
    repo.refs[ref] = c.id
    return c.id.decode()


def branches(repo: Repo) -> list:
    return sorted(k.decode() for k in repo.refs.keys(b"refs/heads/"))


def tags_by_sha(repo: Repo) -> dict:
    out = {}
    for name, sha in repo.refs.as_dict(b"refs/tags/").items():
        out.setdefault(sha.decode(), []).append(name.decode())
    return out


def history(repo: Repo, branch: str = None) -> list:
    ref = b"refs/heads/" + branch.encode() if branch else current_branch(repo)
    if ref not in repo.refs:
        return []
    tags = tags_by_sha(repo)
    out = []
    for e in repo.get_walker(include=[repo.refs[ref]]):
        message = e.commit.message.decode(errors="replace")
        out.append({
            "sha": e.commit.id.decode(),
            "time": e.commit.commit_time,
            "message": message,
            "tags": tags.get(e.commit.id.decode(), []),
            **parse_message(message),
        })
    return out


LEGACY = "main"  # versions saved before plates had their own history: all plates mixed, read-only


def plate_histories(repo: Repo, current: int = None) -> list:
    """The histories the Versions tab can show: one per plate (plate-<n>, the current plate even with no
    versions yet), then the old mixed `main` if it has versions. [{id, plate, name, count, current}]"""
    nums = {int(m.group(1)) for b in branches(repo) if (m := re.fullmatch(r"plate-(\d+)", b))}
    if current:
        nums.add(current)
    out = []
    for n in sorted(nums):
        ref, name = plate_ref(n), ""
        if ref in repo.refs:
            plates = parse_model_settings(read_files(repo, repo.refs[ref]).get("model_settings.config", b""))["plates"]
            name = plates[0]["meta"].get("plater_name", "") if plates else ""
        out.append({"id": f"plate-{n}", "plate": n, "name": name, "current": n == current,
                    "count": len(history(repo, f"plate-{n}"))})
    if b"refs/heads/" + LEGACY.encode() in repo.refs:
        out.append({"id": LEGACY, "plate": None, "name": "", "current": False, "count": len(history(repo, LEGACY))})
    return out


def parse_message(message: str) -> dict:
    """'Export x.gcode — 3 settings changed: a, b\\n\\nDestination: file\\n…' → title, summary, trailers."""
    subject, _, body = message.partition("\n\n")
    title, _, summary = subject.partition(" — ")
    trailers = dict(line.split(": ", 1) for line in body.splitlines() if ": " in line)
    m = re.fullmatch(r"\d+ settings? changed: (.*?)(…?)", summary)
    if m:  # versions saved before INTERNAL_KEYS were left out of the summary
        keys = [k for k in m.group(1).split(", ") if k not in INTERNAL_KEYS]
        summary = f"{len(keys)} setting{'s' if len(keys) != 1 else ''} changed: {', '.join(keys)}{m.group(2)}" if keys else ""
    return {"title": title, "summary": summary, "trailers": trailers}


def diff(repo: Repo, a: str, b: str) -> str:
    old, new = read_files(repo, a.encode()), read_files(repo, b.encode())
    out = []
    for name in sorted((old.keys() | new.keys()) - {BINARY}):
        out += difflib.unified_diff(old.get(name, b"").decode(errors="replace").splitlines(),
                                    new.get(name, b"").decode(errors="replace").splitlines(),
                                    f"{a[:8]}/{name}", f"{b[:8]}/{name}", lineterm="", n=0)
    if old.get(BINARY) != new.get(BINARY):
        out.append(f"# {BINARY}: changed (binary)")
    return "\n".join(out)


# ---------------------------------------------------------------- structured views (Versions tab)
#
# A view is a list of sections: {"id", "kind", "title", "rows"}. A row is {"key", "label", "value"}
# and may have a "group" (shown as a sub-heading). compare_view() pairs two views row by row.

# Plugin bookkeeping, not print settings: shown only in advanced mode
INTERNAL_KEYS = {"plugins", "different_settings_to_system", "print_plugin_config_overrides",
                 "filament_plugin_config_overrides", "printer_plugin_config_overrides"}
PRESET_KEYS = (("print_settings_id", "Process"), ("printer_settings_id", "Printer"),
               ("filament_settings_id", "Filament"))
GCODE_TYPES = {"0": "Color change", "1": "Pause", "2": "Tool change", "3": "Template", "4": "Custom G-code"}
SKIP_PART_META = ("name", "matrix", "source_")


def pretty(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def display(text: str) -> str:
    """A setting as people read it: ["60"] → 60, ["PLA", "PETG"] → PLA, PETG."""
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            return text
        if isinstance(items, list):
            return ", ".join(str(x) for x in items)
    return text


def _numbers(text):
    try:
        items = json.loads(text) if text.startswith("[") else [text]
        return [float(str(x).strip().rstrip("%").strip()) for x in items] if isinstance(items, list) else None
    except (ValueError, TypeError):
        return None


def trend(old, new) -> str:
    """How the newer value moved: up, down, changed (not comparable), added, removed."""
    if old is None:
        return "added"
    if new is None:
        return "removed"
    a, b = _numbers(old), _numbers(new)
    if a and b and len(a) == len(b):
        d = [y - x for x, y in zip(a, b)]
        if all(x >= 0 for x in d) and any(x > 0 for x in d):
            return "up"
        if all(x <= 0 for x in d) and any(x < 0 for x in d):
            return "down"
    return "changed"


def setting_groups(settings: dict) -> dict:
    """{key: "Process" | "Filament" | "Printer"} for the keys changed from the system presets.

    different_settings_to_system is [process, filament 1…n, printer], each a ';'-joined key list.
    """
    try:
        lists = json.loads(settings.get("different_settings_to_system", "[]"))
    except ValueError:
        return {}
    if not isinstance(lists, list) or len(lists) < 2:
        return {}
    named = [("Process", lists[0])] + [("Filament", x) for x in lists[1:-1]] + [("Printer", lists[-1])]
    return {k: group for group, keys in named for k in str(keys).split(";") if k}


def _tuple(text):
    return tuple(float(x) for x in text.strip("()").split(",") if x.strip())


def parse_model_txt(text: str) -> list:
    """model.txt (see model_summary) → [{name, parts, facets, volumes: [...], instances: [...]}]."""
    objects = []
    for line in text.splitlines():
        m = re.match(r"\s*(object|volume|instance)\s*('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")?\s*(.*)", line)
        if not m:
            continue
        kind, name, rest = m.groups()
        item = {k: v for k, v in re.findall(r"(\w+)=(\([^)]*\)|\S+)", rest)}
        if name:
            try:
                item["name"] = ast.literal_eval(name)
            except (ValueError, SyntaxError):
                item["name"] = name.strip("'\"")
        if kind == "object":
            objects.append({**item, "volumes": [], "instances": []})
        elif objects:
            objects[-1]["volumes" if kind == "volume" else "instances"].append(item)
    return objects


def parse_model_settings(text: bytes) -> dict:
    """model_settings.config → {"objects": {id: {name, meta, parts}}, "plates": [{meta, object_ids}]}."""
    out = {"objects": {}, "plates": []}
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    meta = lambda el: {m.get("key"): m.get("value", "") for m in el.findall("metadata")}
    for obj in root.findall("object"):
        m = meta(obj)
        parts = [{"id": p.get("id"), **meta(p)} for p in obj.findall("part")]
        out["objects"][obj.get("id")] = {"name": m.get("name", ""), "meta": m, "parts": parts}
    for plate in root.findall("plate"):
        ids = [meta(i).get("object_id") for i in plate.findall("model_instance")]
        out["plates"].append({"meta": meta(plate), "object_ids": ids})
    return out


def _fmt(values, unit="", scale=1.0, digits=2):
    nums = [round(v * scale, digits) for v in values]
    nums = [0.0 if v == 0 else v for v in nums]  # no "-0"
    return " × ".join(f"{v:g}" for v in nums) + unit


def _object_rows(obj: dict, overrides: dict) -> list:
    rows = []
    add = lambda key, label, value, group=None: rows.append(
        {"key": key, "label": label, "value": value, **({"group": group} if group else {})})
    vols, insts = obj["volumes"], obj["instances"]
    add("parts", "Parts", obj.get("parts", str(len(vols))))
    add("facets", "Facets", obj.get("facets", ""))
    for n, inst in enumerate(insts, 1):
        group = f"Instance {n}" if len(insts) > 1 else None
        scale = _tuple(inst.get("scale", "(1,1,1)"))
        if len(vols) == 1 and "size" in vols[0]:
            size = [s * k for s, k in zip(_tuple(vols[0]["size"]), scale)]
            add(f"i{n}.size", "Size", _fmt(size, " mm"), group)
        add(f"i{n}.position", "Position", _fmt(_tuple(inst.get("offset", "()")), " mm"), group)
        add(f"i{n}.rotation", "Rotation", _fmt(_tuple(inst.get("rot", "()")), "°", 180 / math.pi, 1), group)
        add(f"i{n}.scale", "Scale", _fmt(scale, " %", 100, 1), group)
        mirror = [a for a, v in zip("XYZ", _tuple(inst.get("mirror", "(1,1,1)"))) if v < 0]
        if mirror:
            add(f"i{n}.mirror", "Mirrored", ", ".join(mirror), group)
        add(f"i{n}.printable", "Printable", "Yes" if inst.get("printable", "True") == "True" else "No", group)
    if len(vols) > 1:
        for v in vols:
            kind = v.get("type", "").rsplit(".", 1)[-1]
            size = _fmt(_tuple(v["size"]), " mm") if "size" in v else ""
            add(f"part.{v.get('name')}", v.get("name", "?"),
                f"{kind}, extruder {v.get('extruder', '?')}" + (f", {size}" if size else ""), "Parts")
    for k, v in sorted(overrides.get("meta", {}).items()):
        if k != "name":
            add(f"set.{k}", pretty(k), v, "Object settings")
    for p in overrides.get("parts", []):
        for k, v in sorted(p.items()):
            if k != "id" and not k.startswith(SKIP_PART_META):
                add(f"part.{p.get('name')}.{k}", f"{p.get('name', '?')}: {pretty(k)}", v, "Part settings")
    return rows


def _duration(seconds) -> str:
    s = int(round(float(seconds)))
    return f"{s // 3600}h {s // 60 % 60}m" if s >= 3600 else f"{s // 60}m {s % 60}s"


def version_view(files: dict, advanced: bool = False) -> list:
    """One version, as the sections the Versions tab shows."""
    settings = settings_of(files)
    groups = setting_groups(settings)
    model = parse_model_settings(files.get("model_settings.config", b""))
    visible = lambda k: advanced or k not in INTERNAL_KEYS
    row = lambda k, label=None, **kw: {"key": k, "label": label or pretty(k), "value": display(settings[k]), **kw}
    sections = []

    presets = [row(k, label) for k, label in PRESET_KEYS if k in settings]
    if presets:
        sections.append({"id": "presets", "kind": "presets", "title": "Presets", "rows": presets})

    order = {"Process": 0, "Filament": 1, "Printer": 2}
    changed = sorted((k for k in groups if k in settings and visible(k)), key=lambda k: (order[groups[k]], k))
    sections.append({"id": "modified", "kind": "modified", "title": "Changed from system presets",
                     "rows": [row(k, group=groups[k]) for k in changed]})

    by_name = {o["name"]: o for o in model["objects"].values()}
    for i, obj in enumerate(parse_model_txt(files.get("model.txt", b"").decode(errors="replace"))):
        name = obj.get("name", f"Object {i + 1}")
        sections.append({"id": f"object:{name}", "kind": "object", "title": name,
                         "rows": _object_rows(obj, by_name.get(name, {}))})

    names = {oid: o["name"] for oid, o in model["objects"].items()}
    plate_files = sorted((n for n in files if re.fullmatch(r"plate_\d+\.json", n)), key=lambda n: int(n[6:-5]))
    # by plater_id: a plate's own history holds only that plate, which may not be plate 1
    plates = {p["meta"].get("plater_id") or str(i + 1): p for i, p in enumerate(model["plates"])}
    for n in sorted(set(plates) | {f[6:-5] for f in plate_files}, key=int):
        rows, meta = [], plates.get(n, {}).get("meta", {})
        add = lambda k, label, v, num=None: rows.append(
            {"key": k, "label": label, "value": str(v), **({"num": num} if num is not None else {})})
        if meta.get("plater_name"):
            add("name", "Name", meta["plater_name"])
        if n in plates:
            objs = [names.get(i, f"#{i}") for i in plates[n]["object_ids"]]
            add("objects", "Objects", ", ".join(objs) or "—")
        try:
            info = json.loads(files.get(f"plate_{n}.json", b"{}"))
        except ValueError:
            info = {}
        if info.get("bed_type"):
            add("bed_type", "Bed type", pretty(info["bed_type"]))
        if "is_seq_print" in info:
            add("sequential", "Print sequence", "By object" if info["is_seq_print"] else "By layer")
        if info.get("first_layer_time"):
            add("first_layer_time", "First layer time", _duration(info["first_layer_time"]),
                round(float(info["first_layer_time"])))
        if meta.get("locked") == "true":
            add("locked", "Locked", "Yes")
        sections.append({"id": f"plate:{n}", "kind": "plate", "title": f"Plate {n}", "rows": rows})

    layers = []
    try:
        root = ET.fromstring(files["custom_gcode_per_layer.xml"])
        for plate in root.iter("plate"):
            info = plate.find("plate_info")
            pid = info.get("id", "1") if info is not None else "1"
            for el in plate.iter("layer"):
                kind = GCODE_TYPES.get(el.get("type", ""), "Custom G-code")
                detail = {"Color change": f"extruder {el.get('extruder', '?')} {el.get('color', '')}".strip(),
                          "Tool change": f"extruder {el.get('extruder', '?')}"}.get(kind, el.get("extra") or "")
                if kind in ("Custom G-code", "Template") and el.get("gcode"):
                    detail = el.get("gcode")
                layers.append({"key": f"{pid}@{el.get('top_z')}", "label": f"Z {el.get('top_z')} mm",
                               "value": kind + (f" — {detail}" if detail else ""),
                               **({"group": f"Plate {pid}"} if len(plates) > 1 else {})})
    except (KeyError, ET.ParseError):
        pass
    if layers:
        sections.append({"id": "layers", "kind": "layers", "title": "Layer changes", "rows": layers})

    sections.append({"id": "settings", "kind": "settings", "title": "All settings",
                     "rows": [row(k, **({"group": groups[k]} if k in groups else {}))
                              for k in sorted(settings) if visible(k)]})

    if advanced:
        hashes = [line.split("  ", 1) for line in files.get("geometry.sha1", b"").decode().splitlines() if "  " in line]
        sections.append({"id": "geometry", "kind": "geometry", "title": "Geometry files",
                         "rows": [{"key": n, "label": n, "value": h[:12]} for h, n in hashes]})
    return sections


def _geometry_changes(old: dict, new: dict, advanced: bool) -> list:
    parse = lambda f: dict(reversed(l.split("  ", 1)) for l in f.get("geometry.sha1", b"").decode().splitlines()
                           if "  " in l)
    a, b = parse(old), parse(new)
    rows = []
    for name in sorted(a.keys() | b.keys()):
        if a.get(name) != b.get(name) and not name.startswith(("_rels/", "[Content_Types]")):
            label = Path(name).stem.replace("_", " ") if name.startswith("3D/Objects/") else name
            o, n = a.get(name), b.get(name)
            rows.append({"key": name, "label": label,
                         "old": (o[:12] if advanced else "—") if o else None,
                         "new": (n[:12] if advanced else "modified") if n else None,
                         "trend": trend(o, n) if o is None or n is None else "changed"})
    return rows


def compare_view(old_files: dict, new_files: dict, advanced: bool = False) -> list:
    """The sections of two versions side by side, only the rows that differ. Rows: key, label, old, new,
    trend (old/new are None when the row is missing on that side)."""
    old_view = {s["id"]: s for s in version_view(old_files, advanced) if s["kind"] != "modified"}
    new_view = {s["id"]: s for s in version_view(new_files, advanced) if s["kind"] != "modified"}
    raw_old, raw_new = settings_of(old_files), settings_of(new_files)
    ids = [i for i in new_view] + [i for i in old_view if i not in new_view]
    sections = []
    for sid in ids:
        if sid == "geometry":
            continue
        a, b = old_view.get(sid), new_view.get(sid)
        old_rows = {r["key"]: r for r in (a or {}).get("rows", [])}
        new_rows = {r["key"]: r for r in (b or {}).get("rows", [])}
        rows = []
        for key in [k for k in new_rows] + [k for k in old_rows if k not in new_rows]:
            o, n = old_rows.get(key), new_rows.get(key)
            ov, nv = o and o["value"], n and n["value"]
            if ov == nv:
                continue
            if sid == "settings":  # trend on the raw values: display() flattens lists
                t = trend(raw_old.get(key), raw_new.get(key))
            else:  # "num": the comparable value of a formatted row (e.g. a duration in seconds)
                num = lambda x: None if x is None else str(x["num"]) if "num" in x else x["value"]
                t = trend(num(o), num(n))
            r = n or o
            rows.append({"key": key, "label": r["label"], "old": ov, "new": nv, "trend": t,
                         **({"group": r["group"]} if r.get("group") else {})})
        if rows:
            s = b or a
            sections.append({"id": sid, "kind": s["kind"], "title": "Settings" if sid == "settings" else s["title"],
                             "rows": rows,
                             "status": "added" if a is None else "removed" if b is None else "changed"})
    geometry = _geometry_changes(old_files, new_files, advanced)
    if geometry:
        sections.append({"id": "geometry", "kind": "geometry", "title": "Geometry (meshes, painting)",
                         "rows": geometry, "status": "changed"})
    return sections


def _ref(prefix: bytes, name: str) -> bytes:
    ref = prefix + name.strip().encode()
    if not name.strip() or not check_ref_format(ref):
        raise ValueError(f"invalid name: '{name}'")
    return ref


def branch_create(repo: Repo, name: str, sha: str = None):
    ref = _ref(b"refs/heads/", name)
    if ref in repo.refs:
        raise ValueError(f"branch '{name}' already exists")
    start = sha.encode() if sha else head_sha(repo)
    if start is None:
        raise ValueError("no versions yet")
    repo.refs[ref] = start


def branch_switch(repo: Repo, name: str):
    # ponytail: switching only changes where the next commits go; Orca's state is not touched
    ref = b"refs/heads/" + name.encode()
    if ref not in repo.refs:
        raise ValueError(f"branch '{name}' doesn't exist")
    repo.refs.set_symbolic_ref(b"HEAD", ref)


def tag_name(name: str) -> str:
    """Git names can't have spaces: "good PETG " → "good-PETG"."""
    return re.sub(r"\s+", "-", name.strip())


def tag_create(repo: Repo, name: str, sha: str):
    name = tag_name(name)
    try:
        ref = _ref(b"refs/tags/", name)
    except ValueError:
        raise ValueError(f"invalid tag name '{name}': use letters, digits, - _ . "
                         "(not ~ ^ : ? * [ \\ or '..')") from None
    if ref in repo.refs:
        raise ValueError(f"tag '{name}' already exists")
    repo.refs[ref] = sha.encode()


def adopt_untitled(repos: Path, project_path: str):
    """On the first save, the history built up while the project was unsaved moves to that project."""
    src, dst = repos / "untitled.git", repos / f"{slug_for(project_path)}.git"
    if src.exists() and not dst.exists():
        src.rename(dst)


def archive_untitled(repos: Path):
    """New project: the old unsaved history doesn't mix with the new one."""
    src = repos / "untitled.git"
    if src.exists():
        src.rename(repos / f"untitled-{time.strftime('%Y%m%d-%H%M%S')}.git")


def extract_3mf(repo: Repo, sha: str, dest: Path) -> Path:
    data = read_files(repo, sha.encode()).get(BINARY)
    if not data:
        raise ValueError("this version has no .3mf")
    dest.write_bytes(data)
    return dest


# ---------------------------------------------------------------- orca glue (UI thread only)

def model_summary() -> bytes:
    """Readable geometry/placement summary. The real geometry is in the 3MF."""
    r = lambda t: tuple(round(x, 4) for x in t)
    lines = []
    for o in orca.host.model().objects():
        lines.append(f"object {o.name!r} parts={o.parts_count()} facets={o.facets_count()}")
        for v in o.volumes():
            bb = v.bounding_box()
            lines.append(f"  volume {v.name!r} type={v.type()} extruder={v.extruder_id()} facets={v.facets_count()} "
                         f"size={r(bb.size)} offset={r(v.offset())} rot={r(v.rotation())} scale={r(v.scaling_factor())}")
        for i in o.instances():
            lines.append(f"  instance offset={r(i.offset())} rot={r(i.rotation())} scale={r(i.scaling_factor())} "
                         f"mirror={r(i.mirror())} printable={i.printable}")
    return ("\n".join(lines) + "\n").encode()


_lock = threading.Lock()
_pending = []        # version requests from worker threads, consumed on the UI thread
_relay = None
_relay_ready = False
_page = None         # the VersionsPage instance, used to refresh the tab
_storage_dir = None  # read in on_load: plugin.storage() needs a plugin callback, relay callbacks may not count


def _storage() -> Path:
    return _storage_dir


def _current_repo():
    path = orca.host.plater().project_path()
    return open_repo(_storage() / "repos" / f"{slug_for(path)}.git"), path


def _current_plate() -> int:
    """The plate selected in OrcaSlicer, 1-based like the plate names and plate_<n>.json."""
    return orca.host.model().current_plate_index() + 1


def _log(msg):
    print(f"[orca-versioning] {msg}", file=sys.stderr)  # stderr is tee'd to <data_dir>/log/python_*.log


def request_version(dest: str, output: str):
    """Call from any thread. Only queues the request and wakes the relay."""
    with _lock:
        _pending.append({"dest": dest, "output": output})
    _ensure_relay()
    if _relay_ready:
        _relay.post({"cmd": "tick"})  # non-blocking (CallAfter)


def _ensure_relay():
    global _relay, _relay_ready
    if _relay is not None and _relay.is_open():
        return
    _relay_ready = False
    _relay = orca.host.ui.create_dock_panel(
        html=RELAY_HTML, title="Orca Versioning relay", width=1, height=1, dock="float",
        on_message=_on_relay, on_close=_on_relay_close)


def _on_relay_close():
    global _relay
    _relay = None


def _on_relay(data):  # UI thread
    global _relay_ready
    if data and data.get("cmd") == "ready":
        _relay_ready = True
        _relay.hide()
    with _lock:
        jobs, _pending[:] = list(_pending), []
    for job in jobs:
        try:
            _snapshot_and_commit(**job)
        except Exception as e:
            _log(f"version failed: {e!r}")
            orca.host.ui.push_notification(orca.host.ui.NotificationLevel.WarningNotificationLevel,
                                           f"Orca Versioning: version not saved ({e})")


def _snapshot_and_commit(dest: str, output: str):
    repo, project_path = _current_repo()
    with repo:  # Windows can't rename a repo whose files are still open
        _commit_snapshot(repo, project_path, dest, output)


def _commit_snapshot(repo, project_path, dest, output):
    tmp = _storage() / "tmp" / "snapshot.3mf"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    orca.host.plater().export_3mf_copy(str(tmp))  # unsaved changes included, project left untouched
    files = files_from_3mf(tmp.read_bytes())
    files["model.txt"] = model_summary()
    # export and print always work on the selected plate: each plate has its own history
    plate = _current_plate()
    files = plate_files(files, plate)
    name = Path(output).name if output else "G-code"
    sha = commit_version(repo, files, f"{'Export' if dest == 'file' else 'Print'} {name}",
                         {"Destination": dest, "Output": output, "Project": project_path or "(not saved)",
                          "Plate": str(plate)},
                         ref=plate_ref(plate))
    if sha:
        _log(f"committed {sha[:8]} on plate {plate} ({dest})")
        orca.host.ui.push_notification(orca.host.ui.NotificationLevel.RegularNotificationLevel,
                                       f"Orca Versioning: plate {plate} version {sha[:8]} saved")
        if _page is not None:
            _page.post_message({"type": "refresh"})


CAPABILITY = "Version on export"


def preset_warning(pipeline, plugins):
    """Why exporting to a file won't create a version with the active preset, or None if it will.

    `pipeline` and `plugins` are the serialized `slicing_pipeline_plugin` and `plugins` values of the
    active config (None if the key is missing).
    """
    if CAPABILITY not in (pipeline or ""):
        return (f'G-code exports won\'t be versioned: add "{CAPABILITY}" to the process preset '
                f"(Others > Slicing Pipeline Plugin) and save it as a user preset.")
    if CAPABILITY not in (plugins or ""):  # OrcaSlicer 2.5.0 bug: manifest built before the plugin loaded
        return (f'G-code exports won\'t be versioned (OrcaSlicer bug): in the process preset, remove '
                f'"{CAPABILITY}" from Slicing Pipeline Plugin and add it back.')
    return None


_warning = None  # last preset warning shown, so each one is notified once, not on every slice


def _check_preset():
    """Notify when the active preset stops (or starts again) versioning exports. UI thread."""
    global _warning
    bundle = orca.host.preset_bundle()
    w = preset_warning(bundle.full_config_value("slicing_pipeline_plugin"), bundle.full_config_value("plugins"))
    if w and w != _warning:
        orca.host.ui.push_notification(orca.host.ui.NotificationLevel.WarningNotificationLevel, f"Orca Versioning: {w}")
    if w != _warning and _page is not None:
        _page.post_message({"type": "refresh"})
    _warning = w


# ---------------------------------------------------------------- capabilities

class VersionOnExport(orca.slicing.SlicingPipelineCapabilityBase if orca else object):
    """Add it to the process preset: it's the only hook for 'Export G-code' to a file."""

    def get_name(self):
        return CAPABILITY

    def execute(self, ctx):
        try:
            # BBL printers post-process in place on every slice: output_name == gcode_path → that's not an export
            if ctx.step == orca.slicing.Step.psGCodePostProcess and ctx.output_name != ctx.gcode_path:
                request_version("file" if ctx.host == "File" else f"printer:{ctx.host}", ctx.output_name)
        except Exception as e:  # never block an export
            _log(f"execute failed: {e!r}")
        return orca.ExecutionResult.success()


PRINTER_EVENTS = ("PrintJobStarted", "SendJobStarted", "UploadStarted")
# moments when the active preset may have changed; SlicingJobComplete comes right before an export
PRESET_EVENTS = ("ProjectOpened", "NewProject", "PresetSelected", "PresetSaved", "SlicingJobComplete")
# the Versions tab follows the plate selected in OrcaSlicer (on focus too, if these aren't fired)
PLATE_EVENTS = ("PlateSelected", "PlateCreated", "PlateDeleted", "PlateRenamed")


class VersionsPage(orca.pages.PagesPluginCapabilityBase if orca else object):
    def get_name(self):
        return "Versions"

    def get_icon(self):
        """Tab icon: a clock, drawn like OrcaSlicer's own tab icons (white strokes, 20 px).

        The host looks icons up by name in resources/images, but takes a path as-is when that file
        exists, so the SVG is written to the plugin's storage and its absolute path returned.
        """
        try:
            path = Path(orca.host.plugin.storage()) / "icons" / "versions.svg"
            if not path.exists() or path.read_text() != TAB_ICON:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(TAB_ICON)
            return str(path)
        except Exception as e:
            _log(f"icon failed: {e!r}")
            return "print-time"  # OrcaSlicer's built-in clock

    # ---- settings (Plugins dialog > Config > Versions)

    def get_default_config(self):
        return dict(DEFAULT_CONFIG)

    def has_config_ui(self):
        return True

    def get_config_ui(self):
        return CONFIG_HTML

    def _config(self) -> dict:
        try:
            return {**DEFAULT_CONFIG, **json.loads(self.get_config() or "{}")}
        except Exception as e:
            _log(f"config read failed: {e!r}")
            return dict(DEFAULT_CONFIG)

    def on_load(self):
        global _page, _storage_dir
        _page = self
        _storage_dir = Path(orca.host.plugin.storage())
        _ensure_relay()

    def on_unload(self):
        global _page
        _page = None
        if _relay is not None:
            _relay.close()

    def on_lifecycle_event(self, event, ctx):
        names = {getattr(orca.LifecycleEvent, n): n for n in PRINTER_EVENTS}
        try:
            if event in names:
                request_version(f"printer:{ctx.source or names[event]}", ctx.name)
            elif event == orca.LifecycleEvent.ProjectAfterSave and ctx.name:  # ctx.name = full path
                with _lock:
                    adopt_untitled(_storage() / "repos", ctx.name)
            elif event == orca.LifecycleEvent.NewProject:
                with _lock:
                    archive_untitled(_storage() / "repos")
            if event in (orca.LifecycleEvent.ProjectAfterSave, orca.LifecycleEvent.NewProject,
                         orca.LifecycleEvent.ProjectOpened):
                self.post_message({"type": "refresh"})
            if event in {getattr(orca.LifecycleEvent, n, None) for n in PLATE_EVENTS} - {None}:
                self.post_message({"type": "plate"})
        except Exception as e:
            _log(f"lifecycle {event} failed: {e!r}")
        try:
            if event in {getattr(orca.LifecycleEvent, n, None) for n in PRESET_EVENTS} - {None}:
                _check_preset()
        except Exception as e:
            _log(f"preset check failed: {e!r}")

    def get_ui(self):
        return PAGE_HTML

    def on_message(self, data):  # UI thread
        cmd = data.get("cmd")
        if cmd == "state":  # the page just opened or refreshed: the preset may not have fired any event yet
            try:
                _check_preset()
            except Exception as e:
                _log(f"preset check failed: {e!r}")
        try:
            with _lock:
                repo, project_path = _current_repo()
                with repo:
                    self._handle(cmd, data, repo, project_path)
        except Exception as e:
            self.post_message({"type": "error", "text": str(e)})

    def _handle(self, cmd, data, repo, project_path):
        advanced = bool(self._config().get("advanced_mode"))
        if cmd == "tag_create":
            tag_create(repo, data["name"], data["sha"])
        elif cmd == "show":
            files = read_files(repo, data["sha"].encode())
            self.post_message({"type": "view", "sha": data["sha"], "sections": version_view(files, advanced)})
            return
        elif cmd == "compare":  # a = older, b = newer
            old, new = read_files(repo, data["a"].encode()), read_files(repo, data["b"].encode())
            self.post_message({"type": "compare", "a": data["a"], "b": data["b"],
                               "sections": compare_view(old, new, advanced)})
            return
        elif cmd == "diff":
            self.post_message({"type": "diff", "text": diff(repo, data["a"], data["b"])})
            return
        elif cmd == "extract":
            base = Path(project_path) if project_path else _storage() / "untitled.3mf"
            label = (tags_by_sha(repo).get(data["sha"]) or [data["sha"][:8]])[0]
            out = extract_3mf(repo, data["sha"], base.with_name(f"{base.stem}@{label}.3mf"))
            orca.host.ui.push_notification(orca.host.ui.NotificationLevel.RegularNotificationLevel,
                                           f"Version extracted: {out}")
        current = _current_plate()
        lines = plate_histories(repo, current)
        shown = data.get("plate") if data.get("plate") in {h["id"] for h in lines} else f"plate-{current}"
        self.post_message({
            "type": "state",
            "project": project_path or "(project not saved)",
            "warning": _warning,
            "advanced": advanced,
            "plates": lines,          # the histories to pick from
            "current": f"plate-{current}",  # the plate selected in OrcaSlicer
            "plate": shown,           # the history in `history`
            "history": history(repo, shown),
        })


if orca:
    @orca.plugin
    class OrcaVersioning(orca.base):
        def register_capabilities(self):
            # ponytail: no request_permissions, storage (data_dir()) is already an allowed root
            orca.register_capability(VersionOnExport)
            orca.register_capability(VersionsPage)


# ---------------------------------------------------------------- HTML

RELAY_HTML = """<!doctype html><html><body><script>
orca.onMessage(function (d) { orca.postMessage(d); });
orca.postMessage({cmd: 'ready'});
</script></body></html>"""

DEFAULT_CONFIG = {"advanced_mode": False}

TAB_ICON = """<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">\
<circle cx="10" cy="10" r="7.5" style="fill:none;stroke:#fff;stroke-linecap:round;stroke-linejoin:round"/>\
<polyline points="10 5.5 10 10 13 12" style="fill:none;stroke:#fff;stroke-linecap:round;stroke-linejoin:round"/></svg>"""

# Plugins dialog > Config > Versions. Runs sandboxed: window.orca.getConfig/saveConfig/onConfig only.
CONFIG_HTML = r"""<style>
body{margin:0;padding:16px;font-family:var(--orca-font,system-ui,sans-serif);font-size:13px;
  color:var(--orca-fg,#262e30);background:var(--orca-bg,#fff)}
label{display:flex;gap:10px;align-items:flex-start;cursor:pointer}
input{margin:2px 0 0;accent-color:var(--orca-accent,#009688)}
b{font-weight:600}
p{margin:4px 0 0;color:var(--orca-muted,#6b6b6b);line-height:1.45}
</style>
<label><input type="checkbox" id="adv"><span><b>Advanced mode</b>
<p>Shows version hashes, raw setting keys, the plugin's internal settings, geometry file hashes and the raw
text diff. Off by default.</p></span></label>
<script>
var adv = document.getElementById('adv');
orca.onConfig(function (c) { adv.checked = !!(c && c.advanced_mode); });
adv.onchange = function () {
  var c = Object.assign({}, orca.getConfig() || {}, {advanced_mode: adv.checked});
  orca.saveConfig(c);
};
</script>"""

PAGE_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
:root{
  --bg:var(--orca-bg,#fff); --fg:var(--orca-fg,#262e30); --muted:var(--orca-muted,#6b6f70);
  --border:var(--orca-border,#dbdbdb); --accent:var(--orca-accent,#009688);
  --surface:color-mix(in srgb,var(--fg) 3%,var(--bg));
  --hover:color-mix(in srgb,var(--fg) 6%,var(--bg));
  --selected:color-mix(in srgb,var(--accent) 14%,var(--bg));
  --up:var(--accent); --down:#e0782a; --removed:#d6453d;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--orca-font,system-ui,'Segoe UI',sans-serif);
  font-size:13px;line-height:1.4}
button{font:inherit;border-radius:6px;padding:6px 14px;cursor:pointer;border:1px solid var(--accent);
  background:var(--accent);color:#fff;white-space:nowrap}
button:hover{filter:brightness(1.08)}
button:disabled{opacity:.45;cursor:default;filter:none}
button.ghost{background:transparent;color:var(--fg);border-color:var(--border)}
button.ghost:hover{background:var(--hover)}
code,.mono{font-family:Consolas,'Cascadia Mono',ui-monospace,monospace;font-size:12px}
.muted{color:var(--muted)}

.app{display:grid;grid-template-columns:minmax(260px,340px) 1fr;height:100vh}
.side{display:flex;flex-direction:column;border-right:1px solid var(--border);min-height:0}
.side-head{padding:16px 16px 12px;border-bottom:1px solid var(--border)}
.side-head h1{margin:0;font-size:15px;font-weight:600}
.project{margin-top:2px;color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.plates{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.plates:empty{display:none}
.pill{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:14px;font-size:12px;
  background:transparent;color:var(--fg);border:1px solid var(--border);max-width:100%}
.pill:hover{background:var(--hover)}
.pill.on{background:var(--selected);border-color:var(--accent)}
.pill .n{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill .c{font-size:11px;color:var(--muted)}
.pill .dot{width:6px;height:6px;border-radius:3px;background:var(--accent);flex:none}
.pill.legacy{border-style:dashed}
.note{margin:8px 8px 0;padding:8px 10px;border-radius:8px;font-size:12px;color:var(--muted);background:var(--surface)}
.list{flex:1;overflow:auto;padding:6px 8px 12px;outline:none}
.day{margin:12px 8px 4px;font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.ver{padding:9px 10px 10px;border-radius:8px;cursor:pointer;border:1px solid transparent;margin-bottom:2px}
.ver:hover{background:var(--hover)}
.ver.sel{background:var(--selected);border-color:color-mix(in srgb,var(--accent) 45%,transparent)}
.ver .top{display:flex;gap:8px;align-items:baseline}
.ver .title{font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ver .time{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.ver .sum{color:var(--muted);font-size:12px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ver .badges{display:flex;flex-wrap:wrap;gap:4px;margin-top:5px}
.badge{display:inline-block;font-size:11px;line-height:16px;padding:1px 7px;border-radius:9px;
  background:var(--hover);color:var(--muted)}
.badge.tag{background:var(--accent);color:#fff}
.badge.sha{font-family:Consolas,ui-monospace,monospace}
.side-foot{padding:10px 16px;border-top:1px solid var(--border);color:var(--muted);font-size:12px}
.slot{display:inline-block;min-width:16px;height:16px;line-height:16px;text-align:center;border-radius:8px;
  background:var(--accent);color:#fff;font-size:10px;font-weight:600;margin-left:6px}

.main{overflow:auto;min-width:0}
.content{padding:20px 28px 40px;max-width:1500px}
.banner{margin:16px 28px 0;padding:10px 14px;border-radius:8px;border:1px solid color-mix(in srgb,#e6a700 55%,transparent);
  background:color-mix(in srgb,#e6a700 14%,var(--bg))}
.error{margin:16px 28px 0;padding:10px 14px;border-radius:8px;color:var(--removed);
  border:1px solid color-mix(in srgb,var(--removed) 45%,transparent);background:color-mix(in srgb,var(--removed) 8%,var(--bg))}
.head{display:flex;gap:16px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;margin-bottom:18px}
.kicker{font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.head h2{margin:2px 0 6px;font-size:18px;font-weight:600;word-break:break-all}
.meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;color:var(--muted);font-size:12px}
.actions{display:flex;gap:8px;flex-wrap:wrap}

.vs{display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:stretch;margin:6px 0 4px}
.vs .side-card{padding:10px 14px;border:1px solid var(--border);border-radius:8px;background:var(--surface);min-width:0}
.vs .side-card .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vs .arrow{align-self:center;color:var(--muted);font-size:18px}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:10px 0 18px}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;align-items:start}
.card{border:1px solid var(--border);border-radius:10px;background:var(--bg);overflow:hidden;min-width:0}
.card.wide{grid-column:1/-1}
.card>header{display:flex;gap:8px;align-items:center;padding:10px 14px;background:var(--surface);
  border-bottom:1px solid var(--border)}
.card>header h3{margin:0;font-size:13px;font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card>header .kind{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.count{font-size:11px;color:var(--muted);background:var(--hover);border-radius:9px;padding:1px 7px}
.card .tools{padding:10px 14px;border-bottom:1px solid var(--border)}
.card .tools input{width:100%;max-width:360px;font:inherit;padding:6px 10px;border-radius:6px;
  border:1px solid var(--border);background:var(--bg);color:var(--fg)}
.card .tools input:focus{outline:none;border-color:var(--accent)}
.taginput{font:inherit;width:220px;padding:6px 10px;border-radius:6px;border:1px solid var(--accent);
  background:var(--bg);color:var(--fg);outline:none}
.card .empty{padding:14px;color:var(--muted)}
.card .scroll{max-height:560px;overflow:auto}
.toggle{background:none;border:0;color:var(--accent);padding:0;font-size:12px}
.toggle:hover{text-decoration:underline;filter:none}

table{border-collapse:collapse;width:100%;table-layout:fixed}
td,th{padding:7px 14px;text-align:left;vertical-align:top;border-bottom:1px solid color-mix(in srgb,var(--border) 70%,transparent)}
tr:last-child>td{border-bottom:0}
thead th{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;
  background:var(--bg);position:sticky;top:0;z-index:1}
tr.grp th{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;
  padding-top:12px;background:color-mix(in srgb,var(--fg) 2%,var(--bg))}
td.label{color:var(--muted);width:42%}
td.label code{display:block;color:var(--muted);opacity:.8;margin-top:1px;word-break:break-all}
td.val{word-break:break-word}
td.old{color:var(--muted)}
pre.val{margin:0;white-space:pre-wrap;word-break:break-word;max-height:9em;overflow:auto;
  font-family:Consolas,ui-monospace,monospace;font-size:12px}
.none{color:var(--muted);font-style:italic}
.tr{display:inline-block;min-width:18px;margin-right:6px;font-size:12px;font-weight:700;text-align:center}
.tr.up{color:var(--up)}.tr.down{color:var(--down)}.tr.changed{color:var(--muted)}
.tr.added{color:var(--up)}.tr.removed{color:var(--removed)}
.status{font-size:11px;border-radius:9px;padding:1px 7px}
.status.added{background:color-mix(in srgb,var(--up) 18%,var(--bg));color:var(--up)}
.status.removed{background:color-mix(in srgb,var(--removed) 15%,var(--bg));color:var(--removed)}
pre.diff{margin:0;padding:12px 14px;overflow:auto;max-height:60vh;font-family:Consolas,ui-monospace,monospace;font-size:12px}
.diff .add{color:var(--up)}.diff .del{color:var(--removed)}.diff .hdr{color:var(--muted)}

.placeholder{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;
  gap:8px;color:var(--muted);min-height:60vh;padding:24px}
.placeholder svg{opacity:.5}
.placeholder b{color:var(--fg);font-size:14px}
.loading{opacity:.55;transition:opacity .15s}

@media (max-width:760px){
  .app{grid-template-columns:1fr;grid-template-rows:auto 1fr;height:auto;min-height:100vh}
  .side{border-right:0;border-bottom:1px solid var(--border)}
  .list{max-height:38vh}
  .main{overflow:visible}
  .content{padding:16px}
  .banner,.error{margin:12px 16px 0}
  .grid{grid-template-columns:1fr}
  .vs{grid-template-columns:1fr}.vs .arrow{display:none}
}
</style></head><body>
<div class="app">
  <aside class="side">
    <div class="side-head"><h1>Versions</h1><div class="project" id="project"></div><div class="plates" id="plates"></div></div>
    <div class="list" id="list" tabindex="0"></div>
    <div class="side-foot">Click to view a version · <b>Ctrl</b>+click a second one to compare</div>
  </aside>
  <main class="main" id="main">
    <div class="banner" id="warn" hidden></div>
    <div class="error" id="err" hidden></div>
    <div class="content" id="detail"></div>
  </main>
</div>
<script>
var state = null, sel = [], detail = null, filters = {}, open = {}, rawDiff = null, tagging = false;
var pinned = null;  // the plate history picked here; null = follow the plate selected in OrcaSlicer
var ARROWS = {up: '▲', down: '▼', changed: '●', added: '+', removed: '−'};
function $(id){ return document.getElementById(id); }
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function send(m){ if (!('plate' in m)) m.plate = pinned; orca.postMessage(m); }
function plateLabel(p){ return p.plate ? 'Plate ' + p.plate + (p.name ? ' · ' + p.name : '') : 'Before plates'; }
function shown(){ return (state.plates || []).filter(function(p){ return p.id === state.plate; })[0]; }
function byId(sha){ return (state && state.history || []).filter(function(v){ return v.sha === sha; })[0]; }
function indexOf(sha){ return (state && state.history || []).map(function(v){ return v.sha; }).indexOf(sha); }
function when(v, withDate){
  var d = new Date(v.time * 1000);
  var t = d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  return withDate ? d.toLocaleDateString([], {day:'numeric', month:'short', year:'numeric'}) + ', ' + t : t;
}
function dayLabel(v){
  var d = new Date(v.time * 1000), today = new Date(), y = new Date(); y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return 'Today';
  if (d.toDateString() === y.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], {weekday:'long', day:'numeric', month:'long', year:'numeric'});
}
function dest(v){
  var d = (v.trailers || {}).Destination || '';
  if (d === 'file') return 'Exported to file';
  if (d.indexOf('printer:') === 0) return 'Sent to printer' + (d.length > 8 ? ' · ' + d.slice(8) : '');
  return d;
}
function title(v){ return v.title || v.message.split('\n')[0]; }
function summary(v){ return v.summary ? v.summary.replace(/_/g, ' ') : (indexOf(v.sha) === state.history.length - 1 ? 'First version' : ''); }

// ---------------------------------------------------------------- list
function renderList(){
  $('project').textContent = state.project;
  $('project').title = state.project;
  var plates = state.plates || [];
  $('plates').innerHTML = plates.length > 1 ? plates.map(function(p){
    var tip = p.plate ? (p.current ? 'Selected in OrcaSlicer' : 'Plate ' + p.plate + ' history') :
      'Versions saved before each plate had its own history: all plates mixed';
    return '<button class="pill' + (p.id === state.plate ? ' on' : '') + (p.plate ? '' : ' legacy') + '" data-plate="' +
      esc(p.id) + '" title="' + esc(tip) + '">' + (p.current ? '<span class="dot"></span>' : '') +
      '<span class="n">' + esc(plateLabel(p)) + '</span><span class="c">' + p.count + '</span></button>'; }).join('') : '';
  var html = state.plate === 'main' ? '<div class="note">Saved before each plate had its own history: ' +
    'these versions mix all plates. New versions go to the plate you export.</div>' : '', lastDay = null;
  if (!state.history.length){ $('list').innerHTML = html; return; }
  state.history.forEach(function(v){
    var day = dayLabel(v);
    if (day !== lastDay){ html += '<div class="day">' + esc(day) + '</div>'; lastDay = day; }
    var i = sel.indexOf(v.sha), slot = sel.length === 2 && i >= 0 ?
      '<span class="slot">' + (indexOf(v.sha) > indexOf(sel[0] === v.sha ? sel[1] : sel[0]) ? 'A' : 'B') + '</span>' : '';
    html += '<div class="ver' + (i >= 0 ? ' sel' : '') + '" data-sha="' + v.sha + '">' +
      '<div class="top"><span class="title">' + esc(title(v)) + '</span>' + slot +
      '<span class="time">' + when(v) + '</span></div>' +
      (summary(v) ? '<div class="sum">' + esc(summary(v)) + '</div>' : '') +
      '<div class="badges"><span class="badge">' + esc(dest(v)) + '</span>' +
      v.tags.map(function(t){ return '<span class="badge tag">' + esc(t) + '</span>'; }).join('') +
      (state.advanced ? '<span class="badge sha">' + v.sha.slice(0, 8) + '</span>' : '') + '</div></div>';
  });
  $('list').innerHTML = html;
}
$('list').onclick = function(e){
  var el = e.target.closest('.ver'); if (!el) return;
  var sha = el.dataset.sha, multi = e.ctrlKey || e.metaKey || e.shiftKey;
  tagging = false;
  if (!multi) sel = [sha];
  else if (sel.indexOf(sha) >= 0) { if (sel.length > 1) sel.splice(sel.indexOf(sha), 1); }
  else sel = sel.length ? [sel[sel.length - 1], sha] : [sha];
  select();
};
$('plates').onclick = function(e){
  var el = e.target.closest('[data-plate]'); if (!el) return;
  pinned = el.dataset.plate === state.current ? null : el.dataset.plate;
  send({cmd:'state'});
};
$('list').onkeydown = function(e){
  if (!state || !state.history.length || (e.key !== 'ArrowDown' && e.key !== 'ArrowUp')) return;
  e.preventDefault();
  var i = sel.length ? indexOf(sel[sel.length - 1]) : -1;
  i = Math.max(0, Math.min(state.history.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)));
  sel = [state.history[i].sha]; tagging = false; select();
  var el = document.querySelector('.ver.sel'); if (el) el.scrollIntoView({block:'nearest'});
};
function select(){
  rawDiff = null;
  renderList();
  $('detail').classList.add('loading');
  if (sel.length === 2){
    var ab = sel.slice().sort(function(x, y){ return indexOf(y) - indexOf(x); });  // older first
    send({cmd:'compare', a:ab[0], b:ab[1]});
  } else if (sel.length === 1) send({cmd:'show', sha:sel[0]});
}

// ---------------------------------------------------------------- tables
function cell(v){
  if (v == null) return '<span class="none">—</span>';
  if (v === '') return '<span class="none">empty</span>';
  return (v.length > 90 || v.indexOf('\n') >= 0) ? '<pre class="val">' + esc(v) + '</pre>' : esc(v);
}
function labelCell(r, sectionKind){
  var raw = state.advanced && (sectionKind === 'settings' || sectionKind === 'modified') && r.key !== r.label ?
    '<code>' + esc(r.key) + '</code>' : '';
  return '<td class="label">' + esc(r.label) + raw + '</td>';
}
function rowsHtml(s, compare){
  var html = '', group = null, cols = compare ? 3 : 2, q = (filters[s.id] || '').toLowerCase();
  s.rows.forEach(function(r){
    if (q && (r.label + ' ' + r.key + ' ' + (compare ? r.old + ' ' + r.new : r.value)).toLowerCase().indexOf(q) < 0) return;
    if ((r.group || null) !== group){
      group = r.group || null;
      html += '<tr class="grp"><th colspan="' + cols + '">' + esc(group || 'Other') + '</th></tr>';
    }
    if (compare) html += '<tr>' + labelCell(r, s.kind) + '<td class="val old">' + cell(r.old) + '</td>' +
      '<td class="val"><span class="tr ' + r.trend + '" title="' + r.trend + '">' + ARROWS[r.trend] + '</span>' + cell(r.new) + '</td></tr>';
    else html += '<tr>' + labelCell(r, s.kind) + '<td class="val">' + cell(r.value) + '</td></tr>';
  });
  return html || '<tr><td colspan="' + cols + '" class="none">No matches</td></tr>';
}
var KIND = {object:'Object', plate:'Plate', presets:'', modified:'', settings:'', layers:'', geometry:''};
var WIDE = {modified:1, settings:1, geometry:1, layers:1};
function card(s, compare){
  var big = s.kind === 'settings' && !compare, isOpen = !big || open[s.id];
  var head = '<header>' + (KIND[s.kind] ? '<span class="kind">' + KIND[s.kind] + '</span>' : '') +
    '<h3>' + esc(s.title) + '</h3>' +
    (s.status && s.status !== 'changed' ? '<span class="status ' + s.status + '">' + s.status + '</span>' : '') +
    '<span class="count">' + s.rows.length + '</span>' +
    (big ? '<button class="toggle" data-open="' + esc(s.id) + '">' + (isOpen ? 'Hide' : 'Show') + '</button>' : '') + '</header>';
  if (!isOpen) return '<section class="card wide">' + head + '</section>';
  if (!s.rows.length) return '<section class="card' + (WIDE[s.kind] ? ' wide' : '') + '">' + head +
    '<div class="empty">' + (s.kind === 'modified' ? 'Nothing: this version uses the system presets as they are.' : 'Nothing here.') + '</div></section>';
  var tools = (big || s.rows.length > 25) ? '<div class="tools"><input placeholder="Filter settings…" data-filter="' +
    esc(s.id) + '" value="' + esc(filters[s.id] || '') + '"></div>' : '';
  var cols = compare ? '<colgroup><col style="width:34%"><col style="width:33%"><col></colgroup>' : '';
  var thead = compare ? '<thead><tr><th>Setting</th><th>Older</th><th>Newer</th></tr></thead>' : '';
  return '<section class="card' + (WIDE[s.kind] ? ' wide' : '') + '">' + head + tools +
    '<div class="' + (s.rows.length > 25 ? 'scroll' : '') + '"><table data-table="' + esc(s.id) + '">' + cols + thead +
    rowsHtml(s, compare) + '</table></div></section>';
}

// ---------------------------------------------------------------- detail
function renderDetail(){
  var el = $('detail');
  el.classList.remove('loading');
  if (!state.history.length){
    var p = shown(), many = (state.plates || []).length > 1;
    el.innerHTML = placeholder(many && p ? 'No versions of ' + plateLabel(p) + ' yet' : 'No versions yet',
      'Export a G-code or send it to the printer: each time something changed, a version is saved here.' +
      (many ? ' Each plate has its own history: the version goes to the plate you export.' : ''));
    return;
  }
  if (!detail || !sel.length){
    el.innerHTML = placeholder('Select a version', 'Click a version to see its settings, objects and plates. Ctrl+click a second one to compare them.');
    return;
  }
  if (detail.type === 'view') renderView(el); else renderCompare(el);
}
function placeholder(t, s){
  return '<div class="placeholder"><svg width="40" height="40" viewBox="0 0 20 20"><circle cx="10" cy="10" r="7.5" fill="none" stroke="currentColor"/>' +
    '<polyline points="10 5.5 10 10 13 12" fill="none" stroke="currentColor" stroke-linecap="round"/></svg><b>' + esc(t) + '</b><div>' + esc(s) + '</div></div>';
}
function renderView(el){
  var typed = $('tagname') ? $('tagname').value : '';  // a re-render (e.g. on focus) keeps what's typed
  var v = byId(detail.sha); if (!v) return;
  var prev = state.history[indexOf(v.sha) + 1];
  var out = (v.trailers || {}).Output;
  var p = shown(), where = (state.plates || []).length > 1 && p ? plateLabel(p) + ' · ' : '';
  el.innerHTML = '<div class="head"><div style="min-width:0"><div class="kicker">' + esc(where) + 'Version · ' + esc(when(v, true)) + '</div>' +
    '<h2>' + esc(title(v)) + '</h2><div class="meta"><span class="badge">' + esc(dest(v)) + '</span>' +
    v.tags.map(function(t){ return '<span class="badge tag">' + esc(t) + '</span>'; }).join('') +
    (state.advanced ? '<span class="badge sha">' + v.sha + '</span>' : '') +
    (out ? '<span title="' + esc(out) + '">' + esc(out.split(/[\\/]/).pop()) + '</span>' : '') + '</div></div>' +
    '<div class="actions">' + (prev ? '<button class="ghost" data-act="prev">Compare with previous</button>' : '') +
    (tagging ? '<input class="taginput" id="tagname" placeholder="Tag name, e.g. good-PETG" maxlength="60" autocomplete="off">' +
      '<button data-act="tagsave">Save tag</button><button class="ghost" data-act="tagcancel">Cancel</button>' :
      '<button class="ghost" data-act="tag">Add tag…</button><button data-act="extract">Extract .3mf</button>') + '</div></div>' +
    '<div class="grid">' + detail.sections.map(function(s){ return card(s, false); }).join('') + '</div>';
  if (tagging){ $('tagname').value = typed; $('tagname').focus(); }
}
function saveTag(){
  var name = $('tagname').value.replace(/^-+|-+$/g, '');
  if (!name) { $('tagname').focus(); return; }
  tagging = false;
  send({cmd:'tag_create', name:name, sha:sel[0]});
  renderDetail();
}
function renderCompare(el){
  var a = byId(detail.a), b = byId(detail.b); if (!a || !b) return;
  var n = detail.sections.reduce(function(t, s){ return t + s.rows.length; }, 0);
  var side = function(v, k){ return '<div class="side-card"><div class="kicker">' + k + ' · ' + esc(when(v, true)) +
    (state.advanced ? ' · <span class="mono">' + v.sha.slice(0, 8) + '</span>' : '') + '</div><div class="t">' + esc(title(v)) + '</div></div>'; };
  el.innerHTML = '<div class="head"><div style="min-width:0;flex:1"><div class="kicker">Comparison</div>' +
    '<h2>' + (n ? n + ' difference' + (n === 1 ? '' : 's') : 'No differences') + '</h2></div>' +
    '<div class="actions">' + (state.advanced ? '<button class="ghost" data-act="raw">' + (rawDiff === null ? 'Raw diff' : 'Hide raw diff') + '</button>' : '') +
    '<button class="ghost" data-act="close">Close comparison</button></div></div>' +
    '<div class="vs">' + side(a, 'Older (A)') + '<div class="arrow">→</div>' + side(b, 'Newer (B)') + '</div>' +
    '<div class="legend"><span><span class="tr up">▲</span>higher</span><span><span class="tr down">▼</span>lower</span>' +
    '<span><span class="tr changed">●</span>changed</span><span><span class="tr added">+</span>added</span>' +
    '<span><span class="tr removed">−</span>removed</span></div>' +
    (rawDiff !== null ? '<section class="card wide" style="margin-bottom:16px"><header><h3>Raw diff</h3></header><pre class="diff">' + diffHtml(rawDiff) + '</pre></section>' : '') +
    (n ? '<div class="grid">' + detail.sections.map(function(s){ return card(s, true); }).join('') + '</div>' :
      placeholder('Same content', 'These two versions have the same settings, objects and plates.'));
}
function diffHtml(text){
  return esc(text || '(no differences)').split('\n').map(function(l){
    var c = /^(---|\+\+\+|@@|#)/.test(l) ? 'hdr' : l[0] === '+' ? 'add' : l[0] === '-' ? 'del' : '';
    return '<span class="' + c + '">' + l + '</span>'; }).join('\n');
}
$('detail').onclick = function(e){
  var t = e.target.closest('[data-act],[data-open]'); if (!t) return;
  if (t.dataset.open){ open[t.dataset.open] = !open[t.dataset.open]; renderDetail(); return; }
  var act = t.dataset.act;
  if (act === 'prev'){ var p = state.history[indexOf(sel[0]) + 1]; if (p){ sel = [sel[0], p.sha]; select(); } }
  else if (act === 'tag'){ tagging = true; renderDetail(); }
  else if (act === 'tagsave') saveTag();
  else if (act === 'tagcancel'){ tagging = false; renderDetail(); }
  else if (act === 'extract') send({cmd:'extract', sha:sel[0]});
  else if (act === 'close'){ sel = [detail.b]; select(); }
  else if (act === 'raw'){ if (rawDiff === null) send({cmd:'diff', a:detail.a, b:detail.b}); else { rawDiff = null; renderDetail(); } }
};
$('detail').onkeydown = function(e){
  if (e.target.id !== 'tagname') return;
  if (e.key === 'Enter') saveTag();
  else if (e.key === 'Escape'){ tagging = false; renderDetail(); }
};
$('detail').oninput = function(e){
  if (e.target.id === 'tagname'){  // git names can't have spaces: turn them into dashes as you type
    var t = e.target, caret = t.selectionStart, value = t.value.replace(/\s/g, '-');
    if (value !== t.value){ t.value = value; t.setSelectionRange(caret, caret); }
    return;
  }
  var id = e.target.dataset && e.target.dataset.filter; if (!id || !detail) return;
  filters[id] = e.target.value;
  var s = detail.sections.filter(function(x){ return x.id === id; })[0];
  var table = document.querySelector('table[data-table="' + CSS.escape(id) + '"]');
  if (!s || !table) return;
  // only the rows change: the input keeps focus and caret
  var fixed = ['colgroup', 'thead'].map(function(t){ var x = table.querySelector(t); return x ? x.outerHTML : ''; }).join('');
  table.innerHTML = fixed + rowsHtml(s, detail.type === 'compare');
};

// ---------------------------------------------------------------- messages
function showError(text){ $('err').hidden = !text; $('err').textContent = text || ''; }
orca.onMessage(function(m){
  if (m.type === 'state'){
    // another plate selected in OrcaSlicer: follow it, even if a history was picked here
    if (state && pinned && m.current !== state.current){ pinned = null; send({cmd:'state'}); return; }
    if (state && m.plate !== state.plate){ sel = []; detail = null; tagging = false; rawDiff = null; }
    state = m;
    $('warn').hidden = !m.warning; $('warn').textContent = m.warning || '';
    sel = sel.filter(function(s){ return byId(s); });
    if (!sel.length && m.history.length) sel = [m.history[0].sha];
    if (!sel.length) detail = null;
    showError('');
    renderList();
    if (sel.length) select(); else renderDetail();
  }
  else if (m.type === 'refresh') send({cmd:'state'});
  else if (m.type === 'plate'){ pinned = null; send({cmd:'state'}); }
  else if (m.type === 'error'){ showError(m.text); $('detail').classList.remove('loading'); }
  else if (m.type === 'view'){ if (sel.length === 1 && sel[0] === m.sha){ detail = m; renderDetail(); } }
  else if (m.type === 'compare'){ if (sel.length === 2 && sel.indexOf(m.a) >= 0 && sel.indexOf(m.b) >= 0){ detail = m; renderDetail(); } }
  else if (m.type === 'diff'){ rawDiff = m.text; renderDetail(); }
});
// the advanced mode is changed in the Plugins dialog: pick it up when the tab is back in focus
window.addEventListener('focus', function(){ send({cmd:'state'}); });
document.addEventListener('visibilitychange', function(){ if (!document.hidden) send({cmd:'state'}); });
send({cmd:'state'});
</script></body></html>"""
