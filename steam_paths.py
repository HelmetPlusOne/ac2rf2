"""Where Steam put Assetto Corsa and rFactor 2 on this machine.

Steam can install a game into any of its library folders, listed in
steamapps/libraryfolders.vdf. Hardcoding C:\\Program Files (x86)\\Steam broke
the converter the moment rFactor 2 was moved to another drive, and would miss
everything for anyone whose Steam itself lives elsewhere, so Steam's own folder
is read from the registry, where Steam records it.

Either game can also be named outright, for a copy Steam does not know about:
build_track.py --rf2 / --ac, which set AC2RF2_RF2_DIR / AC2RF2_AC_DIR for every
step it runs.
"""
import os
import re
import sys
import math
import configparser

STEAM_DEFAULT = r"C:\Program Files (x86)\Steam"
DEFAULT_BLENDER = r"D:\Apps\Blender\blender.exe"
INI_NAME = "ac2rf2.ini"
DEFAULT_TEXTURE_SCALE = 1.0
DEFAULT_TEXTURE_SCALE_MIN_SIZE = 1024
DEFAULT_ROAD_SMOOTHNESS = 0.0


def road_smoothness_to_sigma(smoothness):
    """Map normalized road_smoothness in [0.0, 1.0] uniformly to sigma in [0.0, 8.0] m.

      0.0 -> 0.0m (disabled)
      0.5 -> 4.0m (moderate)
      1.0 -> 8.0m (max)
    """
    value = float(smoothness)
    if not math.isfinite(value) or not (0.0 <= value <= 1.0):
        raise ValueError(
            "road_smoothness must be between 0.0 and 1.0, got %r" % (smoothness,)
        )
    if value == 0.0:
        return 0.0
    return round(value * 8.0, 6)


def app_dir():
    """Directory containing the running EXE (frozen) or repository scripts."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def steam_root():
    """Steam's own folder: from the registry, else the default install path."""
    try:
        import winreg
    except ImportError:
        return STEAM_DEFAULT
    keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for hive, key, value in keys:
        try:
            with winreg.OpenKey(hive, key) as k:
                path = winreg.QueryValueEx(k, value)[0]
        except OSError:
            continue
        if path and os.path.isdir(path):
            # Steam stores it lower case; realpath gives the folder's own case.
            return os.path.realpath(path)
    return STEAM_DEFAULT


def libraries():
    """Every Steam library folder, Steam's own first."""
    root = steam_root()
    out = [root]
    if os.path.normcase(STEAM_DEFAULT) != os.path.normcase(root):
        out.append(STEAM_DEFAULT)
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    try:
        text = open(vdf, encoding="utf-8", errors="replace").read()
    except OSError:
        return out
    for path in re.findall(r'"path"\s+"([^"]+)"', text):
        path = os.path.normpath(path.replace("\\\\", "\\"))
        if os.path.normcase(path) not in (os.path.normcase(p) for p in out):
            out.append(path)
    return out


def game_dir(folder):
    """steamapps/common/<folder> in whichever library has it, else Steam's own."""
    for lib in libraries():
        p = os.path.join(lib, "steamapps", "common", folder)
        if os.path.isdir(p):
            return p
    return os.path.join(steam_root(), "steamapps", "common", folder)


APP_DIR = app_dir()
BUNDLED_BLENDER = os.path.join(APP_DIR, "tools", "blender", "blender.exe")
BUNDLED_BLENDER_STAGE = os.path.join(APP_DIR, "tools", "blender_scripts", "blender_stage.py")
BUNDLED_TEXCONV = os.path.join(APP_DIR, "tools", "texconv", "texconv.exe")
BUNDLED_AC_DIR = os.path.join(APP_DIR, "data", "ac")
BUNDLED_MODMGR = os.path.join(APP_DIR, "tools", "modmgr", "ModMgr.exe")
BUNDLED_MAS2 = os.path.join(APP_DIR, "tools", "mas2", "MAS2.exe")

IS_STANDALONE = bool(
    getattr(sys, "frozen", False)
    or (os.path.isfile(BUNDLED_BLENDER) and os.path.isdir(os.path.join(APP_DIR, "data")))
)

_bundled_ac_ok = os.path.isfile(
    os.path.join(BUNDLED_AC_DIR, "system", "data", "surfaces.ini")
)

AC_DIR = (
    os.environ.get("AC2RF2_AC_DIR")
    or (BUNDLED_AC_DIR if _bundled_ac_ok else game_dir("assettocorsa"))
)
RF2_DIR = os.environ.get("AC2RF2_RF2_DIR") or game_dir("rFactor 2")
TEXCONV = (
    os.environ.get("AC2RF2_TEXCONV")
    or (BUNDLED_TEXCONV if os.path.isfile(BUNDLED_TEXCONV)
        else os.path.join(RF2_DIR, "Support", "Tools", "MapConverter", "texconv.exe"))
)
BLENDER = (
    os.environ.get("AC2RF2_BLENDER")
    or (BUNDLED_BLENDER if os.path.isfile(BUNDLED_BLENDER) else DEFAULT_BLENDER)
)
BLENDER_STAGE = (
    BUNDLED_BLENDER_STAGE
    if os.path.isfile(BUNDLED_BLENDER_STAGE)
    else os.path.join(APP_DIR, "blender_stage.py")
)
LOCATIONS = os.path.join(RF2_DIR, "ModDev", "Locations")
INSTALL_LOCATIONS = os.path.join(APP_DIR, "out") if IS_STANDALONE else LOCATIONS
WORK_ROOT = os.path.join(APP_DIR, "work") if IS_STANDALONE else os.path.join(APP_DIR, "out")


def load_config(base_dir=None):
    """Read optional ``ac2rf2.ini`` from the script/EXE directory."""
    root = base_dir if base_dir is not None else APP_DIR
    ini_path = os.path.join(root, INI_NAME)
    result = {
        "path": ini_path,
        "exists": False,
        "texture_scale": DEFAULT_TEXTURE_SCALE,
        "texture_scale_source": "default",
        "texture_scale_min_size": DEFAULT_TEXTURE_SCALE_MIN_SIZE,
        "texture_scale_min_size_source": "default",
        "road_smoothness": DEFAULT_ROAD_SMOOTHNESS,
        "road_smoothness_source": "default",
        "road_smooth_sigma": road_smoothness_to_sigma(DEFAULT_ROAD_SMOOTHNESS),
    }
    if not os.path.isfile(ini_path):
        return result

    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        with open(ini_path, encoding="utf-8-sig") as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as exc:
        raise ValueError("could not parse %s: %s" % (ini_path, exc)) from exc

    result["exists"] = True
    if parser.has_option("track", "texture_scale"):
        raw = parser.get("track", "texture_scale").strip()
        try:
            scale = float(raw)
        except ValueError as exc:
            raise ValueError(
                "[track] texture_scale must be a number, got %r" % (raw,)
            ) from exc
        if not (0.0 < scale <= 1.0):
            raise ValueError(
                "[track] texture_scale must be between 0 and 1, got %r" % (scale,)
            )
        result["texture_scale"] = scale
        result["texture_scale_source"] = "ini"
    if parser.has_option("track", "texture_scale_min_size"):
        raw = parser.get("track", "texture_scale_min_size").strip()
        try:
            min_size = int(raw)
        except ValueError as exc:
            raise ValueError(
                "[track] texture_scale_min_size must be an integer, got %r" % (raw,)
            ) from exc
        if min_size < 0:
            raise ValueError(
                "[track] texture_scale_min_size must be >= 0, got %r" % (min_size,)
            )
        result["texture_scale_min_size"] = min_size
        result["texture_scale_min_size_source"] = "ini"
    if parser.has_option("track", "road_smoothness"):
        raw = parser.get("track", "road_smoothness").strip()
        try:
            smoothness = float(raw)
        except ValueError as exc:
            raise ValueError(
                "[track] road_smoothness must be a number, got %r" % (raw,)
            ) from exc
        if not math.isfinite(smoothness) or not (0.0 <= smoothness <= 1.0):
            raise ValueError(
                "[track] road_smoothness must be between 0.0 and 1.0, got %r"
                % (smoothness,)
            )
        result["road_smoothness"] = smoothness
        result["road_smoothness_source"] = "ini"
        result["road_smooth_sigma"] = road_smoothness_to_sigma(smoothness)
    return result


if __name__ == "__main__":
    import sys
    if "--locations" in sys.argv:       # for scripts that only need this
        print(LOCATIONS)
        sys.exit(0)
    print("Standalone:    %s" % IS_STANDALONE)
    print("App dir:       %s" % APP_DIR)
    print("Steam:         %s" % steam_root())
    print("rFactor 2:     %s" % RF2_DIR)
    print("Assetto Corsa: %s" % AC_DIR)
    print("Blender:       %s (%s)" % (BLENDER, "found" if os.path.exists(BLENDER) else "MISSING"))
    print("texconv:       %s (%s)" % (TEXCONV, "found" if os.path.exists(TEXCONV) else "MISSING"))
    print("install into:  %s" % INSTALL_LOCATIONS)
