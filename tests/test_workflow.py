"""Unit tests for the 3.1 workflow parameter contract (no engine required)."""

import unittest

from scripts import locmodel_workflow as wf


class NormalizeTests(unittest.TestCase):
    def test_aliases_collapse(self):
        clean = wf.normalize({"cfg_scale": 3.5, "sample_steps": 8, "txt_cfg": 4.0})
        self.assertEqual(clean["cfg"], 4.0)          # last writer wins
        self.assertEqual(clean["steps"], 8)
        self.assertNotIn("cfg_scale", clean)

    def test_types_coerced(self):
        clean = wf.normalize({"steps": "12", "cfg": "3.5", "slg_layers": "7,8,9",
                              "vae_tiling": "on"})
        self.assertEqual(clean["steps"], 12)
        self.assertIsInstance(clean["steps"], int)
        self.assertEqual(clean["cfg"], 3.5)
        self.assertEqual(clean["slg_layers"], [7, 8, 9])
        self.assertIs(clean["vae_tiling"], True)

    def test_enum_is_case_insensitive(self):
        self.assertEqual(wf.normalize({"scheduler": "Karras"})["scheduler"], "karras")

    def test_bad_values_rejected(self):
        for bad in ({"cfg": "abc"}, {"scheduler": "bogus"}, {"steps": 500},
                    {"nonsense_key": 1}, {"count": 0}, {"steps": 8.5}):
            with self.assertRaises(wf.ParamError, msg=str(bad)):
                wf.normalize(bad)

    def test_none_dropped(self):
        self.assertEqual(wf.normalize({"cfg": None, "steps": 8}), {"steps": 8})


class ApplyTests(unittest.TestCase):
    def test_nested_paths(self):
        payload = {"sample_params": {"sample_steps": 20}}
        wf.apply(payload, wf.normalize({
            "cfg": 3.5, "steps": 8, "scheduler": "karras",
            "distilled_guidance": 3.0, "slg_scale": 1.0,
            "vae_target_overlap": 0.4, "hires_target_width": 1536,
            "count": 2, "seed": 7,
        }))
        sp = payload["sample_params"]
        self.assertEqual(sp["sample_steps"], 8)
        self.assertEqual(sp["sample_method"], "default") if "sample_method" in sp else None
        self.assertEqual(sp["scheduler"], "karras")
        self.assertEqual(sp["guidance"]["txt_cfg"], 3.5)
        self.assertEqual(sp["guidance"]["distilled_guidance"], 3.0)
        self.assertEqual(sp["guidance"]["slg"]["scale"], 1.0)
        self.assertEqual(payload["vae_tiling_params"]["target_overlap"], 0.4)
        self.assertEqual(payload["hires"]["target_width"], 1536)
        self.assertEqual(payload["batch_count"], 2)
        self.assertEqual(payload["seed"], 7)

    def test_empty_is_noop(self):
        payload = {"sample_params": {"sample_steps": 20}}
        wf.apply(payload, {})
        self.assertEqual(payload, {"sample_params": {"sample_steps": 20}})


class ResolveTests(unittest.TestCase):
    def test_precedence(self):
        # per-call params > explicit tool arguments > session parameters
        merged = wf.resolve({"steps": 8}, {"steps": 12, "cfg": 3.0}, {"cfg": 4.0, "seed": 9})
        self.assertEqual(merged["steps"], 12)
        self.assertEqual(merged["cfg"], 3.0)
        self.assertEqual(merged["seed"], 9)     # session still fills what nobody else set

    def test_session_lower_than_explicit(self):
        merged = wf.resolve({"steps": 8}, None, {"steps": 12})
        self.assertEqual(merged["steps"], 8)

    def test_session_reset(self):
        wf.session_set({"cfg": 3.5})
        self.assertEqual(wf.session_get(), {"cfg": 3.5})
        wf.session_reset()
        self.assertEqual(wf.session_get(), {})
        wf.session_set({"cfg": 3.5, "steps": 10})
        wf.session_reset(keys=["cfg"])
        self.assertEqual(wf.session_get(), {"steps": 10})
        wf.session_reset()

    def test_session_rejects_unknown(self):
        with self.assertRaises(wf.ParamError):
            wf.session_set({"nope": 1})
        self.assertEqual(wf.session_get(), {})


class DefaultsTests(unittest.TestCase):
    CAPS = {
        "defaults": {
            "sample_params": {
                "sample_steps": 20,
                "sample_method": "euler",
                "scheduler": "default",
                "guidance": {"txt_cfg": 7.0},
            },
            "clip_skip": -1,
        },
    }

    def test_reads_guidance_block(self):
        d = wf.defaults_from_capabilities(self.CAPS)
        self.assertEqual(d["cfg"], 7.0)
        self.assertEqual(d["steps"], 20)
        self.assertEqual(d["sampler"], "euler")

    def test_engine_spelling_variant(self):
        caps = {"defaults": {"sample_params": {"guided": {"txt_cfg": 5.0}}}}
        self.assertEqual(wf.defaults_from_capabilities(caps)["cfg"], 5.0)

    def test_source_labels(self):
        rows = {r["param"]: r for r in wf.table(self.CAPS)}
        self.assertEqual(rows["cfg"]["source"], "engine")
        self.assertEqual(rows["steps"]["source"], "engine")
        self.assertEqual(rows["sampler"]["source"], "engine")
        self.assertEqual(rows["img_cfg"]["source"], "unset")    # engine advertises nothing

    def test_table_covers_core_and_folded(self):
        core = wf.table(None)
        self.assertEqual(len(core), len(wf.CORE_KEYS))
        full = wf.table(None, include_folded=True)
        self.assertGreater(len(full), len(core))
        allowed = {"core"} | set(wf.FOLDED_GROUPS)
        self.assertTrue(all(r["tier"] in allowed for r in full))

    def test_core_key_count_in_documented_band(self):
        self.assertGreaterEqual(len(wf.CORE_KEYS), 7)
        self.assertLessEqual(len(wf.CORE_KEYS), 16)


class CapabilityAdoptTests(unittest.TestCase):
    """v3.2: the live /sdcpp/v1/capabilities payload drives every enumeration."""

    CAPS = {
        "current_mode": "img_gen",
        "supported_modes": ["img_gen"],
        "limits": {"min_width": 64, "min_height": 64, "max_width": 4096,
                   "max_height": 4096, "max_batch_count": 8,
                   "max_upscale_width": 8192, "max_upscale_height": 8192},
        "output_formats": ["png", "jpeg", "webp"],
        "preview_modes": ["none", "proj", "tae", "vae"],
        "upscalers": [
            {"name": "None", "model": False}, {"name": "Lanczos", "model": False},
            {"name": "Latent", "model": False}, {"name": "Latent (nearest)", "model": False},
            {"name": "Latent (nearest-exact)", "model": False},
            {"name": "Latent (antialiased)", "model": False},
            {"name": "Latent (bicubic)", "model": False},
            {"name": "Latent (bicubic antialiased)", "model": False},
        ],
        "samplers": ["euler", "dpm++2m", "lcm"],
        "schedulers": ["default", "karras", "beta"],
        "features": {"hires": True, "vae_tiling": True, "lora": False},
        "generation_features": {"clip_skip": True, "flow_shift": True, "img2img": True},
        "defaults_by_mode": {"img_gen": {"sample_params": {"sample_steps": 4, "sample_method": "euler"}}},
    }

    def setUp(self):
        self._snap = {name: list(getattr(wf, name)) for name in
                      ("SAMPLERS", "SCHEDULERS", "UPSCALERS", "OUTPUT_FORMATS", "PREVIEW_MODES")}
        self._limits = dict(wf.LIMITS)
        self._features = dict(wf.FEATURES)

    def tearDown(self):
        for name, vals in self._snap.items():
            getattr(wf, name)[:] = vals
        wf.LIMITS.clear()
        wf.LIMITS.update(self._limits)
        wf.FEATURES.clear()
        wf.FEATURES.update(self._features)

    def test_upscaler_spelling_matches_engine(self):
        wf.adopt_capabilities(self.CAPS)
        self.assertIn("Latent (nearest)", wf.UPSCALERS)
        self.assertNotIn("Latent(nearest)", wf.UPSCALERS)
        wf.normalize({"hires_upscaler": "Latent (nearest)"})   # no ParamError

    def test_enumerations_adopted(self):
        wf.adopt_capabilities(self.CAPS)
        self.assertEqual(wf.OUTPUT_FORMATS, ["png", "jpeg", "webp"])
        self.assertIn("default", wf.SCHEDULERS)     # baseline word kept for the engine
        self.assertEqual(wf.SAMPLERS, ["default", "euler", "dpm++2m", "lcm"])

    def test_limits_adopted(self):
        wf.adopt_capabilities(self.CAPS)
        self.assertEqual(wf.engine_limits()["max_batch_count"], 8)
        self.assertEqual(wf.LIMITS["max_upscale_width"], 8192)

    def test_features_merged(self):
        wf.adopt_capabilities(self.CAPS)
        self.assertTrue(wf.feature_enabled("hires"))
        self.assertTrue(wf.feature_enabled("img2img"))
        self.assertFalse(wf.feature_enabled("lora"))
        self.assertIsNone(wf.feature_enabled("nonexistent"))

    def test_unsupported_keys_reported(self):
        wf.adopt_capabilities(self.CAPS)
        self.assertEqual(wf.unsupported_keys(["hires_scale"]), [])      # hires is on
        self.assertEqual(wf.unsupported_keys(["lora"]), ["lora"])        # lora is off
        self.assertEqual(wf.unsupported_keys(["nonexistent_key"]), [])   # not engine-backed

    def test_fuzzy_enum_matching(self):
        wf.adopt_capabilities(self.CAPS)
        got = wf.normalize({"hires_upscaler": "latent(nearest)", "output_format": "JPEG"})
        self.assertEqual(got["hires_upscaler"], "Latent (nearest)")
        self.assertEqual(got["output_format"], "jpeg")

    def test_defaults_by_mode_used_when_defaults_absent(self):
        caps = dict(self.CAPS)
        caps.pop("defaults", None)
        self.assertEqual(wf.defaults_from_capabilities(caps)["steps"], 4)

    def test_v32_new_keys_normalise(self):
        got = wf.normalize({"hires_scale": 2.0, "vae_rel_size_w": 0.5,
                            "output_compression": 90, "output_format": "webp"})
        self.assertEqual(got["hires_scale"], 2.0)
        self.assertEqual(got["vae_rel_size_w"], 0.5)
        self.assertEqual(got["output_compression"], 90)

    def test_v32_ranges_enforced(self):
        for bad in ({"hires_scale": 0.5}, {"vae_rel_size_w": 1.5},
                    {"output_compression": 200}, {"output_format": "gif"}):
            with self.assertRaises(wf.ParamError):
                wf.normalize(bad)

    def test_v32_payload_paths(self):
        payload = {}
        wf.apply(payload, wf.normalize({"hires_scale": 2.0, "output_format": "jpeg",
                                        "output_compression": 88, "vae_rel_size_w": 0.5}))
        self.assertEqual(payload["hires"]["scale"], 2.0)
        self.assertEqual(payload["output_format"], "jpeg")
        self.assertEqual(payload["output_compression"], 88)
        self.assertEqual(payload["vae_tiling_params"]["rel_size_w"], 0.5)


if __name__ == "__main__":
    unittest.main()
