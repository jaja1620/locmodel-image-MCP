#!/usr/bin/env python3
"""Batch-generate a small set of variants and print a reproducible manifest.

Usage:
    python examples/batch_generate.py --out ./output/series --count 4

Every file gets a fixed seed so any single image can be reproduced later:
    locmodel-image generate --prompt "<same prompt>" --seed <seed>
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE.parent / "scripts" / "locmodel_image.py"


def run(args):
    proc = subprocess.run([sys.executable, str(CLI), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return json.loads(proc.stdout), proc.returncode
    except json.JSONDecodeError:
        return {"raw": proc.stdout[:400], "stderr": proc.stderr[-400:]}, proc.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="modular building, clean geometry, soft overcast light")
    ap.add_argument("--aspect", default="3:2")
    ap.add_argument("--count", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20261109)
    ap.add_argument("--out", default="./output/series")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = []

    for i in range(args.count):
        seed = args.seed + i
        print(f"[{i + 1}/{args.count}] seed={seed} ...", flush=True)
        obj, code = run(["generate", "--prompt", args.prompt, "--aspect", args.aspect,
                         "--seed", str(seed), "--out", str(out), "--prefix", f"v{i + 1:02d}"])
        if code != 0:
            print(f"  failed (exit {code}): {obj.get('message') or obj.get('raw')}")
            manifest.append({"index": i + 1, "seed": seed, "ok": False,
                             "error": obj.get("message") or obj.get("raw")})
            continue
        for f in obj.get("files", []):
            print(f"  {f['path']}")
            manifest.append({"index": i + 1, "seed": seed, "ok": True,
                             "path": f["path"], "size": f"{f.get('width')}x{f.get('height')}"})

    mf = out / "manifest.json"
    mf.write_text(json.dumps({"prompt": args.prompt, "aspect": args.aspect,
                              "requested": args.count, "items": manifest},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for m in manifest if m["ok"])
    print(f"\n{ok}/{args.count} succeeded\nmanifest: {mf}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
