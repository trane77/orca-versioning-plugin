# Orca Versioning

OrcaSlicer plugin that saves a version (a git commit) every time you **export the G-code** or **send it to the printer**. Plain slicing never creates a version.

Each version holds:
- `settings.ini`: all resolved settings (print, filament, printer), one `key = value` per line.
- The text files in the 3MF's `Metadata/` folder, as they are: `model_settings.config` (per-object and per-plate overrides), `custom_gcode_per_layer.xml` (pauses and color changes), `layer_heights_profile.txt`, …
- `geometry.sha1`: hashes of meshes, painting and transforms (`3D/*`).
- `model.txt`: a readable summary of objects, parts, positions, rotations and scale.
- `project.3mf`: a full copy of the project, **unsaved changes included** (via `export_3mf_copy`). Your project file is never touched.

**Each plate has its own history.** Export and print always work on the plate selected in OrcaSlicer, so the version goes to that plate's history (branch `plate-<n>`) and holds only what concerns it: the global settings (shared by all plates), that plate's objects (positions, per-object settings, meshes), its `plate_<n>.json`, its filament sequence and its pauses/color changes. Editing plate 2 doesn't create a version of plate 1; a global setting change shows up in each plate's history at its next export. `project.3mf` stays whole, so extracting a version always restores the full project.

A plate is identified by its **number**: if you delete or reorder plates, the history follows the number, not the content. Versions saved before plates had separate histories stay on `main`, shown as **Before plates** (all plates mixed, read-only).

A new version is created only if at least one of these files changes. The `.3mf` alone doesn't count, because its bytes change on every export.

While the project has never been saved, versions go into `untitled.git`. On the first save that history moves to the project. A *new project* sets `untitled` aside (`untitled-<date>.git`).

The repos are standard bare git repos in the plugin's storage folder, `repos/<project>-<hash>.git`:

```bash
git --git-dir <storage>/repos/<project>.git log --all --graph --oneline
```

## Installation

1. Copy `orca_versioning.py` into `<data_dir>/orca_plugins/orca_versioning/`.
2. Restart OrcaSlicer and enable the plugin in the Plugins dialog. On first load, `uv` installs `dulwich`.
3. **Add "Version on export" to the process preset** (Others > Slicing Pipeline Plugin, Advanced mode) and **save it as a user preset**. System presets can't be changed: an unsaved edit is lost, so every new project would need it again. With a user preset, new projects that use it are versioned with no extra step. Do this for every process preset you use. Without it, exporting to a file isn't caught: OrcaSlicer declares `GCodeExportStarted`/`GCodeExportFinished` but never fires them. Sends to the printer (Bambu print/send, OctoPrint & co.) are caught anyway.
   > A preset that references a plugin can't be sliced on a machine that doesn't have the plugin installed.

If the active preset wouldn't version exports, the plugin says so: a warning notification (once per change, checked on project open/new, preset select/save and after each slice) and a banner in the Versions tab.

## Use

The **Versions** tab (clock icon) lists the versions on the left, grouped by day. With more than one plate, buttons above the list pick the plate history (with its version count; the dot marks the plate selected in OrcaSlicer). The tab follows the plate you select in OrcaSlicer; a plate picked by hand stays until OrcaSlicer's selection changes.

- **Click a version** to see its content as tables: presets, settings changed from the system presets (by process / filament / printer), one table per object (size, position, rotation, scale, per-object settings), one per plate, pauses and color changes per layer, and all settings (with a filter). Actions: **Compare with previous**, **Add tag…**, **Extract .3mf** (writes `<project>@<tag|sha>.3mf` next to the project, ready to open: that's how you restore a version).
- **Ctrl+click a second version** (or *Compare with previous*) to compare: the same tables, only what differs, older and newer side by side. Next to each newer value: ▲ higher, ▼ lower, ● changed (not a number), + added, − removed.
- Arrow keys move through the list.

### Settings

Plugins dialog → Config → **Versions**:
- **Advanced mode** (off by default): shows version hashes, raw setting keys, the plugin's internal settings (`plugins`, `different_settings_to_system`, …), geometry file hashes and a **Raw diff** button in comparisons.

The tab picks up a change when it gets focus again.

## Known issue: OrcaSlicer 2.5.0 skips the plugin on export

In OrcaSlicer 2.5.0 the preset's in-memory plugin manifest (`plugins`) can stay empty even though it's right on disk. It gets built before the plugin has finished loading and isn't rebuilt afterwards. The export then silently skips "Version on export", and no version is created. The plugin detects this and shows an "OrcaSlicer bug" warning. Other signs:
- the exported G-code has `; plugins = ` (empty) next to `; slicing_pipeline_plugin = "Version on export"`;
- the debug log has `Slicing Pipeline: no plugin reference found for capability 'Version on export'; skipping`.

Workaround: in Process > Others > Slicing Pipeline Plugin, remove "Version on export" and add it back (this rebuilds the manifest), then save the preset. Fixed upstream in [OrcaSlicer#16884](https://github.com/OrcaSlicer/OrcaSlicer/pull/16884) (merged 2026-11-11): builds that include it resolve the capability by name.

## Not captured

- With **Bambu Lab** printers, "Export plate sliced file" (`.gcode.3mf`) runs no plugin post-process and fires no event, so it creates no version. The `.gcode` export and printing do create versions.
- "Save As" under a new name starts a new history; the old one stays under the old name.

## How it works (threads)

The triggers (`psGCodePostProcess`, `PrintJobStarted`, `SendJobStarted`, `UploadStarted`) arrive on worker threads. `export_3mf_copy` only works on the UI thread. The worker therefore queues the request and calls `post()` on a hidden dock panel (the "relay"). The panel echoes the message back, and its `on_message` runs on the UI thread, which takes the snapshot and makes the commit. If the content is identical to HEAD, no commit is made, so export + send create a single version.

## Test

```bash
pip install dulwich && python test_orca_versioning.py
```

## To verify in OrcaSlicer (not testable outside it)

- [ ] **Two exports in a row with no changes → one version only.** If you get two, some file inside the 3MF changes on every export: look at the diff in the Versions tab and add that entry to `SKIP`.
- [ ] The relay panel loads hidden and doesn't stay visible (it may flash for a moment at startup).
- [ ] `export_3mf_copy` from the relay works while the export or upload is still running.
- [ ] "Export G-code": a version with `Destination: file`. Slicing alone: no version.
- [ ] Send to your printer: which `Destination:` appears.
- [ ] Logs: `<data_dir>/log/python_*.log`, lines starting with `[orca-versioning]`.
- [ ] The Versions tab shows the clock icon (if not: log line `icon failed`, falls back to OrcaSlicer's `print-time`).
- [ ] Plugins dialog → Config → Versions shows the "Advanced mode" checkbox; toggling it changes the tab after it gets focus again.
- [ ] Two plates: export plate 1, then plate 2 → one version in each history, and the tab switches to the plate selected in OrcaSlicer (on `PlateSelected`, or when the tab gets focus again if that event isn't fired).
- [ ] Moving an object on plate 2 and exporting plate 1 → no new plate 1 version.
- [ ] Preset warning: with a process preset without "Version on export", a warning shows once (not on every slice) and the Versions tab shows the banner; picking a preset with it makes both go away.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md): you can use, study and modify the plugin for personal and other noncommercial purposes. Selling it, or using it for any commercial purpose, is not allowed without the author's permission. Any copy you pass on must keep the license and its `Required Notice` line.
