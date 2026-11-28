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
import shutil
import string
import configparser

STEAM_DEFAULT = r"C:\Program Files (x86)\Steam"
INI_NAME = "ac2rf2.ini"
DEFAULT_TEXTURE_SCALE = 1.0
DEFAULT_TEXTURE_SCALE_MIN_SIZE = 1024
DEFAULT_ROAD_SMOOTHNESS = 0.0
DEFAULT_MESH_COARSENING = 0.0

DEFAULT_AC_SURFACES_INI = """[SURFACE_0]
KEY=ROAD
FRICTION=1
DAMPING=0
WAV=
WAV_PITCH=0
FF_EFFECT=NULL
DIRT_ADDITIVE=0
BLACK_FLAG_TIME=0
IS_VALID_TRACK=1
SIN_HEIGHT=0
SIN_LENGTH=0
IS_PITLANE=0
VIBRATION_GAIN=0
VIBRATION_LENGTH=0

[SURFACE_1]
KEY=GRASS
FRICTION=0.6
DAMPING=0
WAV=grass.wav
WAV_PITCH=0
FF_EFFECT=NULL
DIRT_ADDITIVE=1
IS_VALID_TRACK=0
BLACK_FLAG_TIME=0
SIN_HEIGHT=0.03
SIN_LENGTH=0.5
IS_PITLANE=0
VIBRATION_GAIN=0.2
VIBRATION_LENGTH=0.6

[SURFACE_2]
KEY=KERB
FRICTION=0.92
DAMPING=0
WAV=kerb.wav
WAV_PITCH=1.3
FF_EFFECT=1
DIRT_ADDITIVE=0
BLACK_FLAG_TIME=0
IS_VALID_TRACK=1
SIN_HEIGHT=0
SIN_LENGTH=0
IS_PITLANE=0
VIBRATION_GAIN=1.0
VIBRATION_LENGTH=1.5

[SURFACE_3]
KEY=SAND
FRICTION=0.8
DAMPING=0.1
WAV=sand.wav
WAV_PITCH=0
FF_EFFECT=0
DIRT_ADDITIVE=1
IS_VALID_TRACK=0
BLACK_FLAG_TIME=0
SIN_HEIGHT=0.04
SIN_LENGTH=0.5
IS_PITLANE=0
VIBRATION_GAIN=0.2
VIBRATION_LENGTH=0.3
"""


def validate_mesh_coarsening(coarsening):
    """Validate normalized mesh_coarsening in [0.0, 1.0] and return float."""
    value = float(coarsening)
    if not math.isfinite(value) or not (0.0 <= value <= 1.0):
        raise ValueError(
            "mesh_coarsening must be between 0.0 and 1.0, got %r" % (coarsening,)
        )
    return value


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


def is_rf2_dir(path):
    """True when path points to a plausible rFactor 2 installation folder."""
    if not path or not os.path.isdir(path):
        return False
    indicators = (
        os.path.join(path, "Support", "Tools", "MAS2.exe"),
        os.path.join(path, "Support", "Tools", "MapConverter", "texconv.exe"),
        os.path.join(path, "Bin64", "ModMgr.exe"),
        os.path.join(path, "Bin64", "rFactor2.exe"),
        os.path.join(path, "rFactor2.exe"),
        os.path.join(path, "ModDev"),
        os.path.join(path, "Core"),
    )
    return any(os.path.exists(p) for p in indicators)


def _rf2_from_uninstall_registry():
    """Query Windows uninstall registry for Steam App 365960 (rFactor 2)."""
    try:
        import winreg
    except ImportError:
        return None
    subkeys = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 365960",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 365960",
    )
    for subkey in subkeys:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    path = winreg.QueryValueEx(k, "InstallLocation")[0]
                    if path and is_rf2_dir(path):
                        return os.path.realpath(path)
            except OSError:
                continue
    return None


def probable_rf2_paths():
    """Probe the most probable rFactor 2 installation paths on the system without disk scanning."""
    candidates = []

    # Program Files Steam paths
    for env_var in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        pf = os.environ.get(env_var)
        if pf:
            candidates.append(os.path.join(pf, "Steam", "steamapps", "common", "rFactor 2"))
            candidates.append(os.path.join(pf, "SteamLibrary", "steamapps", "common", "rFactor 2"))

    # Roots of other drives (A-Z)
    for letter in string.ascii_uppercase:
        root = "%s:\\" % letter
        if not os.path.exists(root):
            continue
        candidates.extend([
            os.path.join(root, "Steam", "steamapps", "common", "rFactor 2"),
            os.path.join(root, "SteamLibrary", "steamapps", "common", "rFactor 2"),
            os.path.join(root, "Games", "Steam", "steamapps", "common", "rFactor 2"),
            os.path.join(root, "Games", "SteamLibrary", "steamapps", "common", "rFactor 2"),
            os.path.join(root, "Games", "rFactor 2"),
            os.path.join(root, "rFactor 2"),
            os.path.join(root, "rFactor2"),
        ])

        # Also check libraryfolders.vdf in root\Steam or root\SteamLibrary if present
        for steam_sub in ("Steam", "SteamLibrary"):
            vdf = os.path.join(root, steam_sub, "steamapps", "libraryfolders.vdf")
            if os.path.isfile(vdf):
                try:
                    with open(vdf, encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    for lpath in re.findall(r'"path"\s+"([^"]+)"', text):
                        lpath = os.path.normpath(lpath.replace("\\\\", "\\"))
                        candidates.append(os.path.join(lpath, "steamapps", "common", "rFactor 2"))
                except OSError:
                    pass

    return candidates


def save_rf2_dir_to_config(path, base_dir=None):
    """Persist discovered or entered rFactor 2 path into ac2rf2.ini."""
    root = base_dir if base_dir is not None else APP_DIR
    ini_path = os.path.join(root, INI_NAME)
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    if os.path.isfile(ini_path):
        try:
            with open(ini_path, encoding="utf-8-sig") as f:
                parser.read_file(f)
        except Exception:
            pass
    if not parser.has_section("paths"):
        parser.add_section("paths")
    parser.set("paths", "rf2", path)
    try:
        with open(ini_path, "w", encoding="utf-8") as f:
            parser.write(f)
    except Exception:
        pass


def prompt_for_rf2_dir():
    """Prompt the user interactively to enter the path to rFactor 2."""
    if not (sys.stdin and sys.stdin.isatty()):
        return None
    print("\nrFactor 2 installation was not found automatically.")
    print("Please enter or paste the path to your rFactor 2 folder")
    print("(e.g. C:\\Program Files (x86)\\Steam\\steamapps\\common\\rFactor 2):")
    while True:
        try:
            line = input("> ").strip().strip('"').strip("'")
        except (EOFError, KeyboardInterrupt):
            return None
        if not line:
            return None
        expanded = os.path.abspath(os.path.expanduser(os.path.expandvars(line)))
        if is_rf2_dir(expanded):
            save_rf2_dir_to_config(expanded)
            return expanded
        print("rFactor 2 not found in '%s'. Please enter a valid path (or Enter to skip):" % line)


def find_rf2_dir(prompt=True):
    """Find rFactor 2 installation without scanning the entire disk."""
    # 1. Environment override
    env_dir = os.environ.get("AC2RF2_RF2_DIR")
    if env_dir and is_rf2_dir(env_dir):
        return os.path.realpath(env_dir)

    # 2. Config file (ac2rf2.ini)
    try:
        cfg = load_config()
        cfg_dir = cfg.get("rf2_dir")
        if cfg_dir and is_rf2_dir(cfg_dir):
            return os.path.realpath(cfg_dir)
    except Exception:
        pass

    # 3. Steam uninstall registry key (instant lookup for Steam App 365960)
    reg_dir = _rf2_from_uninstall_registry()
    if reg_dir and is_rf2_dir(reg_dir):
        return os.path.realpath(reg_dir)

    # 4. Standard Steam libraries
    for lib in libraries():
        p = os.path.join(lib, "steamapps", "common", "rFactor 2")
        if is_rf2_dir(p):
            return os.path.realpath(p)

    # 5. Probable paths in Program Files and roots of other drives
    for p in probable_rf2_paths():
        if is_rf2_dir(p):
            return os.path.realpath(p)

    # 6. Interactive prompt
    if prompt:
        pasted = prompt_for_rf2_dir()
        if pasted and is_rf2_dir(pasted):
            return os.path.realpath(pasted)

    return os.path.join(steam_root(), "steamapps", "common", "rFactor 2")


def find_texconv(rf2_dir=None):
    """Locate texconv.exe, preferring rFactor 2's own copy."""
    env = os.environ.get("AC2RF2_TEXCONV")
    if env and os.path.isfile(env):
        return os.path.abspath(env)
    rf2 = rf2_dir or RF2_DIR
    candidates = (
        os.path.join(rf2, "Support", "Tools", "MapConverter", "texconv.exe"),
        os.path.join(rf2, "Support", "Tools", "texconv.exe"),
        os.path.join(rf2, "Bin64", "texconv.exe"),
    )
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    which = shutil.which("texconv.exe") or shutil.which("texconv")
    if which:
        return os.path.abspath(which)
    return os.path.join(rf2, "Support", "Tools", "MapConverter", "texconv.exe")


def _has_dist_layout(root):
    """Return True when ``root`` itself is a standalone dist directory."""
    return (
        os.path.isfile(os.path.join(root, "blender", "blender.exe"))
        and os.path.isfile(os.path.join(root, "blender", "blender_scripts", "blender_stage.py"))
    )


def dist_ready(tools_root=None):
    """Return True when the dist ``blender/`` bundle exists."""
    root = TOOLS_ROOT if tools_root is None else tools_root
    return _has_dist_layout(root)


def resolve_paths(app_root=None, frozen=None, env=None):
    """Resolve app, dist, Blender, and workspace paths without hardcoded local paths.

    - Running from ``dist`` (frozen EXE or directly inside a ``dist`` folder):
      always uses dist-local Blender and scripts under ``app_root``.
    - Running from Python directly: resolves Blender from ``<app_root>/dist/blender/``
      (honoring an explicit ``AC2RF2_BLENDER`` override when set).
    """
    is_frozen = getattr(sys, "frozen", False) if frozen is None else bool(frozen)
    root = os.path.abspath(app_root) if app_root is not None else app_dir()
    environ = os.environ if env is None else env

    is_standalone = bool(is_frozen or _has_dist_layout(root))
    dist_dir = root if is_standalone else os.path.join(root, "dist")
    tools_root = dist_dir

    bundled_blender = os.path.join(tools_root, "blender", "blender.exe")
    bundled_blender_stage = os.path.join(tools_root, "blender", "blender_scripts", "blender_stage.py")

    if is_standalone:
        blender = bundled_blender
        blender_stage = bundled_blender_stage
        work_root = os.path.join(root, "work")
    else:
        blender = environ.get("AC2RF2_BLENDER") or bundled_blender
        local_stage = os.path.join(root, "blender_stage.py")
        blender_stage = local_stage if os.path.isfile(local_stage) else bundled_blender_stage
        work_root = os.path.join(root, "out")

    return {
        "APP_DIR": root,
        "DIST_DIR": dist_dir,
        "TOOLS_ROOT": tools_root,
        "IS_STANDALONE": is_standalone,
        "BUNDLED_BLENDER": bundled_blender,
        "BUNDLED_BLENDER_STAGE": bundled_blender_stage,
        "BLENDER": blender,
        "BLENDER_STAGE": blender_stage,
        "WORK_ROOT": work_root,
    }


_RESOLVED = resolve_paths()
APP_DIR = _RESOLVED["APP_DIR"]
DIST_DIR = _RESOLVED["DIST_DIR"]
TOOLS_ROOT = _RESOLVED["TOOLS_ROOT"]
IS_STANDALONE = _RESOLVED["IS_STANDALONE"]
BUNDLED_BLENDER = _RESOLVED["BUNDLED_BLENDER"]
BUNDLED_BLENDER_STAGE = _RESOLVED["BUNDLED_BLENDER_STAGE"]
BLENDER = _RESOLVED["BLENDER"]
BLENDER_STAGE = _RESOLVED["BLENDER_STAGE"]
WORK_ROOT = _RESOLVED["WORK_ROOT"]

AC_DIR = os.environ.get("AC2RF2_AC_DIR") or game_dir("assettocorsa")
RF2_DIR = find_rf2_dir(prompt=True)
TEXCONV = find_texconv(RF2_DIR)
LOCATIONS = os.path.join(RF2_DIR, "ModDev", "Locations")
TERRAIN_TDF = os.path.join(LOCATIONS, "terrain.tdf")
INSTALL_LOCATIONS = LOCATIONS


def ensure_default_surfaces_ini(work_dir=None):
    """Write the built-in AC default ``surfaces.ini`` into ``work_dir`` and return its path."""
    target_dir = work_dir or WORK_ROOT
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, "surfaces.ini")
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_AC_SURFACES_INI)
    return path


def resolve_surfaces_ini(ac_root=None, work_dir=None):
    """Return AC's global ``surfaces.ini``, writing the built-in default into ``work_dir`` if needed."""
    if ac_root:
        candidate = os.path.join(ac_root, "system", "data", "surfaces.ini")
        if os.path.isfile(candidate):
            return candidate
    return ensure_default_surfaces_ini(work_dir)


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
        "mesh_coarsening": DEFAULT_MESH_COARSENING,
        "mesh_coarsening_source": "default",
        "rf2_dir": None,
        "rf2_dir_source": "default",
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
    if parser.has_option("track", "mesh_coarsening"):
        raw = parser.get("track", "mesh_coarsening").strip()
        try:
            coarsening = float(raw)
        except ValueError as exc:
            raise ValueError(
                "[track] mesh_coarsening must be a number, got %r" % (raw,)
            ) from exc
        if not math.isfinite(coarsening) or not (0.0 <= coarsening <= 1.0):
            raise ValueError(
                "[track] mesh_coarsening must be between 0.0 and 1.0, got %r"
                % (coarsening,)
            )
        result["mesh_coarsening"] = coarsening
        result["mesh_coarsening_source"] = "ini"
    if parser.has_section("paths"):
        for opt in ("rf2", "rf2_dir", "rfactor2"):
            if parser.has_option("paths", opt):
                val = parser.get("paths", opt).strip().strip('"').strip("'")
                if val:
                    result["rf2_dir"] = val
                    result["rf2_dir_source"] = "ini"
                    break
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
