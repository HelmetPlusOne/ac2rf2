"""Convert AC textures into formats a DX11 renderer can actually create.

Assetto Corsa ships some textures in legacy D3D9 pixel formats: 24 bit RGB and
8 bit luminance-alpha. Neither exists in DX11, so rF2 cannot make a texture out
of them and dies when the renderer starts, with nothing written to the log.
Anything not already block compressed is re-encoded to BC3 with mipmaps.

AC also caps its mip chains at five levels whatever the texture size, so a
4096x1024 road stops at 256x64. Asked for anything smaller the hardware has to
clamp to that last level, which is what makes distant tarmac and terrain
sparkle. Those get rebuilt with a full chain, each in its own format.
"""
import os
import shutil
import struct
import argparse
import subprocess

from steam_paths import TEXCONV  # in whichever Steam library has rF2
from parallel import default_jobs, ordered_map
BLOCK = (b"DXT1", b"DXT3", b"DXT5", b"BC4U", b"BC5U", b"ATI1", b"ATI2")

# Rebuilding a block compressed texture means decoding and recompressing it, so
# it is only worth doing in its own format: turning a normal map into BC3 would
# cost far more than the missing mips are worth.
SAME_FORMAT = {b"DXT1": "BC1_UNORM", b"DXT3": "BC2_UNORM", b"DXT5": "BC3_UNORM",
               b"BC4U": "BC4_UNORM", b"ATI1": "BC4_UNORM",
               b"BC5U": "BC5_UNORM", b"ATI2": "BC5_UNORM"}

# Below this a short chain costs nothing to look at and recompressing only adds
# error, so small masks and markers are left exactly as AC shipped them.
MIP_MIN = 128
MAX_TEX_DIM = 4096
DEFAULT_SCALE = 1.0
DEFAULT_SCALE_MIN_SIZE = 1024

# Building mips, texconv weights colour by alpha unless told to treat the two
# separately. AC stores plenty of opaque surfaces with a zero alpha channel, and
# without this their smaller mips come out black.


def is_pow2(n):
    return n >= 4 and (n & (n - 1)) == 0


def pow2_size(n, cap=MAX_TEX_DIM):
    """Next power of two >= max(4, n), capped at cap."""
    n = max(4, int(n))
    return min(cap, 1 << (n - 1).bit_length())


def target_size(n, scale=DEFAULT_SCALE, cap=MAX_TEX_DIM):
    """Scale a texture dimension and snap to a power of two."""
    scaled = max(4, int(round(int(n) * float(scale))))
    max_dim = pow2_size(max(4, int(round(cap * float(scale)))), cap=cap)
    return pow2_size(scaled, cap=max_dim)


def min_scaled_size(min_size, cap=MAX_TEX_DIM):
    """Largest power-of-two size strictly smaller than min_size, or 0."""
    m = int(min_size)
    if m <= 4:
        return 0
    return min(cap, 1 << ((m - 1).bit_length() - 1))


def target_dimensions(w, h, scale=DEFAULT_SCALE, min_size=DEFAULT_SCALE_MIN_SIZE,
                      cap=MAX_TEX_DIM):
    """Compute target (w, h), scaling only when max(w, h) >= min_size.

    When scaling down, the larger target dimension is kept at or above the
    largest power-of-two size strictly smaller than min_size (e.g. 512
    when min_size=1024), preserving aspect ratio.
    """
    w, h = int(w), int(h)
    max_src = max(w, h)
    if max_src < int(min_size):
        return (
            target_size(w, 1.0, cap=cap),
            target_size(h, 1.0, cap=cap),
        )
    effective_scale = float(scale)
    tw = target_size(w, effective_scale, cap=cap)
    th = target_size(h, effective_scale, cap=cap)
    floor = min_scaled_size(min_size, cap=cap)
    if floor and max(tw, th) < floor:
        effective_scale = min(1.0, max(effective_scale, floor / float(min(max_src, cap))))
        tw = target_size(w, effective_scale, cap=cap)
        th = target_size(h, effective_scale, cap=cap)
    return (tw, th)


def full_levels(w, h):
    """How many mip levels a texture of this size needs to reach 1x1."""
    return max(w, h).bit_length()


def expected_dds_bytes(w, h, fourcc, mips):
    """Expected total byte size of a standard 2D block-compressed DDS file."""
    if fourcc not in BLOCK or w <= 0 or h <= 0:
        return None
    bpb = 8 if fourcc == b"DXT1" else 16
    total = 128
    mw, mh = w, h
    for _ in range(max(1, mips)):
        total += max(1, (mw + 3) // 4) * max(1, (mh + 3) // 4) * bpb
        mw, mh = max(1, mw // 2), max(1, mh // 2)
    return total


# AC loads whatever image is inside a .dds name; mod tracks ripped from other
# games often ship PNGs or JPEGs that way. rF2 reads only real DDS and draws
# the rest flat blue (ek_nagao: every texture is a PNG).
IMAGE_MAGIC = ((b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"),
               (b"BM", ".bmp"), (b"GIF8", ".gif"))


def image_kind(path):
    """The real extension of a non-DDS image hiding behind a .dds name."""
    with open(path, "rb") as f:
        head = f.read(8)
    for magic, ext in IMAGE_MAGIC:
        if head.startswith(magic):
            return ext
    return None


def describe(path):
    with open(path, "rb") as f:
        h = f.read(128)
    if len(h) < 128 or h[:4] != b"DDS ":
        return None
    height, width = struct.unpack_from("<II", h, 12)
    fourcc = h[84:88]
    bpp = struct.unpack_from("<I", h, 88)[0]
    mips = struct.unpack_from("<I", h, 28)[0]
    return width, height, fourcc, bpp, mips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tex_dir")
    ap.add_argument("--texconv", default=TEXCONV)
    ap.add_argument("--format", default="BC3_UNORM",
                    help="format for textures DX11 cannot load at all")
    ap.add_argument("--keep-mips", action="store_true",
                    help="leave AC's truncated mip chains alone")
    ap.add_argument("--scale", type=float, default=DEFAULT_SCALE,
                    help="scale factor per dimension (default %.2f; 0.5 halves width and height)" % DEFAULT_SCALE)
    ap.add_argument("--scale-min-size", type=int, default=DEFAULT_SCALE_MIN_SIZE,
                    help="minimum max(w, h) (inclusive) to apply --scale (default %d; 0 scales all)" % DEFAULT_SCALE_MIN_SIZE)
    ap.add_argument("--jobs", type=int, default=default_jobs(),
                    help="textures converted at the same time (1: one by one)")
    args = ap.parse_args()
    if not (0.0 < args.scale <= 1.0):
        raise SystemExit("--scale must be between 0 and 1, got %r" % (args.scale,))
    if args.scale_min_size < 0:
        raise SystemExit("--scale-min-size must be >= 0, got %r" % (args.scale_min_size,))

    tex_dir = os.path.abspath(args.tex_dir)
    tmp = os.path.join(os.path.dirname(tex_dir.rstrip(r"\/")), "_converted")
    os.makedirs(tmp, exist_ok=True)

    todo = []
    for f in sorted(os.listdir(tex_dir)):
        if not f.lower().endswith(".dds"):
            continue
        info = describe(os.path.join(tex_dir, f))
        if info is None:
            kind = image_kind(os.path.join(tex_dir, f))
            if kind:
                todo.append((f, 0, 0, 0, 0, 0, args.format, kind[1:] + " named .dds"))
            else:
                print("skip (not dds): %s" % f)
            continue
        w, h, fourcc, bpp, mips = info
        tw, th = target_dimensions(w, h, args.scale, args.scale_min_size)
        need_resize = (w != tw or h != th)
        exp_bytes = expected_dds_bytes(w, h, fourcc, mips)
        actual_bytes = os.path.getsize(os.path.join(tex_dir, f))
        if min(w, h) < 4:
            todo.append((f, w, h, tw, th, bpp, args.format, "under 4x4"))
        elif fourcc not in BLOCK:
            todo.append((f, w, h, tw, th, bpp, args.format, "%dbpp" % bpp))
        elif need_resize:
            reason = ("non-pow2 -> %dx%d" if (not is_pow2(w) or not is_pow2(h))
                      else "scale -> %dx%d") % (tw, th)
            todo.append((f, w, h, tw, th, bpp, SAME_FORMAT[fourcc], reason))
        elif exp_bytes is not None and actual_bytes != exp_bytes:
            todo.append((f, w, h, tw, th, bpp, SAME_FORMAT[fourcc],
                         "bad size %d!=%d" % (actual_bytes, exp_bytes)))
        elif (not args.keep_mips and fourcc in SAME_FORMAT
              and max(w, h) >= MIP_MIN and mips < full_levels(w, h)):
            todo.append((f, w, h, tw, th, bpp, SAME_FORMAT[fourcc],
                         "%d of %d mips" % (mips, full_levels(w, h))))

    if not todo:
        print("nothing to convert")
        return

    scale_dis = " (disabled)" if float(args.scale) == 1.0 else ""
    print("re-encoding %d textures (scale=%.2f%s, min_size=%d)"
          % (len(todo), args.scale, scale_dis, args.scale_min_size))

    def convert(job, work):
        """One texture; returns the line to print and whether it worked."""
        f, w, h, tw, th, bpp, fmt, why = job
        src = f
        if why.endswith("named .dds"):
            # texconv picks its decoder by extension: give it the real one.
            src = os.path.join(work, os.path.splitext(f)[0] + "." + why.split()[0])
            shutil.copyfile(os.path.join(tex_dir, f), src)
        mip_arg = str(full_levels(tw, th)) if (w > 0 and h > 0) else "0"
        cmd = [args.texconv, "-f", fmt, "-m", mip_arg, "-sepalpha", "-y", "-ft", "dds"]
        if w > 0 and h > 0 and (w != tw or h != th):
            cmd += ["-w", str(tw), "-h", str(th)]
        cmd += ["-o", work, src]
        r = subprocess.run(cmd, cwd=tex_dir, capture_output=True, text=True, errors="replace")
        out = None
        for cand in (f, os.path.splitext(f)[0] + ".DDS",
                     os.path.splitext(f)[0] + ".dds"):
            p = os.path.join(work, cand)
            if os.path.exists(p):
                out = p
                break
        if (w == 0 or h == 0) and r.returncode == 0 and out is not None:
            got = describe(out)
            if got:
                gtw, gth = target_dimensions(got[0], got[1], args.scale, args.scale_min_size)
                if got[0] != gtw or got[1] != gth:
                    r = subprocess.run(
                        [args.texconv, "-f", fmt, "-m", str(full_levels(gtw, gth)),
                         "-sepalpha", "-y", "-ft", "dds",
                         "-w", str(gtw), "-h", str(gth),
                         "-o", work, src],
                        cwd=tex_dir, capture_output=True, text=True, errors="replace")
                    why += ", resized to %dx%d" % (gtw, gth)
        # Block compression works in 4x4 tiles and rF2 will not create a
        # texture smaller than one ("error initializing image surface"); mods
        # embed 1x1 placeholders. Stretch anything that small to 4x4.
        got = describe(out) if out else None
        if r.returncode == 0 and got and min(got[0], got[1]) < 4:
            r = subprocess.run(
                [args.texconv, "-f", fmt, "-m", "0", "-sepalpha", "-y", "-ft", "dds",
                 "-w", str(max(4, got[0])), "-h", str(max(4, got[1])),
                 "-o", work, src], cwd=tex_dir, capture_output=True, text=True, errors="replace")
            why += ", stretched to 4x4"
        if r.returncode != 0 or out is None:
            return False, "  FAILED %-28s %s" % (f, (r.stdout or r.stderr).strip()[-70:])
        shutil.move(out, os.path.join(tex_dir, f))
        return True, "  %-28s %dx%d %-18s -> %s" % (f, w, h, why, fmt)

    # Each texture is its own texconv run, so they go side by side.
    converted = failed = 0
    for ok, line in ordered_map(convert, todo, args.jobs, tmp):
        print(line)
        if ok:
            converted += 1
        else:
            failed += 1

    shutil.rmtree(tmp, ignore_errors=True)
    print("converted %d, failed %d" % (converted, failed))


if __name__ == "__main__":
    main()
