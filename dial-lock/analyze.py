#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
离线分析采集帧 —— 从真机数据搞清楚四件事，给重构定方案：
  1) 圆盘几何：用"亮蓝发光团(指针)"在各帧的位置拟合圆 -> 圆心/指针半径(自动标定，不用手点)
  2) 指针运动：每帧指针角度 -> 角速度/方向/是否匀速(用真实时间戳)
  3) 高亮条：在指针所在环带里找 黄条(高R高G低B) / 蓝条，输出角度区间
  4) 检测可视化：抽样帧叠加"检测到的指针(蓝点)+黄/蓝条弧"存盘，供人工核对检测准不准

用法：
  python analyze.py captures/run_YYYYmmdd_HHMMSS
输出：该目录下 _analysis/ (标注图 + report.txt + angles.csv)
"""

import os
import sys
import json
import glob

import numpy as np
import cv2

try:    # GBK 控制台编不了部分符号会崩, 遇到就替换而非报错
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def fit_circle(pts):
    """代数最小二乘拟合圆 -> (cx, cy, R)"""
    p = np.asarray(pts, np.float64)
    x, y = p[:, 0], p[:, 1]
    A = np.column_stack([2 * x, 2 * y, np.ones(len(x))])
    sol, *_ = np.linalg.lstsq(A, x * x + y * y, rcond=None)
    a, b, c = sol
    return a, b, np.sqrt(max(c + a * a + b * b, 0.0))


def fit_circle_robust(pts, iters=3, keep=0.7):
    """带离群剔除的圆拟合：拟合->按残差保留最接近的 keep 比例->重拟合。抗UI蓝色等离群点。"""
    pts = np.asarray(pts, np.float64)
    cur = pts
    cx, cy, R = fit_circle(cur)
    for _ in range(iters):
        d = np.abs(np.hypot(cur[:, 0] - cx, cur[:, 1] - cy) - R)
        k = max(8, int(len(cur) * keep))
        cur = cur[np.argsort(d)[:k]]
        cx, cy, R = fit_circle(cur)
    return cx, cy, R, len(cur)


def blue_saliency(bgr):
    """蓝色发光显著度：蓝压过红绿 * 亮度。指针是亮蓝白发光团；烛火是橙色(蓝低)会被压掉。"""
    b, g, r = bgr[:, :, 0].astype(np.float32), bgr[:, :, 1].astype(np.float32), bgr[:, :, 2].astype(np.float32)
    blue_excess = np.maximum(0.0, b - np.maximum(r, g))
    bright = bgr.max(axis=2).astype(np.float32)
    return blue_excess * (bright / 255.0)


def pointer_centroid(sal, roi=None):
    """显著度图里最亮团的质心。roi=(x0,y0,x1,y1) 限定范围。返回 (x,y,peak) 或 None。"""
    s = sal.copy()
    if roi:
        m = np.zeros_like(s)
        x0, y0, x1, y1 = roi
        m[y0:y1, x0:x1] = 1
        s = s * m
    s = cv2.GaussianBlur(s, (0, 0), 5)        # 合并发光团
    peak = float(s.max())
    if peak < 5:
        return None
    _, _, _, maxloc = cv2.minMaxLoc(s)
    # 在峰值邻域做加权质心，更稳
    px, py = maxloc
    win = 25
    y0, y1 = max(0, py - win), min(s.shape[0], py + win)
    x0, x1 = max(0, px - win), min(s.shape[1], px + win)
    patch = s[y0:y1, x0:x1]
    ys, xs = np.mgrid[y0:y1, x0:x1]
    w = patch.sum()
    return ((xs * patch).sum() / w, (ys * patch).sum() / w, peak)


def angle_of(cx, cy, x, y):
    return np.degrees(np.arctan2(y - cy, x - cx)) % 360.0


def ang_diff(a, b):
    return (a - b + 180.0) % 360.0 - 180.0


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: python analyze.py captures/run_YYYYmmdd_HHMMSS")
    run_dir = sys.argv[1]
    if not os.path.isdir(run_dir):
        run_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), run_dir)
    meta = json.load(open(os.path.join(run_dir, "frames.json"), encoding="utf-8"))
    frames_meta = meta["frames"]
    out_dir = os.path.join(run_dir, "_analysis")
    os.makedirs(out_dir, exist_ok=True)

    # 预读所有帧路径与时间戳
    items = [(fm["t"], os.path.join(run_dir, fm["file"])) for fm in frames_meta]
    n = len(items)
    print(f"帧数={n}  时长={meta['capture_seconds']}s  fps={meta['avg_fps']}  分辨率={meta['region']['width']}x{meta['region']['height']}")

    # --- Pass 1: 全图找蓝色指针质心，收集后拟合圆 ---
    cents, ts = [], []
    sample_imgs = {}
    for i, (t, path) in enumerate(items):
        img = cv2.imread(path)
        sal = blue_saliency(img)
        c = pointer_centroid(sal)
        if c is not None:
            cents.append((c[0], c[1]))
            ts.append((i, t, c[0], c[1]))
        if i % 30 == 0:
            sample_imgs[i] = path

    if len(cents) < 10:
        sys.exit(f"指针候选太少({len(cents)})，蓝色显著度阈值可能不合适。")

    cx, cy, R, kept = fit_circle_robust(cents)
    print(f"拟合圆: center=({cx:.0f},{cy:.0f})  指针半径R={R:.0f}  (用{kept}/{len(cents)}个内点)")

    # --- Pass 2: 限定环带，重算每帧指针角度(剔除离圆太远的离群帧) ---
    band_in, band_out = R * 0.78, R * 1.18
    recs = []   # (i, t, angle)
    for (i, t, x, y) in ts:
        d = np.hypot(x - cx, y - cy)
        if band_in <= d <= band_out:
            recs.append((i, t, angle_of(cx, cy, x, y)))
    recs.sort()

    # 角速度(展开角度，避免360跳变)
    angs = np.array([r[2] for r in recs])
    tt = np.array([r[1] for r in recs])
    unwrapped = np.unwrap(np.deg2rad(angs))
    omega = np.gradient(unwrapped, tt)        # rad/s
    omega_deg = np.rad2deg(omega)
    med_w = np.median(omega_deg)
    direction = "顺时针(角度增)" if med_w > 0 else "逆时针(角度减)"
    period = abs(360.0 / med_w) if med_w != 0 else float("inf")

    # --- 黄/蓝条检测：在环带里按角度统计颜色 ---
    # 用中间一帧做示例统计，并对若干抽样帧叠加可视化
    def detect_bars(img):
        b, g, r = img[:, :, 0].astype(np.float32), img[:, :, 1].astype(np.float32), img[:, :, 2].astype(np.float32)
        H, W = img.shape[:2]
        yy, xx = np.mgrid[0:H, 0:W]
        rad = np.hypot(xx - cx, yy - cy)
        ang = (np.degrees(np.arctan2(yy - cy, xx - cx)) % 360.0).astype(np.int32)
        band = (rad >= band_in) & (rad <= band_out)
        yellow = band & (r > 120) & (g > 90) & (b < 110) & (r - b > 50) & (g - b > 30)
        prof_y = np.zeros(360, np.float32)
        np.add.at(prof_y, ang[yellow], 1.0)
        return prof_y

    def runs(active):
        idx = np.where(active)[0]
        if len(idx) == 0:
            return []
        out, s = [], idx[0]
        for a, b2 in zip(idx, idx[1:]):
            if b2 != a + 1:
                out.append((s, a)); s = b2
        out.append((s, idx[-1]))
        # 合并跨0/360
        if len(out) >= 2 and out[0][0] == 0 and out[-1][1] == 359:
            (s0, e0), (s1, e1) = out[0], out[-1]
            out = out[1:-1] + [(s1, e0 + 360)]
        return out

    # 对抽样帧叠加可视化
    for i, path in sample_imgs.items():
        img = cv2.imread(path)
        ov = img.copy()
        cv2.circle(ov, (int(cx), int(cy)), int(R), (0, 255, 0), 1)
        cv2.circle(ov, (int(cx), int(cy)), int(band_in), (0, 120, 0), 1)
        cv2.circle(ov, (int(cx), int(cy)), int(band_out), (0, 120, 0), 1)
        # 指针
        match = [r for r in recs if r[0] == i]
        if match:
            a = np.deg2rad(match[0][2])
            px, py = int(cx + R * np.cos(a)), int(cy + R * np.sin(a))
            cv2.circle(ov, (px, py), 8, (0, 0, 255), 2)
            cv2.line(ov, (int(cx), int(cy)), (px, py), (0, 0, 255), 1)
        # 黄条
        prof_y = detect_bars(img)
        for s, e in runs(prof_y > 3):
            length = e - s + 1
            if 4 <= length <= 60:
                cv2.ellipse(ov, (int(cx), int(cy)), (int(R), int(R)), 0, s, e, (0, 255, 255), 4)
        crop = ov[max(0, int(cy - R - 60)):int(cy + R + 60), max(0, int(cx - R - 60)):int(cx + R + 60)]
        cv2.imwrite(os.path.join(out_dir, f"chk_{i:04d}.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 90])

    # 黄条角度区间(用几帧综合)
    bar_info = []
    for i in sorted(sample_imgs)[:6]:
        prof_y = detect_bars(cv2.imread(sample_imgs[i]))
        rs = [(s % 360, e % 360, e - s + 1) for s, e in runs(prof_y > 3) if 4 <= (e - s + 1) <= 60]
        bar_info.append((i, rs))

    # --- 写报告 ---
    csv = os.path.join(out_dir, "angles.csv")
    with open(csv, "w", encoding="utf-8") as f:
        f.write("i,t,angle_deg,omega_deg_s\n")
        for (i, t, a), w in zip(recs, omega_deg):
            f.write(f"{i},{t:.4f},{a:.2f},{w:.1f}\n")

    rep = os.path.join(out_dir, "report.txt")
    with open(rep, "w", encoding="utf-8") as f:
        def p(s):
            print(s); f.write(s + "\n")
        p(f"=== 采集 {os.path.basename(run_dir)} 分析 ===")
        p(f"帧数={n} 有效指针帧={len(recs)} 指针识别率={100*len(recs)/n:.0f}%")
        p(f"圆心=({cx:.0f},{cy:.0f}) 指针半径R={R:.0f} 环带=[{band_in:.0f},{band_out:.0f}]")
        p(f"角速度 中位={med_w:.1f}°/s  均值={omega_deg.mean():.1f}  标准差={omega_deg.std():.1f}")
        p(f"方向={direction}  一圈周期≈{period:.2f}s")
        # 速度分布(看是否匀速/有无加速段)
        qs = np.percentile(np.abs(omega_deg), [10, 50, 90])
        p(f"|角速度| 分位 p10/p50/p90 = {qs[0]:.0f}/{qs[1]:.0f}/{qs[2]:.0f} °/s")
        p(f"两帧最大角跨度≈{np.abs(med_w)/meta['avg_fps']:.1f}°/帧 (fps={meta['avg_fps']})")
        p("黄条角度区间(抽样帧, 角度°/角宽):")
        for i, rs in bar_info:
            p(f"  帧{i}: " + ("; ".join(f"[{s}~{e}] 宽{w}" for s, e, w in rs) if rs else "无"))
        p(f"\n标注图 chk_*.jpg / angles.csv 已存于 {out_dir}")
        p("请打开几张 chk_*.jpg 核对：红点是否压在指针上、黄弧是否压在黄条上。")

    print(f"\n完成 -> {out_dir}")


if __name__ == "__main__":
    main()
