"""Offline tests for the v3.0 editing layer (no engine, no network).

Run with:  python -m pytest tests -q     or    python tests/test_edit.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import locmodel_edit as ed  # noqa: E402


def solid_png(path: Path, size: int, rgb: tuple) -> Path:
    ed.write_png_rgb(path, size, size, bytes(rgb) * (size * size), 3)
    return path


class ModelFamilyTests(unittest.TestCase):
    def test_edit_model_ranked_first(self):
        row = ed.family_of("Qwen-Image-Edit-2509-Q4_K_M.gguf")
        self.assertEqual(row["key"], "qwen-image-edit")
        self.assertTrue(row["can_region_edit"])
        self.assertEqual(row["rank"], 1)

    def test_plain_qwen_image_is_not_region_editable(self):
        row = ed.family_of("Qwen_Image-Q4_K_M.gguf")
        self.assertEqual(row["key"], "qwen-image")
        self.assertFalse(row["can_region_edit"])
        self.assertEqual(row["rank"], 3)

    def test_flux_klein_rank_two(self):
        row = ed.family_of("flux2-klein-4b-Q8_0.gguf")
        self.assertEqual(row["key"], "flux2-klein")
        self.assertTrue(row["can_region_edit"])
        self.assertEqual(row["rank"], 2)

    def test_generation_only_models_are_last(self):
        for name in ("Z-Image-Turbo.gguf", "flux1-schnell-Q4.gguf", "sd_xl_base_1.0.safetensors"):
            with self.subTest(name=name):
                row = ed.family_of(name)
                self.assertEqual(row["family"], "gen")
                self.assertEqual(row["rank"], 99)

    def test_unknown_model_is_generation_only(self):
        row = ed.family_of("some-random-model.gguf")
        self.assertEqual(row["family"], "unknown")
        self.assertFalse(row["can_region_edit"])

    def test_priority_never_misreads_an_edit_model(self):
        # A reordered priority must not make "Qwen-Image-Edit" look like plain Qwen-Image.
        custom = ["qwen-image", "qwen-image-edit"]
        self.assertEqual(ed.family_of("Qwen-Image-Edit.gguf", custom)["key"], "qwen-image-edit")
        self.assertEqual(ed.family_of("Qwen_Image.gguf", custom)["key"], "qwen-image")



class PickTests(unittest.TestCase):
    def _rows(self):
        return [
            {"name": "Qwen-Image-Edit.gguf", "rank": 1, "can_region_edit": True, "loaded": False},
            {"name": "flux2-klein-4b.gguf", "rank": 2, "can_region_edit": True, "loaded": False},
        ]

    def test_lowest_rank_wins(self):
        pick = ed._pick(self._rows(), prefer_loaded=False)
        self.assertEqual(pick["name"], "Qwen-Image-Edit.gguf")

    def test_prefer_loaded_keeps_the_loaded_model(self):
        rows = self._rows()
        rows[1]["loaded"] = True
        self.assertEqual(ed._pick(rows, prefer_loaded=True)["name"], "flux2-klein-4b.gguf")
        self.assertEqual(ed._pick(rows, prefer_loaded=False)["name"], "Qwen-Image-Edit.gguf")


class MaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="locmodel-mask-"))
        self.src = solid_png(self.tmp / "src.png", 8, (10, 20, 30))

    def test_norm_rect_maps_to_pixels(self):
        info = ed.build_mask(self.tmp / "m.png", 8, 8,
                             {"regions": [{"type": "rect", "x": [0, 0, 0.5, 0.5]}]},
                             source=self.src)
        self.assertEqual(info["regions"][0]["px"], [0, 0, 4, 4])
        self.assertAlmostEqual(info["coverage"], 0.25, places=2)

    def test_px_units_and_multiple_boxes(self):
        info = ed.build_mask(self.tmp / "m.png", 8, 8,
                             {"unit": "px", "boxes": [[0, 0, 2, 2], [4, 4, 8, 8]]})
        self.assertEqual([r["px"] for r in info["regions"]], [[0, 0, 2, 2], [4, 4, 8, 8]])
        self.assertGreater(info["coverage"], 0.1)

    def test_empty_region_list_is_rejected(self):
        with self.assertRaises(ed.EditError):
            ed.build_mask(self.tmp / "m.png", 8, 8, {"regions": []})

    def test_full_cover_mask_is_rejected(self):
        with self.assertRaises(ed.EditError):
            ed.build_mask(self.tmp / "m.png", 8, 8,
                          {"regions": [{"type": "rect", "x": [0, 0, 1, 1]}]})

    def test_foreign_mask_is_rescaled(self):
        small = self.tmp / "small.png"
        px = bytearray()
        for y in range(4):                       # top half white, bottom half black
            px += (bytes([255]) if y < 2 else bytes([0])) * 4
        ed.write_png_gray(small, 4, 4, bytes(px))
        info = ed.load_mask(small, 8, 8)
        self.assertTrue(info["rescaled"])
        self.assertAlmostEqual(info["coverage"], 0.5, places=2)


class RecomposeTests(unittest.TestCase):
    def test_pixels_outside_the_mask_are_untouched(self):
        tmp = Path(tempfile.mkdtemp(prefix="locmodel-rc-"))
        orig = solid_png(tmp / "o.png", 8, (10, 20, 30))
        mask = Path(ed.build_mask(tmp / "m.png", 8, 8,
                                  {"regions": [{"type": "rect", "x": [0, 0, 0.5, 0.5]}]},
                                  source=orig)["path"])
        edited = solid_png(tmp / "e.png", 8, (255, 0, 0))
        res = ed.recompose(edited.read_bytes(), orig, mask, tmp / "c.png")

        self.assertTrue(res["ok"])
        self.assertEqual(res["redrawn_pixels"], 16)
        self.assertEqual(res["protected_pixels"], 48)
        px = ed.read_png(tmp / "c.png")["pixels"]
        self.assertEqual(tuple(px[0:3]), (255, 0, 0))      # inside the mask
        self.assertEqual(tuple(px[-3:]), (10, 20, 30))     # bottom-right corner kept


class DryRunTests(unittest.TestCase):
    """A dry run must work on a machine with no engine and no models."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="locmodel-dry-"))
        self.src = solid_png(self.tmp / "src.png", 16, (40, 50, 60))
        self.cfg_path = self.tmp / "config.json"
        os.environ["LOCMODEL_IMAGE_CONFIG"] = str(self.cfg_path)

    def tearDown(self):
        os.environ.pop("LOCMODEL_IMAGE_CONFIG", None)

    def test_edit_dry_run_reports_payload_and_mask(self):
        res = ed.edit_image(str(self.src), "red brick wall", None,
                            regions={"regions": [{"type": "rect", "x": [0.1, 0.1, 0.5, 0.5]}]},
                            dry_run=True, out_dir=str(self.tmp / "out"))
        self.assertTrue(res["ok"])
        self.assertIn("init_image", res["payload_keys"])
        self.assertIn("mask_image", res["payload_keys"])
        self.assertEqual(res["size"], [16, 16])
        self.assertEqual(res["strength"], 0.75)
        self.assertIn("warning", res)  # the model is resolved on submit, not on a dry run

    def test_redraw_dry_run_has_no_mask(self):
        res = ed.redraw_image(str(self.src), "watercolour", None,
                             dry_run=True, out_dir=str(self.tmp / "out"))
        self.assertTrue(res["ok"])
        self.assertNotIn("mask_image", res["payload_keys"])
        self.assertEqual(res["strength"], 0.6)

    def test_missing_source_raises_bad_arguments(self):
        with self.assertRaises(ed.EditError) as ctx:
            ed.edit_image(str(self.tmp / "nope.png"), "x", None,
                          regions={"regions": [{"type": "rect", "x": [0, 0, 0.5, 0.5]}]},
                          dry_run=True)
        self.assertEqual(ctx.exception.code, "bad_arguments")

    def test_edit_without_mask_is_rejected(self):
        with self.assertRaises(ed.EditError) as ctx:
            ed.edit_image(str(self.src), "x", None, dry_run=True)
        self.assertEqual(ctx.exception.code, "bad_arguments")

    def test_empty_prompt_is_rejected(self):
        with self.assertRaises(ed.EditError):
            ed.edit_image(str(self.src), "   ", None,
                          regions={"regions": [{"type": "rect", "x": [0, 0, 0.5, 0.5]}]},
                          dry_run=True)


class ConfigTests(unittest.TestCase):
    def test_edit_defaults_are_wired(self):
        import locmodel_image as core

        edit = core.DEFAULTS["edit"]
        self.assertEqual(edit["priority"], ["qwen-image-edit", "flux2-klein", "qwen-image"])
        self.assertTrue(edit["prefer_loaded"])
        self.assertTrue(edit["auto_load"])
        self.assertEqual(core.VERSION, "3.0.0")

    def test_mcp_exposes_the_edit_tools(self):
        import locmodel_mcp as mcp

        names = {t["name"] for t in mcp.TOOLS}
        for expected in ("edit_image", "redraw_image", "list_edit_models", "load_model"):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
