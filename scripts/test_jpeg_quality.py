"""验证 JPEG 压缩质量对 Vision 模型坐标定位精度的影响。

用法:
  1. 连接 Android 设备，打开一个包含滚轮/精细 UI 的界面
  2. python scripts/test_jpeg_quality.py
  3. 对比不同 q 值下模型返回的坐标差异

也可指定已有截图:
  python scripts/test_jpeg_quality.py --image path/to/screenshot.png
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from io import BytesIO
from pathlib import Path

# ── 项目根 ──
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from PIL import Image

# ── 加载配置 ──
_cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
_local = yaml.safe_load((ROOT / "config.local.yaml").read_text(encoding="utf-8")) or {}

VISION_MODEL = _cfg.get("vision_model", "qwen3.7-flash")
VISION_BASE_URL = _cfg.get("vision_base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
VISION_API_KEY = _local.get("vision_api_key") or os.environ.get("VISION_API_KEY", "")
VISION_TIMEOUT = _cfg.get("vision_timeout", 60)  # 加到 60s 避免限流超时

# 输出目录
OUT_DIR = ROOT / "logs" / "dumps" / "jpeg_quality_test"

# ── JPEG 质量梯度 ──
QUALITIES = [50, 75, 90, 95, 100]


def screenshot_from_device() -> Image.Image:
    """通过 uiautomator2 截图"""
    import uiautomator2 as u2
    d = u2.connect()
    info = d.info
    print(f"[device] connected: {d.serial}")
    print(f"[device] displaySizeDp: {info.get('displaySizeDp', 'N/A')}")
    img = d.screenshot()
    print(f"[device] screenshot: {img.width}x{img.height}")
    return img


def compress_jpeg(img: Image.Image, quality: int, max_dim: int = 1024) -> tuple[bytes, int, int]:
    """等比缩放 + JPEG 压缩，返回 (jpeg_bytes, width, height)"""
    w, h = img.width, img.height
    longer = max(w, h)
    if longer > max_dim:
        ratio = max_dim / longer
        new_size = (int(w * ratio), int(h * ratio))
        img = img.resize(new_size, Image.LANCZOS)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), img.width, img.height


def call_vision(jpeg_bytes: bytes, img_w: int, img_h: int, description: str,
                model_name: str = None) -> dict:
    """调用 vision 模型定位坐标"""
    from langchain_openai import ChatOpenAI

    use_model = model_name or VISION_MODEL
    client = ChatOpenAI(
        model=use_model,
        temperature=0.0,
        api_key=VISION_API_KEY,
        base_url=VISION_BASE_URL,
        timeout=VISION_TIMEOUT,
        max_retries=0,
    )

    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    prompt = (
        f"截图尺寸 {img_w}x{img_h}，坐标原点左上角。"
        f"找到「{description}」所在位置。"
        f'只返回 JSON: {{"x": int, "y": int, "reason": str}}，'
        f"x in [0,{img_w}]，y in [0,{img_h}]。\n"
        f"请严格返回 JSON，且只返回 JSON。"
    )

    msg = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }
    ]

    t0 = time.monotonic()
    resp = client.invoke(msg)
    elapsed = time.monotonic() - t0
    text = str(getattr(resp, "content", "") or "").strip()

    # 提取 JSON
    data = None
    try:
        data = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except Exception:
                pass

    kb = len(jpeg_bytes) // 1024
    return {
        "elapsed": round(elapsed, 1),
        "img_kb": kb,
        "raw": text,
        "data": data,
        "x": data.get("x") if data else None,
        "y": data.get("y") if data else None,
        "reason": (data or {}).get("reason", ""),
    }


def main():
    # 可选模型列表
    AVAILABLE_MODELS = [
        "qwen3.7-flash",
        "qwen3.7-plus",
        "qwen3.7-max",
    ]

    parser = argparse.ArgumentParser(description="Vision model coordinate accuracy test")
    parser.add_argument("--image", type=str, help="已有截图路径（PNG），不指定则从设备截图")
    parser.add_argument("--desc", type=str,
                        default="弹窗中时间滚轮里值为08的那一行",
                        help="让 vision 模型定位的目标描述")
    parser.add_argument("--rounds", type=int, default=2, help="每次重复次数")
    parser.add_argument("--model", type=str, default=None,
                        help=f"指定模型名（逗号分隔多个: qwen3.7-flash,qwen3.7-plus）。"
                             f"不指定则用默认 {VISION_MODEL}")
    parser.add_argument("--quality", type=int, default=75, help="JPEG 质量 (默认 75)")
    parser.add_argument("--delay", type=int, default=5, help="请求间隔秒数 (默认 5)")
    args = parser.parse_args()

    if not VISION_API_KEY:
        print("ERROR: 未配置 vision_api_key (config.local.yaml 或 VISION_API_KEY 环境变量)")
        sys.exit(1)

    # 获取截图
    if args.image:
        print(f"[load] 从文件加载: {args.image}")
        img = Image.open(args.image)
    else:
        print("[load] 从设备截图...")
        img = screenshot_from_device()

    # 确定要测试的模型列表
    if args.model:
        models = [m.strip() for m in args.model.split(",")]
    else:
        models = [VISION_MODEL]

    print(f"[info] 原始尺寸: {img.width}x{img.height}")
    print(f"[info] 测试模型: {models}")
    print(f"[info] base_url: {VISION_BASE_URL}")
    print(f"[info] JPEG quality: {args.quality}")
    print(f"[info] 定位目标: {args.desc}")
    print(f"[info] 每级重复: {args.rounds} 次, 间隔: {args.delay}s")
    print()

    # 保存输出
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 只压缩一次
    jpeg_bytes, w, h = compress_jpeg(img, args.quality)
    jpeg_path = OUT_DIR / f"q{args.quality}.jpg"
    jpeg_path.write_bytes(jpeg_bytes)
    print(f"[save] q={args.quality} → {jpeg_path} ({len(jpeg_bytes) // 1024}KB, {w}x{h})")

    results = []
    for model_name in models:
        print(f"\n{'#'*60}")
        print(f"  MODEL: {model_name}")
        print(f"{'#'*60}")

        for r in range(args.rounds):
            tag = f"{model_name}_r{r}"
            print(f"\n{'='*60}")
            print(f"  [{tag}] {len(jpeg_bytes)//1024}KB, {w}x{h}")
            print(f"{'='*60}")

            try:
                res = call_vision(jpeg_bytes, w, h, args.desc, model_name=model_name)
                print(f"  耗时: {res['elapsed']}s")
                print(f"  坐标: x={res['x']}, y={res['y']}")
                print(f"  原因: {res['reason'][:120]}...")
                if res['x'] is None:
                    print(f"  ⚠ JSON 解析失败, raw={res['raw'][:200]}")
                results.append({"model": model_name, "round": r, "tag": tag,
                                "quality": args.quality, **res})
            except Exception as exc:
                print(f"  ✗ 调用失败: {exc}")
                results.append({"model": model_name, "round": r, "tag": tag,
                                "quality": args.quality, "error": str(exc)})

            # 避免触发限流
            if r < args.rounds - 1 or model_name != models[-1]:
                time.sleep(args.delay)

    # ── 汇总对比 ──
    print(f"\n\n{'='*80}")
    print("  汇总对比")
    print(f"{'='*80}")
    print(f"{'Model':>20} {'Round':>6} {'X':>6} {'Y':>6} {'Time':>6} {'Reason (前50字)':50}")
    print("-" * 90)

    for r in results:
        if "error" in r:
            print(f"{r.get('model',''):>20} {'ERR':>6} {'-':>6} {'-':>6} {'-':>6} {r['error'][:40]}")
        else:
            reason_short = (r.get("reason", "") or "")[:50]
            print(
                f"{r.get('model',''):>20} "
                f"r{r.get('round', 0):>4} "
                f"{r.get('x', '?'):>5} "
                f"{r.get('y', '?'):>5} "
                f"{r.get('elapsed', '?'):>5}s "
                f"{reason_short}"
            )

    # 计算坐标波动
    valid = [r for r in results if r.get("x") is not None]
    if valid:
        xs = [r["x"] for r in valid]
        ys = [r["y"] for r in valid]
        print(f"\n[总统计] X: min={min(xs)} max={max(xs)} range={max(xs)-min(xs)}")
        print(f"[总统计] Y: min={min(ys)} max={max(ys)} range={max(ys)-min(ys)}")

        # 按模型分组统计
        from collections import defaultdict
        by_m = defaultdict(list)
        for r in valid:
            by_m[r["model"]].append(r)
        print(f"\n[模型对比]")
        print(f"{'Model':>20} {'X avg':>8} {'X range':>8} {'Y avg':>8} {'Y range':>8} {'n':>3}")
        for m in sorted(by_m):
            group = by_m[m]
            gx = [r["x"] for r in group]
            gy = [r["y"] for r in group]
            avg_x = sum(gx) / len(gx)
            avg_y = sum(gy) / len(gy)
            print(f"  {m:>20}: "
                  f"X avg={avg_x:.1f} (±{max(gx)-min(gx)})  "
                  f"Y avg={avg_y:.1f} (±{max(gy)-min(gy)})  "
                  f"n={len(group)}")

    # 保存结果
    result_path = OUT_DIR / "results.json"
    with open(result_path, "w", encoding="utf-8") as f:
        # 清理不可序列化的字段
        clean = []
        for r in results:
            cr = {k: v for k, v in r.items() if k != "raw"}
            clean.append(cr)
        json.dump(clean, f, ensure_ascii=False, indent=2)
    print(f"\n[save] 结果 → {result_path}")


if __name__ == "__main__":
    main()
