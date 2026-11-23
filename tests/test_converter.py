import os
import struct
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
