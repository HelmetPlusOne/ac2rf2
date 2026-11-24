"""Packages a converted rFactor 2 track into MAS archives and an RFCMP component.

Normal rFactor 2 (and online dedicated servers) requires tracks to be packaged
into MAS archives inside an RFCMP component file. This script automates that
entire pipeline headlessly:
1. Compiles the MAS archives (assets, materials, layout) using ModMgr.exe.
2. Enforces the 4 GiB MAS / RFCMP 32-bit container limit before invoking MAS2.exe.
3. Writes the component metadata to %APPDATA%\\cmpinfo.dat.
4. Automates MAS2.exe via Win32 WM_COMMAND messages (ctypes) to build the .rfcmp package.
5. Optionally installs the package into rFactor 2 via ModMgr.exe -i.

Usage:
    python package_rfcmp.py <track_or_rf2_stage_dir> [--name "<Track Name>"] [--version 1.0] [--install]
"""

import argparse
import ctypes
from ctypes import wintypes
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import steam_paths

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = wintypes.LPARAM
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

WM_CLOSE = 0x0010
WM_COMMAND = 0x0111

# Measured from MAS2.exe (2.9.4.0) via TB_GETBUTTON and dialog control inspection.
ID_CREATE_PACKAGE = 32824   # Main frame command: open "Mod Packager"
ID_SINGLE_CMP_BTN = 1109    # Mod Packager: "Create Single Cmp Package"
ID_PACKAGE_BTN = 1056       # Create Component Package: "Package"
ID_STATUS_STATIC = 1120     # Create Component Package: status label

# XOR-encoded GMOTOR20MAS / GMOTOR20PKG magic headers. Both use 32-bit offsets (4 GiB max).
MAS_MAGIC = b"f1\x05D }?E-7\x08Z)?\x08U"
PKG_MAGIC = b"f1\x05D }?X'#\x08Z)?\x08U"

MAX_MAS_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB
# .rfcmp wraps all .mas files uncompressed plus ~1.4 KB header/manifest; keep 4 MiB headroom.
MAX_RFCMP_PAYLOAD_BYTES = MAX_MAS_BYTES - (4 * 1024 * 1024)


class _Mas2StartupError(RuntimeError):
    """Transient error while launching MAS2.exe or navigating to its Packager dialog."""


def _fmt_gb(num_bytes):
    return f"{num_bytes / (1024.0 ** 3):.2f} GB"


def is_valid_mas_file(path):
    """True if path is a non-empty file starting with the GMOTOR20MAS header."""
    if not path or not os.path.isfile(path):
        return False
    try:
        if os.path.getsize(path) < len(MAS_MAGIC):
            return False
        with open(path, "rb") as f:
            return f.read(len(MAS_MAGIC)) == MAS_MAGIC
    except OSError:
        return False


def validate_mas_files_for_rfcmp(mas_files, stage_dir=None, require_exists=False):
    """Ensure individual and combined MAS file sizes fit within rF2's 4 GiB limit.

    Returns total size in bytes of existing MAS files. Raises RuntimeError before
    MAS2.exe is launched if any MAS file or their combined payload reaches 4 GiB.
    """
    entries = []
    total_bytes = 0
    for mf in mas_files:
        if not os.path.isfile(mf):
            if require_exists:
                raise RuntimeError(f"Required MAS archive is missing: {mf}")
            continue
        sz = os.path.getsize(mf)
        if sz == 0:
            raise RuntimeError(f"MAS archive is empty (0 bytes): {mf}")
        entries.append((mf, sz))
        total_bytes += sz

    oversized = [(mf, sz) for mf, sz in entries if sz >= MAX_MAS_BYTES]
    if oversized or total_bytes >= MAX_RFCMP_PAYLOAD_BYTES:
        detail_lines = [f"  - {os.path.basename(mf)}: {_fmt_gb(sz)} ({sz:,} bytes)" for mf, sz in entries]
        reason = (
            f"MAS archive '{os.path.basename(oversized[0][0])}' is {_fmt_gb(oversized[0][1])}"
            if oversized
            else f"Combined MAS archives total {_fmt_gb(total_bytes)}"
        )
        hint_stage = f"\nStaged track files remain available at: {stage_dir}" if stage_dir else ""
        raise RuntimeError(
            f"{reason}, exceeding rFactor 2's 4.00 GB MAS/RFCMP limit. "
            f"Skipping RFCMP packaging because MAS2.exe cannot package archives >= 4 GB.\n"
            + "\n".join(detail_lines)
            + "\nTo reduce track size, lower texture_scale (e.g. 0.5 or 0.25) and/or enable "
            "mesh_coarsening in ac2rf2.ini (or via --texture-scale / --mesh-coarsening)."
            + hint_stage
        )
    return total_bytes


def _parse_cmpinfo_mas_files(cmpinfo_content):
    """Extract MASFile= paths from cmpinfo.dat text."""
    out = []
    for line in cmpinfo_content.splitlines():
        if line.startswith("MASFile="):
            val = line.split("=", 1)[1].strip()
            if val:
                out.append(val)
    return out


def find_modmgr(rf2_dir):
    """Find ModMgr.exe, checking Bin64 first then Bin32."""
    for sub in ("Bin64", "Bin32"):
        p = os.path.join(rf2_dir, sub, "ModMgr.exe")
        if os.path.isfile(p):
            return p
    return None


def find_mas2(rf2_dir):
    """Find MAS2.exe in Support/Tools."""
    p = os.path.join(rf2_dir, "Support", "Tools", "MAS2.exe")
    return p if os.path.isfile(p) else None


def locate_stage_dir(path):
    """Resolve track stage directory containing Assets/ and inner layout folder."""
    path = os.path.abspath(path)
    if os.path.isdir(os.path.join(path, "Assets")):
        return path
    # Check out/<track>/rf2/<track>
    rf2_sub = os.path.join(path, "rf2")
    if os.path.isdir(rf2_sub):
        subdirs = [os.path.join(rf2_sub, d) for d in os.listdir(rf2_sub) if os.path.isdir(os.path.join(rf2_sub, d))]
        for d in subdirs:
            if os.path.isdir(os.path.join(d, "Assets")):
                return d
    return path


def read_track_metadata(stage_dir, default_name=None, default_ver="1.0"):
    """Read component Name and Version from .rfbuild, .cmpinfo, or .gdb."""
    name = default_name
    ver = default_ver

    # 1. Check for .rfbuild or .cmpinfo in stage_dir or parent
    cfg_files = (glob.glob(os.path.join(stage_dir, "*.rfbuild")) +
                 glob.glob(os.path.join(os.path.dirname(stage_dir), "*.rfbuild")) +
                 glob.glob(os.path.join(stage_dir, "*.cmpinfo")) +
                 glob.glob(os.path.join(os.path.dirname(stage_dir), "*.cmpinfo")))
    for cfg in cfg_files:
        if os.path.basename(cfg).lower().startswith("temp."):
            continue
        try:
            with open(cfg, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            m = re.search(r'^[ \t]*Name[ \t]*=[ \t]*([^\r\n]+)', content, re.MULTILINE | re.IGNORECASE)
            if m and not name:
                val = m.group(1).strip().strip('"')
                if val:
                    name = val
            m_ver = re.search(r'^[ \t]*Version[ \t]*=[ \t]*([^\r\n]+)', content, re.MULTILINE | re.IGNORECASE)
            if m_ver and ver == default_ver:
                val = m_ver.group(1).strip().strip('"')
                if val:
                    ver = val
        except Exception:
            pass

    # 2. Check .gdb
    if not name or ver == default_ver:
        gdb_files = glob.glob(os.path.join(stage_dir, "*", "*.gdb"))
        if not gdb_files:
            gdb_files = glob.glob(os.path.join(stage_dir, "*.gdb"))
        for gdb in gdb_files:
            try:
                with open(gdb, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                m = re.search(r'^[ \t]*TrackName[ \t]*=[ \t]*([^\r\n]+)', content, re.MULTILINE | re.IGNORECASE)
                if m and not name:
                    val = m.group(1).strip().strip('"')
                    if val:
                        name = val
                m_ver = re.search(r'^[ \t]*Version[ \t]*=[ \t]*([^\r\n]+)', content, re.MULTILINE | re.IGNORECASE)
                if m_ver and ver == default_ver:
                    val = m_ver.group(1).strip().strip('"')
                    if val:
                        ver = val
            except Exception:
                pass

    if "." not in ver:
        ver = f"{ver}.0"
    if not name:
        name = os.path.basename(stage_dir).replace("_", " ").title()
    return name, ver


def build_mas_archive(modmgr_exe, output_mas_path, file_list):
    """Compile a .mas file using ModMgr.exe -m with a hardlinked staging directory."""
    existing = [src for src in file_list if os.path.isfile(src)]
    if not existing:
        return False
    output_mas_path = os.path.abspath(output_mas_path)
    out_parent = os.path.dirname(output_mas_path)
    os.makedirs(out_parent, exist_ok=True)

    # Remove any previous archive so a failed ModMgr run can never look like success.
    if os.path.isfile(output_mas_path):
        try:
            os.remove(output_mas_path)
        except OSError:
            pass

    # Stage on the same volume as output_mas_path so os.link avoids multi-GB copies.
    temp_dir = tempfile.mkdtemp(prefix="_rf2_mas_", dir=out_parent)
    try:
        for src in existing:
            dst = os.path.join(temp_dir, os.path.basename(src))
            if os.path.exists(dst):
                os.remove(dst)
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)

        cmd = [modmgr_exe, f"-m{output_mas_path}", os.path.join(temp_dir, "*.*")]
        proc = subprocess.run(cmd, cwd=temp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0 or not is_valid_mas_file(output_mas_path):
            if os.path.isfile(output_mas_path):
                try:
                    os.remove(output_mas_path)
                except OSError:
                    pass
            stderr = proc.stderr.decode(errors="ignore").strip()
            stdout = proc.stdout.decode(errors="ignore").strip()
            msg = stderr or stdout or "no output"
            raise RuntimeError(
                f"ModMgr failed to generate {os.path.basename(output_mas_path)} "
                f"(exit code {proc.returncode}): {msg}"
            )

        mas_size = os.path.getsize(output_mas_path)
        if mas_size >= MAX_MAS_BYTES:
            raise RuntimeError(
                f"Generated MAS archive '{os.path.basename(output_mas_path)}' is "
                f"{_fmt_gb(mas_size)} ({mas_size:,} bytes), exceeding rFactor 2's 4.00 GB limit. "
                "Lower texture_scale or enable mesh_coarsening in ac2rf2.ini."
            )
        return True
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _enum_pid_windows(target_pid):
    """Return list of (hwnd, class_name, title, visible) for top-level windows of target_pid."""
    windows = []

    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == target_pid:
            cbuf = ctypes.create_unicode_buffer(256)
            tbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            user32.GetWindowTextW(hwnd, tbuf, 256)
            vis = bool(user32.IsWindowVisible(hwnd))
            windows.append((hwnd, cbuf.value, tbuf.value, vis))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return windows


def _find_child_by_id(parent_hwnd, ctrl_id):
    found = None

    def cb(ch, _):
        nonlocal found
        if user32.GetDlgCtrlID(ch) == ctrl_id:
            found = ch
            return False
        return True

    user32.EnumChildWindows(parent_hwnd, WNDENUMPROC(cb), 0)
    return found


def _collect_dialog_text(hwnd):
    """Collect title and visible child static/text strings of a Win32 dialog."""
    parts = []
    tbuf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, tbuf, 512)
    if tbuf.value.strip():
        parts.append(tbuf.value.strip())

    def cb(ch, _):
        cbuf = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(ch, cbuf, 64)
        if cbuf.value.lower() in ("static", "edit"):
            txt = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(ch, txt, 512)
            s = txt.value.strip()
            if s and s not in parts:
                parts.append(s)
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return " | ".join(parts)


def _is_error_text(text):
    low = text.lower()
    if "no errors" in low or "complete with no errors" in low:
        return False
    return any(
        tok in low
        for tok in ("error", "failed", "unable", "cannot", "corrupt", "too large", "not enough", "invalid")
    )


def _run_mas2_once(mas2_exe, target_rfcmp_path, total_mas_bytes, timeout, startup_timeout=30.0):
    """Single MAS2.exe automation attempt using Win32 WM_COMMAND messages."""
    mas2_cwd = os.path.dirname(mas2_exe)
    proc = subprocess.Popen([mas2_exe], cwd=mas2_cwd)
    mas2_hwnd = None

    try:
        # 1. Wait for MAS2 main window (visible Afx: frame)
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise _Mas2StartupError(
                    f"MAS2.exe exited during startup with exit code {proc.returncode}"
                )
            for hwnd, cls, _title, vis in _enum_pid_windows(proc.pid):
                if cls.startswith("Afx:") and vis:
                    mas2_hwnd = hwnd
                    break
            if mas2_hwnd:
                break
            time.sleep(0.1)

        if not mas2_hwnd:
            seen = _enum_pid_windows(proc.pid)
            summary = ", ".join(f"{cls!r}({title!r}, vis={vis})" for _, cls, title, vis in seen) or "none"
            raise _Mas2StartupError(
                f"Could not find MAS2 main window within {startup_timeout:.0f}s (windows seen: {summary})"
            )
        print(f"MAS2 main HWND: {mas2_hwnd}", flush=True)
        time.sleep(0.2)

        # 2. Open "Mod Packager" directly via MFC command ID 32824 (avoids ToolbarWindow32 mouse-click races)
        packager_dlg = None
        btn1109 = None
        step_deadline = time.monotonic() + 15.0
        next_post = 0.0
        while time.monotonic() < step_deadline:
            if proc.poll() is not None:
                raise _Mas2StartupError(
                    f"MAS2.exe exited while opening Mod Packager (exit code {proc.returncode})"
                )
            now = time.monotonic()
            if now >= next_post:
                user32.PostMessageW(mas2_hwnd, WM_COMMAND, ID_CREATE_PACKAGE, 0)
                if next_post == 0.0:
                    print("Sent 'Create package' command to MAS2", flush=True)
                next_post = now + 2.0

            for hwnd, cls, title, vis in _enum_pid_windows(proc.pid):
                if cls == "#32770" and "Mod Packager" in title and vis:
                    ch = _find_child_by_id(hwnd, ID_SINGLE_CMP_BTN)
                    if ch:
                        packager_dlg = hwnd
                        btn1109 = ch
                        break
            if packager_dlg and btn1109:
                break
            time.sleep(0.1)

        if not packager_dlg or not btn1109:
            raise _Mas2StartupError("Could not find 'Create Single Cmp Package' button (1109)")
        print(f"Found 'Create Single Cmp Package' button (HWND {btn1109}), clicking...", flush=True)

        # 3. Open "Create Component Package" dialog via WM_COMMAND (BN_CLICKED = 0)
        cmp_dlg = None
        pkg_btn = None
        status_static = None
        step_deadline = time.monotonic() + 15.0
        next_post = 0.0
        while time.monotonic() < step_deadline:
            if proc.poll() is not None:
                raise _Mas2StartupError(
                    f"MAS2.exe exited while opening Create Component Package dialog (exit code {proc.returncode})"
                )
            now = time.monotonic()
            if now >= next_post:
                user32.PostMessageW(packager_dlg, WM_COMMAND, ID_SINGLE_CMP_BTN, btn1109)
                next_post = now + 2.0

            for hwnd, cls, title, vis in _enum_pid_windows(proc.pid):
                if cls == "#32770" and vis and "Mod Packager" not in title and ("Component" in title or "Package" in title):
                    ch_pkg = _find_child_by_id(hwnd, ID_PACKAGE_BTN)
                    if ch_pkg:
                        cmp_dlg = hwnd
                        pkg_btn = ch_pkg
                        status_static = _find_child_by_id(hwnd, ID_STATUS_STATIC)
                        break
            if cmp_dlg and pkg_btn:
                break
            time.sleep(0.1)

        if not cmp_dlg or not pkg_btn:
            raise _Mas2StartupError("Could not find 'Create Component Package' dialog or Package button (1056)")

        print(f"Found 'Create Component Package' (HWND {cmp_dlg}), Package button (HWND {pkg_btn})", flush=True)
        time.sleep(0.3)

        # 4. Trigger "Package" once via WM_COMMAND
        print("Clicking 'Package' button...", flush=True)
        user32.PostMessageW(cmp_dlg, WM_COMMAND, ID_PACKAGE_BTN, pkg_btn)

        # 5. Monitor packaging status, output growth, completion, and error popups
        print("Packaging in progress...", flush=True)
        stall_timeout = (
            timeout
            if timeout is not None
            else max(180, int(120 + (total_mas_bytes / (1024.0 * 1024.0)) * 0.5))
        )
        last_progress = time.monotonic()
        last_size = -1
        last_status = ""
        success = False
        known_dialogs = {packager_dlg, cmp_dlg}

        while time.monotonic() - last_progress < stall_timeout:
            time.sleep(0.5)

            # Track target RFCMP growth so large writes never hit a premature timeout
            if os.path.isfile(target_rfcmp_path):
                try:
                    cur_size = os.path.getsize(target_rfcmp_path)
                except OSError:
                    cur_size = last_size
                if cur_size > last_size:
                    last_size = cur_size
                    last_progress = time.monotonic()

            # Check if MAS2 exited early
            if proc.poll() is not None:
                if os.path.isfile(target_rfcmp_path) and os.path.getsize(target_rfcmp_path) >= max(1, total_mas_bytes):
                    success = True
                    break
                raise RuntimeError(
                    f"MAS2 terminated unexpectedly with exit code {proc.returncode} "
                    f"(last status: {last_status or 'none'})"
                )

            # Check status label (ctrl 1120)
            if status_static:
                sbuf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(status_static, sbuf, 512)
                stext = sbuf.value.strip()
                if stext and stext != last_status:
                    last_status = stext
                    last_progress = time.monotonic()
                low = stext.lower()
                if "complete" in low and ("no errors" in low or not _is_error_text(stext)):
                    print(f"Packaging completed successfully (status: {stext})!", flush=True)
                    time.sleep(0.5)
                    success = True
                    break
                if _is_error_text(stext):
                    raise RuntimeError(f"MAS2 packaging error: {stext}")

            # Check any secondary popup dialogs (completion or error MessageBox)
            for hwnd, cls, _title, vis in _enum_pid_windows(proc.pid):
                if cls == "#32770" and vis and hwnd not in known_dialogs:
                    dlg_text = _collect_dialog_text(hwnd)
                    if "complete with no errors" in dlg_text.lower():
                        print("Packaging completed successfully (completion dialog confirmed)!", flush=True)
                        time.sleep(0.5)
                        success = True
                        break
                    if _is_error_text(dlg_text):
                        raise RuntimeError(f"MAS2 reported an error: {dlg_text}")
            if success:
                break

        if not success:
            raise RuntimeError(
                f"MAS2 packaging timed out after {stall_timeout}s without progress "
                f"(last status: {last_status or 'none'}, output bytes: {max(0, last_size):,})"
            )

        if not os.path.isfile(target_rfcmp_path):
            raise RuntimeError(f"MAS2 reported completion but output file is missing: {target_rfcmp_path}")
        final_size = os.path.getsize(target_rfcmp_path)
        if final_size < total_mas_bytes:
            raise RuntimeError(
                f"Output RFCMP appears truncated ({final_size:,} bytes < {total_mas_bytes:,} bytes of input MAS files)"
            )

    finally:
        if proc.poll() is None:
            if mas2_hwnd:
                try:
                    user32.PostMessageW(mas2_hwnd, WM_CLOSE, 0, 0)
                    proc.wait(timeout=1.5)
                except Exception:
                    pass
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass


def build_rfcmp_via_mas2(mas2_exe, cmpinfo_content, target_rfcmp_path, timeout=None, retries=1):
    """Launch MAS2.exe and automate component creation using Win32 WM_COMMAND messages."""
    mas_files = _parse_cmpinfo_mas_files(cmpinfo_content)
    total_mas_bytes = validate_mas_files_for_rfcmp(mas_files, require_exists=True)

    appdata = os.environ.get("APPDATA")
    cmpinfo_dat = os.path.join(appdata, "cmpinfo.dat")
    cmpinfo_bak = os.path.join(appdata, "cmpinfo.dat.bak")

    # Clean previous target package to prevent overwrite conflicts
    if os.path.isfile(target_rfcmp_path):
        try:
            os.remove(target_rfcmp_path)
        except OSError:
            pass

    if os.path.exists(cmpinfo_dat) and not os.path.exists(cmpinfo_bak):
        shutil.copyfile(cmpinfo_dat, cmpinfo_bak)

    # Must be written with exact CRLF without extra carriage returns
    with open(cmpinfo_dat, "wb") as f:
        f.write(cmpinfo_content.encode("mbcs", errors="replace"))

    try:
        attempts = max(1, int(retries) + 1)
        for attempt in range(1, attempts + 1):
            try:
                _run_mas2_once(mas2_exe, target_rfcmp_path, total_mas_bytes, timeout=timeout)
                return
            except _Mas2StartupError as exc:
                if attempt < attempts:
                    print(f"MAS2 startup attempt {attempt}/{attempts} failed ({exc}); retrying...", flush=True)
                    time.sleep(2.0)
                    continue
                raise
    finally:
        if os.path.exists(cmpinfo_bak):
            shutil.copyfile(cmpinfo_bak, cmpinfo_dat)
            os.remove(cmpinfo_bak)
        elif os.path.exists(cmpinfo_dat):
            try:
                os.remove(cmpinfo_dat)
            except OSError:
                pass


def package_track(stage_dir, output_rfcmp=None, name=None, version=None, install=False):
    stage_dir = locate_stage_dir(stage_dir)
    venue = os.path.basename(stage_dir)
    rf2_dir = steam_paths.RF2_DIR
    modmgr = find_modmgr(rf2_dir)
    mas2 = find_mas2(rf2_dir)

    if not modmgr:
        raise SystemExit(f"ModMgr.exe not found under {rf2_dir}")
    if not mas2:
        raise SystemExit(f"MAS2.exe not found at {rf2_dir}\\Support\\Tools\\MAS2.exe")

    track_name, track_ver = read_track_metadata(stage_dir, name, version or "1.0")
    print(f"Packaging track: '{track_name}' (Version {track_ver})")
    print(f"Source folder:   {stage_dir}")

    # 1. Identify files for the 3 MAS archives
    # Assets MAS
    gmt_files = glob.glob(os.path.join(stage_dir, "Assets", "GMTs", "*.*"))
    map_files = glob.glob(os.path.join(stage_dir, "Assets", "MAPs", "*.*"))
    tdf_files = glob.glob(os.path.join(stage_dir, "*.tdf"))
    assets_files = gmt_files + map_files + tdf_files

    # Materials MAS
    materials_files = glob.glob(os.path.join(stage_dir, "Assets", "Materials", "*.*"))

    # Layout MAS
    layout_files = []
    for item in os.listdir(stage_dir):
        p = os.path.join(stage_dir, item)
        if os.path.isdir(p) and item != "Assets":
            layout_files.extend(glob.glob(os.path.join(p, "*.*")))

    mas_files = []

    # Assets MAS
    assets_mas = os.path.join(stage_dir, f"{venue}_assets.mas")
    if not is_valid_mas_file(assets_mas):
        print(f"Building {os.path.basename(assets_mas)} ({len(assets_files)} files)...")
        build_mas_archive(modmgr, assets_mas, assets_files)
    else:
        print(f"Reusing {os.path.basename(assets_mas)} ({os.path.getsize(assets_mas):,} bytes)")
    mas_files.append(assets_mas)

    # Materials MAS (if non-empty)
    if materials_files:
        mat_mas = os.path.join(stage_dir, f"{venue}_materials.mas")
        if not is_valid_mas_file(mat_mas):
            print(f"Building {os.path.basename(mat_mas)} ({len(materials_files)} files)...")
            build_mas_archive(modmgr, mat_mas, materials_files)
        else:
            print(f"Reusing {os.path.basename(mat_mas)} ({os.path.getsize(mat_mas):,} bytes)")
        mas_files.append(mat_mas)

    # Layout MAS
    layout_mas = os.path.join(stage_dir, f"{venue}_layout.mas")
    if not is_valid_mas_file(layout_mas):
        print(f"Building {os.path.basename(layout_mas)} ({len(layout_files)} files)...")
        build_mas_archive(modmgr, layout_mas, layout_files)
    else:
        print(f"Reusing {os.path.basename(layout_mas)} ({os.path.getsize(layout_mas):,} bytes)")
    mas_files.append(layout_mas)

    # Enforce 4 GiB MAS / RFCMP limit before launching MAS2.exe
    validate_mas_files_for_rfcmp(mas_files, stage_dir=stage_dir, require_exists=False)

    # 2. Prepare RFCMP target path
    safe_name = track_name.replace(" ", "_")
    default_filename = f"{safe_name}_v{track_ver}.rfcmp"
    if not output_rfcmp:
        if steam_paths.IS_STANDALONE:
            target_dir = os.path.join(steam_paths.APP_DIR, "out")
        else:
            rf2_parent = os.path.dirname(stage_dir)
            if os.path.basename(rf2_parent).lower() == "rf2":
                target_dir = os.path.dirname(rf2_parent)
            else:
                target_dir = stage_dir
        output_rfcmp = os.path.join(target_dir, default_filename)
    elif os.path.isdir(output_rfcmp) or not output_rfcmp.lower().endswith(".rfcmp"):
        os.makedirs(output_rfcmp, exist_ok=True)
        output_rfcmp = os.path.join(output_rfcmp, default_filename)

    output_rfcmp = os.path.abspath(output_rfcmp)
    os.makedirs(os.path.dirname(output_rfcmp), exist_ok=True)

    # 3. Prepare cmpinfo.dat
    now_filetime = int(time.time() * 10000000 + 116444736000000000)
    lines = [
        "[Component]",
        f"Name={track_name}",
        "Type=1",
        f"Version={track_ver}",
        "BaseVersion=",
        "MinVersion=",
        "Author=",
        f"Date={now_filetime}",
        "ID=",
        "URL=",
        "Desc=",
        "Category=0",
        "Origin=0",
        "Flags=0",
        "CurLocation=0",
        "NumLocations=1",
        f"Location={output_rfcmp}",
        f"NumMASFiles={len(mas_files)}",
    ]
    for mf in mas_files:
        lines.append(f"MASFile={mf}")
    lines.extend([
        "rFmFile=",
        "IconFile=",
        "SmallIconFile=",
        "",
        "CurComponent=0",
        "",
    ])
    cmpinfo_content = "\r\n".join(lines) + "\r\n"

    # 4. Run MAS2 automation
    print(f"Compiling RFCMP via MAS2: {output_rfcmp}...")
    print("\nRF2 MAS2.EXE WINDOW WILL BE OPENED AND CLOSED AUTOMATICALLY TO CREATE THE RFCMP FILE.\n")
    build_rfcmp_via_mas2(mas2, cmpinfo_content, output_rfcmp)
    size_mb = os.path.getsize(output_rfcmp) / (1024 * 1024)
    print(f"Successfully created: {output_rfcmp} ({size_mb:.1f} MB)")

    # 5. Optional install
    if install:
        print("Installing package into rFactor 2...")
        cmd = [modmgr, f"-d{rf2_dir}", f"-i{output_rfcmp}"]
        proc = subprocess.run(cmd, cwd=rf2_dir, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ModMgr failed to install {output_rfcmp} (exit code {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        print("Package installed successfully!")


def main():
    parser = argparse.ArgumentParser(description="Package a converted rF2 track into MAS and RFCMP.")
    parser.add_argument("track", help="Path to track folder (e.g. out/<track> or out/<track>/rf2/<track>)")
    parser.add_argument("--name", help="Display name for the component (defaults to TrackName in .gdb)")
    parser.add_argument("--version", default="1.0", help="Component version (defaults to 1.0)")
    parser.add_argument("--output", help="Target .rfcmp path")
    parser.add_argument("--install", action="store_true", help="Install package into rFactor 2 after building")
    args = parser.parse_args()

    package_track(args.track, output_rfcmp=args.output, name=args.name, version=args.version, install=args.install)


if __name__ == "__main__":
    main()
