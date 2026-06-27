#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
高速连续截图采集器 —— 给"轮盘撬锁"重构用的真机数据采集工具。

思路：把游戏跑起来，本工具以「尽可能小的间隔」连续抓帧存盘，
之后把整批帧 + frames.json(每帧相对时间戳)交给我，用真实数据确定检测/自动化方案。

为什么快：采集循环里【只抓屏 + 去 alpha 存进内存】，不在循环内做 PNG/JPG 编码或写盘
(编码/写盘单帧 5~30ms，会把帧率拖垮)。采集结束后再一次性落盘。
代价是总帧数受内存限制，所以默认会按区域大小自动算上限、防止 OOM。

用法(PowerShell)：
  python capture.py                      # 倒计时3秒 -> 抓主屏 6 秒 -> 落盘
  python capture.py --seconds 10         # 抓 10 秒
  python capture.py --select             # 先拖框只抓圆盘区域(区域越小帧率越高)
  python capture.py --region 700,300,520,520   # 直接指定区域 x,y,w,h
  python capture.py --fps-cap 120        # 限制最高帧率(默认不限)
  采集中按 F9 可提前结束(需 keyboard 生效；不灵就等时长到)。

输出：captures/run_YYYYmmdd_HHMMSS/  下一堆 fNNNN_Tms.jpg + frames.json
"""

import os
import sys
import json
import time
import argparse
import ctypes
import subprocess

import numpy as np
import cv2
import mss

# 让截屏坐标与屏幕真实像素一致(避免 Windows DPI 缩放导致区域错位)
if sys.platform == "win32":
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def select_region(sct):
    """抓一帧主屏，缩放到能完整显示，拖框选区域；返回屏幕坐标 dict 或 None(取消)。"""
    mon = sct.monitors[1]
    frame = np.asarray(sct.grab(mon))[:, :, :3]
    H, W = frame.shape[:2]
    # 缩到屏幕的 0.9，方便完整显示再映射回原坐标
    scale = min(1.0, 0.9 * mon["height"] / H, 0.9 * mon["width"] / W)
    disp = cv2.resize(frame, (int(W * scale), int(H * scale)))
    win = "select region: drag a box, ENTER=confirm, c=cancel"
    cv2.namedWindow(win)
    try:
        cv2.setWindowProperty(win, cv2.WND_PROP_TOPMOST, 1)
    except Exception:
        pass
    r = cv2.selectROI(win, disp, showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()
    cv2.waitKey(1)
    x, y, w, h = r
    if w <= 0 or h <= 0:
        return None
    return {
        "left": int(mon["left"] + x / scale),
        "top": int(mon["top"] + y / scale),
        "width": int(w / scale),
        "height": int(h / scale),
    }


def parse_region(s):
    """'x,y,w,h' -> region dict"""
    x, y, w, h = (int(v) for v in s.split(","))
    return {"left": x, "top": y, "width": w, "height": h}


def main():
    ap = argparse.ArgumentParser(description="高速连续截图采集器")
    ap.add_argument("--seconds", type=float, default=8.0, help="采集时长(秒)，默认8")
    ap.add_argument("--countdown", type=int, default=5, help="开始前倒计时(秒)，默认5")
    ap.add_argument("--push", action="store_true", help="采完自动 git add/commit/push 把帧发出去")
    ap.add_argument("--select", action="store_true", help="先拖框选采集区域(区域越小帧率越高)")
    ap.add_argument("--region", type=str, default=None, help="直接指定区域 x,y,w,h(屏幕坐标)")
    ap.add_argument("--monitor", type=int, default=1, help="显示器编号(1=主屏)，--select/--region 时忽略")
    ap.add_argument("--fps-cap", type=float, default=0.0, help="最高帧率上限(0=不限)")
    ap.add_argument("--max-frames", type=int, default=0, help="最多帧数(0=按内存自动算上限)")
    ap.add_argument("--ram-gb", type=float, default=3.0, help="内存缓冲上限(GB)，默认3")
    ap.add_argument("--format", choices=["jpg", "png"], default="jpg", help="落盘格式，默认jpg(快/小)")
    ap.add_argument("--quality", type=int, default=95, help="jpg质量(1-100)，默认95")
    ap.add_argument("--out", type=str, default=None, help="输出目录(默认 captures/run_时间戳)")
    args = ap.parse_args()

    sct = mss.mss()

    # 1) 确定采集区域
    if args.select:
        region = select_region(sct)
        if region is None:
            sys.exit("未选择区域，已取消。")
    elif args.region:
        region = parse_region(args.region)
    else:
        m = sct.monitors[args.monitor]
        region = {"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]}

    W, H = region["width"], region["height"]
    frame_bytes = W * H * 3
    # 内存上限换算成最大帧数，防 OOM
    ram_cap = int(args.ram_gb * (1024 ** 3) / frame_bytes)
    max_frames = ram_cap if args.max_frames <= 0 else min(args.max_frames, ram_cap)
    print(f"采集区域: {region}  单帧≈{frame_bytes/1e6:.1f}MB  "
          f"内存上限{args.ram_gb}GB -> 最多{max_frames}帧")

    # 2) 可选 F9 提前停
    stop = {"flag": False}
    try:
        import keyboard
        keyboard.add_hotkey("f9", lambda: stop.update(flag=True))
        print("提示：采集中按 F9 可提前结束。")
    except Exception as e:
        print(f"(keyboard 不可用，无法用F9提前停，将采满时长；{e})")

    # 3) 倒计时（本工具拍的是"屏幕"，所以这几秒内必须把游戏切到屏幕最前、让它在转）
    print("【马上点一下游戏窗口，让游戏显示在屏幕最前、那局正在转！本工具拍的是屏幕画面】")
    for s in range(args.countdown, 0, -1):
        print(f"\r  {s} 秒后开始连拍屏幕 -> 现在就切到游戏！ ...", end="")
        time.sleep(1)
    print("\r采集中（保持游戏在屏幕上、别最小化）...                 ")

    # 4) 采集循环：只抓屏 + 去 alpha 进内存，最快
    frames = []
    times = []
    min_dt = (1.0 / args.fps_cap) if args.fps_cap > 0 else 0.0
    t0 = time.perf_counter()
    last_log = t0
    next_t = t0
    grab = sct.grab
    while True:
        now = time.perf_counter()
        elapsed = now - t0
        if elapsed >= args.seconds or stop["flag"] or len(frames) >= max_frames:
            break
        if min_dt and now < next_t:               # 帧率上限：忙等到下一时隙
            continue
        next_t = now + min_dt
        frames.append(np.asarray(grab(region))[:, :, :3].copy())
        times.append(now - t0)
        if now - last_log >= 0.5:
            fps = len(frames) / max(1e-6, elapsed)
            print(f"\r  已采 {len(frames)} 帧  elapsed={elapsed:4.1f}s  ~{fps:5.1f} fps", end="")
            last_log = now
    cap_dur = time.perf_counter() - t0
    n = len(frames)
    fps = n / max(1e-6, cap_dur)
    why = "F9停止" if stop["flag"] else ("到达内存/帧数上限" if n >= max_frames else "时长到")
    print(f"\r采集完成：{n} 帧 / {cap_dur:.2f}s  平均 {fps:.1f} fps  ({why})            ")
    if n == 0:
        sys.exit("没采到帧。")

    # 5) 落盘
    out = args.out or os.path.join(BASE_DIR, "captures", "run_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    ext = args.format
    enc_param = ([cv2.IMWRITE_JPEG_QUALITY, args.quality] if ext == "jpg"
                 else [cv2.IMWRITE_PNG_COMPRESSION, 3])
    print(f"落盘到 {out} ...")
    meta_frames = []
    save_t0 = time.perf_counter()
    for i, (img, t) in enumerate(zip(frames, times)):
        name = f"f{i:04d}_{int(t*1000):06d}ms.{ext}"
        cv2.imwrite(os.path.join(out, name), img, enc_param)
        meta_frames.append({"i": i, "file": name, "t": round(t, 4)})
        if i % 50 == 0:
            print(f"\r  {i+1}/{n}", end="")
    print(f"\r  {n}/{n} 落盘完成，用时 {time.perf_counter()-save_t0:.1f}s")

    meta = {
        "region": region,
        "count": n,
        "capture_seconds": round(cap_dur, 3),
        "avg_fps": round(fps, 2),
        "format": ext,
        "frames": meta_frames,
    }
    with open(os.path.join(out, "frames.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"已写 frames.json -> {out}")

    # 6) 可选：自动 git 提交+推送，省得手动敲 git
    if args.push:
        print("自动提交+推送中 ...")
        rel = os.path.relpath(out, BASE_DIR)
        steps = [
            ["git", "add", out],
            ["git", "commit", "-m", f"采集真机帧 {os.path.basename(out)}"],
            ["git", "push"],
        ]
        ok = True
        for cmd in steps:
            r = subprocess.run(cmd, cwd=BASE_DIR, capture_output=True, text=True)
            out_txt = (r.stdout + r.stderr).strip()
            if out_txt:
                print("  " + out_txt.replace("\n", "\n  "))
            if r.returncode != 0:
                ok = False
                break
        print("✅ 已推送，跟我说一声“好了”。" if ok else
              "⚠ 自动推送失败(见上)。请手动执行：\n"
              f"  git add {rel}\n  git commit -m 采集帧\n  git push")
    else:
        print(f"把整个目录发我，或下次加 --push 让我自动推送。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
