# Orca Versioning

OrcaSlicer plugin that saves a version (a git commit) every time you **export the G-code** or **send it to the printer**. Plain slicing never creates a version.

Each version holds:
- `settings.ini`: all resolved settings (print, filament, printer), one `key = value` per line.
- The text files in the 3MF's `Metadata/` folder, as they are: `model_settings.config` (per-object and per-plate overrides), `custom_gcode_per_layer.xml` (pauses and color changes), `layer_heights_profile.txt`, …
- `geometry.sha1`: hashes of meshes, painting and transforms (`3D/*`).
- `model.txt`: a readable summary of objects, parts, positions, rotations and scale.
- `project.3mf`: a full copy of the project, **unsaved changes included** (via `export_3mf_copy`). Your project file is never touched.

A new version is created only if at least one of these files changes. The `.3mf` alone doesn't count, because its bytes change on every export.

While the project has never been saved, versions go into `untitled.git`. On the first save that history moves to the project. A *new project* sets `untitled` aside (`untitled-<date>.git`).

The repos are standard bare git repos in the plugin's storage folder, `repos/<project>-<hash>.git`:

```bash
git --git-dir <storage>/repos/<project>.git log --all --graph --oneline
```

## Installation

1. Copy `orca_versioning.py` into `<data_dir>/orca_plugins/orca_versioning/`.
2. Restart OrcaSlicer and enable the plugin in the Plugins dialog. On first load, `uv` installs `dulwich`.
3. **Add "Version on export" to the process preset** (option `slicing_pipeline_plugin`). Without this, exporting to a file isn't caught, because OrcaSlicer has no lifecycle event for file export. Sends to the printer (Bambu print/send, OctoPrint & co.) are caught anyway.
   > A preset that references a plugin can't be sliced on a machine that doesn't have the plugin installed.

## Use

The **Versions** tab shows the project's history. Every version goes on `main`: branches aren't in the UI for now (the core functions are still in the code, ready to be wired back). You can:
- Click 1 version → **Tag…** or **Extract .3mf**. Extracting writes `<project>@<tag|sha>.3mf` next to the project, ready to open. To restore a version, use Extract .3mf.
- Click 2 versions → **Diff** of settings, objects and geometry.

## Known issue: OrcaSlicer 2.5.0 skips the plugin on export

In OrcaSlicer 2.5.0 the preset's in-memory plugin manifest (`plugins`) can stay empty even though it's right on disk. It gets built before the plugin has finished loading and isn't rebuilt afterwards. The export then silently skips "Version on export", and no version is created. Signs:
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
