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
import difflib
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import zipfile
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


def changed_keys(old: dict, new: dict) -> list:
    a, b = parse_settings(old.get("settings.ini", b"")), parse_settings(new.get("settings.ini", b""))
    return sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))


def commit_version(repo: Repo, files: dict, subject: str, trailers: dict):
    """Commit `files` on the current branch. Returns the sha, or None if nothing changed.

    Only the text files decide whether something changed. The 3MF is rewritten with new timestamps
    on every export, so comparing it would create a version on every export.
    """
    parent = head_sha(repo)
    old = read_files(repo, parent)
    same = lambda n: old.get(n, b"") == files.get(n, b"")
    if parent is not None and all(same(n) for n in old.keys() | files.keys() if n != BINARY):
        return None

    keys = changed_keys(old, files)
    if parent is not None and keys:
        subject += f" — {len(keys)} settings changed: " + ", ".join(keys[:8]) + ("…" if len(keys) > 8 else "")
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
    repo.refs[current_branch(repo)] = c.id
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
    return [{
        "sha": e.commit.id.decode(),
        "time": e.commit.commit_time,
        "message": e.commit.message.decode(errors="replace"),
        "tags": tags.get(e.commit.id.decode(), []),
    } for e in repo.get_walker(include=[repo.refs[ref]])]


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


def tag_create(repo: Repo, name: str, sha: str):
    ref = _ref(b"refs/tags/", name)
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
    name = Path(output).name if output else "G-code"
    sha = commit_version(repo, files, f"{'Export' if dest == 'file' else 'Print'} {name}",
                         {"Destination": dest, "Output": output, "Project": project_path or "(not saved)"})
    if sha:
        _log(f"committed {sha[:8]} ({dest})")
        orca.host.ui.push_notification(orca.host.ui.NotificationLevel.RegularNotificationLevel,
                                       f"Orca Versioning: version {sha[:8]} saved")
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


class VersionsPage(orca.pages.PagesPluginCapabilityBase if orca else object):
    def get_name(self):
        return "Versions"

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
        if cmd == "tag_create":
            tag_create(repo, data["name"], data["sha"])
        elif cmd == "diff":
            self.post_message({"type": "diff", "text": diff(repo, data["a"], data["b"])})
            return
        elif cmd == "extract":
            base = Path(project_path) if project_path else _storage() / "untitled.3mf"
            label = (tags_by_sha(repo).get(data["sha"]) or [data["sha"][:8]])[0]
            out = extract_3mf(repo, data["sha"], base.with_name(f"{base.stem}@{label}.3mf"))
            orca.host.ui.push_notification(orca.host.ui.NotificationLevel.RegularNotificationLevel,
                                           f"Version extracted: {out}")
        self.post_message({
            "type": "state",
            "project": project_path or "(project not saved)",
            "warning": _warning,
            "history": history(repo),
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

PAGE_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{font:13px system-ui,sans-serif;margin:12px;color:var(--fg,#222)}
header{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
#project{opacity:.7;margin-right:auto}
table{border-collapse:collapse;width:100%}
td{padding:4px 6px;border-bottom:1px solid #8883;vertical-align:top}
tr.sel{background:#09f2}
.sha{font-family:monospace}.tag{background:#0a6;color:#fff;border-radius:3px;padding:0 4px;margin-left:4px;font-size:11px}
.msg{white-space:pre-wrap}.msg small{opacity:.6}
pre{background:#8881;padding:8px;overflow:auto;max-height:50vh}
.add{color:#080}.del{color:#c00}.hdr{opacity:.6}
#err{color:#c00}
#warn{background:#fc03;border-left:3px solid #e90;padding:6px 8px;margin-bottom:10px}
</style></head><body>
<header>
  <span id="project"></span>
  <button id="tag" disabled>Tag…</button>
  <button id="extract" disabled>Extract .3mf</button>
  <button id="diff" disabled>Diff (2)</button>
</header>
<div id="warn" hidden></div>
<div id="err"></div>
<table id="hist"></table>
<pre id="out" hidden></pre>
<script>
var sel = [], state = null;
function $(id){ return document.getElementById(id); }
function send(m){ $('err').textContent=''; orca.postMessage(m); }
function esc(s){ return s.replace(/[&<>]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]; }); }
function buttons(){
  $('tag').disabled = $('extract').disabled = sel.length !== 1;
  $('diff').disabled = sel.length !== 2;
}
function render(){
  $('project').textContent = state.project;
  $('warn').hidden = !state.warning;
  $('warn').textContent = state.warning || '';
  $('hist').innerHTML = state.history.map(function(c){
    var lines = c.message.split('\\n'), d = new Date(c.time*1000).toLocaleString();
    return '<tr data-sha="' + c.sha + '" class="' + (sel.indexOf(c.sha) >= 0 ? 'sel' : '') + '">' +
      '<td class="sha">' + c.sha.slice(0,8) + c.tags.map(function(t){ return '<span class="tag">' + esc(t) + '</span>'; }).join('') + '</td>' +
      '<td>' + d + '</td><td class="msg">' + esc(lines[0]) + '<br><small>' + esc(lines.slice(2).join('  ')) + '</small></td></tr>';
  }).join('') || '<tr><td>No versions yet: export or send a G-code.</td></tr>';
  buttons();
}
$('hist').onclick = function(e){
  var tr = e.target.closest('tr[data-sha]'); if (!tr) return;
  var s = tr.dataset.sha, i = sel.indexOf(s);
  if (i >= 0) sel.splice(i,1); else { sel.push(s); if (sel.length > 2) sel.shift(); }
  render();
};
$('tag').onclick = function(){ var n = prompt('Tag name'); if (n) send({cmd:'tag_create', name:n, sha:sel[0]}); };
$('extract').onclick = function(){ send({cmd:'extract', sha:sel[0]}); };
$('diff').onclick = function(){
  var order = state.history.map(function(c){ return c.sha; });
  var ab = sel.slice().sort(function(x,y){ return order.indexOf(y) - order.indexOf(x); });  // old → new
  send({cmd:'diff', a:ab[0], b:ab[1]});
};
orca.onMessage(function(m){
  if (m.type === 'state') { state = m; sel = sel.filter(function(s){ return m.history.some(function(c){ return c.sha === s; }); }); render(); }
  else if (m.type === 'refresh') send({cmd:'state'});
  else if (m.type === 'error') $('err').textContent = m.text;
  else if (m.type === 'diff') {
    $('out').hidden = false;
    $('out').innerHTML = esc(m.text || '(no differences)').split('\\n').map(function(l){
      var c = /^(---|\\+\\+\\+|@@|#)/.test(l) ? 'hdr' : l[0] === '+' ? 'add' : l[0] === '-' ? 'del' : '';
      return '<span class="' + c + '">' + l + '</span>'; }).join('\\n');
  }
});
send({cmd:'state'});
</script></body></html>"""
