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

        # Scaled down when >= min_size
        self.assertEqual(fix_textures.target_dimensions(2048, 2048, scale=0.5, min_size=1024), (1024, 1024))
        self.assertEqual(fix_textures.target_dimensions(1024, 512, scale=0.5, min_size=1024), (512, 256))
        # Not scaled down when < min_size
        self.assertEqual(fix_textures.target_dimensions(512, 512, scale=0.5, min_size=1024), (512, 512))
        self.assertEqual(fix_textures.target_dimensions(256, 128, scale=0.5, min_size=1024), (256, 128))
        # Non power of two snaps up to power of two
        self.assertEqual(fix_textures.target_dimensions(300, 200, scale=1.0, min_size=1024), (512, 256))

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


if __name__ == "__main__":
    unittest.main()
