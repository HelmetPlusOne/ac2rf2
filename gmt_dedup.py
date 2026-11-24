"""Deduplicate unshared vertices in rFactor 2 GMT v2.52 files.

Blender's io_rf2_gmt_release addon exports 3 unshared vertices per triangle and
introduces ~1e-5 per-loop rounding noise in split normals and tangents. This
module merges vertices within each mesh that share exact position, vertex
colours, extra vertex fields, all 4 UV channels, quantized normal (1/500 ~=
0.11 deg), and tangent-space handedness, then remaps both MeshTriList and
FaceData indices while copying the materials section verbatim.
"""
import math
import os
import struct
import sys

try:
    import numpy as _np
except ImportError:
    _np = None

RF2_GMT_MAGIC = bytes.fromhex("c8cfd2d8ced8e6c3d6ded8bebbabc190")  # GMOTOR_GMT_2.52\0
RF2_MAGIC_OFFSET = 0x1BC
RF2_HEADER_SIZE = 0x378  # 888 bytes
RF2_MESH_SIZE = 156
FACE_SIZE = 32
NORMAL_SIZE = 16
RF2_VERTEX_SIZE = 96

DEFAULT_NORMAL_QUANT = 500.0
MAX_COARSEN_NORMAL_DEG = 8.0


def normal_dedup_quant(mesh_coarsening=0.0):
    """Map mesh_coarsening in [0.0, 1.0] to the vertex normal dedup scale K.

    At 0.0 (disabled), returns DEFAULT_NORMAL_QUANT (500.0 ~= 0.11 deg) for
    exact legacy deduplication. For mesh_coarsening > 0.0, loosens smoothly up
    to MAX_COARSEN_NORMAL_DEG (8.0 deg, K ~= 7.16) at 1.0 so smooth surfaces
    deduplicate cleanly while sharp creases (> ~8 deg) stay split.
    """
    c = float(mesh_coarsening)
    if not math.isfinite(c) or not (0.0 <= c <= 1.0):
        raise ValueError(
            "mesh_coarsening must be between 0.0 and 1.0, got %r"
            % (mesh_coarsening,)
        )
    if c == 0.0:
        return DEFAULT_NORMAL_QUANT
    return min(DEFAULT_NORMAL_QUANT, 1.0 / math.radians(MAX_COARSEN_NORMAL_DEG * c))


def is_rf2_gmt(data):
    """Return True when raw GMT bytes carry the GMOTOR_GMT_2.52 magic."""
    return (len(data) >= RF2_MAGIC_OFFSET + len(RF2_GMT_MAGIC)
            and data[RF2_MAGIC_OFFSET:RF2_MAGIC_OFFSET + len(RF2_GMT_MAGIC)] == RF2_GMT_MAGIC)


def _dedup_mesh_vertices(data, vert_offset_bytes, vert_count,
                         normal_quant=DEFAULT_NORMAL_QUANT):
    """Deduplicate 96-byte rF2 TVertex2 records in first-appearance order.

    Returns (new_verts_bytes, new_vcount, remap) where remap[old_idx] == new_idx.
    """
    span = vert_count * RF2_VERTEX_SIZE
    q_scale = float(normal_quant)
    if vert_count <= 1:
        remap = (_np.arange(vert_count, dtype=_np.uint16)
                 if _np is not None else list(range(vert_count)))
        return data[vert_offset_bytes:vert_offset_bytes + span], vert_count, remap

    if _np is not None:
        raw_u8 = _np.frombuffer(
            data, dtype=_np.uint8, count=span, offset=vert_offset_bytes
        ).reshape(vert_count, RF2_VERTEX_SIZE)
        vf = _np.frombuffer(
            data, dtype="<f4", count=vert_count * 24, offset=vert_offset_bytes
        ).reshape(vert_count, 24)
        vu = _np.frombuffer(
            data, dtype="<u4", count=vert_count * 24, offset=vert_offset_bytes
        ).reshape(vert_count, 24)

        pos = _np.where(_np.isfinite(vf[:, 0:3]), vf[:, 0:3], 0.0).astype("<f4") + _np.float32(0.0)
        nf64 = _np.clip(
            _np.where(_np.isfinite(vf[:, 3:6]), vf[:, 3:6], 0.0).astype(_np.float64),
            -1.0, 1.0,
        )
        nrm = _np.rint(nf64 * q_scale).astype("<i2")
        extra = vu[:, 6:10]  # dcolor, scolor, + 8 bytes before tangent
        tf64 = _np.where(_np.isfinite(vf[:, 10:16]), vf[:, 10:16], 0.0).astype(_np.float64)
        cx = nf64[:, 1] * tf64[:, 2] - nf64[:, 2] * tf64[:, 1]
        cy = nf64[:, 2] * tf64[:, 0] - nf64[:, 0] * tf64[:, 2]
        cz = nf64[:, 0] * tf64[:, 1] - nf64[:, 1] * tf64[:, 0]
        handed = ((cx * tf64[:, 3] + cy * tf64[:, 4] + cz * tf64[:, 5]) >= 0.0).astype(_np.uint8)
        uvs = _np.where(_np.isfinite(vf[:, 16:24]), vf[:, 16:24], 0.0).astype("<f4") + _np.float32(0.0)

        keys = _np.empty(vert_count, dtype=[
            ("p", "<f4", 3), ("n", "<i2", 3), ("e", "<u4", 4),
            ("u", "<f4", 8), ("h", "u1"),
        ])
        keys["p"] = pos
        keys["n"] = nrm
        keys["e"] = extra
        keys["u"] = uvs
        keys["h"] = handed

        _, first_idx, inv = _np.unique(keys, return_index=True, return_inverse=True)
        new_vcount = int(len(first_idx))
        if new_vcount == vert_count:
            return (data[vert_offset_bytes:vert_offset_bytes + span],
                    vert_count, _np.arange(vert_count, dtype=_np.uint16))

        order = _np.argsort(first_idx)
        keep = first_idx[order]
        rank = _np.empty(new_vcount, dtype=_np.uint16)
        rank[order] = _np.arange(new_vcount, dtype=_np.uint16)
        remap = rank[inv]
        new_verts = _np.ascontiguousarray(raw_u8[keep]).tobytes()
        return new_verts, new_vcount, remap

    def _clean(x):
        return (x + 0.0) if math.isfinite(x) else 0.0

    def _qnorm(x):
        v = _clean(x)
        if v > 1.0:
            v = 1.0
        elif v < -1.0:
            v = -1.0
        return int(round(v * q_scale))

    seen = {}
    keep = []
    remap = [0] * vert_count
    for i in range(vert_count):
        off = vert_offset_bytes + i * RF2_VERTEX_SIZE
        px, py, pz, nx, ny, nz = struct.unpack_from("<6f", data, off)
        extra = struct.unpack_from("<4I", data, off + 24)
        tx, ty, tz, bx, by, bz = struct.unpack_from("<6f", data, off + 40)
        uv = struct.unpack_from("<8f", data, off + 64)
        fnx = max(-1.0, min(1.0, _clean(nx)))
        fny = max(-1.0, min(1.0, _clean(ny)))
        fnz = max(-1.0, min(1.0, _clean(nz)))
        ftx, fty, ftz = _clean(tx), _clean(ty), _clean(tz)
        fbx, fby, fbz = _clean(bx), _clean(by), _clean(bz)
        cx = fny * ftz - fnz * fty
        cy = fnz * ftx - fnx * ftz
        cz = fnx * fty - fny * ftx
        handed = 1 if (cx * fbx + cy * fby + cz * fbz) >= 0.0 else 0
        key = (
            _clean(px), _clean(py), _clean(pz),
            _qnorm(nx), _qnorm(ny), _qnorm(nz),
            extra,
            tuple(_clean(c) for c in uv),
            handed,
        )
        idx = seen.get(key)
        if idx is None:
            idx = len(keep)
            seen[key] = idx
            keep.append(i)
        remap[i] = idx

    new_vcount = len(keep)
    if new_vcount == vert_count:
        return data[vert_offset_bytes:vert_offset_bytes + span], vert_count, remap

    new_verts = bytearray(new_vcount * RF2_VERTEX_SIZE)
    for dst_i, src_i in enumerate(keep):
        s = vert_offset_bytes + src_i * RF2_VERTEX_SIZE
        d = dst_i * RF2_VERTEX_SIZE
        new_verts[d:d + RF2_VERTEX_SIZE] = data[s:s + RF2_VERTEX_SIZE]
    return bytes(new_verts), new_vcount, remap


def _remap_triangles(data, tri_src, tcount, remap, old_vcount):
    if tcount == 0:
        return b""
    if _np is not None:
        tris = _np.frombuffer(data, dtype="<u2", count=tcount, offset=tri_src)
        valid = tris < old_vcount
        safe = _np.where(valid, tris, 0)
        return _np.where(valid, remap[safe], tris).astype("<u2").tobytes()
    tris = struct.unpack_from("<%dH" % tcount, data, tri_src)
    return struct.pack("<%dH" % tcount, *(remap[i] if i < old_vcount else i for i in tris))


def _remap_face_vertices(data, foff, nface, global_remap, old_total_vcount):
    if nface == 0:
        return b""
    if _np is not None:
        face_dt = _np.dtype([
            ("v0", "<u2"), ("v1", "<u2"), ("v2", "<u2"), ("flg", "<u2"),
            ("mesh", "<u4"), ("ni", "<u4"),
            ("cx", "<f4"), ("cy", "<f4"), ("cz", "<f4"), ("area", "<f4"),
        ])
        faces = _np.frombuffer(data, dtype=face_dt, count=nface, offset=foff).copy()
        for col in ("v0", "v1", "v2"):
            v = faces[col]
            valid = v < old_total_vcount
            safe = _np.where(valid, v, 0)
            faces[col] = _np.where(valid, global_remap[safe], v).astype("<u2")
        return faces.tobytes()

    out = bytearray(data[foff:foff + nface * FACE_SIZE])
    for fi in range(nface):
        off = fi * FACE_SIZE
        v0, v1, v2 = struct.unpack_from("<3H", out, off)
        nv0 = global_remap[v0] if v0 < old_total_vcount else v0
        nv1 = global_remap[v1] if v1 < old_total_vcount else v1
        nv2 = global_remap[v2] if v2 < old_total_vcount else v2
        struct.pack_into("<3H", out, off, nv0, nv1, nv2)
    return bytes(out)


def dedup_rf2_gmt(data, label="GMT", mesh_coarsening=0.0):
    """Deduplicate vertices inside raw rF2 GMT v2.52 bytes.

    Returns (new_bytes, old_vcount, new_vcount). Non-v2.52, rigged (bcnt != 0),
    or already-deduplicated GMTs are returned unchanged.
    """
    if not is_rf2_gmt(data) or len(data) < RF2_HEADER_SIZE:
        return data, 0, 0

    n_quant = normal_dedup_quant(mesh_coarsening)

    mat_off_rel = struct.unpack_from("<I", data, 0)[0]
    mat_off = mat_off_rel + 4
    (vcount, voff_rel, tcount, toff_rel, nmesh, moff_rel,
     nface, foff_rel, nnorm, noff_rel, bcnt, boff_rel) = struct.unpack_from(
        "<12I", data, 0x13C)

    if bcnt != 0 or vcount <= 1 or nmesh == 0:
        return data, vcount, vcount

    voff = voff_rel + 4
    toff = toff_rel + 4
    moff = moff_rel + 4
    foff = foff_rel + 4
    noff = noff_rel + 4

    # Only rewrite standard contiguous v2.52 layouts (vertices -> triangles -> materials).
    v_end = voff + vcount * RF2_VERTEX_SIZE
    t_end = toff + tcount * 2
    if (moff + nmesh * RF2_MESH_SIZE > len(data)
            or foff + nface * FACE_SIZE > len(data)
            or noff + nnorm * NORMAL_SIZE > len(data)
            or v_end > len(data) or t_end > len(data) or mat_off > len(data)
            or toff < v_end or mat_off < t_end):
        return data, vcount, vcount

    if _np is not None:
        global_remap = _np.arange(vcount, dtype=_np.uint16)
    else:
        global_remap = list(range(vcount))

    mesh_chunks = []
    vert_chunks = []
    tri_chunks = []
    new_vstart = 0
    for mi in range(nmesh):
        mbase = moff + mi * RF2_MESH_SIZE
        vals = struct.unpack_from("<10I", data, mbase)
        m_vcount = vals[1]
        m_tcount = vals[3]
        m_vstart = vals[6]
        m_tstart = vals[8]
        if m_vstart + m_vcount > vcount or m_tstart + m_tcount > tcount:
            return data, vcount, vcount
        vbytes, new_vc, remap = _dedup_mesh_vertices(
            data, voff + m_vstart * RF2_VERTEX_SIZE, m_vcount, normal_quant=n_quant)
        tbytes = _remap_triangles(data, toff + m_tstart * 2, m_tcount, remap, m_vcount)
        if m_vcount:
            if _np is not None:
                global_remap[m_vstart:m_vstart + m_vcount] = (
                    remap.astype(_np.uint32) + new_vstart).astype(_np.uint16)
            else:
                for local_i in range(m_vcount):
                    global_remap[m_vstart + local_i] = new_vstart + remap[local_i]
        mesh_chunks.append((mbase, new_vc, new_vstart))
        vert_chunks.append(vbytes)
        tri_chunks.append(tbytes)
        new_vstart += new_vc

    new_vcount = new_vstart
    if new_vcount >= vcount:
        return data, vcount, vcount

    # Keep header..voff identical in size so moff/foff/noff/voff stay unchanged.
    out = bytearray(data[:voff])
    if nface:
        out[foff:foff + nface * FACE_SIZE] = _remap_face_vertices(
            data, foff, nface, global_remap, vcount)

    for vbytes in vert_chunks:
        out.extend(vbytes)
    # Preserve any inter-section padding between vertices and triangles.
    if toff > v_end:
        out.extend(data[v_end:toff])

    new_toff = len(out)
    for tbytes in tri_chunks:
        out.extend(tbytes)
    if mat_off > t_end:
        out.extend(data[t_end:mat_off])

    new_mat_off = len(out)
    out.extend(data[mat_off:])

    new_mat_off_rel = new_mat_off - 4
    new_toff_rel = new_toff - 4
    new_boff_rel = new_mat_off_rel if boff_rel == mat_off_rel else boff_rel

    struct.pack_into("<I", out, 0, new_mat_off_rel)
    struct.pack_into("<I", out, 0x13C, new_vcount)
    struct.pack_into("<I", out, 0x148, new_toff_rel)
    struct.pack_into("<I", out, 0x168, new_boff_rel)

    for mbase, new_vc, m_new_vstart in mesh_chunks:
        struct.pack_into("<I", out, mbase + 0x04, new_vc)
        struct.pack_into("<I", out, mbase + 0x18, m_new_vstart)
        if struct.unpack_from("<I", out, mbase + 0x60)[0] == mat_off_rel:
            struct.pack_into("<I", out, mbase + 0x60, new_mat_off_rel)

    return bytes(out), vcount, new_vcount


def dedup_file(src, dst=None, mesh_coarsening=0.0):
    """Deduplicate an rF2 GMT file from src to dst (in-place if dst is None).

    Returns (old_verts, new_verts, old_bytes, new_bytes).
    """
    if dst is None:
        dst = src
    with open(src, "rb") as f:
        original = f.read()
    deduped, old_v, new_v = dedup_rf2_gmt(
        original, label=src, mesh_coarsening=mesh_coarsening)
    if dst != src or deduped is not original:
        with open(dst, "wb") as f:
            f.write(deduped)
    return old_v, new_v, len(original), len(deduped)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python gmt_dedup.py <file_or_dir>")
    target = sys.argv[1]
    paths = ([target] if os.path.isfile(target) else
             [os.path.join(target, f) for f in sorted(os.listdir(target))
              if f.lower().endswith(".gmt")])
    ov = nv = ob = nb = 0
    for p in paths:
        a, b, c, d = dedup_file(p)
        ov += a
        nv += b
        ob += c
        nb += d
    print("%d GMTs: %d -> %d verts, %.2f -> %.2f MB"
          % (len(paths), ov, nv, ob / 1048576.0, nb / 1048576.0))


if __name__ == "__main__":
    main()
