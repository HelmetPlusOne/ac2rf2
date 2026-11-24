# Assetto Corsa to rFactor 2 Track Converter

Standalone Windows converter for converting Assetto Corsa tracks into rFactor 2
locations and RFCMP packages.

Converted tracks are automatically packaged into MAS archives and an `.rfcmp`
component file placed in the `out\` folder next to `ac2rf2.exe`. You can install
the `.rfcmp` in-game using the **Content & Settings -> Manage Content** menu,
or directly using rFactor 2's Mod Manager (`ModMgr.exe`).

## Usage

### 1. Drag and Drop (Windows Explorer)

Drag and drop any of the following directly onto `ac2rf2.exe`:
- An **Assetto Corsa track folder** (e.g. `content\tracks\monza`)
- A **track `.zip` archive**
- A **`tracks.txt` batch list** containing one `<track_path>[;<layout>]` per line

If a track has multiple layouts, an interactive prompt asks which layout to convert.
When conversion completes, the console stays open until you press Enter.

### 2. Command Line

From Command Prompt or PowerShell:

```cmd
:: Convert a track (packages into MAS archives and RFCMP automatically)
ac2rf2.exe "D:\Steam\steamapps\common\assettocorsa\content\tracks\monza"

:: Convert a specific layout
ac2rf2.exe "D:\...\tracks\monza" --layout junior

:: Batch convert multiple tracks
ac2rf2.exe convert_multiple_tracks tracks.txt

:: Check bundled tools and system configuration
ac2rf2.exe check_setup

:: Manually package a staged track directory into RFCMP
ac2rf2.exe package_rfcmp out\monza\rf2\monza
```

### 3. Configuration (`ac2rf2.ini`)

An optional `ac2rf2.ini` next to `ac2rf2.exe` configures defaults:

```ini
[track]
# Scale factor per texture dimension in (0, 1].
# 1.0 keeps full resolution (default); 0.5 halves width and height.
texture_scale = 1.0

# Minimum texture size (max(w, h)) to apply texture_scale.
texture_scale_min_size = 1024

# Road surface smoothing in [0.0, 1.0] mapped to sigma in [0m, 8m].
# 0.0 disables smoothing; 0.5 applies moderate smoothing (sigma=4.0m) to remove
# high-frequency photogrammetry point-cloud jitter.
road_smoothness = 0.0
```

CLI options `--texture-scale`, `--texture-scale-min-size`, and `--road-smoothness`
override these settings on individual runs.

## Bundled Tools and Third-Party Licenses

This standalone package includes third-party software components:

| Component | Path | License |
| :--- | :--- | :--- |
| **Blender 3.6 LTS** | `tools\blender\` | **GNU GPL v2 or later** — Blender Foundation (https://www.blender.org). |
| **rF2 GMT Exporter** | `tools\blender\3.6\scripts\addons\io_rf2_gmt_release\` | **GNU GPL v2 or later** — Studio 397 (https://forum.studio-397.com). |
| **DirectXTex `texconv.exe`** | `tools\texconv\texconv.exe` | **MIT License** — Microsoft Corporation (https://github.com/microsoft/DirectXTex). |

This converter is licensed under the GNU General Public License v3 (see `LICENSE`).
