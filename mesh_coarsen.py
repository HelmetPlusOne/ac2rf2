"""Deterministic, boundary-preserving mesh coarsening for Blender stage.

Runs inside Blender (Stage 6, before ``split_node``) using Blender's C++
Quadric Error Metric (QEM) ``DECIMATE`` modifier in ``COLLAPSE`` mode:

  - Controlled by ``mesh_coarsening`` in ``[0.0, 1.0]`` (0.0 disables).
  - Welds KN5-duplicated vertices at UV seams and hard creases while
    preserving per-corner UVs and custom AC split normals, so flat-shaded
    scenery and roads decimate cleanly instead of appearing 100% boundary.
  - Pins all true open boundary / non-manifold vertices (chunk borders and
    billboard quad edges) via a vertex group so adjacent track chunks stay
    watertight and tree/grass billboard cards remain intact.
  - Preserves UV seams and detected sharp creases via ``delimit``.
  - Applies role-specific target ratios so physical roads/kerbs and decals
    stay conservative while visual terrain and scenery reduce more strongly.
"""
import math

# Meshes below this triangle count (signs, posts, single cards, tiny props)
# save negligible memory and are left untouched.
MIN_TRIS = 64

# Target collapse ratio at mesh_coarsening == 1.0 (strongest safe coarsening
# where major track geometry, UVs, and chunk seams remain intact).
ROLE_MIN_RATIO = {
    "road_physical": 0.50,       # drivable physical meshes, visual+physical roads, decals
    "track_visual": 0.40,        # non-physical visual road/verge/grass/gravel surfaces
    "collision_offtrack": 0.35,  # invisible off-track & wall collision shells
    "scenery": 0.25,             # hills, buildings, grandstands, props
}

# Surface keywords (matched against AC surface name) treated as off-track or
# wall collision when render=False and physical=True. Any unknown physical
# surface defaults to the safer ``road_physical`` tier.
OFFTRACK_SURFACE_TOKENS = (
    "GRASS", "SAND", "GRAVEL", "DIRT", "WALL", "FENCE",
    "BARRIER", "TERRAIN", "SOIL", "MUD", "SNOW", "NOCUT",
)

# Dot-product threshold below which an edge between two faces with different
# AC loop normals is marked sharp (approx. 8 degrees).
SHARP_NORMAL_DOT = 0.99

# Welding distance in metres for collapsing KN5 seam-duplicate vertices.
WELD_DIST = 1e-4

# Billboard / foliage card thinning configuration (for disconnected ksGrass /
# ksTree card clusters that boundary-pinned edge collapse leaves untouched).
BILLBOARD_MIN_COMPS = 16
BILLBOARD_MIN_SMALL_FRAC = 0.70
BILLBOARD_WELD_DECIMALS = 2  # 1 cm position quantization to group crossed cards

# (min_keep_ratio_at_c1, max_tris_per_component)
BILLBOARD_ROLES = {
    "grass_cards": (0.30, 16),   # ksGrass: keep 100% -> 30% (up to -70%)
    "tree_cards": (0.50, 128),   # ksTree:  keep 100% -> 50% (up to -50%)
}


def validate_coarsening(value):
    """Validate normalized mesh_coarsening in [0.0, 1.0] and return float."""
    val = float(value)
    if not math.isfinite(val) or not (0.0 <= val <= 1.0):
        raise ValueError(
            "mesh_coarsening must be between 0.0 and 1.0, got %r" % (value,)
        )
    return val


def classify_mesh_role(row, ntris=None):
    """Return the coarsening role for a material_map mesh row, or None to skip."""
    if row is None:
        return None
    tris = int(row.get("tris", 0) if ntris is None else ntris)
    if tris < MIN_TRIS:
        return None
    if row.get("backdrop") or row.get("timing"):
        return None
    render = bool(row.get("render", True))
    physical = bool(row.get("physical", False))
    if not render and not physical:
        return None
    if physical:
        if not render:
            surf = str(row.get("surface") or "").upper()
            if any(tok in surf for tok in OFFTRACK_SURFACE_TOKENS):
                return "collision_offtrack"
        return "road_physical"
    if row.get("decal"):
        return "road_physical"
    if row.get("track_surface"):
        return "track_visual"
    return "scenery"


def coarsen_ratio(coarsening, role):
    """Map mesh_coarsening in [0.0, 1.0] and role to a Blender DECIMATE ratio."""
    c = validate_coarsening(coarsening)
    if c == 0.0 or role is None:
        return 1.0
    min_ratio = ROLE_MIN_RATIO.get(role)
    if min_ratio is None:
        return 1.0
    return round(1.0 - c * (1.0 - min_ratio), 6)


def classify_billboard_role(row, ntris=None):
    """Return a billboard thinning role for ksGrass/ksTree meshes, else None."""
    if row is None:
        return None
    tris = int(row.get("tris", 0) if ntris is None else ntris)
    if tris < MIN_TRIS:
        return None
    if not row.get("render", True) or row.get("physical"):
        return None
    if row.get("backdrop") or row.get("decal") or row.get("timing"):
        return None
    shader = str(row.get("shader") or "")
    if shader == "ksGrass":
        return "grass_cards"
    if shader == "ksTree":
        return "tree_cards"
    return None


def billboard_keep_ratio(coarsening, billboard_role):
    """Map mesh_coarsening in [0.0, 1.0] to fraction of foliage cards kept."""
    c = validate_coarsening(coarsening)
    if c == 0.0 or billboard_role not in BILLBOARD_ROLES:
        return 1.0
    min_keep, _max_comp = BILLBOARD_ROLES[billboard_role]
    return round(1.0 - c * (1.0 - min_keep), 6)


def _part1by1(n):
    """Spread lower 16 bits of n into even bit positions for 2D Morton coding."""
    n &= 0xFFFF
    n = (n | (n << 8)) & 0x00FF00FF
    n = (n | (n << 4)) & 0x0F0F0F0F
    n = (n | (n << 2)) & 0x33333333
    n = (n | (n << 1)) & 0x55555555
    return n


def _morton2d(x, z):
    return _part1by1(x) | (_part1by1(z) << 1)


def thin_billboard_cards(verts, uvs, faces, normals, keep_ratio,
                         max_comp_tris=128):
    """Deterministically thin disconnected foliage card components in space.

    Groups triangles into connected components by quantized 3D vertex position
    (so crossed X-cards and multi-quad tree canopies stay together), sorts
    eligible small components along a 2D horizontal Morton (Z-order) curve, and
    keeps ``keep_ratio`` of them via a Bresenham accumulator for uniform spatial
    density without bald patches.

    Returns ``(out_verts, out_uvs, out_faces, out_normals)`` if triangle count
    decreased, otherwise the original ``(verts, uvs, faces, normals)``.
    """
    ntri = len(faces)
    if keep_ratio >= 0.9999 or keep_ratio <= 0.0 or ntri < MIN_TRIS:
        return verts, uvs, faces, normals

    # Union-Find over faces sharing any quantized vertex position (1 cm).
    parent = list(range(ntri))
    vmap = {}
    dec = BILLBOARD_WELD_DECIMALS
    for t, (a, b, c) in enumerate(faces):
        for vi in (a, b, c):
            vx, vy, vz = verts[vi]
            key = (round(vx, dec), round(vy, dec), round(vz, dec))
            prev = vmap.get(key)
            if prev is None:
                vmap[key] = t
            else:
                ra = prev
                while parent[ra] != ra:
                    parent[ra] = parent[parent[ra]]
                    ra = parent[ra]
                rb = t
                while parent[rb] != rb:
                    parent[rb] = parent[parent[rb]]
                    rb = parent[rb]
                if ra != rb:
                    parent[rb] = ra

    comps = {}
    for t in range(ntri):
        r = t
        while parent[r] != r:
            parent[r] = parent[parent[r]]
            r = parent[r]
        lst = comps.get(r)
        if lst is None:
            comps[r] = [t]
        else:
            lst.append(t)

    small_comps = []
    large_comps = []
    small_tris = 0
    for f_list in comps.values():
        if len(f_list) <= max_comp_tris:
            small_comps.append(f_list)
            small_tris += len(f_list)
        else:
            large_comps.append(f_list)

    if (len(small_comps) < BILLBOARD_MIN_COMPS
            or small_tris < ntri * BILLBOARD_MIN_SMALL_FRAC):
        return verts, uvs, faces, normals

    # Compute component centroids (AC world coordinates: X/Z horizontal, Y up).
    centroids = []
    min_x = min_z = float("inf")
    max_x = max_z = float("-inf")
    for f_list in small_comps:
        sx = sy = sz = 0.0
        cnt = len(f_list) * 3
        for t in f_list:
            a, b, c = faces[t]
            va, vb, vc = verts[a], verts[b], verts[c]
            sx += va[0] + vb[0] + vc[0]
            sy += va[1] + vb[1] + vc[1]
            sz += va[2] + vb[2] + vc[2]
        cx, cy, cz = sx / cnt, sy / cnt, sz / cnt
        if cx < min_x:
            min_x = cx
        if cx > max_x:
            max_x = cx
        if cz < min_z:
            min_z = cz
        if cz > max_z:
            max_z = cz
        centroids.append((cx, cy, cz, f_list[0], f_list))

    span_x = (max_x - min_x) or 1.0
    span_z = (max_z - min_z) or 1.0
    scale_x = 65535.0 / span_x
    scale_z = 65535.0 / span_z

    order = [
        (
            _morton2d(int((cx - min_x) * scale_x), int((cz - min_z) * scale_z)),
            round(cy, 2),
            first_t,
            f_list,
        )
        for cx, cy, cz, first_t, f_list in centroids
    ]
    order.sort(key=lambda item: (item[0], item[1], item[2]))

    kept_face_mask = [False] * ntri
    for f_list in large_comps:
        for t in f_list:
            kept_face_mask[t] = True

    acc = 0.0
    kept_small = 0
    for _m, _y, _ft, f_list in order:
        acc += keep_ratio
        if acc >= 1.0 - 1e-9:
            acc -= 1.0
            kept_small += 1
            for t in f_list:
                kept_face_mask[t] = True

    if kept_small == 0 or kept_small >= len(small_comps):
        return verts, uvs, faces, normals

    has_uvs = bool(uvs) and len(uvs) == len(verts)
    has_normals = bool(normals) and len(normals) == len(verts)
    remap = {}
    out_verts = []
    out_uvs = [] if has_uvs else uvs
    out_normals = [] if has_normals else None
    out_faces = []

    for t, keep in enumerate(kept_face_mask):
        if not keep:
            continue
        face = []
        for vi in faces[t]:
            ni = remap.get(vi)
            if ni is None:
                ni = remap[vi] = len(out_verts)
                out_verts.append(verts[vi])
                if has_uvs:
                    out_uvs.append(uvs[vi])
                if has_normals:
                    out_normals.append(normals[vi])
            face.append(ni)
        out_faces.append(tuple(face))

    if not out_faces or len(out_faces) >= ntri:
        return verts, uvs, faces, normals
    return out_verts, out_uvs, out_faces, out_normals


def coarsen_geometry(verts, uvs, faces, normals, ratio, preserve_uvs=True,
                     preserve_normals=True, weld_dist=WELD_DIST):
    """Decimate one triangle mesh in Blender while pinning open boundaries.

    Args:
        verts: list of ``(x, y, z)`` positions.
        uvs: list of ``(u, v)`` coordinates matching ``verts`` (or None/empty).
        faces: list of ``(a, b, c)`` triangle vertex index tuples.
        normals: list of ``(nx, ny, nz)`` per-vertex normals matching ``verts``
            (or None).
        ratio: Blender ``DECIMATE`` target ratio in ``(0.0, 1.0)``.
        preserve_uvs: keep per-corner UVs and delimit collapses at UV seams.
        preserve_normals: preserve custom AC split normals and mark sharp creases.
        weld_dist: distance for welding coincident seam vertices before boundary
            detection.

    Returns:
        ``(out_verts, out_uvs, out_faces, out_normals)`` if triangle count
        decreased, otherwise the original ``(verts, uvs, faces, normals)``.
    """
    if ratio >= 0.9999 or len(faces) < MIN_TRIS:
        return verts, uvs, faces, normals

    import bpy
    import bmesh
    from mathutils import Vector

    orig_ntris = len(faces)
    has_uvs = bool(uvs) and len(uvs) == len(verts)
    has_normals = bool(preserve_normals and normals and len(normals) == len(verts))

    me = bpy.data.meshes.new("_coarsen_tmp")
    ob = None
    ev = None
    em = None
    try:
        me.from_pydata(verts, [], faces)
        me.validate(verbose=False)
        if len(me.polygons) < MIN_TRIS:
            return verts, uvs, faces, normals

        # Attach UV layer before welding so bmesh keeps per-corner UVs across
        # UV seams when coincident vertices merge.
        if has_uvs:
            uv_layer = me.uv_layers.new(name="UVMap")
            nloops = len(me.loops)
            lv = [0] * nloops
            me.loops.foreach_get("vertex_index", lv)
            uvflat = [0.0] * (nloops * 2)
            for i, vi in enumerate(lv):
                u, v = uvs[vi]
                uvflat[i * 2] = u
                uvflat[i * 2 + 1] = v
            uv_layer.data.foreach_set("uv", uvflat)

        bm = bmesh.new()
        try:
            bm.from_mesh(me)
            n_layer = None
            if has_normals and len(bm.verts) == len(normals):
                n_layer = bm.loops.layers.float_vector.new("_ac_nrm")
                for f in bm.faces:
                    f.smooth = True
                    for loop in f.loops:
                        loop[n_layer] = normals[loop.vert.index]

            if weld_dist > 0.0:
                bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=weld_dist)

            bm.verts.index_update()
            bm.edges.ensure_lookup_table()

            # Pin every vertex incident to an open boundary or non-manifold edge.
            boundary_v = set()
            has_sharp = False
            for e in bm.edges:
                lf = e.link_faces
                if len(lf) != 2:
                    boundary_v.add(e.verts[0].index)
                    boundary_v.add(e.verts[1].index)
                elif n_layer is not None:
                    # Mark edges whose pre-weld AC normals disagree as sharp so
                    # DECIMATE preserves architectural creases and kerb steps.
                    v0 = e.verts[0]
                    n0 = n1 = None
                    for l in lf[0].loops:
                        if l.vert == v0:
                            n0 = l[n_layer]
                            break
                    for l in lf[1].loops:
                        if l.vert == v0:
                            n1 = l[n_layer]
                            break
                    if n0 is not None and n1 is not None:
                        if Vector(n0).dot(Vector(n1)) < SHARP_NORMAL_DOT:
                            e.smooth = False
                            has_sharp = True

            nverts = len(bm.verts)
            if nverts == 0 or len(boundary_v) >= nverts:
                return verts, uvs, faces, normals

            loop_nrms = None
            if n_layer is not None:
                loop_nrms = [
                    tuple(loop[n_layer])
                    for f in bm.faces
                    for loop in f.loops
                ]
                bm.loops.layers.float_vector.remove(n_layer)

            bm.to_mesh(me)
        finally:
            bm.free()

        if loop_nrms is not None and len(loop_nrms) == len(me.loops):
            me.use_auto_smooth = True
            me.normals_split_custom_set(loop_nrms)

        ob = bpy.data.objects.new("_coarsen_tmp", me)
        bpy.context.collection.objects.link(ob)

        if boundary_v:
            interior = [i for i in range(len(me.vertices)) if i not in boundary_v]
            if not interior:
                return verts, uvs, faces, normals
            vg = ob.vertex_groups.new(name="_interior")
            vg.add(interior, 1.0, "REPLACE")

        mod = ob.modifiers.new("Decimate", "DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = float(ratio)
        mod.use_collapse_triangulate = True
        if boundary_v:
            mod.vertex_group = "_interior"
            mod.vertex_group_factor = 1.0
        delimit = set()
        if preserve_uvs and me.uv_layers:
            delimit.add("UV")
        if has_sharp:
            delimit.add("SHARP")
        if delimit:
            mod.delimit = delimit

        deps = bpy.context.evaluated_depsgraph_get()
        ev = ob.evaluated_get(deps)
        em = ev.to_mesh()

        # Count output triangles (fanning any unexpected >3-gon defensively).
        out_ntris = sum(max(0, len(p.loop_indices) - 2) for p in em.polygons)
        if out_ntris == 0 or out_ntris >= orig_ntris:
            return verts, uvs, faces, normals

        if has_normals:
            em.calc_normals_split()

        uv_data = em.uv_layers[0].data if (has_uvs and em.uv_layers) else None
        em_verts = em.vertices
        em_loops = em.loops

        out_verts = []
        out_uvs = []
        out_normals = [] if has_normals else None
        out_faces = []
        corner_map = {}

        for poly in em.polygons:
            lidx = poly.loop_indices
            n = len(lidx)
            if n < 3:
                continue
            ring = []
            for li in lidx:
                loop = em_loops[li]
                vi = loop.vertex_index
                if uv_data is not None:
                    u, v = uv_data[li].uv
                    uv_t = (float(u), float(v))
                    uv_k = (round(uv_t[0], 5), round(uv_t[1], 5))
                else:
                    uv_t = (0.0, 0.0)
                    uv_k = None
                if has_normals:
                    no = loop.normal
                    n_t = (float(no.x), float(no.y), float(no.z))
                    n_k = (round(n_t[0], 4), round(n_t[1], 4), round(n_t[2], 4))
                else:
                    n_t = None
                    n_k = None
                key = (vi, uv_k, n_k)
                idx = corner_map.get(key)
                if idx is None:
                    idx = corner_map[key] = len(out_verts)
                    co = em_verts[vi].co
                    out_verts.append((float(co.x), float(co.y), float(co.z)))
                    out_uvs.append(uv_t)
                    if has_normals:
                        out_normals.append(n_t)
                ring.append(idx)
            a = ring[0]
            for j in range(1, n - 1):
                b, c = ring[j], ring[j + 1]
                if a != b and b != c and c != a:
                    out_faces.append((a, b, c))

        if not out_faces or len(out_faces) >= orig_ntris:
            return verts, uvs, faces, normals
        return out_verts, out_uvs, out_faces, out_normals
    finally:
        if ev is not None and em is not None:
            ev.to_mesh_clear()
        if ob is not None:
            bpy.data.objects.remove(ob, do_unlink=True)
        bpy.data.meshes.remove(me, do_unlink=True)
