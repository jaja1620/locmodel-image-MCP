# LocModel —— 唱歌 / 说话对口型视频（InfiniteTalk）
#
# 由 LocModel 自动写到「程序数据目录/lipsync/runner.py」，用 LocModel 准备的独立 Python 环境运行：
#     python runner.py job.json
# 进度按行输出：@@LM {"stage": ..., "pct": ..., "msg": ...}，最后一行 @@LM {"done": "输出文件"}
#
# 用的是 MeiGen-AI/InfiniteTalk 官方代码（Apache-2.0）里的模型结构（WanModel / T5 / CLIP / VAE / wav2vec2），
# 生成流程按官方 generate_infinitetalk 改写（单人），区别：
#   - 主模型用 Wan2.1-I2V-14B-480P 的 fp8 权重（17 GB）+ InfiniteTalk 音频模块，不用 65 GB 的 fp32 原版；
#     加速 LoRA（lightx2v）在推理时叠加，不合并进 fp8 权重（不损失精度）
#   - 显存不够放下整个模型时，一部分层留在内存里，用到时再搬到显卡（不需要 flash-attn / xformers / xfuser）
#   - 生成的画面边生成边写进视频文件，长歌不会把内存占满；支持自定义宽高和裁剪
#   - 先用 Demucs（MIT）把人声从歌曲里分离出来驱动口型，成品视频里放原曲
import json
import math
import os
import subprocess
import sys
import time
import types
import traceback

JOB = None


def emit(**kw):
    sys.stdout.write("@@LM " + json.dumps(kw, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def stage(name, msg, pct=None):
    emit(stage=name, msg=msg, pct=pct)


PERF_FILE = None


def perf(msg):
    """速度、显存的诊断信息：边生成边写进 lipsync/perf.log（生成中也能看），也打到日志里"""
    line = f"[perf {time.strftime('%H:%M:%S')}] {msg}"
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    if PERF_FILE:
        try:
            with open(PERF_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


class UserError(Exception):
    """给用户看的错误（不带堆栈）"""


# ------------------------------------------------------------------ 环境准备：不需要的重依赖用替身


def stub_module(name, path=None):
    """造一个替身模块放进 sys.modules。必须带 __spec__：transformers / diffusers 会用
    importlib.util.find_spec 检查这些包装没装，__spec__ 是 None 会直接报 ValueError"""
    import importlib.machinery

    m = types.ModuleType(name)
    spec = importlib.machinery.ModuleSpec(name, None, is_package=path is not None)
    if path is not None:
        spec.submodule_search_locations = list(path)
        m.__path__ = list(path)
    m.__spec__ = spec
    sys.modules[name] = m
    return m


def install_stubs(code_dir, use_sage):
    import torch
    import torch.nn.functional as F

    sys.path.insert(0, code_dir)

    # xfuser：只有多卡并行才用到
    for name in ("xfuser", "xfuser.core"):
        if name not in sys.modules:
            stub_module(name, [])
    dist = stub_module("xfuser.core.distributed")
    dist.get_sequence_parallel_rank = lambda: 0
    dist.get_sequence_parallel_world_size = lambda: 1
    dist.get_sp_group = lambda: None
    dist.init_distributed_environment = lambda **k: None
    dist.initialize_model_parallel = lambda **k: None
    sys.modules["xfuser.core.distributed"] = dist

    # xformers：音频交叉注意力用的，换成 PyTorch 自带的 scaled_dot_product_attention（布局 B M H K）
    try:
        import xformers.ops  # noqa: F401
    except Exception:
        xf = stub_module("xformers", [])
        ops = stub_module("xformers.ops")

        def memory_efficient_attention(q, k, v, attn_bias=None, op=None, **kw):
            if attn_bias is not None:
                raise RuntimeError("attn_bias not supported")
            o = F.scaled_dot_product_attention(q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2))
            return o.transpose(1, 2)

        ops.memory_efficient_attention = memory_efficient_attention
        ops.fmha = types.SimpleNamespace(attn_bias=types.SimpleNamespace(BlockDiagonalMask=None))
        xf.ops = ops
        sys.modules["xformers"] = xf
        sys.modules["xformers.ops"] = ops

    if not use_sage:
        sys.modules["sageattention"] = None  # import 时报错 → 官方代码自动不用 sage

    # 官方包的 __init__ 会把文生视频、VACE 等用不到的模块全导入一遍（还要 decord、dashscope），只建空包
    for pkg in ("wan", "wan.modules", "wan.utils", "src", "src.audio_analysis"):
        stub_module(pkg, [os.path.join(code_dir, *pkg.split("."))])

    import wan.modules.attention as att

    def flash_attention(q, k, v, q_lens=None, k_lens=None, dropout_p=0.0, softmax_scale=None, q_scale=None,
                        causal=False, window_size=(-1, -1), deterministic=False, dtype=torch.bfloat16, version=None):
        out_dtype = q.dtype
        half = (torch.float16, torch.bfloat16)
        cdt = q.dtype if q.dtype in half else dtype
        if q_scale is not None:
            q = q * q_scale
        outs = []
        for b in range(q.size(0)):
            lq = int(q_lens[b]) if q_lens is not None else q.size(1)
            lk = int(k_lens[b]) if k_lens is not None else k.size(1)
            qb = q[b:b + 1, :lq].transpose(1, 2).to(cdt)
            kb = k[b:b + 1, :lk].transpose(1, 2).to(cdt)
            vb = v[b:b + 1, :lk].transpose(1, 2).to(cdt)
            o = F.scaled_dot_product_attention(qb, kb, vb, is_causal=causal, scale=softmax_scale).transpose(1, 2)
            if lq < q.size(1):
                o = torch.cat([o, o.new_zeros(1, q.size(1) - lq, *o.shape[2:])], dim=1)
            outs.append(o)
        return torch.cat(outs).type(out_dtype)

    att.flash_attention = flash_attention
    att.FLASH_ATTN_2_AVAILABLE = False
    att.FLASH_ATTN_3_AVAILABLE = False

    import wan.modules.multitalk_model as mm

    # 单人时官方代码每一层都会算一张「画面 ↔ 参考图」的注意力图，只有两个人对唱时才用到；
    # 720P 时这张图要占十几 GB 显存，单人直接跳过
    mm.get_attn_map_with_target = lambda *a, **k: None
    install_lowmem(mm)
    return mm


CHUNK = 8192  # 按 token 分块算的块大小（128 的倍数：sage-attn 按 128 个 token 一组量化，分块不改变结果）


def install_lowmem(mm):
    """省显存：逐 token 的计算（归一化、调制、前馈层、文本交叉注意力、位置编码）分块算，
    结果和官方写法一样，只是不再一次性生成好几份整段的大张量（720P 时能省好几 GB 显存）"""
    import torch

    amp = torch.cuda.amp

    def rope_apply(x, grid_sizes, freqs):
        s, n, c = x.size(1), x.size(2), x.size(3) // 2
        fr = freqs.split([c - 2 * (c // 3), c // 3, c // 3], dim=1)
        out = torch.empty(x.shape, dtype=torch.bfloat16, device=x.device)
        for i, (f, h, w) in enumerate(grid_sizes.tolist()):
            seq_len = f * h * w
            freqs_i = torch.cat([
                fr[0][:f].view(f, 1, 1, -1).expand(f, h, w, -1),
                fr[1][:h].view(1, h, 1, -1).expand(f, h, w, -1),
                fr[2][:w].view(1, 1, w, -1).expand(f, h, w, -1),
            ], dim=-1).reshape(seq_len, 1, -1).to(device=x.device)
            for a in range(0, seq_len, CHUNK):
                b = min(seq_len, a + CHUNK)
                xi = torch.view_as_complex(x[i, a:b].to(torch.float64).reshape(b - a, n, -1, 2))
                out[i, a:b] = torch.view_as_real(xi * freqs_i[a:b]).flatten(2).float().to(out.dtype)
            if seq_len < s:
                out[i, seq_len:] = x[i, seq_len:].float().to(out.dtype)
        return out

    def block_forward(self, x, e, seq_lens, grid_sizes, freqs, context, context_lens,
                      audio_embedding=None, ref_target_masks=None, human_num=None):
        dtype = x.dtype
        L = x.size(1)
        parts = [(a, min(L, a + CHUNK)) for a in range(0, L, CHUNK)]
        with amp.autocast(dtype=torch.float32):
            e = (self.modulation.to(e.device) + e).chunk(6, dim=1)
        # 自注意力
        inp = torch.empty_like(x)
        for a, b in parts:
            inp[:, a:b] = (self.norm1(x[:, a:b]).float() * (1 + e[1]) + e[0]).type_as(x)
        y, x_ref_attn_map = self.self_attn(inp, seq_lens, grid_sizes, freqs, ref_target_masks=ref_target_masks)
        del inp
        for a, b in parts:
            with amp.autocast(dtype=torch.float32):
                x[:, a:b] = (x[:, a:b] + y[:, a:b] * e[2]).to(dtype)
        del y
        # 文本 / 参考图交叉注意力（每个 token 各算各的）
        for a, b in parts:
            x[:, a:b] = x[:, a:b] + self.cross_attn(self.norm3(x[:, a:b]), context, context_lens)
        # 声音交叉注意力（按帧算，要整段）
        x_a = self.audio_cross_attn(self.norm_x(x), encoder_hidden_states=audio_embedding,
                                    shape=grid_sizes[0], x_ref_attn_map=x_ref_attn_map, human_num=human_num)
        x += x_a
        del x_a
        # 前馈层
        for a, b in parts:
            xc = x[:, a:b]
            yc = self.ffn((self.norm2(xc).float() * (1 + e[4]) + e[3]).to(dtype))
            with amp.autocast(dtype=torch.float32):
                x[:, a:b] = (xc + yc * e[5]).to(dtype)
        return x

    mm.rope_apply = rope_apply
    mm.WanAttentionBlock.forward = block_forward


# ------------------------------------------------------------------ 小工具


def ffmpeg_exe():
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def run_ffmpeg(args, what):
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"] + args
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    if r.returncode != 0:
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise UserError(f"{what}失败：{' '.join(tail) or '退出码 ' + str(r.returncode)}")


def round16(x):
    return max(16, int(round(x / 16.0)) * 16)


def gpu_free():
    import torch

    free, total = torch.cuda.mem_get_info()
    return free, total


def gc_cuda():
    import gc

    import torch

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


# ------------------------------------------------------------------ 画面尺寸和裁剪


BUCKET_480 = {
    0.26: (320, 1216), 0.38: (384, 1024), 0.50: (448, 896), 0.67: (512, 768), 0.82: (576, 704), 1.00: (640, 640),
    1.22: (704, 576), 1.50: (768, 512), 1.86: (832, 448), 2.00: (896, 448), 2.50: (960, 384), 2.83: (1088, 384),
    3.60: (1152, 320), 3.80: (1216, 320), 4.00: (1280, 320),
}
BUCKET_720 = {
    0.22: (448, 2048), 0.29: (512, 1792), 0.36: (576, 1600), 0.45: (640, 1408), 0.55: (704, 1280), 0.63: (768, 1216),
    0.76: (832, 1088), 0.88: (896, 1024), 1.00: (960, 960), 1.14: (1024, 896), 1.31: (1088, 832), 1.50: (1152, 768),
    1.58: (1216, 768), 1.82: (1280, 704), 1.91: (1344, 704), 2.20: (1408, 640), 2.30: (1472, 640), 2.67: (1536, 576),
    2.89: (1664, 576), 3.62: (1856, 512), 3.75: (1920, 512),
}


def target_size(job, src_w, src_h):
    """返回 (宽, 高, 裁剪框)：裁剪框是原图上的像素区域 (x, y, w, h)，比例和输出一致"""
    crop = job.get("crop") or None
    if crop:
        cx, cy, cw, ch = (float(crop[k]) for k in ("x", "y", "w", "h"))
        cx, cy = max(0.0, min(cx, 1.0)), max(0.0, min(cy, 1.0))
        cw, ch = max(0.02, min(cw, 1.0 - cx)), max(0.02, min(ch, 1.0 - cy))
        box = (cx * src_w, cy * src_h, cw * src_w, ch * src_h)
    else:
        box = (0.0, 0.0, float(src_w), float(src_h))
    res = job.get("res", "480")
    if res == "custom":
        w, h = round16(int(job.get("width") or 832)), round16(int(job.get("height") or 480))
        w, h = min(max(w, 256), 1920), min(max(h, 256), 1920)
    else:
        table = BUCKET_720 if res == "720" else BUCKET_480
        ratio = box[3] / box[2]
        key = min(table, key=lambda r: abs(r - ratio))
        h, w = table[key]
    # 裁剪框按输出比例再居中收一下（和官方的 resize_and_centercrop 一样）
    bx, by, bw, bh = box
    want = w / h
    if bw / bh > want:
        nw = bh * want
        bx += (bw - nw) / 2
        bw = nw
    else:
        nh = bw / want
        by += (bh - nh) / 2
        bh = nh
    return w, h, (int(round(bx)), int(round(by)), max(2, int(round(bw))), max(2, int(round(bh))))


# ------------------------------------------------------------------ 第 1 步：声音


def prepare_audio(job, work):
    """原曲转成 wav；需要时分离出人声。返回 (原曲 wav, 驱动口型用的人声 wav, 时长秒)"""
    import soundfile as sf

    song = os.path.join(work, "song.wav")
    stage("audio", "读取音频…")
    run_ffmpeg(["-i", job["audio"], "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", song], "读取音频")
    info = sf.info(song)
    dur = info.frames / float(info.samplerate)
    if dur < 1.0:
        raise UserError("音频太短（不到 1 秒）")
    limit = float(job.get("max_seconds") or 0)
    if limit and dur > limit + 0.01:
        cut = os.path.join(work, "song_cut.wav")
        run_ffmpeg(["-i", song, "-t", f"{limit:.3f}", "-c:a", "pcm_s16le", cut], "截取音频")
        song, dur = cut, limit
    if job.get("vocals", "auto") != "auto":
        return song, song, dur
    vocals = os.path.join(work, "vocals.wav")
    separate_vocals(job, song, vocals)
    return song, vocals, dur


def demucs_stubs():
    """demucs 要 dora（训练框架）里的两个打印函数，不用装整个 dora"""
    if "dora.log" in sys.modules:
        return
    dora = stub_module("dora", [])
    dlog = stub_module("dora.log")
    dlog.fatal = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(" ".join(map(str, a))))
    dlog.bold = lambda s: s
    dora.log = dlog


def separate_vocals(job, song, out):
    import numpy as np
    import soundfile as sf
    import torch

    stage("separate", "分离人声（去掉伴奏，口型更准）…", 0)
    demucs_stubs()
    from demucs.apply import apply_model
    from demucs.states import load_model

    pkg = torch.load(job["models"]["demucs"], map_location="cpu", weights_only=False)
    model = load_model(pkg)
    model.eval()
    data, sr = sf.read(song, dtype="float32", always_2d=True)
    wav = torch.from_numpy(data.T.copy())
    if sr != model.samplerate:
        import julius

        wav = julius.resample_frac(wav, sr, model.samplerate)
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    with torch.no_grad():
        src = apply_model(model, ((wav - mean) / std)[None], device=dev, shifts=1, split=True, overlap=0.25, progress=False)[0]
    vocals = src[model.sources.index("vocals")] * std + mean
    sf.write(out, vocals.T.numpy().astype(np.float32), model.samplerate)
    del model, src
    gc_cuda()
    stage("separate", "人声分离完成", 100)


def audio_embedding(job, vocals, dev):
    """wav2vec2 提取口型特征：每秒 25 帧（和视频帧率一样）"""
    import numpy as np
    import pyloudnorm as pyln
    import soundfile as sf
    import torch
    from einops import rearrange
    from transformers import Wav2Vec2FeatureExtractor

    from src.audio_analysis.wav2vec2 import Wav2Vec2Model

    stage("embed", "分析声音…")
    mono = os.path.join(os.path.dirname(vocals), "voice16k.wav")
    run_ffmpeg(["-i", vocals, "-ac", "1", "-ar", "16000", "-c:a", "pcm_f32le", mono], "处理人声")
    speech, _ = sf.read(mono, dtype="float32")
    meter = pyln.Meter(16000)
    loud = meter.integrated_loudness(speech)
    if abs(loud) <= 100:
        speech = pyln.normalize.loudness(speech, loud, -23)
    wdir = job["models"]["wav2vec"]
    fe = Wav2Vec2FeatureExtractor.from_pretrained(wdir, local_files_only=True)
    enc = Wav2Vec2Model.from_pretrained(wdir, local_files_only=True).to(dev)
    enc.feature_extractor._freeze_parameters()
    frames = int(len(speech) / 16000 * 25)
    feat = np.squeeze(fe(speech, sampling_rate=16000).input_values)
    feat = torch.from_numpy(feat).float().to(dev)[None]
    try:
        with torch.no_grad():
            out = enc(feat, seq_len=frames, output_hidden_states=True)
    except torch.cuda.OutOfMemoryError:
        enc, feat = enc.to("cpu"), feat.to("cpu")
        gc_cuda()
        with torch.no_grad():
            out = enc(feat, seq_len=frames, output_hidden_states=True)
    emb = torch.stack(out.hidden_states[1:], dim=1).squeeze(0)
    emb = rearrange(emb, "b s d -> s b d").cpu()
    del enc, out, feat
    gc_cuda()
    return emb


# ------------------------------------------------------------------ 第 2 步：画面


def load_frames(job, work, w, h, box, starts):
    """返回每一段开头用的参考画面（uint8，H W 3）。图片：同一张；视频：对应时间的那一帧（视频比歌短就来回播放）"""
    import numpy as np
    from PIL import Image, ImageOps

    src = job["image"]
    ext = os.path.splitext(src)[1].lower()
    is_video = ext in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg")
    if not is_video:
        img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
        x, y, bw, bh = box
        img = img.crop((x, y, x + bw, y + bh)).resize((w, h), Image.LANCZOS)
        img.save(os.path.join(work, "ref.png"))
        a = np.asarray(img)
        return [a for _ in starts], False
    import cv2

    stage("frames", "读取视频画面…")
    prep = os.path.join(work, "ref.mp4")
    x, y, bw, bh = box
    bw, bh = bw // 2 * 2, bh // 2 * 2
    run_ffmpeg(["-i", src, "-an", "-vf", f"crop={bw}:{bh}:{x}:{y},scale={w}:{h}:flags=lanczos,fps=25",
                "-c:v", "libx264", "-crf", "14", "-preset", "veryfast", "-pix_fmt", "yuv420p", prep], "处理视频")
    cap = cv2.VideoCapture(prep)
    allf = []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        allf.append(fr)
    cap.release()
    if not allf:
        raise UserError("读不出视频画面，换一个视频试试（建议 mp4）")
    n = len(allf)

    def pick(i):
        if n == 1:
            return allf[0]
        period = 2 * (n - 1)
        k = i % period
        return allf[k if k < n else period - k]

    return [cv2.cvtColor(pick(s), cv2.COLOR_BGR2RGB) for s in starts], True


def size_of(job, work):
    from PIL import Image, ImageOps

    src = job["image"]
    ext = os.path.splitext(src)[1].lower()
    if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg"):
        import cv2

        cap = cv2.VideoCapture(src)
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        if w <= 0 or h <= 0:
            raise UserError("读不出视频尺寸，换一个视频试试（建议 mp4）")
        return w, h
    img = ImageOps.exif_transpose(Image.open(src))
    return img.width, img.height


# ------------------------------------------------------------------ 第 3 步：主模型（fp8 + 推理时叠加 LoRA + 显存不够时分层搬运）


def dequantize_quanto(model, dtype):
    """把 optimum-quanto 的量化层换回普通的 nn.Linear（权重还原成 dtype），之后就完全不用 quanto 了"""
    import torch

    try:
        from optimum.quanto.nn import QModuleMixin
    except Exception:
        return model
    targets = [(n, m) for n, m in model.named_modules() if isinstance(m, QModuleMixin)]
    for name, m in targets:
        w = m.weight
        w = w.dequantize() if hasattr(w, "dequantize") else w
        if not isinstance(m, torch.nn.Linear):
            raise RuntimeError(f"不支持的量化层：{type(m).__name__}")
        lin = torch.nn.Linear(m.in_features, m.out_features, bias=m.bias is not None, device="cpu", dtype=dtype)
        with torch.no_grad():
            lin.weight.copy_(w.to("cpu", dtype))
            if m.bias is not None:
                b = m.bias.dequantize() if hasattr(m.bias, "dequantize") else m.bias
                lin.bias.copy_(b.to("cpu", dtype))
        lin.requires_grad_(False)
        parent_name, _, attr = name.rpartition(".")
        parent = model.get_submodule(parent_name) if parent_name else model
        setattr(parent, attr, lin)
    return model


def quanto_to_fp8(model, dtype):
    """optimum-quanto 的 fp8 层换成普通 Linear，但权重保持 fp8（约 6.7 GB，还原成 bf16 要 11 GB），
    算的时候由 patch_linear 现场转成 bf16 再乘缩放（和 quanto 自己还原的算法一样）。不是 fp8 的层照旧还原"""
    import torch

    try:
        from optimum.quanto.nn import QModuleMixin
    except Exception:
        return model
    fp8 = {getattr(torch, n) for n in ("float8_e4m3fn", "float8_e5m2") if hasattr(torch, n)}
    targets = [(n, m) for n, m in model.named_modules() if isinstance(m, QModuleMixin)]
    for name, m in targets:
        if not isinstance(m, torch.nn.Linear):
            raise RuntimeError(f"不支持的量化层：{type(m).__name__}")
        w = m.weight
        data, scale = getattr(w, "_data", None), getattr(w, "_scale", None)
        with torch.device("meta"):
            lin = torch.nn.Linear(m.in_features, m.out_features, bias=m.bias is not None)
        if data is not None and scale is not None and data.dtype in fp8 and scale.numel() == m.out_features:
            lin.weight = torch.nn.Parameter(data.detach().to("cpu"), requires_grad=False)
            lin.register_buffer("scale_weight", scale.detach().to("cpu").reshape(m.out_features, 1), persistent=False)
        else:
            d = w.dequantize() if hasattr(w, "dequantize") else w
            lin.weight = torch.nn.Parameter(d.detach().to("cpu", dtype), requires_grad=False)
        if m.bias is not None:
            b = m.bias.dequantize() if hasattr(m.bias, "dequantize") else m.bias
            lin.bias = torch.nn.Parameter(b.detach().to("cpu", dtype), requires_grad=False)
        patch_linear(lin)
        parent_name, _, attr = name.rpartition(".")
        parent = model.get_submodule(parent_name) if parent_name else model
        setattr(parent, attr, lin)
    return model


# ------------------------------------------------------------------ GGUF 量化模型（省内存版）
# 反量化的写法来自 city96/ComfyUI-GGUF 的 dequant.py（Apache-2.0）

GGUF_READERS = []  # 读出来的张量直接映射文件（不占内存），读取器要一直留着


def gguf_tensors(path):
    """读 GGUF：返回 {名字: (数据, 量化类型或 None, 形状)}；F32 / F16 直接是普通张量，其他是原始字节"""
    import warnings

    import gguf
    import torch

    r = gguf.GGUFReader(path)
    GGUF_READERS.append(r)
    T = gguf.GGMLQuantizationType
    out = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # 映射的文件是只读的，PyTorch 会提醒一句
        for t in r.tensors:
            shape = None
            f = r.get_field(f"comfy.gguf.orig_shape.{t.name}")
            if f is not None:
                shape = tuple(int(f.parts[i][0]) for i in f.data)
            if shape is None:
                shape = tuple(int(v) for v in reversed(t.shape))
            data = torch.from_numpy(t.data)
            if t.tensor_type in (T.F32, T.F16):
                out[t.name] = (data.reshape(shape), None, shape)
            else:
                out[t.name] = (data, int(t.tensor_type), shape)
    return out


def _gg_split(blocks, *args):
    import torch

    n = blocks.shape[1]
    return torch.split(blocks, list(args) + [n - sum(args)], dim=1)


def _gg_u32(x):
    import torch

    x = x.view(torch.uint8).to(torch.int32)
    return (x[:, 0] | x[:, 1] << 8 | x[:, 2] << 16 | x[:, 3] << 24).unsqueeze(1)


def _gg_scale_min(scales):
    import torch

    n = scales.shape[0]
    scales = scales.view(torch.uint8).reshape((n, 3, 4))
    d, m, m_d = torch.split(scales, 1, dim=-2)
    sc = torch.cat([d & 0x3F, (m_d & 0x0F) | ((d >> 2) & 0x30)], dim=-1)
    mn = torch.cat([m & 0x3F, (m_d >> 4) | ((m >> 2) & 0x30)], dim=-1)
    return sc.reshape((n, 8)), mn.reshape((n, 8))


def _gg_shifts(vals, dev, shape):
    import torch

    return torch.tensor(vals, device=dev, dtype=torch.uint8).reshape(shape)


def gguf_dequant_blocks(qt, blocks, bs, dtype):
    import torch

    n = blocks.shape[0]
    dev = blocks.device
    QK = 256
    if qt == 30:  # BF16
        return (blocks.view(torch.int16).to(torch.int32) << 16).view(torch.float32).to(dtype)
    if qt == 8:  # Q8_0
        d, x = _gg_split(blocks, 2)
        return d.view(torch.float16).to(dtype) * x.view(torch.int8)
    if qt in (2, 3):  # Q4_0 / Q4_1
        if qt == 2:
            d, qs = _gg_split(blocks, 2)
            m = None
        else:
            d, m, qs = _gg_split(blocks, 2, 2)
            m = m.view(torch.float16).to(dtype)
        d = d.view(torch.float16).to(dtype)
        qs = qs.reshape((n, -1, 1, bs // 2)) >> _gg_shifts([0, 4], dev, (1, 1, 2, 1))
        qs = (qs & 0x0F).reshape((n, -1))
        return d * (qs.to(torch.int8) - 8) if m is None else d * qs + m
    if qt in (6, 7):  # Q5_0 / Q5_1
        if qt == 6:
            d, qh, qs = _gg_split(blocks, 2, 4)
            m = None
        else:
            d, m, qh, qs = _gg_split(blocks, 2, 2, 4)
            m = m.view(torch.float16).to(dtype)
        d = d.view(torch.float16).to(dtype)
        qh = _gg_u32(qh).reshape(n, 1) >> torch.arange(32, device=dev, dtype=torch.int32).reshape(1, 32)
        ql = qs.reshape(n, -1, 1, bs // 2) >> _gg_shifts([0, 4], dev, (1, 1, 2, 1))
        qh = (qh & 1).to(torch.uint8)
        ql = (ql & 0x0F).reshape(n, -1)
        q = ql | (qh << 4)
        return d * (q.to(torch.int8) - 16) if m is None else d * q + m
    if qt == 14:  # Q6_K
        ql, qh, scales, d = _gg_split(blocks, QK // 2, QK // 4, QK // 16)
        scales = scales.view(torch.int8).to(dtype)
        d = (d.view(torch.float16).to(dtype) * scales).reshape((n, QK // 16, 1))
        ql = ql.reshape((n, -1, 1, 64)) >> _gg_shifts([0, 4], dev, (1, 1, 2, 1))
        ql = (ql & 0x0F).reshape((n, -1, 32))
        qh = qh.reshape((n, -1, 1, 32)) >> _gg_shifts([0, 2, 4, 6], dev, (1, 1, 4, 1))
        qh = (qh & 0x03).reshape((n, -1, 32))
        q = ((ql | (qh << 4)).to(torch.int8) - 32).reshape((n, QK // 16, -1))
        return (d * q).reshape((n, QK))
    if qt in (12, 13):  # Q4_K / Q5_K
        if qt == 12:
            d, dmin, scales, qs = _gg_split(blocks, 2, 2, 12)
            qh = None
        else:
            d, dmin, scales, qh, qs = _gg_split(blocks, 2, 2, 12, QK // 8)
        d = d.view(torch.float16).to(dtype)
        dmin = dmin.view(torch.float16).to(dtype)
        sc, m = _gg_scale_min(scales)
        d = (d * sc).reshape((n, -1, 1))
        dm = (dmin * m).reshape((n, -1, 1))
        ql = qs.reshape((n, -1, 1, 32)) >> _gg_shifts([0, 4], dev, (1, 1, 2, 1))
        q = (ql & 0x0F).reshape((n, -1, 32))
        if qh is not None:
            h = qh.reshape((n, -1, 1, 32)) >> _gg_shifts(list(range(8)), dev, (1, 1, 8, 1))
            q = q | ((h & 0x01).reshape((n, -1, 32)) << 4)
        return (d * q - dm).reshape((n, QK))
    if qt == 11:  # Q3_K
        hmask, qs, scales, d = _gg_split(blocks, QK // 8, QK // 4, 12)
        d = d.view(torch.float16).to(dtype)
        ls, hs = scales[:, :8], scales[:, 8:]
        ls = (ls.reshape((n, 1, 8)) >> _gg_shifts([0, 4], dev, (1, 2, 1))).reshape((n, 16))
        hs = (hs.reshape((n, 1, 4)) >> _gg_shifts([0, 2, 4, 6], dev, (1, 4, 1))).reshape((n, 16))
        sc = ((ls & 0x0F) | ((hs & 0x03) << 4)).to(torch.int8) - 32
        dl = (d * sc).reshape((n, 16, 1))
        ql = qs.reshape((n, -1, 1, 32)) >> _gg_shifts([0, 2, 4, 6], dev, (1, 1, 4, 1))
        qh = hmask.reshape(n, -1, 1, 32) >> _gg_shifts(list(range(8)), dev, (1, 1, 8, 1))
        ql = ql.reshape((n, 16, QK // 16)) & 3
        qh = (qh.reshape((n, 16, QK // 16)) & 1) ^ 1
        q = ql.to(torch.int8) - (qh << 2).to(torch.int8)
        return (dl * q).reshape((n, QK))
    if qt == 10:  # Q2_K
        scales, qs, d, dmin = _gg_split(blocks, QK // 16, QK // 4, 2)
        d = d.view(torch.float16).to(dtype)
        dmin = dmin.view(torch.float16).to(dtype)
        dl = (d * (scales & 0xF)).reshape((n, QK // 16, 1))
        ml = (dmin * (scales >> 4)).reshape((n, QK // 16, 1))
        q = (qs.reshape((n, -1, 1, 32)) >> _gg_shifts([0, 2, 4, 6], dev, (1, 1, 4, 1))) & 3
        q = q.reshape((n, QK // 16, 16))
        return (dl * q - ml).reshape((n, -1))
    raise UserError(f"不支持的 GGUF 量化类型（{qt}），请换一个模型文件")


# (每块的元素数, 每块的字节数)
GGUF_SIZES = {30: (1, 2), 8: (32, 34), 2: (32, 18), 3: (32, 20), 6: (32, 22), 7: (32, 24),
              14: (256, 210), 12: (256, 144), 13: (256, 176), 11: (256, 110), 10: (256, 84)}


def gguf_dequant(raw, qt, shape, dtype):
    import torch

    if qt not in GGUF_SIZES:
        raise UserError(f"不支持的 GGUF 量化类型（{qt}），请换一个模型文件")
    bs, ts = GGUF_SIZES[qt]
    rows = raw.reshape(-1).view(torch.uint8)
    blocks = rows.reshape((rows.numel() // ts, ts))
    return gguf_dequant_blocks(qt, blocks, bs, dtype).reshape(shape)


def strip_key(k):
    for p in ("model.diffusion_model.", "diffusion_model.", "model."):
        if k.startswith(p):
            return k[len(p):]
    return k


def patch_linear(m):
    """让 Linear 能用 fp8 / GGUF 量化权重（算的时候现场转成 bf16），并支持推理时叠加 LoRA"""
    import torch
    import torch.nn.functional as F

    def forward(self, x):
        w = self.weight
        gq = getattr(self, "gguf_q", None)
        if gq is not None:
            w = gguf_dequant(w, gq[0], gq[1], torch.float32).to(x.dtype)  # 先按 fp32 还原再转，精度和离线还原一样
        elif w.dtype != x.dtype:
            w = w.to(x.dtype)
        sw = getattr(self, "scale_weight", None)
        if sw is not None:
            w = w * sw.to(x.dtype)
        b = self.bias
        if b is not None and b.dtype != x.dtype:
            b = b.to(x.dtype)
        out = F.linear(x, w, b)
        ld = getattr(self, "lora_down", None)
        if ld is not None:
            out = out + F.linear(F.linear(x, ld.to(x.dtype)), self.lora_up.to(x.dtype)) * self.lora_scale
        return out

    m.forward = types.MethodType(forward, m)


class Swapper:
    """前 n 层常驻显卡，其余层平时放内存（尽量锁页，搬得快），算到它时搬上显卡、算完放回"""

    def __init__(self, model, dev):
        import torch

        self.model = model
        self.dev = dev
        self.n = len(model.blocks)
        self.pinned = True
        # 锁页内存搬得快，但锁住的内存别的程序就用不了：只在空闲内存充足时锁（留 6 GB 给系统）
        try:
            import psutil

            self.pin_left = psutil.virtual_memory().available - 6 * 2**30
        except Exception:
            self.pin_left = 1 << 62
        self.slots = []  # 每层：[(模块, 名字, 是否参数, 内存里的那份)]
        for i, blk in enumerate(model.blocks):
            items = []
            for mod in blk.modules():
                for name, p in list(mod._parameters.items()):
                    if p is not None:
                        items.append((mod, name, True, None))
                for name, b in list(mod._buffers.items()):
                    if b is not None:
                        items.append((mod, name, False, None))
            self.slots.append(items)
            blk.register_forward_pre_hook(self._pre(i))
            blk.register_forward_hook(self._post(i))
        self.torch = torch

    def block_bytes(self, i):
        tot = 0
        for mod, name, is_p, _ in self.slots[i]:
            t = mod._parameters[name] if is_p else mod._buffers[name]
            tot += t.numel() * t.element_size()
        return tot

    def _get(self, mod, name, is_p):
        return mod._parameters[name] if is_p else mod._buffers[name]

    def _set(self, mod, name, is_p, t):
        if is_p:
            mod._parameters[name] = self.torch.nn.Parameter(t, requires_grad=False)
        else:
            mod._buffers[name] = t

    def place(self, n):
        """前 n 层放显卡，其余放内存"""
        torch = self.torch
        n = max(0, min(n, len(self.slots)))
        for i, items in enumerate(self.slots):
            new = []
            for mod, name, is_p, cpu in items:
                cur = self._get(mod, name, is_p)
                if i < n:
                    if cur.device.type != "cuda":
                        self._set(mod, name, is_p, (cpu if cpu is not None else cur).to(self.dev))
                    # 内存里那份（锁页）留着：之后要给 VAE 腾显存时直接用，不用再拷一遍
                    new.append((mod, name, is_p, cpu))
                else:
                    if cpu is None:
                        cpu = cur.detach().to("cpu")
                        nb = cpu.numel() * cpu.element_size()
                        if self.pin_left > nb:
                            try:
                                cpu = cpu.pin_memory()
                                self.pin_left -= nb
                            except Exception:
                                self.pinned = False
                        else:
                            self.pinned = False
                    self._set(mod, name, is_p, cpu)
                    new.append((mod, name, is_p, cpu))
            self.slots[i] = new
            if i >= n:
                gc_cuda() if i % 8 == 0 else None
        self.n = n
        gc_cuda()

    def _pre(self, i):
        def hook(mod, args):
            if i >= self.n:
                for m, name, is_p, cpu in self.slots[i]:
                    self._set(m, name, is_p, cpu.to(self.dev, non_blocking=True))
        return hook

    def _post(self, i):
        def hook(mod, args, out):
            if i >= self.n:
                for m, name, is_p, cpu in self.slots[i]:
                    self._set(m, name, is_p, cpu)
        return hook


VAE_TILE_PX = 256  # 分块算 VAE 时每块的画面边长（像素），相邻块重叠 1/4 再渐变拼接


def vae_tiled(fn, x, tile, ov, scale, out_dev):
    """VAE 分块算：x 是 [C, T, H, W]，按空间切成重叠的小块分别算，再用渐变权重拼回去（显存只要整张的几分之一）。
    scale = 输出边长 / 输入边长（还原画面 8，编码 1/8）"""
    import torch

    C, T, H, W = x.shape

    def starts(n):
        if n <= tile:
            return [0]
        return sorted(set(list(range(0, n - tile, tile - ov)) + [n - tile]))

    def ramp(n, o, lo, hi):
        w = torch.ones(n, device=out_dev)
        if o > 0:
            r = torch.arange(1, o + 1, dtype=torch.float32, device=out_dev) / (o + 1)
            if lo:
                w[:o] = torch.minimum(w[:o], r)
            if hi:
                w[n - o:] = torch.minimum(w[n - o:], r.flip(0))
        return w

    out = wsum = None
    o = int(round(ov * scale))
    for y0 in starts(H):
        for x0 in starts(W):
            y1, x1 = min(H, y0 + tile), min(W, x0 + tile)
            part = fn([x[:, :, y0:y1, x0:x1]])[0].float().to(out_dev)
            if out is None:
                oh, ow = int(round(H * scale)), int(round(W * scale))
                out = torch.zeros(part.shape[0], part.shape[1], oh, ow, device=out_dev)
                wsum = torch.zeros(1, 1, oh, ow, device=out_dev)
            ph, pw = part.shape[-2:]
            oy, ox = int(round(y0 * scale)), int(round(x0 * scale))
            m = (ramp(ph, o, y0 > 0, y1 < H)[:, None] * ramp(pw, o, x0 > 0, x1 < W)[None, :])[None, None]
            out[:, :, oy:oy + ph, ox:ox + pw] += part * m
            wsum[:, :, oy:oy + ph, ox:ox + pw] += m
            del part
    return out / wsum


def load_dit(job, mm, dev, reserve, report):
    import accelerate
    import torch
    from safetensors import safe_open

    models = job["models"]
    cfg = json.load(open(os.path.join(models["base"], "config.json"), "r", encoding="utf-8"))
    with accelerate.init_empty_weights():
        model = mm.WanModel(weight_init=False, **cfg)
    model.init_freqs()
    for m in model.modules():
        if isinstance(m, torch.nn.Linear):
            patch_linear(m)
    bf16 = torch.bfloat16
    fp8 = {getattr(torch, n) for n in ("float8_e4m3fn", "float8_e5m2") if hasattr(torch, n)}
    named = dict(model.named_modules())
    seen = set()
    files = [models["dit"], models["infinitetalk"]]
    gg = {f: gguf_tensors(f) for f in files if f.lower().endswith(".gguf")}
    total = 0
    for f in files:
        if f in gg:
            total += len(gg[f])
        else:
            with safe_open(f, framework="pt", device="cpu") as h:
                total += len(list(h.keys()))

    def items(f):
        if f in gg:
            for key, (t, qt, shape) in gg[f].items():
                yield key, (lambda t=t: t), qt, shape
        else:
            with safe_open(f, framework="pt", device="cpu") as h:
                for key in h.keys():
                    yield key, (lambda key=key: h.get_tensor(key)), None, None

    done = 0
    for f in files:
        for key, get, qt, shape in items(f):
            done += 1
            if done % 40 == 0:
                report(done / max(total, 1))
            name = strip_key(key)
            mod_name, _, attr = name.rpartition(".")
            mod = named.get(mod_name)
            if mod is None:
                continue
            t = get()
            is_lin = isinstance(mod, torch.nn.Linear)
            if attr == "scale_weight":
                mod.register_buffer("scale_weight", t.to(torch.float32), persistent=False)
                continue
            old = mod._parameters.get(attr) if attr in mod._parameters else mod._buffers.get(attr)
            want = tuple(old.shape) if old is not None else None
            if qt is not None:
                if want is not None and tuple(shape) != want:
                    raise UserError(f"模型文件和程序对不上（{name}：{tuple(shape)} ≠ {want}），请换回默认的模型版本")
                if is_lin and attr == "weight":
                    # 量化的权重原样放着（省一半以上的内存 / 显存），算的时候现场还原
                    mod._parameters[attr] = torch.nn.Parameter(t, requires_grad=False)
                    mod.gguf_q = (qt, tuple(shape))
                    seen.add(name)
                    continue
                t = gguf_dequant(t, qt, shape, torch.float32)
            if want is not None and tuple(t.shape) != want:
                if t.numel() != math.prod(want):
                    raise UserError(f"模型文件和程序对不上（{name}：{tuple(t.shape)} ≠ {want}），请换回默认的模型版本")
                t = t.reshape(want)
            if t.is_floating_point() and not (is_lin and attr == "weight" and t.dtype in fp8):
                t = t.to(bf16)
            if attr in mod._parameters:
                mod._parameters[attr] = torch.nn.Parameter(t, requires_grad=False)
                seen.add(name)
            elif attr in mod._buffers:
                mod._buffers[attr] = t
                seen.add(name)
    missing = [n for n, _ in model.named_parameters() if n not in seen]
    if missing:
        audio = [n for n in missing if "audio" in n]
        if audio:
            raise UserError(f"对口型模型文件不完整（缺 {len(audio)} 项，例如 {audio[0]}），请在设置里重新下载模型")
        raise UserError(f"主模型权重文件不完整（缺 {len(missing)} 项，例如 {missing[0]}），请重新下载模型")
    model.eval().requires_grad_(False)
    model.disable_teacache()  # 官方流程里每次生成前都会设置；不设置的话第一次前向就报 enable_teacache 不存在
    apply_lora(job, model)
    # 不在各层里的部分（嵌入层、输出头、音频投影）常驻显卡
    for name, mod in model.named_children():
        if name != "blocks":
            mod.to(dev)
    sw = Swapper(model, dev)
    free, total_mem = gpu_free()
    per = max(sw.block_bytes(i) for i in range(len(model.blocks)))
    n = int(max(0, free - reserve) // per)
    n = min(n, len(model.blocks))
    sw.place(n)
    return model, sw


def apply_lora(job, model):
    import torch
    from safetensors.torch import load_file

    path = job["models"].get("lora")
    if not path or not job.get("accel", True):
        return 0
    scale = float(job.get("lora_scale", 1.0))
    sd = load_file(path)
    named = dict(model.named_modules())
    params = dict(model.named_parameters())
    used = 0
    for key in list(sd.keys()):
        k = strip_key(key)
        for down_s, up_s in ((".lora_down.weight", ".lora_up.weight"), (".lora_A.weight", ".lora_B.weight")):
            if k.endswith(down_s):
                base = k[: -len(down_s)]
                up_key = key[: -len(down_s)] + up_s
                if up_key not in sd or base not in named:
                    break
                mod = named[base]
                if not isinstance(mod, torch.nn.Linear):
                    break
                down, up = sd[key].to(torch.bfloat16), sd[up_key].to(torch.bfloat16)
                rank = down.shape[0]
                alpha_key = key[: -len(down_s)] + ".alpha"
                a = float(sd[alpha_key].item()) / rank if alpha_key in sd else 1.0
                mod.register_buffer("lora_down", down, persistent=False)
                mod.register_buffer("lora_up", up, persistent=False)
                mod.lora_scale = a * scale
                used += 1
                break
        else:
            for suf, target in ((".diff_b", ".bias"), (".diff", ".weight")):
                if k.endswith(suf):
                    pname = k[: -len(suf)] + target
                    if pname not in params and k[: -len(suf)] in params:
                        pname = k[: -len(suf)]  # 直接挂在层上的参数（例如 modulation）
                    p = params.get(pname)
                    if p is not None:
                        mod_name, _, attr = pname.rpartition(".")
                        gq = getattr(named[mod_name], "gguf_q", None) if attr == "weight" else None
                        if gq is not None:
                            p = torch.nn.Parameter(gguf_dequant(p.data, gq[0], gq[1], torch.bfloat16), requires_grad=False)
                            named[mod_name].gguf_q = None
                        nv = (p.data.to(torch.float32) + sd[key].to(torch.float32) * scale).to(torch.bfloat16 if p.dtype not in (torch.float32,) else p.dtype)
                        mod_name, _, attr = pname.rpartition(".")
                        named[mod_name]._parameters[attr] = torch.nn.Parameter(nv, requires_grad=False)
                        used += 1
                    break
    if used == 0:
        raise UserError("加速 LoRA 文件格式认不出来，请在设置里重新下载模型")
    return used


# ------------------------------------------------------------------ 主流程


def main():
    global JOB
    job = json.load(open(sys.argv[1], "r", encoding="utf-8"))
    JOB = job
    work = job["work_dir"]
    os.makedirs(work, exist_ok=True)
    t0 = time.time()

    stage("init", "启动中…")
    import numpy as np
    import torch

    test_dev = job.get("test_device")  # 只给开发时在没有显卡的机器上测流程用
    if not test_dev and not torch.cuda.is_available():
        raise UserError("PyTorch 检测不到 NVIDIA 显卡（CUDA）。请把显卡驱动更新到最新版再试")
    dev = torch.device(test_dev or "cuda:0")
    torch.backends.cuda.matmul.allow_tf32 = True
    global PERF_FILE
    PERF_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "perf.log")
    try:
        open(PERF_FILE, "w", encoding="utf-8").close()
    except Exception:
        PERF_FILE = None
    # 开始时显卡还空着多少：这是本程序能用的上限。超过它 Windows 会偷偷借用内存当显存（不报错，但慢几十倍）
    budget = None
    if dev.type == "cuda":
        free0, total0 = torch.cuda.mem_get_info()
        budget = free0 - int(0.8 * 2**30)
        perf(f"torch {torch.__version__} cuda {torch.version.cuda} · {torch.cuda.get_device_name(0)} · "
             f"显存共 {total0 / 2**30:.1f}G，开始时空闲 {free0 / 2**30:.1f}G（其他程序占 {(total0 - free0) / 2**30:.1f}G）")
        try:
            a = torch.randn(4096, 4096, device=dev, dtype=torch.bfloat16)
            for _ in range(3):
                a @ a
            torch.cuda.synchronize()
            t1 = time.time()
            for _ in range(20):
                a @ a
            torch.cuda.synchronize()
            perf(f"显卡算力测试：{20 * 2 * 4096 ** 3 / (time.time() - t1) / 1e12:.0f} TFLOPS（bf16）")
            del a
        except Exception as e:
            perf(f"算力测试失败：{e}")
    use_sage = bool(job.get("sage", True))
    mm = install_stubs(job["code_dir"], use_sage)
    sage_on = bool(getattr(mm, "USE_SAGEATTN", False))
    import torch.nn.functional as F

    # ---- 声音
    song, vocals, dur = prepare_audio(job, work)
    emb = audio_embedding(job, vocals, dev)  # [帧数, 层, 768]
    total = int(dur * 25)
    if total < 2:
        raise UserError("音频太短")

    # ---- 分段：每段 81 帧，后一段用前一段最后 9 帧接上（官方的长视频方式）
    FRAMES, MOTION = 81, 9
    starts = [0]
    while starts[-1] + FRAMES < total:
        starts.append(starts[-1] + FRAMES - MOTION)
    need = starts[-1] + FRAMES
    while emb.shape[0] < need + 3:  # 歌比最后一段短：声音特征倒着接上（官方做法），生成完再按歌的长度裁掉
        pad = min(emb.shape[0], need + 3 - emb.shape[0])
        emb = torch.cat([emb, torch.flip(emb[-pad:], dims=[0])], dim=0)

    # ---- 画面
    src_w, src_h = size_of(job, work)
    W, H, box = target_size(job, src_w, src_h)
    refs, is_video = load_frames(job, work, W, H, box, starts)
    steps = int(job.get("steps") or (4 if job.get("accel", True) else 40))
    accel = bool(job.get("accel", True))
    tg = 1.0 if accel else float(job.get("text_guide", 5.0))
    ag = 2.0 if accel else float(job.get("audio_guide", 4.0))
    hd = W * H > 700 * 700
    shift = float(job.get("shift") or (2.0 if accel else (11.0 if hd else 7.0)))
    seed = int(job.get("seed", -1))
    if seed < 0:
        seed = int.from_bytes(os.urandom(4), "little") % 99999999
    color_fix = float(job.get("color_fix", 1.0))
    passes = 2 if math.isclose(tg, 1.0) else 3
    stage("plan", f"{W}×{H}，共 {total / 25:.1f} 秒，分 {len(starts)} 段生成，每段 {steps} 步", 0)
    emit(info={"width": W, "height": H, "clips": len(starts), "steps": steps, "seconds": total / 25, "sage": sage_on, "seed": seed})

    # ---- 提示词（T5，fp8）
    from wan.modules.t5 import T5EncoderModel

    stage("text", "理解提示词…")
    base = job["models"]["base"]
    neg = job.get("negative") or "bright tones, overexposed, static, blurred details, subtitles, style, works, paintings, images, static, overall gray, worst quality, low quality, JPEG compression residue, ugly, incomplete, extra fingers, poorly drawn hands, poorly drawn faces, deformed, disfigured, misshapen limbs, fused fingers, still picture, messy background, three legs, many people in the background, walking backwards"
    t5 = T5EncoderModel(text_len=512, dtype=torch.bfloat16, device=torch.device("cpu"), checkpoint_path="",
                        tokenizer_path=os.path.join(base, "google", "umt5-xxl"), quant="fp8", quant_dir=job["models"]["t5_dir"])
    # fp8 权重放到显卡上时，optimum-quanto 会现场编译它的 CUDA 扩展（要 Ninja 和 VS 编译器，一般电脑都没有），
    # 所以换成普通层自己算：权重保持 fp8（6.7 GB，不还原成 11 GB 的 bf16），只算一次提示词，算完就删
    t5.model = quanto_to_fp8(t5.model, torch.bfloat16)
    try:
        t5.model.to(dev)
        with torch.no_grad():
            context, context_null = t5([job.get("prompt") or "a person is singing", neg], dev)
    except torch.cuda.OutOfMemoryError:
        t5.model.to("cpu")
        gc_cuda()
        with torch.no_grad():
            context, context_null = t5([job.get("prompt") or "a person is singing", neg], torch.device("cpu"))
        context, context_null = context.to(dev), context_null.to(dev)
    context, context_null = context.to(torch.bfloat16), context_null.to(torch.bfloat16)
    del t5
    gc_cuda()

    # ---- 参考画面：CLIP 特征 + VAE 编码（每段要用的先全部算好，然后把 CLIP 卸掉，显存留给主模型）
    from wan.modules.clip import CLIPModel
    from wan.modules.vae import WanVAE

    stage("ref", "分析参考画面…")
    vae = WanVAE(vae_pth=os.path.join(base, "Wan2.1_VAE.pth"), device=dev)
    clip = CLIPModel(dtype=torch.float16, device=dev, checkpoint_path=os.path.join(base, "models_clip_open-clip-xlm-roberta-large-vit-huge-14.pth"),
                     tokenizer_path=os.path.join(base, "xlm-roberta-large"))
    lat_h, lat_w = H // 8, W // 8
    msk = torch.ones(1, FRAMES, lat_h, lat_w, device=dev)
    msk[:, 1:] = 0
    msk = torch.concat([torch.repeat_interleave(msk[:, 0:1], repeats=4, dim=1), msk[:, 1:]], dim=1)
    msk = msk.view(1, msk.shape[1] // 4, 4, lat_h, lat_w).transpose(1, 2).to(torch.bfloat16)

    def to_tensor(a):
        t = torch.from_numpy(np.ascontiguousarray(a)).permute(2, 0, 1).float()[None, :, None]  # 1 C 1 H W
        return ((t / 255.0) - 0.5) * 2

    # ---- VAE（画面 ↔ 潜空间）显存：整张一次算要好几 GB（576×704 约 7.7 GB）。
    # 放得下就整张算（先把几层主模型暂时挪回内存腾地方），放不下就分块算再拼起来（小显存的卡）
    held = {"sw": None, "per": 0}
    vae_need = {"enc": int(H * W * 22 * 1024 + 2**29), "dec": int(H * W * 22 * 1024 + 2**29)}
    measured = {}

    def vae_run(kind, full, tiled):
        if dev.type != "cuda":
            return full()
        sw_, per = held["sw"], held["per"]
        keep = sw_.n if sw_ is not None else 0
        gc_cuda()
        free, _ = gpu_free()
        need = vae_need[kind]
        use_tiled = bool(job.get("vae_tiled")) or need > free + keep * per - 2**28
        target = int(need * min(1.0, (VAE_TILE_PX * VAE_TILE_PX) / (H * W)) * 1.5 + 2**29) if use_tiled else need
        if sw_ is not None and free < target and keep > 0:
            sw_.place(max(0, keep - int(math.ceil((target - free) / per)) - 1))
            perf(f"给 VAE 腾显存：{keep} → {sw_.n} 层（需要约 {target / 2**30:.1f}G，原来空闲 {free / 2**30:.1f}G）")
        base = torch.cuda.memory_allocated()
        torch.cuda.reset_peak_memory_stats()
        t1 = time.time()
        try:
            out = tiled() if use_tiled else full()
        except RuntimeError as e:
            low = str(e).lower()
            if use_tiled or not ("out of memory" in low or "cudnn" in low):
                raise
            perf(f"VAE 整张算显存不够（{str(e).splitlines()[0][:100]}），改成分块算")
            gc_cuda()
            base = torch.cuda.memory_allocated()
            torch.cuda.reset_peak_memory_stats()
            use_tiled = True
            out = tiled()
        torch.cuda.synchronize()
        used = torch.cuda.max_memory_allocated() - base
        if not use_tiled:
            # 第一次实测后换掉估计值；之后取最大（只编码 1 帧时用得少，不能把估计值降下来）
            m = max(measured.get(kind, 0), int(used * 1.15 + 2**29))
            measured[kind] = vae_need[kind] = m
        perf(f"VAE {'编码' if kind == 'enc' else '还原画面'}{'（分块）' if use_tiled else ''} {time.time() - t1:.1f} 秒，用了 {used / 2**30:.1f}G 显存")
        if sw_ is not None and sw_.n != keep:
            gc_cuda()
            sw_.place(keep)
        return out

    def vae_encode(v):
        """v: [1, 3, T, H, W] → 潜空间 [16, t, H/8, W/8]（在显卡上）"""
        return vae_run("enc", lambda: vae.encode(v)[0],
                       lambda: vae_tiled(lambda xs: vae.encode(xs), v[0], VAE_TILE_PX, VAE_TILE_PX // 4, 1 / 8, dev))

    def vae_decode(z):
        """z: [16, t, h, w] → 画面 [3, T, H, W]（在内存里）"""
        return vae_run("dec", lambda: vae.decode([z])[0].cpu(),
                       lambda: vae_tiled(lambda xs: vae.decode(xs), z, VAE_TILE_PX // 8, VAE_TILE_PX // 32, 8, torch.device("cpu")))

    conds = []  # 每段：(参考图 tensor（CPU）, clip 特征, y)
    cache = {}
    for i, a in enumerate(refs):
        k = id(a)
        if k in cache:
            conds.append(cache[k])
            continue
        img = to_tensor(a).to(dev)
        with torch.no_grad():
            clip_fea = clip.visual(img[:, :, -1:, :, :]).to(torch.bfloat16)
            pad = torch.zeros(1, 3, FRAMES - 1, H, W, device=dev)
            y = vae_encode(torch.concat([img, pad], dim=2))[None]
            y = y.to(torch.bfloat16)
            y = torch.concat([msk, y], dim=1)
        item = (img.cpu(), clip_fea, y)
        cache[k] = item
        conds.append(item)
        if is_video:
            stage("ref", f"分析参考画面 {i + 1}/{len(refs)}…")
    del clip, cache
    gc_cuda()

    # ---- 主模型
    stage("load", "加载主模型（第一次较慢）…", 0)
    tokens = 21 * (H // 16) * (W // 16)
    # 先保守一点（宁可多搬几层，也别超出显存），算完第一步按实际用量再多放几层
    reserve = int(3.0 * 2**30 + tokens * 200 * 1024)
    model, sw = load_dit(job, mm, dev, reserve, lambda p: stage("load", f"加载主模型 {p * 100:.0f}%…", p * 100))
    seq_len = tokens
    per_block = max(sw.block_bytes(i) for i in range(len(model.blocks)))
    held["sw"], held["per"] = sw, per_block
    if dev.type == "cuda":
        perf(f"画面 {W}x{H}，{tokens} tokens · 每层 {per_block / 2**20:.0f}MB · {sw.n}/{len(model.blocks)} 层放显卡 · "
             f"锁页内存 {'是' if sw.pinned else '否'} · sage {'开' if sage_on else '关'} · "
             f"已占 {torch.cuda.memory_reserved() / 2**30:.1f}G，上限 {budget / 2**30:.1f}G")
    emit(info={"resident_blocks": sw.n, "blocks": len(model.blocks)})
    if sw.n < len(model.blocks):
        stage("load", f"主模型 {sw.n}/{len(model.blocks)} 层放显卡，其余放内存、算到时再搬（对速度影响很小）")

    fwd_n = [0]
    def adapt(secs, peak):
        """按这一步实际用的显存调整放在显卡上的层数：超了就少放（避免借用内存变得极慢），有富余就多放（少搬一些更快）"""
        fwd_n[0] += 1
        old = sw.n
        nb = len(model.blocks)
        perf(f"第 {fwd_n[0]} 次计算 {secs:.1f} 秒 · 显存峰值 {peak / 2**30:.1f}G / 上限 {budget / 2**30:.1f}G · {old}/{nb} 层在显卡")
        if peak > budget and old == 0 and not fwd_n[1:]:
            fwd_n.append(1)
            emit(warn="显存不够这个画面大小：主模型已经全部放在内存里了还是放不下，生成会非常慢。建议换小一点的分辨率（480P 或自定义更小的宽高）")
        if peak > budget and old > 0:
            k = int(math.ceil((peak - budget) / per_block)) + 1
            new = max(0, old - k)
        elif peak + per_block + 2**29 < budget and old < nb and fwd_n[0] <= 3:
            new = min(nb, old + int((budget - peak - 2**29) // per_block))
        else:
            return
        if new == old:
            return
        gc_cuda()
        sw.place(new)
        perf(f"调整：{old} → {new} 层放显卡")
        emit(info={"resident_blocks": sw.n, "blocks": nb})

    def forward(lat, t, ctx, audio, y, clip_fea):
        nonlocal sage_on
        args = dict(context=[ctx], clip_fea=clip_fea, seq_len=seq_len, y=y, audio=audio, ref_target_masks=torch.ones(3, lat_h, lat_w, device=dev))
        for attempt in range(6):
            try:
                if dev.type == "cuda":
                    torch.cuda.reset_peak_memory_stats()
                t1 = time.time()
                with torch.no_grad():
                    out = model([lat], t=t, **args)[0]
                if dev.type == "cuda":
                    torch.cuda.synchronize()
                    adapt(time.time() - t1, torch.cuda.max_memory_reserved())
                return out
            except torch.cuda.OutOfMemoryError:
                gc_cuda()
                if sw.n == 0:
                    raise UserError("显存不够：换小一点的分辨率（480P），或关掉其他占显存的程序再试")
                sw.place(max(0, sw.n - max(2, sw.n // 4)))
                stage("gen", f"显存紧张，改成 {sw.n}/{len(model.blocks)} 层放显卡")
            except UserError:
                raise
            except Exception as e:
                if sage_on and attempt == 0:
                    mm.USE_SAGEATTN = False
                    sage_on = False
                    emit(warn=f"sage-attn 加速在这台电脑上用不了，改用普通模式（{str(e).splitlines()[0][:160]}）")
                    continue
                raise
        raise UserError("显存不够")

    # ---- 输出：画面直接写进视频文件
    tmp_video = os.path.join(work, "video.mp4")
    ff = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
          "-r", "25", "-i", "-", "-c:v", "libx264", "-crf", str(int(job.get("crf", 17))), "-preset", "medium", "-pix_fmt", "yuv420p", tmp_video]
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    writer = subprocess.Popen(ff, stdin=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    written = 0

    timesteps = list(np.linspace(1000, 1, steps, dtype=np.float32)) + [0.0]
    timesteps = [torch.tensor([t], device=dev) for t in timesteps]
    timesteps = [shift * (t / 1000) / (1 + (shift - 1) * (t / 1000)) * 1000 for t in timesteps]
    indices = torch.arange(5) - 2
    total_calls = len(starts) * steps * passes
    calls = 0
    gen_t0 = time.time()
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed % (2**32))
    cond_frame = None
    from wan.utils.multitalk_utils import match_and_blend_colors

    for ci, s in enumerate(starts):
        first = ci == 0
        ref_img, clip_fea, y = conds[ci]
        center = torch.clamp(torch.arange(s, s + FRAMES)[:, None] + indices[None], 0, emb.shape[0] - 1)
        audio = emb[center][None].to(dev).to(torch.bfloat16)
        cur_motion = 1 if first else MOTION
        cur_lat = 1 + (cur_motion - 1) // 4
        with torch.no_grad():
            motion_lat = vae_encode(ref_img.to(dev) if first else cond_frame)
        noise = torch.randn(16, (FRAMES - 1) // 4 + 1, lat_h, lat_w, dtype=torch.float32, device=dev)
        latent = noise

        def add_noise(x0, ts):
            ts = ts.float() / 1000
            return (1 - ts) * x0 + ts * torch.randn_like(x0)

        if not first:
            motion_lat = motion_lat.to(latent.dtype)
            add = add_noise(motion_lat, timesteps[0])
            latent[:, : add.shape[1]] = add
        silent = torch.zeros_like(audio)[-1:]
        for i in range(len(timesteps) - 1):
            t = timesteps[i]
            latent[:, :cur_lat] = motion_lat
            cond = forward(latent, t, context, audio, y, clip_fea)
            calls += 1
            if math.isclose(tg, 1.0):
                drop_a = forward(latent, t, context, silent, y, clip_fea)
                calls += 1
                pred = drop_a + ag * (cond - drop_a)
            else:
                drop_t = forward(latent, t, context_null, audio, y, clip_fea)
                unc = forward(latent, t, context_null, silent, y, clip_fea)
                calls += 2
                pred = unc + tg * (cond - drop_t) + ag * (drop_t - unc)
            dt = (timesteps[i] - timesteps[i + 1]) / 1000
            latent = latent + (-pred) * dt[:, None, None, None]
            if not first:
                add = add_noise(motion_lat, timesteps[i + 1])
                latent[:, : add.shape[1]] = add
            latent[:, :cur_lat] = motion_lat
            el = time.time() - gen_t0
            left = el / calls * (total_calls - calls) if calls else 0
            stage("gen", f"第 {ci + 1}/{len(starts)} 段，第 {i + 1}/{steps} 步", calls / total_calls * 100)
            emit(eta=int(left), elapsed=int(el))
        with torch.no_grad():
            video = vae_decode(latent)[None]  # 1 C T H W（内存里）
        if color_fix > 0:
            video = match_and_blend_colors(video, ref_img, min(color_fix, 1.0))
        cond_frame = video[:, :, -MOTION:].to(torch.float32).to(dev)
        part = video[0] if first else video[0][:, cur_motion:]
        frames = ((part.clamp(-1, 1) + 1) / 2 * 255).round().to(torch.uint8).permute(1, 2, 3, 0).numpy()
        n = min(frames.shape[0], total - written)
        if n > 0:
            try:
                writer.stdin.write(np.ascontiguousarray(frames[:n]).tobytes())
            except BrokenPipeError:
                err = writer.stderr.read().decode("utf-8", "replace")[-300:]
                raise UserError(f"写入视频失败：{err}")
            written += n
        del video, part, frames, latent
        gc_cuda()

    writer.stdin.close()
    writer.wait()
    if writer.returncode != 0:
        raise UserError("保存视频失败：" + writer.stderr.read().decode("utf-8", "replace")[-300:])

    # ---- 配上原曲
    stage("mux", "合成声音…", 100)
    out = job["out"]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    run_ffmpeg(["-i", tmp_video, "-i", song, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
                "-shortest", "-movflags", "+faststart", out], "合成声音")
    emit(done=out, secs=round(time.time() - t0, 1), width=W, height=H, frames=written, seed=seed)


if __name__ == "__main__":
    try:
        main()
    except UserError as e:
        emit(error=str(e))
        sys.exit(3)
    except Exception as e:
        traceback.print_exc()
        msg = f"{type(e).__name__}: {e}"
        low = msg.lower()
        if "out of memory" in low:
            msg = "显存或内存不够：换小一点的分辨率（480P），关掉其他程序再试（" + msg[:200] + "）"
        emit(error=msg[:600])
        sys.exit(4)
