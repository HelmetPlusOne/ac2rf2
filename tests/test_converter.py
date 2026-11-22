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


if __name__ == "__main__":
    unittest.main()
