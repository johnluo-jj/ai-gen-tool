#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
新检测器(形状+半径分层, 颜色无关) —— 先在采集帧上离线验证, 通过后再接入实机 bot。

核心思路(基于真机实测)：
  - 圆环分两层：内层只有指针, 外层才有黄/蓝条。
  - 指针 = 内层每角度显著度的峰(不依赖蓝色, 故指针变红也能认; 条不进内层, 天然不混蓝条)。
  - 条   = 外层里排除指针角窗后的"切向弧", 再按颜色分 黄(高亮)/蓝(加时)。
  - 整环带普遍变亮(落空闪红) -> 低置信, 不点。

用法：
  python detect2.py captures/run_YYYYmmdd_HHMMSS [--profile]
  --profile 只打印径向显著度剖面(用来定内/外层半径), 不做逐帧检测。
输出：该目录 _detect2/ (标注图 chk2_*.jpg + det.csv + report.txt)
"""

import os
import sys
import json
import argparse

import numpy as np
import cv2

try:    # GBK 控制台编不了部分符号会崩, 遇到就替换而非报错
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# 采集时圆心(由 analyze.py 拟合得到; 后续可被实机标定覆盖)
CENTER = (956, 700)


def ang_diff(a, b):
    return (a - b + 180.0) % 360.0 - 180.0


def circular_runs(active):
    n = len(active)
    if active.all():
        return [(0, n)]
    if not active.any():
        return []
    off = int(np.where(~active)[0][0])
    rolled = np.roll(active, -off)
    runs, i = [], 0
    while i < n:
        if rolled[i]:
            j = i
            while j < n and rolled[j]:
                j += 1
            runs.append(((i + off) % n, j - i))
            i = j
        else:
            i += 1
    return runs


class Geom:
    """ROI 内预计算角度/半径/各层掩膜。"""
    def __init__(self, cx, cy, r_max=210):
        m = 6
        self.left = int(cx - r_max - m)
        self.top = int(cy - r_max - m)
        self.size = int(2 * (r_max + m))
        self.cxl = cx - self.left
        self.cyl = cy - self.top
        yy, xx = np.mgrid[0:self.size, 0:self.size]
        dx, dy = xx - self.cxl, yy - self.cyl
        self.rad = np.hypot(dx, dy)
        self.ang = (np.degrees(np.arctan2(dy, dx)) % 360.0)
        self.ang_i = self.ang.astype(np.int32)

    def crop(self, full_bgr):
        return full_bgr[self.top:self.top + self.size, self.left:self.left + self.size]


def saliency(bgr):
    """亮度显著度: 每像素取最亮通道。指针/黄条/蓝条都比暗环亮 -> 颜色无关都能突出。"""
    return bgr.max(axis=2).astype(np.float32)


def radial_profile(geom, sal, r0=40, r1=205):
    """环带内按半径分bin的平均显著度(减去该环中位), 用于定位指针层/条层边界。"""
    prof = np.zeros(r1 - r0, np.float32)
    rad_i = geom.rad.astype(np.int32)
    for k, r in enumerate(range(r0, r1)):
        ring = (rad_i == r)
        if ring.any():
            v = sal[ring]
            prof[k] = float(np.median(v))
    return np.arange(r0, r1), prof


def angle_profile(geom, sal, r_in, r_out):
    """指定半径层内, 每角度(0-359)的显著度(减环中位后的最大值成廓)。"""
    band = (geom.rad >= r_in) & (geom.rad <= r_out)
    if not band.any():
        return np.zeros(360, np.float32), 0.0
    base = float(np.median(sal[band]))
    s = np.maximum(0.0, sal - base)
    prof = np.zeros(360, np.float32)
    np.maximum.at(prof, geom.ang_i[band], s[band])
    return prof, base


def detect(bgr, geom, P):
    """返回 dict: pointer(角度或None), ptr_conf, sectors[{center,len,color,str}], flash(bool)"""
    sal = saliency(bgr)
    # 指针: 内层峰角
    pin, base_in = angle_profile(geom, sal, P["ptr_in"], P["ptr_out"])
    # 落空闪红/整环变亮 -> 内层基底高 + 廓不尖锐 -> 低置信
    peak = float(pin.max())
    ptr_angle, ptr_conf = None, 0.0
    if peak >= P["ptr_min_sal"]:
        pk = int(pin.argmax())
        glow = P["ptr_glow"]
        idxs = (pk + np.arange(-glow, glow + 1)) % 360
        w = pin[idxs]
        ar = np.deg2rad(idxs.astype(np.float64))
        ptr_angle = float(np.degrees(np.arctan2((np.sin(ar) * w).sum(), (np.cos(ar) * w).sum())) % 360.0)
        # 置信: 峰相对内层整体的尖锐度(峰/均值)
        ptr_conf = peak / max(1.0, float(pin.mean()))

    flash = base_in >= P["flash_base"]   # 内层基底过高 = 整环发亮(闪红)

    # 条: 外层全廓找弧(不挖指针窗, 否则指针压上真条时会把目标删掉), 再剔除"针自身的蓝色窄弧"
    pout, _ = angle_profile(geom, sal, P["bar_in"], P["bar_out"])
    yflag, bflag = band_color_flags(bgr, geom, P)   # 每角度黄/蓝(向量化, 一帧一次)
    active = pout >= P["bar_min_sal"]
    sectors = []
    for start, length in circular_runs(active):
        if not (P["bar_min_arc"] <= length <= P["bar_max_arc"]):
            continue
        center = (start + length / 2.0) % 360.0
        ar = (start + np.arange(length)) % 360
        ny, nb = int(yflag[ar].sum()), int(bflag[ar].sum())
        color = "yellow" if ny >= max(2, 0.3 * length) else "blue" if nb >= 0.5 * length else "?"
        # 针自身在外层的辉光: 蓝色、窄、且中心紧贴指针 -> 不是条, 跳过。
        # (真黄条发黄不会被剔; 真蓝条多从边缘切入, 中心偏离指针>tol, 会保留; 加速模糊的针弧中心恰在指针上被剔)
        if (ptr_angle is not None and color == "blue" and length <= P["needle_max_len"]
                and abs(ang_diff(center, ptr_angle)) <= P["needle_center_tol"]):
            continue
        sectors.append({"center": center, "len": float(length),
                        "color": color, "str": float(pout[ar].sum())})
    sectors.sort(key=lambda c: -c["str"])
    sectors = sectors[:P["max_sectors"]]
    return {"pointer": ptr_angle, "ptr_conf": ptr_conf, "sectors": sectors,
            "flash": flash, "base_in": base_in, "ptr_peak": peak}


def band_color_flags(bgr, geom, P):
    """每角度(0-359)在条层的均色 -> (黄, 蓝) 布尔数组。向量化(bincount), 一帧算一次。
    逐角而非整段均值: 黄条被蓝针部分压住时, 仍能凭"有相当一段发黄"判成黄(针蓝, 不会发黄)。"""
    in_band = (geom.rad >= P["bar_in"]) & (geom.rad <= P["bar_out"])
    ai = geom.ang_i[in_band]
    px = bgr[in_band].astype(np.float32)            # (N,3) BGR
    cnt = np.bincount(ai, minlength=360).astype(np.float32)
    c = np.maximum(cnt, 1.0)
    mb = np.bincount(ai, weights=px[:, 0], minlength=360) / c
    mg = np.bincount(ai, weights=px[:, 1], minlength=360) / c
    mr = np.bincount(ai, weights=px[:, 2], minlength=360) / c
    yellow = (cnt > 0) & (mr > mb + 25) & (mg > mb + 10)
    blue = (cnt > 0) & (mb > mr + 25)
    return yellow, blue


DEFAULT_P = {
    # 指针层收到最内圈[104,118]: 真帧实测 r105~115 只有指针亮(~130-160), 条在此处暗(~30-75);
    # r>=120 起金条压过来(133~254)会偷峰, 故必须避开。<104 是中心文字盘也排除。
    "ptr_in": 104, "ptr_out": 118,
    "bar_in": 148, "bar_out": 180,      # 条层(黄/蓝条; 条其实从~120亮到175, 取外半段够判有无+颜色; >186是外圈装饰环排除)
    "ptr_glow": 11,                     # 指针角半窗(度)
    "needle_max_len": 16,               # 针自身外层蓝弧最大角宽(静止~9, 加速模糊到~16); 更宽的蓝条算真条
    "needle_center_tol": 5,             # 蓝弧中心距指针<此度才算"针自身"(真蓝条切入时中心偏离更大)
    "ptr_min_sal": 60,                  # 指针层峰阈值(峰相对环中位)
    "bar_min_sal": 55,                  # 条层显著阈值
    "flash_base": 70,                   # 指针层基底中位超过此值 = 闪红/整环亮(待测调)
    "bar_min_arc": 6, "bar_max_arc": 60,
    "max_sectors": 3,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--profile", action="store_true", help="只打印径向显著度剖面(定半径层)")
    args = ap.parse_args()

    run_dir = args.run_dir
    if not os.path.isdir(run_dir):
        run_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), run_dir)
    meta = json.load(open(os.path.join(run_dir, "frames.json"), encoding="utf-8"))
    fm = meta["frames"]
    geom = Geom(*CENTER)
    P = dict(DEFAULT_P)

    if args.profile:
        # 多帧平均径向剖面 -> 看指针层/条层/暗环边界
        acc = None
        cnt = 0
        for f in fm[::5]:
            img = cv2.imread(os.path.join(run_dir, f["file"]))
            sal = saliency(geom.crop(img))
            rs, prof = radial_profile(geom, sal)
            acc = prof if acc is None else acc + prof
            cnt += 1
        acc /= cnt
        print("半径 : 平均显著度(中位)   —— 找两个隆起: 内=指针层, 外=条层; 谷=暗环")
        for r, v in zip(rs, acc):
            if r % 3 == 0:
                bar = "#" * int(v / 4)
                print(f"  {r:3d}: {v:6.1f} {bar}")
        return

    out = os.path.join(run_dir, "_detect2")
    os.makedirs(out, exist_ok=True)
    rows = []
    for i, f in enumerate(fm):
        img = cv2.imread(os.path.join(run_dir, f["file"]))
        roi = geom.crop(img)
        d = detect(roi, geom, P)
        rows.append((i, f["t"], d))
        if i % 15 == 0:
            ov = roi.copy()
            for r in (P["ptr_in"], P["ptr_out"], P["bar_in"], P["bar_out"]):
                cv2.circle(ov, (int(geom.cxl), int(geom.cyl)), r, (60, 60, 60), 1)
            for s in d["sectors"]:
                col = (0, 255, 255) if s["color"] == "yellow" else (255, 150, 0) if s["color"] == "blue" else (180, 180, 180)
                a0, a1 = s["center"] - s["len"] / 2, s["center"] + s["len"] / 2
                cv2.ellipse(ov, (int(geom.cxl), int(geom.cyl)), (int((P["bar_in"]+P["bar_out"])/2),)*2, 0, a0, a1, col, 4)
            if d["pointer"] is not None:
                a = np.deg2rad(d["pointer"])
                p2 = (int(geom.cxl + P["bar_out"] * np.cos(a)), int(geom.cyl + P["bar_out"] * np.sin(a)))
                cv2.line(ov, (int(geom.cxl), int(geom.cyl)), p2, (0, 0, 255), 2)
            tag = f"ptr={'-' if d['pointer'] is None else int(d['pointer'])} conf={d['ptr_conf']:.1f} flash={'Y' if d['flash'] else 'n'}"
            cv2.putText(ov, tag, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
            cv2.imwrite(os.path.join(out, f"chk2_{i:04d}.jpg"), ov, [cv2.IMWRITE_JPEG_QUALITY, 88])

    # 报告: 指针角度连续性 + 条检出 + 闪红帧
    csvp = os.path.join(out, "det.csv")
    with open(csvp, "w", encoding="utf-8") as fp:
        fp.write("i,t,ptr,conf,flash,nSec,sectors\n")
        for i, t, d in rows:
            secs = "|".join(f"{s['color']}:{int(s['center'])}/{int(s['len'])}" for s in d["sectors"])
            fp.write(f"{i},{t:.4f},{'' if d['pointer'] is None else round(d['pointer'],1)},"
                     f"{d['ptr_conf']:.1f},{int(d['flash'])},{len(d['sectors'])},{secs}\n")

    seen = [d for _, _, d in rows if d["pointer"] is not None and not d["flash"]]
    flash_n = sum(1 for _, _, d in rows if d["flash"])
    # 角速度跳变率: 相邻有效帧角差>预期上限(127°/s/30fps≈5°,留余量15°)算跳变
    jumps = 0
    prev = None
    for i, t, d in rows:
        if d["pointer"] is not None and not d["flash"]:
            if prev is not None and abs(ang_diff(d["pointer"], prev[1])) > 15 and (t - prev[0]) < 0.1:
                jumps += 1
            prev = (t, d["pointer"])
    rep = os.path.join(out, "report.txt")
    with open(rep, "w", encoding="utf-8") as fp:
        def p(s):
            print(s); fp.write(s + "\n")
        p(f"=== detect2 在 {os.path.basename(run_dir)} ===")
        p(f"帧={len(rows)} 指针有效(非闪红)={len(seen)} ({100*len(seen)/len(rows):.0f}%) 闪红帧={flash_n}")
        p(f"指针角度相邻跳变(>15°/帧)次数={jumps}  <- 越少越好(0=全程平滑无锁错)")
        ny = sum(1 for _, _, d in rows for s in d["sectors"] if s["color"] == "yellow")
        nb = sum(1 for _, _, d in rows for s in d["sectors"] if s["color"] == "blue")
        nq = sum(1 for _, _, d in rows for s in d["sectors"] if s["color"] == "?")
        p(f"条检出: 黄={ny} 蓝={nb} 未分类={nq} (累计跨帧)")
        p(f"标注图 chk2_*.jpg / det.csv 已存 {out}")


if __name__ == "__main__":
    main()
