"""Workflow parameter contract for the LocModel / stable-diffusion.cpp bridge.

Why this module exists
----------------------
LocModel's built-in workflow exposes dozens of sampling knobs, but the GUI owns
``config.json`` and rewrites the user's custom values from its own panel state.
Anything stored in that file is therefore volatile. The only durable channel is
the HTTP submit payload (``POST /sdcpp/v1/img_gen``), which is read per job.

Everything here is therefore *payload-only*: no file in LocModel's tree is ever
touched. Three layers of state, resolved in this order (later wins):

1. engine defaults  - read live from ``/sdcpp/v1/capabilities``
2. session params   - an in-memory dict held by the MCP process, cleared on exit
3. per-call params  - named tool arguments and the free-form ``params`` mapping

The engine validates almost nothing: it silently accepts a bogus ``scheduler``
and then stalls the job queue. Validation therefore has to happen here, and it
fails loudly with the legal range / enumeration instead of dropping keys.
"""

from __future__ import annotations

import copy

VERSION = "3.2.0"

# --- enumerations measured on sd-server master-1050 (58bb3c1) ---------------
# These are the *offline* baseline. Whenever the engine is reachable the real
# lists from ``/sdcpp/v1/capabilities`` replace them in place (see
# ``adopt_capabilities``), because a value the engine does not recognise is
# rejected with HTTP 400 ("invalid generation parameters"). The lists are
# mutated in place on purpose: SPEC holds references to them, so every later
# validation / listing call sees the live set without touching SPEC.
SCHEDULERS = [
    "default", "discrete", "normal", "karras", "exponential", "ays", "gits",
    "sgm_uniform", "simple", "smoothstep", "kl_optimal", "lcm", "bong_tangent",
    "ltx2", "logit_normal", "flux2", "flux", "beta", "llada_image",
]
SAMPLERS = [
    "default", "euler", "euler_a", "heun", "dpm2", "dpm++2s_a", "dpm++2m",
    "dpm++2mv2", "ipndm", "ipndm_v", "lcm", "ddim_trailing", "tcd",
    "res_multistep", "res_2s", "er_sde", "euler_cfg_pp", "euler_a_cfg_pp",
    "euler_ge", "dpm++2m_sde", "dpm++2m_sde_bt", "lms",
]
# The engine spells the Latent variants WITH a space ("Latent (nearest)").
# v3.1 shipped them without one, so every Latent upscaler was rejected with
# HTTP 400 and hi-res fix silently fell back to the non-Latent path.
UPSCALERS = [
    "None", "Lanczos", "Nearest", "Latent", "Latent (nearest)",
    "Latent (nearest-exact)", "Latent (antialiased)", "Latent (bicubic)",
    "Latent (bicubic antialiased)",
]
OUTPUT_FORMATS = ["png", "jpeg", "webp"]
PREVIEW_MODES = ["none", "proj", "tae", "vae"]

#: engine limits, filled from ``capabilities.limits`` (module-level fallbacks
#: match the sd-server defaults so the tool still works with an old engine)
LIMITS = {
    "min_width": 64, "min_height": 64, "max_width": 4096, "max_height": 4096,
    "max_batch_count": 8, "max_upscale_width": 8192, "max_upscale_height": 8192,
}
FEATURES: dict = {}

# --- the contract -----------------------------------------------------------
# path     : where the value lands inside the payload
# kind     : int | float | bool | str | enum | intlist | json
# core     : True -> shown expanded in the skill table, False -> folded group
# group    : folded section name used by the docs / params listing
# default  : fallback when the engine does not report a value
SPEC: dict[str, dict] = {
    # ---------------- core, image generation ------------------------------
    "cfg": dict(path=("sample_params", "guidance", "txt_cfg"), kind="float",
                min=0.0, max=30.0, core=True, alias=("cfg_scale", "txt_cfg"),
                label="CFG / guidance scale",
                note="1.0-1.5 keeps composition, 2.0+ follows the prompt harder and blows out highlights quickly"),
    "img_cfg": dict(path=("sample_params", "guidance", "img_cfg"), kind="float",
                    min=0.0, max=30.0, core=True, label="image CFG (img2img side)",
                    note="only used when the workflow runs the conditional image branch; null disables it"),
    "steps": dict(path=("sample_params", "sample_steps"), kind="int",
                  min=1, max=200, core=True, alias=("sample_steps",),
                  label="sampling steps",
                  note="turbo/flow models are done at 8-12; above 30 the gain is invisible and the wait doubles"),
    "sampler": dict(path=("sample_params", "sample_method"), kind="enum",
                    enum=SAMPLERS, core=True, alias=("sample_method",),
                    label="sampling method",
                    note="euler is the safe default; dpm++2m is a good general upgrade"),
    "scheduler": dict(path=("sample_params", "scheduler"), kind="enum",
                      enum=SCHEDULERS, core=True, label="sigma scheduler",
                      note="karras / beta sharpen, sgm_uniform is smoother; 'default' lets the engine map it per model"),
    "flow_shift": dict(path=("sample_params", "flow_shift"), kind="float",
                       min=0.0, max=100.0, core=True, nullable=True,
                       label="flow shift (flow matching models)",
                       note="null = auto; 2.0-5.0 changes how much low-frequency structure the flow step keeps"),
    "shifted_timestep": dict(path=("sample_params", "shifted_timestep"), kind="float",
                             min=-1.0, max=1000.0, core=True,
                             label="shifted timestep"),
    "eta": dict(path=("sample_params", "eta"), kind="float", min=0.0, max=1.0,
                core=True, nullable=True, label="eta (SDE noise injection)",
                note="0 = deterministic, 1 = fully stochastic; only SDE samplers use it"),
    "distilled_guidance": dict(path=("sample_params", "guidance", "distilled_guidance"),
                               kind="float", min=0.0, max=30.0, core=True,
                               label="distilled guidance",
                               note="guidance baked into distilled/turbo checkpoints; 3.5 is the upstream default"),
    "slg_scale": dict(path=("sample_params", "guidance", "slg", "scale"), kind="float",
                      min=-10.0, max=10.0, core=True, label="SLG scale",
                      note="0 disables skip-layer guidance; +/-1-3 is the useful band"),
    "clip_skip": dict(path=("clip_skip",), kind="int", min=-64, max=12, core=True,
                      label="CLIP skip", note="-1 = engine default (usually 1 layer skipped)"),
    "strength": dict(path=("strength",), kind="float", min=0.0, max=1.0, core=True,
                     label="denoising strength",
                     note="img2img / edit only: 0.35 subtle, 0.75 the usual, 1.0 ignores the source"),
    "seed": dict(path=("seed",), kind="int", min=-1, max=2 ** 31 - 1, core=True,
                 label="seed", note="-1 random; a fixed seed is how you prove a parameter change did something"),
    "count": dict(path=("batch_count",), kind="int", min=1, max=8, core=True,
                  alias=("batch_count",), label="images per job"),

    # ---------------- folded: SLG details ---------------------------------
    "slg_layers": dict(path=("sample_params", "guidance", "slg", "layers"),
                       kind="intlist", core=False, group="slg",
                       label="SLG layers", default=[7, 8, 9]),
    "slg_layer_start": dict(path=("sample_params", "guidance", "slg", "layer_start"),
                            kind="float", min=0.0, max=1.0, core=False, group="slg",
                            label="SLG layer start ratio"),
    "slg_layer_end": dict(path=("sample_params", "guidance", "slg", "layer_end"),
                          kind="float", min=0.0, max=1.0, core=False, group="slg",
                          label="SLG layer end ratio"),

    # ---------------- folded: hires fix -----------------------------------
    "hires_enabled": dict(path=("hires", "enabled"), kind="bool", core=False,
                          group="hires", label="hires fix on/off"),
    "hires_upscaler": dict(path=("hires", "upscaler"), kind="enum", enum=UPSCALERS,
                           core=False, group="hires", label="hires upscaler"),
    "hires_target_width": dict(path=("hires", "target_width"), kind="int",
                               min=64, max=8192, core=False, group="hires",
                               label="hires target width"),
    "hires_target_height": dict(path=("hires", "target_height"), kind="int",
                                min=64, max=8192, core=False, group="hires",
                                label="hires target height"),
    "hires_steps": dict(path=("hires", "steps"), kind="int", min=1, max=200,
                        core=False, group="hires", label="hires second-pass steps"),
    "hires_denoising_strength": dict(path=("hires", "denoising_strength"),
                                     kind="float", min=0.0, max=1.0, core=False,
                                     group="hires", label="hires denoising strength"),
    "hires_upscale_tile_size": dict(path=("hires", "upscale_tile_size"), kind="int",
                                    min=0, max=4096, core=False, group="hires",
                                    label="hires upscale tile size"),
    # v3.2: the engine accepts a multiplier as well as absolute target sizes.
    # scale is what the GUI slider drives, so expose it directly instead of
    # making the caller recompute width/height by hand.
    "hires_scale": dict(path=("hires", "scale"), kind="float", min=1.0, max=8.0,
                        core=False, group="hires", label="hires scale multiplier",
                        note="only used when hires_target_width/height stay 0; 2.0 doubles both edges"),
    "hires_custom_sigmas": dict(path=("hires", "custom_sigmas"), kind="json",
                                core=False, group="hires",
                                label="hires custom sigma schedule (list)"),

    # ---------------- folded: VAE tiling ----------------------------------
    "vae_tiling": dict(path=("vae_tiling_params", "enabled"), kind="bool",
                       core=False, group="vae_tiling", label="VAE tiling on/off",
                       note="the low-VRAM escape hatch for big canvases"),
    "vae_tile_size_w": dict(path=("vae_tiling_params", "tile_size_w"), kind="int",
                            min=0, max=4096, core=False, group="vae_tiling",
                            label="tile width"),
    "vae_tile_size_h": dict(path=("vae_tiling_params", "tile_size_h"), kind="int",
                            min=0, max=4096, core=False, group="vae_tiling",
                            label="tile height"),
    "vae_rel_size_w": dict(path=("vae_tiling_params", "rel_size_w"), kind="float",
                           min=0.0, max=1.0, core=False, group="vae_tiling",
                           label="tile width as a fraction of the image",
                           note="0 lets the engine pick; 0.5 is the usual safe value"),
    "vae_rel_size_h": dict(path=("vae_tiling_params", "rel_size_h"), kind="float",
                           min=0.0, max=1.0, core=False, group="vae_tiling",
                           label="tile height as a fraction of the image"),
    "vae_extra_tiling_args": dict(path=("vae_tiling_params", "extra_tiling_args"),
                                  kind="json", core=False, group="vae_tiling",
                                  label="extra tiling args (key=value map)"),
    "vae_target_overlap": dict(path=("vae_tiling_params", "target_overlap"),
                               kind="float", min=0.0, max=1.0, core=False,
                               group="vae_tiling", label="tile overlap"),
    "vae_temporal_tiling": dict(path=("vae_tiling_params", "temporal_tiling"),
                                kind="bool", core=False, group="vae_tiling",
                                label="temporal tiling (video models)"),

    # ---------------- folded: output --------------------------------------
    "output_format": dict(path=("output_format",), kind="enum", enum=OUTPUT_FORMATS,
                          core=False, group="output", label="output file format",
                          note="png is lossless; jpeg/webp are much smaller for review copies"),
    "output_compression": dict(path=("output_compression",), kind="int",
                               min=0, max=100, core=False, group="output",
                               label="output compression level",
                               note="only meaningful for jpeg/webp; 100 = best quality"),

    # ---------------- folded: high noise (dual-model models) --------------
    "high_noise_steps": dict(path=("high_noise_sample_params", "sample_steps"),
                             kind="int", min=1, max=200, core=False,
                             group="high_noise", label="high-noise steps"),
    "high_noise_cfg": dict(path=("high_noise_sample_params", "guidance", "txt_cfg"),
                           kind="float", min=0.0, max=30.0, core=False,
                           group="high_noise", label="high-noise CFG"),
    "high_noise_sampler": dict(path=("high_noise_sample_params", "sample_method"),
                               kind="enum", enum=SAMPLERS, core=False,
                               group="high_noise", label="high-noise sampler"),

    # ---------------- folded: pass-through -------------------------------
    "custom_sigmas": dict(path=("sample_params", "custom_sigmas"), kind="json",
                          core=False, group="raw",
                          label="custom sigma schedule (list)"),
    "extra_sample_args": dict(path=("sample_params", "extra_sample_args"),
                              kind="json", core=False, group="raw",
                              label="extra sampler args (key=value map)"),
    "lora": dict(path=("lora",), kind="json", core=False, group="raw",
                 label='LoRA list, e.g. [{"path":"name.safetensors","multiplier":0.8}]',
                 note="needs the engine started with --lora-model-dir; unavailable otherwise"),
}

#: names accepted for every knob (canonical + aliases)
_LOOKUP: dict[str, str] = {}
for _k, _v in SPEC.items():
    _LOOKUP[_k.lower()] = _k
    for _a in _v.get("alias", ()):
        _LOOKUP[_a.lower()] = _k

CORE_KEYS = [k for k, v in SPEC.items() if v.get("core")]
FOLDED_GROUPS: dict[str, list[str]] = {}
for _k, _v in SPEC.items():
    if not _v.get("core"):
        FOLDED_GROUPS.setdefault(_v.get("group", "other"), []).append(_k)


class ParamError(ValueError):
    """Raised for unknown / malformed workflow parameters."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


# --- coercion ---------------------------------------------------------------
_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}
_NULL = {"", "null", "none", "auto", "default"}


def _squash(text: str) -> str:
    """Lower-case and drop everything that is not alphanumeric."""
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def _coerce(key: str, value, spec: dict):
    kind = spec["kind"]
    name = f"{key}={value!r}"

    if kind == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ParamError(f"{name} is not a boolean", "use true/false or on/off")

    if kind == "int":
        if isinstance(value, bool):
            raise ParamError(f"{name} is not an integer")
        try:
            iv = int(str(value).strip())
        except (TypeError, ValueError):
            raise ParamError(f"{name} is not an integer") from None
        return _range(key, iv, spec)

    if kind == "float":
        if isinstance(value, bool):
            raise ParamError(f"{name} is not a number")
        if value is None or str(value).strip().lower() in _NULL:
            if spec.get("nullable"):
                return None
            raise ParamError(f"{name} must be a number")
        try:
            fv = float(str(value).strip())
        except (TypeError, ValueError):
            raise ParamError(f"{name} is not a number") from None
        return _range(key, fv, spec)

    if kind == "enum":
        s = str(value).strip()
        enum = spec["enum"]
        if s in enum:
            return s
        # exact, then case-insensitive, then punctuation/space-insensitive:
        # users type "latent(nearest)" while the engine spells it
        # "Latent (nearest)", and a stricter matcher rejected both spellings
        low = {e.lower(): e for e in enum}
        if s.lower() in low:
            return low[s.lower()]
        squash = {_squash(e): e for e in enum}
        hit = squash.get(_squash(s))
        if hit is not None:
            return hit
        raise ParamError(f"{name} is not one of the supported values",
                         "supported: " + ", ".join(enum))

    if kind == "intlist":
        if isinstance(value, (list, tuple)):
            try:
                return [int(v) for v in value]
            except (TypeError, ValueError):
                raise ParamError(f"{name} must be a list of integers") from None
        s = str(value).strip().strip("[]")
        if not s:
            return []
        try:
            return [int(p) for p in s.replace(" ", "").split(",") if p != ""]
        except ValueError:
            raise ParamError(f"{name} must be a comma-separated list of integers") from None

    if kind == "json":
        if isinstance(value, (dict, list, int, float, bool)) or value is None:
            return value
        import json as _json
        try:
            return _json.loads(str(value))
        except Exception:
            raise ParamError(f"{name} must be valid JSON") from None

    # str
    return str(value)


def _range(key: str, num, spec: dict):
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and num < lo:
        raise ParamError(f"{key}={num} is below the minimum",
                         f"valid range: {lo} .. {hi}")
    if hi is not None and num > hi:
        raise ParamError(f"{key}={num} exceeds the maximum",
                         f"valid range: {lo} .. {hi}")
    return num


def canonical(name: str) -> str:
    key = _LOOKUP.get(str(name).strip().lower())
    if key is None:
        raise ParamError(
            f"unknown workflow parameter {name!r}",
            "known parameters: " + ", ".join(CORE_KEYS + sorted(
                k for grp in FOLDED_GROUPS.values() for k in grp)),
        )
    return key


def normalize(params: dict | None) -> dict:
    """Validate a {name: value} mapping and return canonical keys."""
    if not params:
        return {}
    if not isinstance(params, dict):
        raise ParamError("params must be a mapping of name -> value",
                         "example: {\"cfg\": 3.5, \"scheduler\": \"karras\"}")
    out: dict = {}
    for raw_key, raw_val in params.items():
        key = canonical(raw_key)
        if raw_val is None and not SPEC[key].get("nullable"):
            # null means "do not touch this knob"
            continue
        out[key] = _coerce(key, raw_val, SPEC[key])
    return out


# --- payload assembly -------------------------------------------------------
def _assign(payload: dict, path: tuple, value) -> None:
    node = payload
    for part in path[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[path[-1]] = value


def apply(payload: dict, resolved: dict) -> dict:
    """Write validated parameters into the submit payload, in place."""
    for key, value in resolved.items():
        spec = SPEC.get(key)
        if not spec:
            continue
        _assign(payload, spec["path"], value)
    return payload


def resolve(explicit: dict | None = None, params: dict | None = None,
            session: dict | None = None) -> dict:
    """Merge the three layers. Later layers win; None values are ignored."""
    out: dict = {}
    for layer in (session, explicit, params):
        if not layer:
            continue
        for key, value in layer.items():
            if value is None:
                continue
            out[key] = value
    return out


# --- session-scoped parameter set ------------------------------------------
_SESSION: dict = {}


def session_get() -> dict:
    return copy.deepcopy(_SESSION)


def session_set(params: dict, merge: bool = True) -> dict:
    """Store parameters for the rest of this process (MCP session / one CLI run)."""
    clean = normalize(params)
    if not merge:
        _SESSION.clear()
    _SESSION.update(clean)
    return session_get()


def session_reset(keys: list[str] | None = None) -> dict:
    if not keys:
        _SESSION.clear()
        return {}
    for k in keys:
        _SESSION.pop(canonical(k), None)
    return session_get()


# --- defaults ---------------------------------------------------------------
#: what the engine reports when it is reachable, keyed by our parameter names
_CAPS_PATHS = {
    "cfg": ("sample_params", "guidance", "txt_cfg"),
    "img_cfg": ("sample_params", "guidance", "img_cfg"),
    "distilled_guidance": ("sample_params", "guidance", "distilled_guidance"),
    "slg_scale": ("sample_params", "guidance", "slg", "scale"),
    "steps": ("sample_params", "sample_steps"),
    "sampler": ("sample_params", "sample_method"),
    "scheduler": ("sample_params", "scheduler"),
    "flow_shift": ("sample_params", "flow_shift"),
    "shifted_timestep": ("sample_params", "shifted_timestep"),
    "eta": ("sample_params", "eta"),
    "clip_skip": ("clip_skip",),
    "strength": ("strength",),
    "seed": ("seed",),
    "count": ("batch_count",),
    "vae_tiling": ("vae_tiling_params", "enabled"),
    "vae_target_overlap": ("vae_tiling_params", "target_overlap"),
    "vae_rel_size_w": ("vae_tiling_params", "rel_size_w"),
    "vae_rel_size_h": ("vae_tiling_params", "rel_size_h"),
    "hires_scale": ("hires", "scale"),
    "hires_upscaler": ("hires", "upscaler"),
    "hires_denoising_strength": ("hires", "denoising_strength"),
    "hires_steps": ("hires", "steps"),
    "hires_target_width": ("hires", "target_width"),
    "hires_target_height": ("hires", "target_height"),
    "output_format": ("output_format",),
    "output_compression": ("output_compression",),
}

#: last-resort values, measured on sd-server master-1050 with a flux-2-klein
#: model loaded (cfg 1.0 / 4 steps / euler). v3.1 shipped 7.0 / 20 here, which
#: is an SD1.5-era pair and made the tool look wrong on every modern engine.
#: The engine's own ``defaults`` block always wins over these; they only matter
#: when it cannot be reached at all.
FALLBACK = {
    "cfg": 1.0, "img_cfg": None, "steps": 4, "sampler": "euler",
    "scheduler": "default", "flow_shift": None, "eta": None,
    "distilled_guidance": 3.5, "slg_scale": 0.0,
    "clip_skip": -1, "strength": 0.75, "seed": 42, "count": 1,
}


def _dig(obj, path):
    node = obj
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def defaults_from_capabilities(caps: dict | None) -> dict:
    """Read live defaults. The engine spells the guidance block 'guided' in
    capabilities while the request body uses 'guidance', so try both.

    v3.3.0 also ships ``defaults_by_mode``: when the engine is running in a
    specific mode (``img_gen``, ``img2img`` ...) that block is the authoritative
    one and the flat ``defaults`` is only the fallback.
    """
    caps = caps or {}
    d = caps.get("defaults") or {}
    mode = caps.get("current_mode")
    by_mode = caps.get("defaults_by_mode") or {}
    if mode and isinstance(by_mode.get(mode), dict):
        merged = dict(d)
        merged.update(by_mode[mode])
        d = merged
    flat: dict = {}
    for key, path in _CAPS_PATHS.items():
        val = _dig(d, path)
        if val is None and "guidance" in path:
            val = _dig(d, tuple("guided" if p == "guidance" else p for p in path))
        if val is not None:
            flat[key] = val
    return flat


def adopt_capabilities(caps: dict | None) -> dict:
    """Replace the offline enumerations / limits with the live engine ones.

    Called by every entry point that has a ``capabilities`` dict in hand. The
    lists are updated **in place** so that SPEC's enum references keep pointing
    at the live data (a rebound list would leave SPEC validating against the
    stale baseline forever, which is exactly the v3.1 upscaler bug).
    Returns a small summary so callers can log / report what was adopted.
    """
    caps = caps or {}
    adopted: dict = {}

    def _sync(label: str, target: list, live) -> None:
        if not isinstance(live, list) or not live:
            return
        vals = []
        for item in live:
            if isinstance(item, dict):
                item = item.get("name")
            if isinstance(item, str) and item and item not in vals:
                vals.append(item)
        if not vals:
            return
        # the engine accepts "default" for both sampler and scheduler (it is
        # the shipped scheduler default) but does not list it among the values,
        # so keep it when the baseline had it instead of dropping a valid word
        if "default" in target and "default" not in vals:
            vals.insert(0, "default")
        if vals != target:
            target[:] = vals
            adopted[label] = len(vals)

    _sync("samplers", SAMPLERS, caps.get("samplers"))
    _sync("schedulers", SCHEDULERS, caps.get("schedulers"))
    _sync("output_formats", OUTPUT_FORMATS, caps.get("output_formats"))
    _sync("preview_modes", PREVIEW_MODES, caps.get("preview_modes"))
    # upscalers arrive as [{"name": ..., "model": bool, "image_upscale": bool}]
    _sync("upscalers", UPSCALERS, caps.get("upscalers"))

    lim = caps.get("limits")
    if isinstance(lim, dict):
        changed = {}
        for key in list(LIMITS):
            val = lim.get(key)
            if isinstance(val, int) and val > 0 and val != LIMITS[key]:
                LIMITS[key] = val
                changed[key] = val
        if changed:
            adopted["limits"] = len(changed)

    # ``features`` carries the coarse switches (hires, vae_tiling, lora ...),
    # ``generation_features`` the per-knob ones. Merge both, coarse first, so a
    # key present in only one of them is still known; only real booleans count,
    # anything else stays unknown instead of being reported as disabled.
    merged: dict = {}
    for block in (caps.get("features"), caps.get("generation_features")):
        if isinstance(block, dict):
            for key, val in block.items():
                if isinstance(val, bool):
                    merged[key] = val
    if merged and merged != FEATURES:
        FEATURES.clear()
        FEATURES.update(merged)
        adopted["features"] = len(merged)

    return adopted


def feature_enabled(feature: str) -> bool | None:
    """True/False when the engine reported the feature, None when unknown."""
    if not feature or feature not in FEATURES:
        return None
    return bool(FEATURES[feature])


#: which engine feature switch has to be on for a parameter to do anything
_KEY_FEATURE = {
    "hires_enabled": "hires", "hires_upscaler": "hires", "hires_scale": "hires",
    "hires_steps": "hires", "hires_target_width": "hires",
    "hires_target_height": "hires", "hires_denoising_strength": "hires",
    "hires_upscale_tile_size": "hires", "hires_custom_sigmas": "hires",
    "vae_tiling": "vae_tiling", "vae_tile_size_w": "vae_tiling",
    "vae_tile_size_h": "vae_tiling", "vae_rel_size_w": "vae_tiling",
    "vae_rel_size_h": "vae_tiling", "vae_target_overlap": "vae_tiling",
    "vae_temporal_tiling": "vae_tiling", "vae_extra_tiling_args": "vae_tiling",
    "lora": "lora", "clip_skip": "clip_skip", "flow_shift": "flow_shift",
    "shifted_timestep": "shifted_timestep", "slg_scale": "slg",
    "slg_layers": "slg", "slg_layer_start": "slg", "slg_layer_end": "slg",
    "img_cfg": "img_cfg", "distilled_guidance": "distilled_guidance",
    "high_noise_steps": "high_noise", "high_noise_cfg": "high_noise",
    "high_noise_sampler": "high_noise",
}


def unsupported_keys(keys) -> list[str]:
    """Which requested knobs the loaded model cannot honour (warn, never drop)."""
    out: list[str] = []
    for key in keys or ():
        feat = _KEY_FEATURE.get(key)
        if feat and feature_enabled(feat) is False:
            out.append(key)
    return out


def effective_defaults(caps: dict | None = None, session: dict | None = None) -> dict:
    """Engine value -> fallback -> session override, with the source recorded."""
    live = defaults_from_capabilities(caps)
    out: dict = {}
    for key in SPEC:
        if key in live:
            out[key] = {"value": live[key], "source": "engine"}
        elif key in FALLBACK and FALLBACK[key] is not None:
            out[key] = {"value": FALLBACK[key], "source": "tool"}
        elif "default" in SPEC[key]:
            out[key] = {"value": SPEC[key]["default"], "source": "tool"}
        else:
            out[key] = {"value": None, "source": "unset"}
    for key, val in (session or {}).items():
        out[key] = {"value": val, "source": "session"}
    return out


def table(caps: dict | None = None, session: dict | None = None,
          include_folded: bool = False) -> list[dict]:
    """Rows for the CLI/MCP parameter listing."""
    eff = effective_defaults(caps, session)
    rows = []
    for key, spec in SPEC.items():
        if not spec.get("core") and not include_folded:
            continue
        row = {
            "param": key,
            "default": eff[key]["value"],
            "source": eff[key]["source"],
            "kind": spec["kind"],
            "range": _range_text(spec),
            "tier": "core" if spec.get("core") else spec.get("group"),
            "payload_path": ".".join(spec["path"]),
            "label": spec.get("label", ""),
        }
        if spec.get("note"):
            row["note"] = spec["note"]
        if spec.get("enum"):
            row["options"] = list(spec["enum"])
        feat = _KEY_FEATURE.get(key)
        if feat:
            avail = feature_enabled(feat)
            if avail is False:
                row["available"] = False
                row["unavailable_reason"] = (
                    f"the engine reports {feat}=false for the loaded model, "
                    "so this knob will be accepted but has no effect")
        rows.append(row)
    return rows


def engine_limits() -> dict:
    """Live size / batch limits (from capabilities.limits when reachable)."""
    return dict(LIMITS)


def _range_text(spec: dict) -> str:
    if spec.get("enum"):
        return f"{len(spec['enum'])} options"
    lo, hi = spec.get("min"), spec.get("max")
    if lo is None and hi is None:
        return ""
    return f"{lo} .. {hi}"


def spec_of(key: str) -> dict:
    return SPEC[canonical(key)]
