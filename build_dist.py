"""Build a standalone Windows ``dist/`` bundle for AC-to-rF2 conversion.

Recreates ``dist/`` from scratch with:
  - ``ac2rf2.exe`` (bundled Python runtime + NumPy + converter exposing
    ``build_track``, ``convert_multiple_tracks``, ``package_rfcmp``, and drag-and-drop support)
  - ``ac2rf2.ini`` (default configuration, e.g. ``[track] texture_scale``)
  - ``README.md`` (usage instructions and list of bundled tools)
  - ``blender/`` (portable Blender 3.6 LTS with ``io_rf2_gmt_release``
    pre-installed inside ``3.6/scripts/addons/``)
  - ``blender/blender_scripts/`` (``blender_stage.py``, ``kn5.py``, ``backdrop.py``, ``mesh_coarsen.py``)
  - ``out/`` (destination for converted track .rfcmp component files, with ``readme.txt``)
  - ``work/`` (intermediate build workspace, with ``readme.txt``)

Usage:
    python build_dist.py [--dist <output dir>] [--archive]
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys
import tempfile

import steam_paths

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_NAME = "io_rf2_gmt_release"
BLENDER_SCRIPTS = ("blender_stage.py", "kn5.py", "backdrop.py", "mesh_coarsen.py")
HIDDEN_IMPORTS = (
    "backdrop",
    "bake_layers",
    "build_track",
    "check_setup",
    "convert_multiple_tracks",
    "convert_track",
    "finalize_track",
    "fix_textures",
    "gmt_dedup",
    "kn5",
    "make_aiw",
    "make_cam",
    "mesh_coarsen",
    "numpy",
    "package_rfcmp",
    "parallel",
    "road_smooth",
    "steam_paths",
    "track_zip",
    "verify_install",
)


OUT_README = (
    "Converted rFactor 2 track component (.rfcmp) files are written here.\n\n"
    "To install a track into rFactor 2:\n"
    "1. Launch rFactor 2.\n"
    "2. In the main menu, navigate to Content & Settings -> Manage Content.\n"
    "3. Locate the track component and click Install.\n\n"
    "Alternatively, you can install the .rfcmp package using rFactor 2's\n"
    "Mod Manager (ModMgr.exe) or copy it into your rFactor 2\\Packages folder.\n"
)

WORK_README = (
    "Intermediate conversion files (extracted textures, baked layers, raw GMTs,\n"
    "AIW/CAM files, and extracted track zips) are stored here during builds.\n"
    "Subfolders here can be deleted safely after conversion, or kept to reuse\n"
    "exported geometry with --skip-geometry.\n"
)


def find_blender_dir(explicit):
    if explicit:
        path = os.path.abspath(explicit)
        if os.path.isfile(path):
            path = os.path.dirname(path)
        return path
    return os.path.dirname(os.path.abspath(steam_paths.BLENDER))


def find_addon_dir(blender_dir, explicit):
    candidates = []
    if explicit:
        candidates.append(os.path.abspath(explicit))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            os.path.join(appdata, "Blender Foundation", "Blender", "3.6",
                         "scripts", "addons", ADDON_NAME)
        )
    candidates.append(
        os.path.join(blender_dir, "3.6", "scripts", "addons", ADDON_NAME)
    )
    for path in candidates:
        if os.path.isdir(path):
            return path
    raise FileNotFoundError(
        "Blender add-on %s not found; checked: %s" % (ADDON_NAME, ", ".join(candidates))
    )


def _ignore_blender(_dirpath, names):
    skip = set()
    for name in names:
        low = name.lower()
        if low == "blender.pdb" or low == "__pycache__":
            skip.add(name)
    return skip


def _ignore_pycache(_dirpath, names):
    return {n for n in names if n == "__pycache__"}


def dir_size_mb(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / (1024.0 * 1024.0)


def build_exe(dist_dir):
    try:
        import PyInstaller  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "PyInstaller is required to build ac2rf2.exe. "
            "Install it with: python -m pip install pyinstaller"
        ) from exc

    cli_script = os.path.join(HERE, "ac2rf2.py")
    with tempfile.TemporaryDirectory(prefix="ac2rf2-pyi-") as tmp:
        cmd = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--console",
            "--name", "ac2rf2",
            "--distpath", dist_dir,
            "--workpath", os.path.join(tmp, "build"),
            "--specpath", tmp,
            "--paths", HERE,
        ]
        for mod in HIDDEN_IMPORTS:
            cmd.extend(["--hidden-import", mod])
        cmd.append(cli_script)
        print("Building ac2rf2.exe with PyInstaller...", flush=True)
        result = subprocess.run(cmd, cwd=HERE)
        if result.returncode != 0:
            raise SystemExit("PyInstaller failed with exit code %d" % result.returncode)

    exe_path = os.path.join(dist_dir, "ac2rf2.exe")
    if not os.path.isfile(exe_path):
        raise SystemExit("PyInstaller did not produce %s" % exe_path)
    return exe_path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dist", default=os.path.join(HERE, "dist"),
                    help="output distribution directory (default: ./dist)")
    ap.add_argument("--blender", default=None,
                    help="path to Blender folder or blender.exe")
    ap.add_argument("--addon", default=None,
                    help="path to io_rf2_gmt_release addon folder")
    ap.add_argument("--archive", action="store_true",
                    help="create a .7z or .zip release archive with SHA-256 checksum")
    args = ap.parse_args(argv)

    dist_dir = os.path.abspath(args.dist)
    blender_src = find_blender_dir(args.blender)
    addon_src = find_addon_dir(blender_src, args.addon)

    for label, path, check in (
        ("Blender", os.path.join(blender_src, "blender.exe"), os.path.isfile),
        ("Blender addon", addon_src, os.path.isdir),
    ):
        if not check(path):
            raise SystemExit("Required source missing (%s): %s" % (label, path))

    print("Recreating %s ..." % dist_dir, flush=True)
    if os.path.exists(dist_dir):
        shutil.rmtree(dist_dir)
    os.makedirs(dist_dir, exist_ok=True)

    # 1. Copy Blender + embed io_rf2_gmt_release directly in Blender's addons folder.
    blender_dst = os.path.join(dist_dir, "blender")
    print("Copying Blender -> %s ..." % blender_dst, flush=True)
    shutil.copytree(blender_src, blender_dst, ignore=_ignore_blender)
    addon_dst = os.path.join(blender_dst, "3.6", "scripts", "addons", ADDON_NAME)
    if os.path.exists(addon_dst):
        shutil.rmtree(addon_dst)
    print("Copying %s -> %s ..." % (ADDON_NAME, addon_dst), flush=True)
    shutil.copytree(addon_src, addon_dst, ignore=_ignore_pycache)

    # 2. Copy plain scripts executed inside Blender's own Python runtime.
    scripts_dst = os.path.join(blender_dst, "blender_scripts")
    os.makedirs(scripts_dst, exist_ok=True)
    for script in BLENDER_SCRIPTS:
        shutil.copy2(os.path.join(HERE, script), os.path.join(scripts_dst, script))

    # 3. Copy standalone README.md, ac2rf2.ini, and create out/ and work/ folders with readme.txt.
    dist_readme = os.path.join(HERE, "DIST_README.md")
    if not os.path.isfile(dist_readme):
        raise SystemExit("Required source missing (DIST_README.md): %s" % dist_readme)
    shutil.copy2(dist_readme, os.path.join(dist_dir, "README.md"))

    ini_src = os.path.join(HERE, steam_paths.INI_NAME)
    if not os.path.isfile(ini_src):
        raise SystemExit("Required source missing (%s): %s" % (steam_paths.INI_NAME, ini_src))
    shutil.copy2(ini_src, os.path.join(dist_dir, steam_paths.INI_NAME))

    out_dir = os.path.join(dist_dir, "out")
    work_dir = os.path.join(dist_dir, "work")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(work_dir, exist_ok=True)
    with open(os.path.join(out_dir, "readme.txt"), "w", encoding="utf-8") as f:
        f.write(OUT_README)
    with open(os.path.join(work_dir, "readme.txt"), "w", encoding="utf-8") as f:
        f.write(WORK_README)

    # 7. Bundle Python runtime + converter CLI into dist/ac2rf2.exe.
    exe_path = build_exe(dist_dir)

    print("\nStandalone distribution ready: %s" % dist_dir)
    print("  executable:         %s (%.1f MB)" % (
        os.path.relpath(exe_path, dist_dir),
        os.path.getsize(exe_path) / (1024.0 * 1024.0),
    ))
    print("  documentation:      README.md")
    print("  blender:            %.1f MB (with %s)" % (dir_size_mb(blender_dst), ADDON_NAME))
    print("  total size:         %.1f MB" % dir_size_mb(dist_dir))
    print("\nUsage:")
    print("  Drag and drop an AC track folder onto %s" % exe_path)
    print("  %s build_track \"<AC track folder or zip>\"" % exe_path)
    print("  %s convert_multiple_tracks \"<tracks.txt>\"" % exe_path)

    if args.archive:
        stamp = datetime.date.today().strftime("%Y%m%d")
        archive_name = "ac2rf2_%s" % stamp
        archive_path = os.path.join(os.path.dirname(dist_dir), archive_name + ".zip")
        print("\nCreating archive %s..." % archive_path, flush=True)
        shutil.make_archive(os.path.splitext(archive_path)[0], "zip", dist_dir)
        import sha256sum
        digest = sha256sum.sha256_file(archive_path)
        sha_file = archive_path + ".sha256.txt"
        with open(sha_file, "w", encoding="utf-8") as sf:
            sf.write("%s  %s\n" % (digest, os.path.basename(archive_path)))
        print("Archive ready: %s (SHA-256: %s)" % (archive_path, digest))

    return 0


if __name__ == "__main__":
    sys.exit(main())
