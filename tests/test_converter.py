import os
import sys
import types
import struct
import tempfile
import unittest
from unittest import mock

import kn5


class Kn5Tests(unittest.TestCase):
    def test_kn5_reads_skinned_mesh_nodes(self):
        def kstr(s):
            b = s.encode("utf-8")
            return struct.pack("<I", len(b)) + b

        buf = bytearray(b"sc6969")
        buf += struct.pack("<II", 6, 0)  # version 6 + extra u32
        buf += struct.pack("<I", 0)      # 0 textures
        # 1 minimal material
        buf += struct.pack("<I", 1)
        buf += kstr("marshal_mat") + kstr("ksPerPixel")
        buf += struct.pack("<BBI", 0, 0, 0)
        buf += struct.pack("<II", 0, 0)  # 0 props, 0 textures
        # Root type=1 node with 1 skinned child (type=3)
        buf += struct.pack("<I", 1) + kstr("root") + struct.pack("<IB", 1, 1)
        buf += struct.pack("<16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
        # Type=3 skinned mesh: cs, vis, tr BEFORE bones; ends at lod_out (no sphere/renderable)
        buf += struct.pack("<I", 3) + kstr("Marshall_02") + struct.pack("<IB", 0, 1)
        buf += struct.pack("<BBB", 1, 1, 0)  # cast_shadows, visible, transparent
        buf += struct.pack("<I", 1) + kstr("Hips") + struct.pack("<16f", *([0.0] * 16))
        buf += struct.pack("<I", 3)          # 3 vertices * 76 bytes
        for x in (0.0, 1.0, 2.0):
            buf += struct.pack("<3f3f2f3f4f4f",
                               x, 1.5, 0.0,       # pos
                               0.0, 1.0, 0.0,     # normal
                               0.25, 0.75,        # uv
                               1.0, 0.0, 0.0,     # tangent
                               1.0, 0.0, 0.0, 0.0,# bone weights
                               0.0, 0.0, 0.0, 0.0)# bone indices
        buf += struct.pack("<I3H", 3, 0, 1, 2)
        buf += struct.pack("<IIff", 0, 0, 0.0, 250.0)  # material_id, layer, lod_in, lod_out

        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "marshal.kn5")
            with open(path, "wb") as f:
                f.write(buf)
            k = kn5.KN5(path, geometry=True)
            self.assertEqual(k.consumed, k.size)
            skinned = [n for n in k.walk() if n.type == 3]
            self.assertEqual(len(skinned), 1)
            n = skinned[0]
            self.assertEqual((n.cast_shadows, n.visible, n.transparent, n.renderable), (1, 1, 0, 1))
            self.assertEqual(n.lod_out, 250.0)
            self.assertEqual(n.verts, [(0.0, 1.5, 0.0), (1.0, 1.5, 0.0), (2.0, 1.5, 0.0)])
            self.assertEqual(n.normals[0], (0.0, 1.0, 0.0))
            self.assertEqual(n.uvs[0], (0.25, 0.75))
            self.assertEqual(n.indices, (0, 1, 2))

    def test_read_ini_and_cameras_tolerate_preamble_and_bom(self):
        import convert_track
        import make_cam

        with tempfile.TemporaryDirectory() as root:
            surf = os.path.join(root, "surfaces.ini")
            with open(surf, "wb") as f:
                f.write(
                    b"\xef\xbb\xbfV1.0\r\n"
                    b"[SURFACE_0]\r\n"
                    b"// comment\r\n"
                    b"KEY=ROAD\r\n"
                    b"FRICTION=0.97\r\n"
                )
            cp = convert_track.read_ini(surf)
            self.assertEqual(cp["SURFACE_0"]["KEY"], "ROAD")
            self.assertEqual(cp["SURFACE_0"]["FRICTION"], "0.97")

            cam = os.path.join(root, "cameras.ini")
            with open(cam, "wb") as f:
                f.write(
                    b"\xef\xbb\xbfV1.0\r\n"
                    b"[CAMERA_0]\r\n"
                    b"POSITION=1,2,3\r\n"
                    b"FORWARD=0,0,1\r\n"
                )
            cams = make_cam.read_cameras(root)
            self.assertEqual(len(cams), 1)
            self.assertEqual(cams[0]["pos"], [-1.0, 2.0, 3.0])

    def test_long_texture_filenames_stay_within_gmt_limit(self):
        import convert_track

        long_a = "veg_nurb_tree_pine_b_xlarge_background_002_diff_packmaskNEW.dds"
        long_b = "veg_nurb_tree_pine_b_xlarge_background_002_diff_packmaskOLD.dds"
        mapped = convert_track.texture_filenames([long_a, long_b, "short.dds"])
        self.assertEqual(mapped["short.dds"], "short.dds")
        for orig in (long_a, long_b):
            self.assertLessEqual(len(mapped[orig]), convert_track.MAX_TEX_FILENAME)
            self.assertTrue(mapped[orig].endswith(".dds"))
        self.assertNotEqual(mapped[long_a].lower(), mapped[long_b].lower())

        # Suffixes appended in bake_layers keep total filename <= MAX_TEX_FILENAME
        # while preserving the trailing suffix (_a102, _a102_d80, _baked, _base).
        stem = os.path.splitext(mapped[long_a])[0]
        for suf in ("_a102", "_a102_d80", "_baked", "_base", "_detail_nm"):
            out = convert_track.fit_tex_stem(stem, suf) + ".dds"
            self.assertLessEqual(len(out), convert_track.MAX_TEX_FILENAME)
            self.assertTrue(out.endswith(suf + ".dds"))

    def test_instance_identifier_caps_overlong_names(self):
        import finalize_track

        used = {"skyboxi", "clouds", "sky", "xpitin", "xpitout"}
        self.assertEqual(finalize_track.instance_identifier("xfinish", used), "xfinish")
        self.assertEqual(
            finalize_track.instance_identifier("a" * 31, used), "a" * 31
        )

        long_names = [
            "pitstop__metal_fence_extruded_001_SUB0",
            "pitstop__metal_fence_extruded_001_SUB1",
            "corkscrew_traliccio_fili_001_SUB1",
            "ADV_corkscrew_mazda_coopertires_navidad_SUB0",
            "ADV_corkscrew_mazda_coopertires_navidad_SUB1",
        ]
        shortened = [finalize_track.instance_identifier(n, used) for n in long_names]
        for s in shortened:
            self.assertLessEqual(len(s), finalize_track.MAX_INSTANCE_NAME_LENGTH)
        self.assertEqual(len({s.casefold() for s in shortened}), len(long_names))

        # Deterministic without collision set.
        self.assertEqual(
            finalize_track.instance_identifier(long_names[0]),
            finalize_track.instance_identifier(long_names[0]),
        )

        # Duplicate short names get disambiguated when a used set is supplied.
        dup_used = set()
        first = finalize_track.instance_identifier("x" * 20, dup_used)
        second = finalize_track.instance_identifier("x" * 20, dup_used)
        self.assertNotEqual(first.casefold(), second.casefold())
        self.assertLessEqual(len(second), finalize_track.MAX_INSTANCE_NAME_LENGTH)

    def test_road_smoothness_to_sigma_maps_uniformly(self):
        import steam_paths

        self.assertEqual(steam_paths.road_smoothness_to_sigma(0.0), 0.0)
        self.assertEqual(steam_paths.road_smoothness_to_sigma(0.5), 4.0)
        self.assertEqual(steam_paths.road_smoothness_to_sigma(1.0), 8.0)
        self.assertEqual(steam_paths.road_smoothness_to_sigma(0.25), 2.0)
        with self.assertRaises(ValueError):
            steam_paths.road_smoothness_to_sigma(-0.1)
        with self.assertRaises(ValueError):
            steam_paths.road_smoothness_to_sigma(1.1)

    def test_smooth_positions_mls_reproduces_quadratic_surfaces_exact(self):
        import road_smooth
        import numpy as np

        xs = np.linspace(-15.0, 15.0, 21)
        zs = np.linspace(-15.0, 15.0, 21)
        gx, gz = np.meshgrid(xs, zs)
        x = gx.ravel()
        z = gz.ravel()
        # Quadratic surface: Y = 2.0 + 0.1*X - 0.05*Z + 0.003*X^2 - 0.002*X*Z + 0.004*Z^2
        y = 2.0 + 0.1 * x - 0.05 * z + 0.003 * x * x - 0.002 * x * z + 0.004 * z * z
        pos = np.column_stack((x, y, z))

        # Build grid triangulation
        tris = []
        nx, nz = len(xs), len(zs)
        for r in range(nz - 1):
            for c in range(nx - 1):
                i0 = r * nx + c
                i1 = i0 + 1
                i2 = (r + 1) * nx + c
                i3 = i2 + 1
                tris.append((i0, i1, i2))
                tris.append((i1, i3, i2))

        smoothed, stats = road_smooth.smooth_positions_mls(pos, tris, sigma=4.0)
        self.assertEqual(stats["verts"], len(pos))
        # Interior points must match quadratic surface to < 1e-4 m
        interior = (np.abs(x) < 8.0) & (np.abs(z) < 8.0)
        self.assertTrue(interior.any())
        max_err = float(np.abs(smoothed[interior, 1] - pos[interior, 1]).max())
        self.assertLess(max_err, 1e-4)

    def test_track_preview_prefers_layout_map_and_skips_tiny_placeholders(self):
        import build_track

        def write_png_header(path, width, height):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            header = (
                b"\x89PNG\r\n\x1a\n"
                + struct.pack(">I", 13)
                + b"IHDR"
                + struct.pack(">II", width, height)
                + b"\x08\x06\x00\x00\x00"
            )
            with open(path, "wb") as f:
                f.write(header)

        with tempfile.TemporaryDirectory() as root:
            write_png_header(os.path.join(root, "map.png"), 800, 600)
            write_png_header(os.path.join(root, "ui", "nords", "preview.png"), 355, 200)
            write_png_header(os.path.join(root, "ui", "nords", "outline.png"), 365, 192)
            # Layout-specific outline beats root map when layout map is missing or tiny.
            write_png_header(os.path.join(root, "nords", "map.png"), 20, 20)
            self.assertEqual(
                build_track.track_preview(root, "nords"),
                os.path.join(root, "ui", "nords", "outline.png"),
            )
            # Full-size layout map beats outline and preview.
            write_png_header(os.path.join(root, "nords", "map.png"), 1579, 1244)
            self.assertEqual(
                build_track.track_preview(root, "nords"),
                os.path.join(root, "nords", "map.png"),
            )

    def test_stages_menu_images_from_ac_track_map(self):
        import numpy as np
        import finalize_track

        seen_loading_canvas = {}

        def fake_texconv(command, **_kwargs):
            output_dir = command[command.index("-o") + 1]
            image_format = command[command.index("-ft") + 1].upper()
            src = command[-1]
            stem = os.path.splitext(os.path.basename(src))[0]
            out_path = os.path.join(output_dir, stem + "." + image_format)

            if image_format == "TGA":
                if "-w" in command and "-h" in command:
                    tw = int(command[command.index("-w") + 1])
                    th = int(command[command.index("-h") + 1])
                    src_img = finalize_track._read_tga_bgra(src)
                    ys = np.linspace(0, src_img.shape[0] - 1, th).astype(np.intp)
                    xs = np.linspace(0, src_img.shape[1] - 1, tw).astype(np.intp)
                    resized = src_img[np.ix_(ys, xs)]
                    finalize_track._write_tga_bgra(out_path, resized, bottom_up=False)
                else:
                    # Synthetic 200x400 RGBA track map: transparent border + bright vertical stripe
                    arr = np.zeros((400, 200, 4), dtype=np.uint8)
                    arr[40:360, 80:120, :3] = 255
                    arr[40:360, 80:120, 3] = 255
                    finalize_track._write_tga_bgra(out_path, arr, bottom_up=False)
            elif image_format == "JPG":
                seen_loading_canvas["img"] = finalize_track._read_tga_bgra(src)
                with open(out_path, "wb") as f:
                    f.write(b"\xff\xd8\xffJPG")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as root:
            preview = os.path.join(root, "map.png")
            layout_dir = os.path.join(root, "layout")
            os.makedirs(layout_dir)
            with open(preview, "wb") as f:
                f.write(b"png")

            with mock.patch.object(finalize_track.subprocess, "run",
                                   side_effect=fake_texconv):
                count = finalize_track.stage_menu_images(
                    preview, layout_dir, "sample_track", "texconv.exe")

            self.assertEqual(count, 4)
            # Loading screen canvas is 1920x1080, dark background, centered bright track.
            canvas = seen_loading_canvas["img"]
            self.assertEqual(canvas.shape, (1080, 1920, 4))
            self.assertLess(int(canvas[0, 0, 0]), 40)
            self.assertGreater(int(canvas[540, 960, 0]), 200)
            with open(os.path.join(layout_dir, "sample_track_loading.jpg"), "rb") as f:
                self.assertEqual(f.read(), b"\xff\xd8\xffJPG")

            # Mini TGA is 252x249 32-bit bottom-up RGBA with transparent corners and visible center.
            mini_path = os.path.join(layout_dir, "sample_trackmini.tga")
            with open(mini_path, "rb") as f:
                hdr = f.read(18)
            self.assertEqual(hdr[2], 2)
            self.assertEqual(struct.unpack_from("<HH", hdr, 12), (252, 249))
            self.assertEqual((hdr[16], hdr[17]), (32, 8))
            mini = finalize_track._read_tga_bgra(mini_path)
            self.assertEqual(int(mini[0, 0, 3]), 0)
            self.assertEqual(int(mini[124, 126, 3]), 255)

            # Check that Thmb.tga and icon.tga were created as copies of mini.tga
            with open(mini_path, "rb") as mf:
                expected_bytes = mf.read()
            for extra in ("sample_trackThmb.tga", "sample_trackicon.tga"):
                self.assertTrue(os.path.isfile(os.path.join(layout_dir, extra)))
                with open(os.path.join(layout_dir, extra), "rb") as f:
                    self.assertEqual(f.read(), expected_bytes)

    def test_target_dimensions_scales_and_snaps_power_of_two(self):
        import fix_textures

        # Scaled down when >= min_size, respecting destination floor (512 for min_size=1024)
        self.assertEqual(fix_textures.target_dimensions(2048, 2048, scale=0.5, min_size=1024), (1024, 1024))
        self.assertEqual(fix_textures.target_dimensions(1024, 512, scale=0.5, min_size=1024), (512, 256))
        # Destination floor prevents scaling 1024 down to 256
        self.assertEqual(fix_textures.target_dimensions(1024, 1024, scale=0.25, min_size=1024), (512, 512))
        # Not scaled down when < min_size
        self.assertEqual(fix_textures.target_dimensions(512, 512, scale=0.5, min_size=1024), (512, 512))
        self.assertEqual(fix_textures.target_dimensions(256, 128, scale=0.5, min_size=1024), (256, 128))
        # Non power of two snaps up to power of two
        self.assertEqual(fix_textures.target_dimensions(300, 200, scale=1.0, min_size=1024), (512, 256))

    def test_expected_dds_bytes(self):
        import fix_textures

        # DXT1 4x4 with 3 mips: header (128) + 4x4 (8) + 2x2 (8) + 1x1 (8) = 152 bytes
        self.assertEqual(fix_textures.expected_dds_bytes(4, 4, b"DXT1", 3), 152)
        # DXT5 4x4 with 3 mips: header (128) + 4x4 (16) + 2x2 (16) + 1x1 (16) = 176 bytes
        self.assertEqual(fix_textures.expected_dds_bytes(4, 4, b"DXT5", 3), 176)
        # DXT1 1024x1024 1 mip: 128 + 256*256*8 = 524416 bytes
        self.assertEqual(fix_textures.expected_dds_bytes(1024, 1024, b"DXT1", 1), 128 + 256 * 256 * 8)
        # Non-block format returns None
        self.assertIsNone(fix_textures.expected_dds_bytes(100, 100, b"RGBA", 1))

    def test_load_config_reads_texture_scaling_and_road_smoothness(self):
        import steam_paths

        with tempfile.TemporaryDirectory() as root:
            # Default when no file
            cfg = steam_paths.load_config(root)
            self.assertFalse(cfg["exists"])
            self.assertEqual(cfg["texture_scale"], 1.0)
            self.assertEqual(cfg["texture_scale_min_size"], 1024)
            self.assertEqual(cfg["road_smoothness"], 0.0)

            # Custom INI
            ini_path = os.path.join(root, "ac2rf2.ini")
            with open(ini_path, "w", encoding="utf-8") as f:
                f.write("[track]\ntexture_scale = 0.5\ntexture_scale_min_size = 512\nroad_smoothness = 0.25\n")
            cfg = steam_paths.load_config(root)
            self.assertTrue(cfg["exists"])
            self.assertEqual(cfg["texture_scale"], 0.5)
            self.assertEqual(cfg["texture_scale_source"], "ini")
            self.assertEqual(cfg["texture_scale_min_size"], 512)
            self.assertEqual(cfg["road_smoothness"], 0.25)
            self.assertEqual(cfg["road_smooth_sigma"], 2.0)
            self.assertEqual(cfg["mesh_coarsening"], 0.0)

            # Custom INI with mesh_coarsening
            with open(ini_path, "w", encoding="utf-8") as f:
                f.write("[track]\nmesh_coarsening = 0.5\n")
            cfg = steam_paths.load_config(root)
            self.assertEqual(cfg["mesh_coarsening"], 0.5)
            self.assertEqual(cfg["mesh_coarsening_source"], "ini")

    def test_mesh_coarsening_role_classification_and_monotonic_ratios(self):
        import mesh_coarsen

        # Skipped roles: tiny meshes, backdrops, timing, non-rendered non-physical.
        self.assertIsNone(mesh_coarsen.classify_mesh_role(
            {"render": True, "physical": False, "tris": 32}))
        self.assertIsNone(mesh_coarsen.classify_mesh_role(
            {"render": True, "physical": False, "backdrop": True, "tris": 500}))
        self.assertIsNone(mesh_coarsen.classify_mesh_role(
            {"render": False, "physical": True, "timing": True, "tris": 500}))
        self.assertIsNone(mesh_coarsen.classify_mesh_role(
            {"render": False, "physical": False, "tris": 500}))

        # Active roles.
        self.assertEqual(
            mesh_coarsen.classify_mesh_role(
                {"render": False, "physical": True, "surface": "ROAD", "tris": 500}),
            "road_physical",
        )
        self.assertEqual(
            mesh_coarsen.classify_mesh_role(
                {"render": True, "physical": True, "surface": "GRASS", "tris": 500}),
            "road_physical",
        )
        self.assertEqual(
            mesh_coarsen.classify_mesh_role(
                {"render": False, "physical": True, "surface": "WALL", "tris": 500}),
            "collision_offtrack",
        )
        self.assertEqual(
            mesh_coarsen.classify_mesh_role(
                {"render": True, "physical": False, "decal": True, "tris": 500}),
            "road_physical",
        )
        self.assertEqual(
            mesh_coarsen.classify_mesh_role(
                {"render": True, "physical": False, "track_surface": True, "tris": 500}),
            "track_visual",
        )
        self.assertEqual(
            mesh_coarsen.classify_mesh_role(
                {"render": True, "physical": False, "track_surface": False, "tris": 500}),
            "scenery",
        )

        # Disabled at 0.0; strictly decreasing (stronger coarsening) as c -> 1.0.
        for role, expected_min in mesh_coarsen.ROLE_MIN_RATIO.items():
            self.assertEqual(mesh_coarsen.coarsen_ratio(0.0, role), 1.0)
            r25 = mesh_coarsen.coarsen_ratio(0.25, role)
            r50 = mesh_coarsen.coarsen_ratio(0.50, role)
            r75 = mesh_coarsen.coarsen_ratio(0.75, role)
            r100 = mesh_coarsen.coarsen_ratio(1.0, role)
            self.assertTrue(1.0 > r25 > r50 > r75 > r100 == expected_min)

        # Invalid coarsening values raise ValueError.
        for bad in (-0.1, 1.1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                mesh_coarsen.coarsen_ratio(bad, "scenery")

        # Early no-op guard returns input unchanged without needing Blender.
        v = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
        u = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
        f = [(0, 1, 2)]
        n = [(0.0, 0.0, 1.0)] * 3
        self.assertEqual(mesh_coarsen.coarsen_geometry(v, u, f, n, 1.0), (v, u, f, n))
        self.assertEqual(mesh_coarsen.coarsen_geometry(v, u, f, n, 0.5), (v, u, f, n))

    def test_normal_dedup_scales_with_mesh_coarsening_and_preserves_creases(self):
        import gmt_dedup
        import math
        from unittest import mock

        # 0.0 preserves legacy 500.0 quantization; higher coarsening loosens monotonically.
        self.assertEqual(gmt_dedup.normal_dedup_quant(0.0), gmt_dedup.DEFAULT_NORMAL_QUANT)
        q25 = gmt_dedup.normal_dedup_quant(0.25)
        q50 = gmt_dedup.normal_dedup_quant(0.50)
        q75 = gmt_dedup.normal_dedup_quant(0.75)
        q100 = gmt_dedup.normal_dedup_quant(1.0)
        self.assertTrue(gmt_dedup.DEFAULT_NORMAL_QUANT > q25 > q50 > q75 > q100 > 6.0)
        for bad in (-0.1, 1.1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                gmt_dedup.normal_dedup_quant(bad)

        # Build 3 96-byte rF2 TVertex2 records:
        #   v0: normal (0, 1, 0)
        #   v1: normal tilted ~2.5 deg (sin=0.0436, cos=0.9990) -> smooth jitter
        #   v2: normal tilted 90 deg (1, 0, 0) -> hard architectural crease
        a = math.radians(2.5)
        tan = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
        uvs = (0.25, 0.75, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        v0 = struct.pack("<6f4I6f8f", 1.0, 2.0, 3.0, 0.0, 1.0, 0.0, 0xFFFFFFFF, 0, 0, 0, *tan, *uvs)
        v1 = struct.pack("<6f4I6f8f", 1.0, 2.0, 3.0, math.sin(a), math.cos(a), 0.0, 0xFFFFFFFF, 0, 0, 0, *tan, *uvs)
        v2 = struct.pack("<6f4I6f8f", 1.0, 2.0, 3.0, 1.0, 0.0, 0.0, 0xFFFFFFFF, 0, 0, 0, *tan, *uvs)
        raw_verts = v0 + v1 + v2

        # At coarsening=0.0 (K=500): all 3 remain separate.
        q0 = gmt_dedup.normal_dedup_quant(0.0)
        _, vc0, _ = gmt_dedup._dedup_mesh_vertices(raw_verts, 0, 3, normal_quant=q0)
        self.assertEqual(vc0, 3)

        # At coarsening=1.0 (K~=7.16, ~8 deg): v0 and v1 merge, 90 deg crease v2 stays split!
        new_vb, vc_fast, remap_fast = gmt_dedup._dedup_mesh_vertices(
            raw_verts, 0, 3, normal_quant=q100)
        self.assertEqual(vc_fast, 2)
        self.assertEqual(int(remap_fast[0]), int(remap_fast[1]))
        self.assertNotEqual(int(remap_fast[0]), int(remap_fast[2]))

        # NumPy and pure-Python paths must stay byte-for-byte identical.
        with mock.patch.object(gmt_dedup, "_np", None):
            new_vb_py, vc_py, remap_py = gmt_dedup._dedup_mesh_vertices(
                raw_verts, 0, 3, normal_quant=q100)
        self.assertEqual((new_vb, vc_fast, list(remap_fast)),
                         (new_vb_py, vc_py, list(remap_py)))

    def test_cli_dispatches_build_track_and_convert_multiple_tracks(self):
        import ac2rf2
        import build_track
        import convert_multiple_tracks

        seen = []

        def fake_build():
            seen.append(("build", list(sys.argv)))
            return 0

        def fake_batch():
            seen.append(("batch", list(sys.argv)))
            return 0

        with mock.patch.object(build_track, "main", side_effect=fake_build), \
                mock.patch.object(convert_multiple_tracks, "main", side_effect=fake_batch):
            self.assertEqual(ac2rf2.main(["build_track", "C:\\track", "--hour", "10:00"]), 0)
            self.assertEqual(ac2rf2.main(["convert_multiple_tracks", "tracks.txt"]), 0)
            self.assertEqual(ac2rf2.main(["--stage", "build_track", "C:\\track2"]), 0)

        self.assertEqual(seen[0][0], "build")
        self.assertEqual(seen[0][1][1:], ["C:\\track", "--hour", "10:00"])
        self.assertEqual(seen[1][0], "batch")
        self.assertEqual(seen[1][1][1:], ["tracks.txt"])
        self.assertEqual(seen[2][0], "build")
        self.assertEqual(seen[2][1][1:], ["C:\\track2"])

    def test_cli_stage_dispatch_handles_script_only_modules(self):
        import ac2rf2

        for stage in ("make_aiw", "make_cam"):
            self.assertEqual(ac2rf2.main(["--stage", stage, "--help"]), 0)

    def test_drag_and_drop_directory_and_batch_file(self):
        import ac2rf2
        import build_track
        import convert_multiple_tracks

        seen = []

        def fake_build():
            seen.append(("build", list(sys.argv)))
            return 0

        def fake_batch():
            seen.append(("batch", list(sys.argv)))
            return 0

        with tempfile.TemporaryDirectory() as root:
            single_track = os.path.join(root, "targa")
            multi_track = os.path.join(root, "nords")
            batch_file = os.path.join(root, "tracks.txt")
            os.makedirs(single_track)
            os.makedirs(multi_track)
            with open(os.path.join(multi_track, "models_endurance.ini"), "w") as f:
                f.write("")
            with open(os.path.join(multi_track, "models_nordschleife.ini"), "w") as f:
                f.write("")
            with open(batch_file, "w") as f:
                f.write(single_track + ";\n")

            with mock.patch.object(build_track, "main", side_effect=fake_build), \
                    mock.patch.object(convert_multiple_tracks, "main", side_effect=fake_batch), \
                    mock.patch.object(ac2rf2, "_pause_if_explorer"), \
                    mock.patch.object(sys.stdin, "isatty", return_value=True), \
                    mock.patch("builtins.input", return_value="2"):
                self.assertEqual(ac2rf2.main([single_track]), 0)
                self.assertEqual(ac2rf2.main([multi_track]), 0)
                self.assertEqual(ac2rf2.main([batch_file]), 0)

        self.assertEqual(seen[0][0], "build")
        self.assertEqual(seen[0][1][1:], [single_track])
        self.assertEqual(seen[1][0], "build")
        self.assertEqual(seen[1][1][1:], [multi_track, "--layout", "nordschleife"])
        self.assertEqual(seen[2][0], "batch")
        self.assertEqual(seen[2][1][1:], [batch_file])

    def test_convert_multiple_tracks_build_command_install_flag(self):
        import convert_multiple_tracks

        cmd_default = convert_multiple_tracks.build_command("C:\\track", "layout1")
        self.assertNotIn("--install", cmd_default)
        self.assertIn("layout1", cmd_default)

        cmd_custom = convert_multiple_tracks.build_command("C:\\track", None, install="auto")
        self.assertIn("--install", cmd_custom)
        self.assertIn("auto", cmd_custom)

    def test_package_rfcmp_target_path_resolution(self):
        import package_rfcmp
        import steam_paths

        with tempfile.TemporaryDirectory() as root:
            # Stage directory: <root>/work/my_track/rf2/my_track
            track_work = os.path.join(root, "work", "my_track")
            stage_dir = os.path.join(track_work, "rf2", "my_track")
            assets_dir = os.path.join(stage_dir, "Assets")
            os.makedirs(assets_dir)
            dist_dir = os.path.join(root, "dist")
            dist_out = os.path.join(dist_dir, "out")
            os.makedirs(dist_out)

            # Metadata in .gdb
            with open(os.path.join(stage_dir, "my_track.gdb"), "w") as f:
                f.write("TrackName = High Plains\n")

            calls = []

            def fake_build_rfcmp(mas2, cmpinfo_content, target_rfcmp):
                calls.append((target_rfcmp, cmpinfo_content))
                # Create the file so os.path.getsize succeeds
                with open(target_rfcmp, "wb") as f:
                    f.write(b"RFCMP_DATA")

            with mock.patch.object(package_rfcmp, "find_modmgr", return_value="ModMgr.exe"), \
                    mock.patch.object(package_rfcmp, "find_mas2", return_value="MAS2.exe"), \
                    mock.patch.object(package_rfcmp, "build_mas_archive"), \
                    mock.patch.object(package_rfcmp, "build_rfcmp_via_mas2", side_effect=fake_build_rfcmp):

                # 1. Standalone mode: output_rfcmp=None should resolve to dist/out
                with mock.patch.object(steam_paths, "IS_STANDALONE", True), \
                        mock.patch.object(steam_paths, "APP_DIR", dist_dir):
                    package_rfcmp.package_track(stage_dir, output_rfcmp=None)
                    expected_standalone = os.path.abspath(os.path.join(dist_out, "High_Plains_v1.0.rfcmp"))
                    self.assertEqual(calls[-1][0], expected_standalone)

                # 2. Standalone mode with target directory: output_rfcmp=dist_out
                with mock.patch.object(steam_paths, "IS_STANDALONE", True), \
                        mock.patch.object(steam_paths, "APP_DIR", dist_dir):
                    package_rfcmp.package_track(stage_dir, output_rfcmp=dist_out)
                    self.assertEqual(calls[-1][0], expected_standalone)

                # 3. Repo mode: output_rfcmp=None should resolve to track_work (parent of rf2/)
                with mock.patch.object(steam_paths, "IS_STANDALONE", False):
                    package_rfcmp.package_track(stage_dir, output_rfcmp=None)
                    expected_repo = os.path.abspath(os.path.join(track_work, "High_Plains_v1.0.rfcmp"))
                    self.assertEqual(calls[-1][0], expected_repo)

                # 4. Explicit .rfcmp filename
                custom_rfcmp = os.path.join(root, "custom_package.rfcmp")
                with mock.patch.object(steam_paths, "IS_STANDALONE", True):
                    package_rfcmp.package_track(stage_dir, output_rfcmp=custom_rfcmp)
                    self.assertEqual(calls[-1][0], os.path.abspath(custom_rfcmp))

    def test_check_setup_disabled_settings_formatting(self):
        import io
        import contextlib
        import check_setup
        import steam_paths

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # All disabled
            check_setup.check(
                track_dir=None,
                need_blender=False,
                texture_scale=1.0,
                road_smoothness=0.0,
                mesh_coarsening=0.0,
            )
        out = buf.getvalue()
        self.assertIn("texture_scale=1.0 (disabled)", out)
        self.assertIn("road_smoothness=0.0 (disabled)", out)
        self.assertIn("mesh_coarsening=0.0 (disabled)", out)

        # Enabled values should NOT have "(disabled)"
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            check_setup.check(
                track_dir=None,
                need_blender=False,
                texture_scale=0.5,
                road_smoothness=0.25,
                mesh_coarsening=0.5,
            )
        out2 = buf2.getvalue()
        self.assertIn("texture_scale=0.5", out2)
        self.assertNotIn("texture_scale=0.5 (disabled)", out2)
        self.assertIn("road_smoothness=0.25", out2)
        self.assertNotIn("road_smoothness=0.25 (disabled)", out2)
        self.assertIn("mesh_coarsening=0.5", out2)
        self.assertNotIn("mesh_coarsening=0.5 (disabled)", out2)

    def test_package_rfcmp_prints_mas2_capitals_message(self):
        import io
        import contextlib
        import package_rfcmp
        import steam_paths

        with tempfile.TemporaryDirectory() as root:
            stage_dir = os.path.join(root, "track", "rf2", "track")
            os.makedirs(os.path.join(stage_dir, "Assets"))
            with open(os.path.join(stage_dir, "track.gdb"), "w") as f:
                f.write("TrackName = TestTrack\n")

            def fake_rfcmp(mas2, cmpinfo, out_file):
                with open(out_file, "wb") as f:
                    f.write(b"PKG")

            buf = io.StringIO()
            with mock.patch.object(package_rfcmp, "find_modmgr", return_value="ModMgr.exe"), \
                    mock.patch.object(package_rfcmp, "find_mas2", return_value="MAS2.exe"), \
                    mock.patch.object(package_rfcmp, "build_mas_archive"), \
                    mock.patch.object(package_rfcmp, "build_rfcmp_via_mas2", side_effect=fake_rfcmp), \
                    contextlib.redirect_stdout(buf):
                package_rfcmp.package_track(stage_dir)

            out = buf.getvalue()
            self.assertIn(
                "RF2 MAS2.EXE WINDOW WILL BE OPENED AND CLOSED AUTOMATICALLY TO CREATE THE RFCMP FILE.",
                out
            )

    def test_steam_paths_rf2_discovery_and_texconv(self):
        import steam_paths

        with tempfile.TemporaryDirectory() as root:
            # Not an rF2 dir
            self.assertFalse(steam_paths.is_rf2_dir(root))

            # Make it look like an rF2 dir
            tools_dir = os.path.join(root, "Support", "Tools", "MapConverter")
            os.makedirs(tools_dir)
            texconv_exe = os.path.join(tools_dir, "texconv.exe")
            with open(texconv_exe, "wb") as f:
                f.write(b"TEXCONV")

            mas2_exe = os.path.join(root, "Support", "Tools", "MAS2.exe")
            with open(mas2_exe, "wb") as f:
                f.write(b"MAS2")

            self.assertTrue(steam_paths.is_rf2_dir(root))

            # find_texconv should locate Support/Tools/MapConverter/texconv.exe
            found_texconv = steam_paths.find_texconv(root)
            self.assertEqual(os.path.abspath(found_texconv), os.path.abspath(texconv_exe))

            # Test save_rf2_dir_to_config and load_config
            steam_paths.save_rf2_dir_to_config(root, base_dir=root)
            cfg = steam_paths.load_config(base_dir=root)
            self.assertEqual(cfg["rf2_dir"], root)
            self.assertEqual(cfg["rf2_dir_source"], "ini")

            # find_rf2_dir with config should return root
            with mock.patch.object(steam_paths, "load_config", return_value={"rf2_dir": root}):
                self.assertEqual(steam_paths.find_rf2_dir(prompt=False), os.path.realpath(root))

        # probable_rf2_paths should return non-empty list of path candidates
        candidates = steam_paths.probable_rf2_paths()
        self.assertIsInstance(candidates, list)
        self.assertTrue(len(candidates) > 0)

    def test_surfaces_ini_generated_in_work_dir_when_ac_missing(self):
        import io
        import contextlib
        import check_setup
        import convert_track
        import steam_paths

        with tempfile.TemporaryDirectory() as root:
            missing_ac = os.path.join(root, "no_ac")
            work_dir = os.path.join(root, "work_track")
            track_dir = os.path.join(root, "track")
            os.makedirs(os.path.join(track_dir, "data"))
            with open(os.path.join(track_dir, "data", "surfaces.ini"), "w", encoding="utf-8") as f:
                f.write("[SURFACE_0]\nKEY=ASPHALT\nFRICTION=0.98\nIS_VALID_TRACK=1\n")

            surfaces = convert_track.load_surfaces(track_dir, missing_ac, work_dir=work_dir)
            written_path = os.path.join(work_dir, "surfaces.ini")
            self.assertTrue(os.path.isfile(written_path))
            for key in ("ROAD", "GRASS", "KERB", "SAND", "ASPHALT"):
                self.assertIn(key, surfaces)
            self.assertTrue(surfaces["ROAD"]["valid"])
            self.assertEqual(surfaces["ASPHALT"]["friction"], 0.98)

            # When AC system/data/surfaces.ini exists, prefer it without writing to a fresh work_dir
            real_ac = os.path.join(root, "real_ac")
            ac_surf_dir = os.path.join(real_ac, "system", "data")
            os.makedirs(ac_surf_dir)
            ac_surf = os.path.join(ac_surf_dir, "surfaces.ini")
            with open(ac_surf, "w", encoding="utf-8") as f:
                f.write("[SURFACE_0]\nKEY=CUSTOMROAD\nFRICTION=0.95\nIS_VALID_TRACK=1\n")
            fresh_work = os.path.join(root, "fresh_work")
            resolved = steam_paths.resolve_surfaces_ini(real_ac, work_dir=fresh_work)
            self.assertEqual(os.path.abspath(resolved), os.path.abspath(ac_surf))
            self.assertFalse(os.path.exists(os.path.join(fresh_work, "surfaces.ini")))

            # check_setup should report ok (not problem) when AC_DIR does not exist
            buf = io.StringIO()
            with mock.patch.object(steam_paths, "AC_DIR", missing_ac), \
                    contextlib.redirect_stdout(buf):
                check_setup.check(track_dir=None, need_blender=False)
            out = buf.getvalue()
            self.assertIn("built-in default surfaces.ini", out)
            self.assertNotIn("Assetto Corsa not found", out)


if __name__ == "__main__":
    unittest.main()
