"""Single-executable CLI and drag-and-drop entry point for AC-to-rF2.

Supports both command-line invocation and Windows Explorer drag-and-drop:

    ac2rf2.exe "<AC track folder or zip>"              (drag-and-drop or direct path)
    ac2rf2.exe "<tracks.txt>"                          (drag-and-drop batch list)
    ac2rf2.exe build_track "<AC track>" [options...]
    ac2rf2.exe convert_multiple_tracks "<tracks.txt>"
    ac2rf2.exe package_rfcmp "<stage_dir>"
"""
import importlib
import multiprocessing
import os
import runpy
import sys


COMMANDS = {
    "build_track": "build_track",
    "build_track.py": "build_track",
    "build": "build_track",
    "convert_multiple_tracks": "convert_multiple_tracks",
    "convert_multiple_tracks.py": "convert_multiple_tracks",
    "batch": "convert_multiple_tracks",
    "check_setup": "check_setup",
    "check_setup.py": "check_setup",
    "verify_install": "verify_install",
    "verify_install.py": "verify_install",
    "package_rfcmp": "package_rfcmp",
    "package_rfcmp.py": "package_rfcmp",
    "package": "package_rfcmp",
}

STAGES = {
    "build_track": "build_track",
    "convert_multiple_tracks": "convert_multiple_tracks",
    "convert_track": "convert_track",
    "fix_textures": "fix_textures",
    "bake_layers": "bake_layers",
    "make_aiw": "make_aiw",
    "make_cam": "make_cam",
    "finalize_track": "finalize_track",
    "check_setup": "check_setup",
    "verify_install": "verify_install",
    "package_rfcmp": "package_rfcmp",
    "track_zip": "track_zip",
}


def print_usage(prog):
    print(
        "AC to rFactor 2 standalone converter\n"
        "\n"
        "Usage:\n"
        "  Drag and drop an AC track folder, track .zip, or tracks .txt list onto %s\n"
        "\n"
        "  %s <track_dir_or_zip> [build_track options...]\n"
        "  %s build_track <track_dir_or_zip> [build_track options...]\n"
        "  %s convert_multiple_tracks <tracks.txt>\n"
        "  %s check_setup [track_dir] [options...]\n"
        "  %s verify_install <installed_dir> <work_dir>\n"
        "  %s package_rfcmp <stage_dir>\n"
        "\n"
        "Commands:\n"
        "  build_track (alias: build, build_track.py)\n"
        "      Convert a single AC track and package it into RFCMP.\n"
        "  convert_multiple_tracks (alias: batch, convert_multiple_tracks.py)\n"
        "      Batch-convert tracks listed as <path>;<layout> per line.\n"
        "  check_setup\n"
        "      Verify bundled tools and track setup.\n"
        "  verify_install\n"
        "      Verify an installed rF2 track against its working directory.\n"
        "  package_rfcmp\n"
        "      Compile MAS archives and an RFCMP component from a staged track folder.\n"
        "\n"
        "Run '%s <command> --help' for command-specific options."
        % (prog, prog, prog, prog, prog, prog, prog, prog)
    )


def _launched_from_explorer():
    """True when Windows created a dedicated console window (double-click / drag-and-drop)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        buf = (ctypes.c_uint32 * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(buf, len(buf))
        # PyInstaller --onefile keeps its parent bootloader attached to the console (2).
        limit = 2 if getattr(sys, "frozen", False) else 1
        return 0 < count <= limit
    except Exception:
        return False


def _pause():
    """Wait for a key press so the CLI window does not close automatically."""
    if os.environ.get("AC2RF2_NO_PAUSE"):
        return
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return
    except Exception:
        return
    try:
        print("\nPress any key to close...")
        sys.stdout.flush()
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
        msvcrt.getch()
    except Exception:
        try:
            input("\nPress Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass


def _pause_if_explorer():
    _pause()


def _dispatch(module_name, prog_name, args):
    mod = importlib.import_module(module_name)
    entry = getattr(mod, "main", None)
    old_argv = sys.argv[:]
    sys.argv = [prog_name] + list(args)
    try:
        if callable(entry):
            result = entry()
        else:
            runpy.run_module(module_name, run_name="__main__", alter_sys=True)
            result = 0
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    finally:
        sys.argv = old_argv
    if result is None:
        return 0
    return int(result)


def _prompt_layout(track_dir):
    """If track_dir has multiple layouts, prompt interactively when stdin is a TTY."""
    import check_setup

    if not os.path.isdir(track_dir):
        return None
    available = check_setup.layouts(track_dir)
    if not available:
        return None
    if not (sys.stdin and sys.stdin.isatty()):
        return None

    has_default = (
        os.path.exists(os.path.join(track_dir, "models.ini"))
        or os.path.isdir(os.path.join(track_dir, "ai"))
    )
    print("\nTrack '%s' has multiple layouts:" % os.path.basename(os.path.normpath(track_dir)))
    for idx, name in enumerate(available, 1):
        print("  %d. %s" % (idx, name))
    prompt = (
        "Choose layout (name or number, Enter for default): "
        if has_default
        else "Choose layout (name or number): "
    )
    while True:
        try:
            choice = input(prompt).strip()
        except EOFError:
            return None
        if not choice:
            if has_default:
                return None
            print("This track has no default layout; please choose one from the list.")
            continue
        if choice.isdigit():
            num = int(choice)
            if 1 <= num <= len(available):
                return available[num - 1]
        for name in available:
            if choice.casefold() == name.casefold():
                return name
        print("Unknown layout %r; enter a number (1-%d) or exact layout name."
              % (choice, len(available)))


def _is_track_path(path):
    if os.path.isdir(path):
        return True
    return os.path.isfile(path) and path.lower().endswith(".zip")


def _run_dropped_target(prog, target, extra_args):
    """Run conversion for a single dropped path (directory, .zip, or .txt batch list)."""
    if os.path.isfile(target) and target.lower().endswith(".txt") and not extra_args:
        return _dispatch("convert_multiple_tracks", "%s convert_multiple_tracks" % prog, [target])

    args = [target] + list(extra_args)
    has_layout_arg = any(a == "--layout" or a.startswith("--layout=") for a in extra_args)
    if not has_layout_arg:
        inspect_dir = target
        if os.path.isfile(target) and target.lower().endswith(".zip"):
            import track_zip
            inspect_dir = track_zip.extract(target)
            args[0] = inspect_dir
        chosen = _prompt_layout(inspect_dir)
        if chosen:
            args.extend(["--layout", chosen])

    return _dispatch("build_track", "%s build_track" % prog, args)


def _handle_direct_paths(prog, raw):
    """Handle one or more dropped track folders / zips / batch .txt files."""
    if len(raw) > 1 and all(_is_track_path(p) for p in raw):
        rc = 0
        total = len(raw)
        for index, target in enumerate(raw, 1):
            print("\n[%d/%d] Processing %s" % (index, total, target), flush=True)
            code = _run_dropped_target(prog, target, [])
            if code != 0:
                print("Conversion failed for %s (exit code %d)" % (target, code),
                      file=sys.stderr, flush=True)
                rc = code
        return rc
    return _run_dropped_target(prog, raw[0], raw[1:])


def main(argv=None):
    multiprocessing.freeze_support()
    raw = list(sys.argv[1:] if argv is None else argv)
    prog = os.path.basename(sys.argv[0]) or "ac2rf2.exe"

    if not raw:
        print_usage(prog)
        _pause_if_explorer()
        return 0

    if raw[0] in ("-h", "--help"):
        print_usage(prog)
        return 0

    if raw[0] == "--stage":
        if len(raw) < 2 or raw[1] not in STAGES:
            print("Unknown stage: %s" % (raw[1] if len(raw) >= 2 else ""),
                  file=sys.stderr)
            return 2
        stage_name = raw[1]
        return _dispatch(STAGES[stage_name], stage_name + ".py", raw[2:])

    cmd = raw[0]
    key = cmd.lower()
    if key in COMMANDS:
        mod_name = COMMANDS[key]
        try:
            return _dispatch(mod_name, "%s %s" % (prog, cmd), raw[1:])
        finally:
            _pause_if_explorer()

    if _is_track_path(cmd) or (os.path.isfile(cmd) and cmd.lower().endswith(".txt")):
        code = 1
        try:
            code = _handle_direct_paths(prog, raw)
        except Exception as exc:
            print("Error: %s" % exc, file=sys.stderr)
            code = 1
        finally:
            _pause_if_explorer()
        return code

    print("Unknown command or path not found: %s\n" % cmd, file=sys.stderr)
    print_usage(prog)
    _pause_if_explorer()
    return 2


if __name__ == "__main__":
    sys.exit(main())
