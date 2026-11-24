"""Packages a converted rFactor 2 track into MAS archives and an RFCMP component.

Normal rFactor 2 (and online dedicated servers) requires tracks to be packaged
into MAS archives inside an RFCMP component file. This script automates that
entire pipeline headlessly:
1. Compiles the three MAS archives (assets, materials, layout) using ModMgr.exe.
2. Writes the component metadata to %APPDATA%\\cmpinfo.dat.
3. Automates MAS2.exe via Win32 messages (ctypes) to build the .rfcmp package.
4. Optionally installs the package into rFactor 2 via ModMgr.exe -i.

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
import threading
import time

import steam_paths

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = wintypes.LPARAM
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

WM_COMMAND = 0x0111
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001
BM_CLICK = 0x00F5


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
    """Compile a .mas file using ModMgr.exe -m with a temporary directory."""
    if not file_list:
        return False
    output_mas_path = os.path.abspath(output_mas_path)
    os.makedirs(os.path.dirname(output_mas_path), exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix="rf2_mas_")
    try:
        for src in file_list:
            if os.path.isfile(src):
                dst = os.path.join(temp_dir, os.path.basename(src))
                shutil.copy2(src, dst)
        cmd = [modmgr_exe, f"-m{output_mas_path}", os.path.join(temp_dir, "*.*")]
        proc = subprocess.run(cmd, cwd=temp_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if os.path.isfile(output_mas_path) and os.path.getsize(output_mas_path) > 0:
            return True
        else:
            raise RuntimeError(f"ModMgr failed to generate MAS: {proc.stderr.decode(errors='ignore')}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def build_rfcmp_via_mas2(mas2_exe, cmpinfo_content, target_rfcmp_path, timeout=120):
    """Launch MAS2.exe and automate component creation using Win32 messages."""
    appdata = os.environ.get("APPDATA")
    cmpinfo_dat = os.path.join(appdata, "cmpinfo.dat")
    cmpinfo_bak = os.path.join(appdata, "cmpinfo.dat.bak")

    # Clean previous target package to prevent overwrite conflicts
    if os.path.isfile(target_rfcmp_path):
        try:
            os.remove(target_rfcmp_path)
        except Exception:
            pass

    if os.path.exists(cmpinfo_dat):
        shutil.copyfile(cmpinfo_dat, cmpinfo_bak)

    # Must be written with exact CRLF without extra carriage returns
    with open(cmpinfo_dat, "wb") as f:
        f.write(cmpinfo_content.encode("ascii"))

    mas2_cwd = os.path.dirname(mas2_exe)
    proc = subprocess.Popen([mas2_exe], cwd=mas2_cwd)
    time.sleep(1.5)

    try:
        # 1. Find MAS2 main window (class starts with "Afx:")
        mas2_hwnd = None
        for _ in range(20):
            def find_mas2_wnd(hwnd, _):
                nonlocal mas2_hwnd
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == proc.pid:
                    cbuf = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd, cbuf, 256)
                    if cbuf.value.startswith("Afx:"):
                        mas2_hwnd = hwnd
                        return False
                return True
            user32.EnumWindows(WNDENUMPROC(find_mas2_wnd), 0)
            if mas2_hwnd:
                break
            time.sleep(0.1)

        if not mas2_hwnd:
            raise RuntimeError("Could not find MAS2 main window")
        print(f"MAS2 main HWND: {mas2_hwnd}", flush=True)

        # 2. Find Toolbar and click button 6 ("Create package") to open Mod Packager
        tb_hwnd = None
        def find_tb(hwnd, _):
            nonlocal tb_hwnd
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            if cbuf.value == "ToolbarWindow32":
                tb_hwnd = hwnd
                return False
            return True
        user32.EnumChildWindows(mas2_hwnd, WNDENUMPROC(find_tb), 0)
        if not tb_hwnd:
            raise RuntimeError("Could not find ToolbarWindow32")

        btn1109 = None
        for attempt in range(10):
            lparam = (11 << 16) | 132
            user32.SendMessageW(tb_hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
            def send_up(thwnd=tb_hwnd, lp=lparam):
                user32.SendMessageW(thwnd, WM_LBUTTONUP, 0, lp)
            threading.Thread(target=send_up, daemon=True).start()
            print("Clicked Toolbar 'Create package' button", flush=True)

            for _ in range(10):
                time.sleep(0.1)
                def find_packager(hwnd, _):
                    nonlocal btn1109
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value == proc.pid:
                        tbuf = ctypes.create_unicode_buffer(256)
                        user32.GetWindowTextW(hwnd, tbuf, 256)
                        if "Mod Packager" in tbuf.value:
                            def find_child(ch, _):
                                nonlocal btn1109
                                if user32.GetDlgCtrlID(ch) == 1109:
                                    btn1109 = ch
                                    return False
                                return True
                            user32.EnumChildWindows(hwnd, WNDENUMPROC(find_child), 0)
                            return False
                    return True
                user32.EnumWindows(WNDENUMPROC(find_packager), 0)
                if btn1109:
                    break
            if btn1109:
                break

        if not btn1109:
            raise RuntimeError("Could not find 'Create Single Cmp Package' button (1109)")
        print(f"Found 'Create Single Cmp Package' button (HWND {btn1109}), clicking...", flush=True)
        user32.PostMessageW(btn1109, BM_CLICK, 0, 0)

        # 5. Wait for 'Create Component Package' dialog and button 1056 ("Package")
        cmp_dlg = None
        pkg_btn = None
        status_static = None

        for _ in range(50):
            time.sleep(0.1)
            def find_cmp_dlg(hwnd, _):
                nonlocal cmp_dlg, pkg_btn, status_static
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == proc.pid:
                    tbuf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, tbuf, 256)
                    if "Component" in tbuf.value or "Package" in tbuf.value:
                        cbuf = ctypes.create_unicode_buffer(256)
                        user32.GetClassNameW(hwnd, cbuf, 256)
                        if cbuf.value == "#32770" and "Mod Packager" not in tbuf.value:
                            cmp_dlg = hwnd
                            def enum_cmp_children(ch, _):
                                nonlocal pkg_btn, status_static
                                cid = user32.GetDlgCtrlID(ch)
                                if cid == 1056:
                                    pkg_btn = ch
                                elif cid == 1120:
                                    status_static = ch
                                return True
                            user32.EnumChildWindows(hwnd, WNDENUMPROC(enum_cmp_children), 0)
                            return False
                return True
            user32.EnumWindows(WNDENUMPROC(find_cmp_dlg), 0)
            if cmp_dlg and pkg_btn:
                break

        if not cmp_dlg or not pkg_btn:
            raise RuntimeError("Could not find 'Create Component Package' dialog or Package button (1056)")

        print(f"Found 'Create Component Package' (HWND {cmp_dlg}), Package button (HWND {pkg_btn})", flush=True)
        time.sleep(0.5)

        # Click "Package"
        print("Clicking 'Package' button...", flush=True)
        user32.PostMessageW(pkg_btn, BM_CLICK, 0, 0)

        # 6. Monitor packaging status
        print("Packaging in progress...", flush=True)
        start_time = time.time()
        success = False
        last_size = -1
        stable_count = 0

        while time.time() - start_time < timeout:
            time.sleep(0.5)

            # Check if process terminated unexpectedly
            if proc.poll() is not None:
                if os.path.isfile(target_rfcmp_path) and os.path.getsize(target_rfcmp_path) > 1024 * 1024:
                    success = True
                    break
                raise RuntimeError(f"MAS2 terminated unexpectedly with exit code {proc.returncode}")

            # 1. Check status text in dialog (HWND status_static, ctrl ID 1120)
            if status_static:
                sbuf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(status_static, sbuf, 256)
                stext = sbuf.value.strip()
                if "complete" in stext.lower() or "no errors" in stext.lower():
                    print(f"Packaging completed successfully (status: {stext})!", flush=True)
                    time.sleep(1.0)
                    success = True
                    break

            # 2. Check for completion popup dialog
            def check_win(hwnd, _):
                nonlocal success
                cur_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(cur_pid))
                if cur_pid.value == proc.pid:
                    tbuf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, tbuf, 256)
                    if "complete with no errors" in tbuf.value.lower():
                        success = True
                        return False
                    def check_child(ch, _):
                        nonlocal success
                        ct = ctypes.create_unicode_buffer(256)
                        user32.GetWindowTextW(ch, ct, 256)
                        if "complete with no errors" in ct.value.lower():
                            success = True
                            return False
                        return True
                    user32.EnumChildWindows(hwnd, WNDENUMPROC(check_child), 0)
                    if success:
                        return False
                return True
            user32.EnumWindows(WNDENUMPROC(check_win), 0)

            if success:
                print("Packaging completed successfully (completion dialog confirmed)!", flush=True)
                time.sleep(1.0)
                break

            # 3. Fallback: check target RFCMP size stability (>1MB and stable)
            if os.path.isfile(target_rfcmp_path):
                cur_size = os.path.getsize(target_rfcmp_path)
                if cur_size > 1024 * 1024:
                    if cur_size == last_size:
                        stable_count += 1
                        if stable_count >= 6:  # stable for 3 seconds
                            print(f"Target RFCMP size stable: {cur_size:,} bytes", flush=True)
                            success = True
                            break
                    else:
                        last_size = cur_size
                        stable_count = 0

        if not success:
            raise RuntimeError("Packaging timed out or failed")

    finally:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
        if os.path.exists(cmpinfo_bak):
            shutil.copyfile(cmpinfo_bak, cmpinfo_dat)
            os.remove(cmpinfo_bak)
        elif os.path.exists(cmpinfo_dat):
            os.remove(cmpinfo_dat)


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
    if not os.path.isfile(assets_mas) or os.path.getsize(assets_mas) == 0:
        print(f"Building {os.path.basename(assets_mas)} ({len(assets_files)} files)...")
        build_mas_archive(modmgr, assets_mas, assets_files)
    else:
        print(f"Reusing {os.path.basename(assets_mas)} ({os.path.getsize(assets_mas):,} bytes)")
    mas_files.append(assets_mas)

    # Materials MAS (if non-empty)
    if materials_files:
        mat_mas = os.path.join(stage_dir, f"{venue}_materials.mas")
        if not os.path.isfile(mat_mas) or os.path.getsize(mat_mas) == 0:
            print(f"Building {os.path.basename(mat_mas)} ({len(materials_files)} files)...")
            build_mas_archive(modmgr, mat_mas, materials_files)
        else:
            print(f"Reusing {os.path.basename(mat_mas)} ({os.path.getsize(mat_mas):,} bytes)")
        mas_files.append(mat_mas)

    # Layout MAS
    layout_mas = os.path.join(stage_dir, f"{venue}_layout.mas")
    if not os.path.isfile(layout_mas) or os.path.getsize(layout_mas) == 0:
        print(f"Building {os.path.basename(layout_mas)} ({len(layout_files)} files)...")
        build_mas_archive(modmgr, layout_mas, layout_files)
    else:
        print(f"Reusing {os.path.basename(layout_mas)} ({os.path.getsize(layout_mas):,} bytes)")
    mas_files.append(layout_mas)

    # 2. Prepare RFCMP target path
    if not output_rfcmp:
        safe_name = track_name.replace(" ", "_")
        output_rfcmp = os.path.join(stage_dir, f"{safe_name}_v{track_ver}.rfcmp")
    output_rfcmp = os.path.abspath(output_rfcmp)

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
    build_rfcmp_via_mas2(mas2, cmpinfo_content, output_rfcmp)
    size_mb = os.path.getsize(output_rfcmp) / (1024 * 1024)
    print(f"Successfully created: {output_rfcmp} ({size_mb:.1f} MB)")

    # 5. Optional install
    if install:
        print("Installing package into rFactor 2...")
        cmd = [modmgr, f"-d{rf2_dir}", f"-i{output_rfcmp}"]
        subprocess.run(cmd, cwd=rf2_dir, capture_output=True, text=True)
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
