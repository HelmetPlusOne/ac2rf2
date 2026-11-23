"""Spatial Y-only road surface smoothing for high-density AC laser-scanned tracks.

Assetto Corsa laser-scanned road meshes often have ~0.5 m triangles with
millimetre-scale point-cloud jitter that AC's tire contact-patch compliance
filters out. rFactor 2 physics HAT raycasts run against the collision mesh at
high frequency, which turns those high-frequency triangle creases into harsh
artificial bumpiness.

This module applies a C2 compact-support triweight local quadratic Moving Least
Squares (MLS) filter in (X, Z) to the vertical (Y) coordinates of physical
``road`` meshes:

  - Welds all physical road chunks together first so inter-chunk seams stay
    0.00 mm watertight.
  - Uses the C2 triweight kernel w(r) = (1 - (r/R)^2)^3 with support radius
    R = 2.5 * sigma, which vanishes smoothly with zero slope at r = R so larger
    ``sigma`` values produce monotonically smoother surfaces with no cutoff
    ringing.
  - Fits a local quadratic surface Y(u, v) = c0 + c1*u + c2*v + c3*u^2 +
    c4*u*v + c5*v^2 in normalized coordinates (u = dx/R, v = dz/R), which
    reproduces sloped straights, banking, crests, and valleys with near-zero
    macro distortion while damping ripples shorter than ~R.
  - Filters neighbors by vertical proximity (|dy| <= eff_max_dy) so bridges and
    overpasses never bleed into roads beneath them.
"""
import os
import struct

import numpy as np

import gmt_dedup

DEFAULT_SIGMA = 4.0
RADIUS_MULT = 2.5
MAX_NEIGHBOR_DY = 1.5
MAX_NEIGHBOR_DY_CAP = 3.5
MAX_DELTA_Y = 0.05
MAX_DELTA_Y_CAP = 0.20
CELL_BATCH = 256


def smooth_positions_mls(pos, tris, sigma=DEFAULT_SIGMA,
                         radius_mult=RADIUS_MULT, max_dy=MAX_NEIGHBOR_DY,
                         max_delta=MAX_DELTA_Y):
    """Smooth Y coordinates of a welded road surface with C2 triweight quadratic MLS.

    Larger ``sigma`` increases the support radius ``R = radius_mult * sigma`` and
    monotonically flattens longer-wavelength road undulations while preserving
    macro slopes, crests, and banking.

    Returns ``(smoothed_pos, stats_dict)``.
    """
    pos = np.asarray(pos, dtype=np.float64)
    tris = np.asarray(tris, dtype=np.int64)
    n = len(pos)
    if n < 8 or len(tris) == 0 or sigma <= 0.0:
        return pos.copy(), {
            "verts": n, "smoothed": 0, "dy_rms_mm": 0.0, "dy_max_mm": 0.0,
        }

    sigma = float(sigma)
    R = sigma * float(radius_mult)
    inv_R = 1.0 / R
    # Scale vertical gate gently with R so steep hills (e.g. 18% Corkscrew) stay
    # inside the window at larger sigma while 5-6m overpasses stay isolated.
    eff_max_dy = max(float(max_dy), min(MAX_NEIGHBOR_DY_CAP, 0.25 * R))
    eff_max_delta = min(MAX_DELTA_Y_CAP, max(float(max_delta), 0.025 * sigma))

    xz = pos[:, [0, 2]]
    y = pos[:, 1]

    gx = np.floor(xz[:, 0] * inv_R).astype(np.int64)
    gz = np.floor(xz[:, 1] * inv_R).astype(np.int64)
    cell_keys = (gx << 32) ^ (gz & 0xFFFFFFFF)
    order = np.argsort(cell_keys)
    sk = cell_keys[order]
    uk, starts, counts = np.unique(sk, return_index=True, return_counts=True)
    cell_map = {int(k): order[s:s + c] for k, s, c in zip(uk, starts, counts)}

    kdim = 6
    M = np.zeros((n, kdim, kdim), dtype=np.float64)
    rhs = np.zeros((n, kdim), dtype=np.float64)
    cnt = np.zeros(n, dtype=np.int32)

    for idx_i_all in cell_map.values():
        cx = int(gx[idx_i_all[0]])
        cz = int(gz[idx_i_all[0]])
        nb_list = []
        for ox in (-1, 0, 1):
            for oz in (-1, 0, 1):
                nk = ((cx + ox) << 32) ^ ((cz + oz) & 0xFFFFFFFF)
                arr = cell_map.get(nk)
                if arr is not None:
                    nb_list.append(arr)
        idx_j = np.concatenate(nb_list)
        xj = xz[idx_j, 0]
        zj = xz[idx_j, 1]
        yj = y[idx_j]

        for b0 in range(0, len(idx_i_all), CELL_BATCH):
            idx_i = idx_i_all[b0:b0 + CELL_BATCH]
            u = (xj[None, :] - xz[idx_i, 0][:, None]) * inv_R
            v = (zj[None, :] - xz[idx_i, 1][:, None]) * inv_R
            dy = yj[None, :] - y[idx_i][:, None]
            r2 = u * u + v * v
            mask = (r2 < 1.0) & (np.abs(dy) <= eff_max_dy)
            t = np.where(mask, 1.0 - r2, 0.0)
            w = t * t * t
            cnt[idx_i] = mask.sum(axis=1)

            B = np.stack(
                (np.ones_like(u), u, v, u * u, u * v, v * v),
                axis=-1,
            )
            Bw = B * w[:, :, None]
            M[idx_i] = np.einsum("ijk,ijl->ikl", Bw, B)
            rhs[idx_i] = np.einsum("ijk,ij->ik", Bw, yj[None, :])

    # Solve local quadratic normal equations for all vertices with enough support.
    # Mild regularization on higher-order terms prevents overshoot on 1-sided edges
    # while keeping interior quadratic fits exact to < 1e-4 m.
    reg = np.diag([0.0, 1e-6, 1e-6, 1e-5, 1e-5, 1e-5])
    M_reg = M + reg[None, :, :]
    good = cnt >= kdim + 2

    new_y = y.copy()
    smoothed_count = 0
    if good.any():
        sol = np.linalg.solve(M_reg[good], rhs[good, :, None])[:, :, 0]
        finite = np.isfinite(sol[:, 0])
        good_idx = np.where(good)[0][finite]
        delta = np.clip(sol[finite, 0] - y[good_idx], -eff_max_delta, eff_max_delta)
        new_y[good_idx] = y[good_idx] + delta
        smoothed_count = int(len(good_idx))

    out = pos.copy()
    out[:, 1] = new_y
    dy_mm = np.abs(new_y - y) * 1000.0
    stats = {
        "verts": n,
        "smoothed": smoothed_count,
        "dy_rms_mm": float(np.sqrt(np.mean(dy_mm * dy_mm))) if n else 0.0,
        "dy_max_mm": float(dy_mm.max()) if n else 0.0,
    }
    return out, stats


def _is_road_gmt(row, by_id, by_name, gmt_path):
    if not row.get("physical") or row.get("timing"):
        return False
    cur = by_id.get(row["id"]) if row.get("id") else by_name.get(row.get("mesh"))
    if cur is not None:
        return str(cur.get("rf2_material", "")).lower().startswith("road_")
    # Fallback: read material name from rF2 GMT header.
    try:
        with open(gmt_path, "rb") as f:
            b = f.read()
        if not gmt_dedup.is_rf2_gmt(b):
            return False
        mat_off = struct.unpack_from("<I", b, 0)[0] + 4
        if mat_off + 68 <= len(b):
            mname = b[mat_off + 4:mat_off + 68].split(b"\x00", 1)[0].decode(
                "ascii", errors="replace").lower()
            return mname.startswith("road_")
    except OSError:
        return False
    return False


def smooth_staged_road_gmts(gmt_dir, exported, mdata=None, sigma=DEFAULT_SIGMA):
    """Smooth all staged physical road rF2 GMTs in ``gmt_dir`` in place.

    Welds all physical ``road_*`` meshes together so inter-chunk boundaries stay
    seamless, updates vertex Y coordinates and GMT bounding-box Y extents in the
    staged rF2 GMTs.
    """
    if sigma <= 0.0:
        return {"meshes": 0, "verts": 0, "smoothed": 0, "dy_rms_mm": 0.0, "dy_max_mm": 0.0}

    meshes_meta = (mdata or {}).get("meshes", [])
    by_name = {m["mesh"]: m for m in meshes_meta}
    by_id = {m["id"]: m for m in meshes_meta if "id" in m}

    records = []
    all_pos = []
    all_tris = []
    v_offset = 0

    for row in exported:
        path = os.path.join(gmt_dir, row["gmt"])
        if not os.path.isfile(path) or not _is_road_gmt(row, by_id, by_name, path):
            continue
        with open(path, "rb") as f:
            raw = f.read()
        if not gmt_dedup.is_rf2_gmt(raw) or len(raw) < gmt_dedup.RF2_HEADER_SIZE:
            continue
        (vcount, voff_rel, tcount, toff_rel,
         _nm, _mo, _nf, _fo, _nn, _no, _bc, _bo) = struct.unpack_from("<12I", raw, 0x13C)
        if vcount == 0 or tcount < 3:
            continue
        voff = voff_rel + 4
        toff = toff_rel + 4
        verts_f32 = np.frombuffer(
            raw, dtype="<f4", count=vcount * (gmt_dedup.RF2_VERTEX_SIZE // 4), offset=voff
        ).reshape(vcount, gmt_dedup.RF2_VERTEX_SIZE // 4)
        pos = verts_f32[:, :3].astype(np.float64)
        tris = np.frombuffer(raw, dtype="<u2", count=tcount, offset=toff).reshape(-1, 3).astype(np.int64)
        valid_t = (tris[:, 0] < vcount) & (tris[:, 1] < vcount) & (tris[:, 2] < vcount)
        tris = tris[valid_t]
        if len(tris) == 0:
            continue

        records.append((path, raw, voff, vcount, v_offset))
        all_pos.append(pos)
        all_tris.append(tris + v_offset)
        v_offset += vcount

    if not records:
        return {"meshes": 0, "verts": 0, "smoothed": 0, "dy_rms_mm": 0.0, "dy_max_mm": 0.0}

    P = np.vstack(all_pos)
    T = np.vstack(all_tris)
    # Weld vertices within 1 mm across all road chunks so seams move identically.
    q = np.rint(P * 1000.0).astype(np.int64)
    _, u_idx, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    welded_pos = P[u_idx]
    welded_tris = inv[T]

    smoothed_welded, stats = smooth_positions_mls(welded_pos, welded_tris, sigma=sigma)
    new_y_all = smoothed_welded[inv, 1].astype(np.float32)

    stride = gmt_dedup.RF2_VERTEX_SIZE
    for path, raw, voff, vcount, start in records:
        buf = bytearray(raw)
        ys = new_y_all[start:start + vcount]
        view = np.frombuffer(buf, dtype="<f4", count=vcount * (stride // 4), offset=voff).reshape(
            vcount, stride // 4
        )
        view[:, 1] = ys
        # Update rF2 GMT bounding-box Y extents (bbox_min at 0x0C8, bbox_max at 0x0D4).
        bmin = list(struct.unpack_from("<3f", buf, 0x0C8))
        bmax = list(struct.unpack_from("<3f", buf, 0x0D4))
        bmin[1] = float(ys.min())
        bmax[1] = float(ys.max())
        struct.pack_into("<3f", buf, 0x0C8, *bmin)
        struct.pack_into("<3f", buf, 0x0D4, *bmax)
        with open(path, "wb") as f:
            f.write(buf)

    stats["meshes"] = len(records)
    return stats
