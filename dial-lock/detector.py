#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
轮盘撬锁 检测器（全新实现，取代旧 detect2/analyze）。

— 基于真机帧实测的几何事实（见 README）——
圆盘是一条**暗色环形轨道**，上面有两类东西：
  • 指针(开锁器)：亮蓝白的「**径向**」流光，从内到外贯穿整条轨道；角向很窄(~6-8°)，
                  且**在轨道最内圈就已经很亮**。
  • 高亮条      ：「**切向**」弧段，只在轨道**外半段**亮、内半段是暗的；角向较宽(~15-25°)。
                  黄条=得分，蓝条=加时。

关键：**指针和蓝条颜色都偏蓝，颜色分不开**；能分开它们的是「半径形状」。所以：
  1) 指针 = 在「内半带」找最亮峰 —— 此处只有指针亮，条还没亮起来，天然不混。
  2) 条   = 在「外半带」找弧，但**先把指针那一窄角挖掉**（否则指针的径向流光自己会被当成一条，
            这正是旧版「指针落入扇形判断不对」的根因）；再要求弧「够宽 且 颜色饱和(明确黄或明确蓝)」，
            不饱和的(指针残辉/噪声)一律丢弃。

detect() 是无状态纯函数；角速度/命中预测由调用方(unlock.py)做。

离线自检：
  python detector.py captures/run_YYYYmmdd_HHMMSS      # 跑检测, 出标注图 + 健康报告 -> _check/
  python detector.py captures/botrec_YYYYmmdd_HHMMSS   # 同上(自动识别 frames.json / rec.json)
"""

import os
import sys
import json
import argparse

import numpy as np
import cv2

try:    # GBK 控制台编不了部分符号会崩, 遇到就替换而非报错(中文照常)
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


# ----------------------------- 几何常量(可被标定覆盖) -----------------------------
# 半径分带：取自真机帧(圆心(956,700), 暗轨道 r∈[102,182], 外圈亮装饰环 r>186)。
# 标定时按「外轨道边缘半径 R」的比例缩放(见 unlock.py)，故这里也给出比例供参考。
DEFAULT_P = {
    "ptr_in": 106,      # 指针带内沿  (≈0.58·R) —— 此带只有指针亮, 条还暗
    "ptr_out": 120,     # 指针带外沿  (≈0.66·R)
    "bar_in": 135,      # 条带内沿    (≈0.74·R) —— 条在外半段才亮
    "bar_out": 176,     # 条带外沿    (≈0.97·R) —— 再外是装饰亮环, 排除
    "ptr_glow": 12,     # 指针角半窗(度)：指针质心提取窗 + 从条带挖掉的半宽
    "ptr_excise_pad": 4,  # 挖指针时在 ptr_glow 之外再多挖几度, 防残辉漏成窄条
    "ptr_min_sal": 45,    # 指针峰显著阈值(峰相对内带中位); 真机指针 sal 常>120
    "bar_min_sal": 38,    # 条显著阈值(外带每角度均亮相对外带中位)
    "bar_min_arc": 11,    # 条最小角宽(度)：窄于此=噪点/残辉, 丢
    "bar_max_arc": 55,    # 条最大角宽(度)：宽于此=整圈反光, 丢
    "max_sectors": 2,     # 最多保留几条(游戏最多2)
    # 颜色判定(条带内每角度均色)：明确黄 or 明确蓝, 否则判'?'并丢弃
    "yellow_rb": 18, "yellow_gb": 8, "blue_br": 18,
}


def ang_diff(a, b):
    """a-b 归一化到 (-180, 180]"""
    return (a - b + 180.0) % 360.0 - 180.0


def circular_runs(active):
    """长度360布尔数组里的连续True段(处理跨0/360), 返回 [(start, length), ...]"""
    n = len(active)
    if active.all():
        return [(0, n)]
    if not active.any():
        return []
    off = int(np.where(~active)[0][0])      # 找一个 False 作切口
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
    """ROI 内预计算角度索引与各带掩膜(一次算好, 每帧复用)。"""
    def __init__(self, cx, cy, P=DEFAULT_P):
        self.cx, self.cy = cx, cy
        rmax = P["bar_out"]
        m = 8
        self.left = int(cx - rmax - m)
        self.top = int(cy - rmax - m)
        self.size = int(2 * (rmax + m))
        self.cxl = cx - self.left
        self.cyl = cy - self.top
        yy, xx = np.mgrid[0:self.size, 0:self.size]
        rad = np.hypot(xx - self.cxl, yy - self.cyl)
        self.ang_i = (np.degrees(np.arctan2(yy - self.cyl, xx - self.cxl)) % 360.0).astype(np.int32)
        self.ptr_band = (rad >= P["ptr_in"]) & (rad <= P["ptr_out"])
        self.bar_band = (rad >= P["bar_in"]) & (rad <= P["bar_out"])
        self.ptr_ai = self.ang_i[self.ptr_band]
        self.bar_ai = self.ang_i[self.bar_band]
        self.bar_cnt = np.bincount(self.bar_ai, minlength=360).astype(np.float32)

    def region(self):
        return {"left": self.left, "top": self.top, "width": self.size, "height": self.size}

    def crop(self, full_bgr):
        return full_bgr[self.top:self.top + self.size, self.left:self.left + self.size]


def _band_colors(bgr, geom, P):
    """条带内每角度(0-359)均色 -> (yellow, blue) 布尔数组。向量化 bincount, 一帧一次。"""
    px = bgr[geom.bar_band]                          # (N,3) BGR float
    c = np.maximum(geom.bar_cnt, 1.0)
    mb = np.bincount(geom.bar_ai, weights=px[:, 0], minlength=360) / c
    mg = np.bincount(geom.bar_ai, weights=px[:, 1], minlength=360) / c
    mr = np.bincount(geom.bar_ai, weights=px[:, 2], minlength=360) / c
    has = geom.bar_cnt > 0
    yellow = has & (mr > mb + P["yellow_rb"]) & (mg > mb + P["yellow_gb"])
    blue = has & (mb > mr + P["blue_br"])
    return yellow, blue


def detect(roi_bgr, geom, P=DEFAULT_P):
    """单帧检测。返回 dict:
       pointer : 指针角度(度)或 None
       conf    : 指针置信(峰/均, 越大越尖锐可信)
       sectors : [{center, len, color('yellow'|'blue'), str}], 已按强度截断 max_sectors
       base_in : 内带亮度中位(整环发亮/闪红时会偏高)
    """
    bgr = roi_bgr.astype(np.float32)
    bright = bgr.max(axis=2)        # 亮度=最亮通道; 指针/黄条/蓝条都比暗轨亮 -> 颜色无关都突出

    # ---- 指针: 内带, 每角度取「径向最大亮度」, 减内带中位 ----
    pv = bright[geom.ptr_band]
    base_in = float(np.median(pv))
    pmax = np.zeros(360, np.float32)
    np.maximum.at(pmax, geom.ptr_ai, pv)
    pmax = np.maximum(0.0, pmax - base_in)
    peak = float(pmax.max())
    pointer, conf = None, 0.0
    if peak >= P["ptr_min_sal"]:
        pk = int(pmax.argmax())
        g = P["ptr_glow"]
        idx = (pk + np.arange(-g, g + 1)) % 360
        w = pmax[idx]
        ar = np.deg2rad(idx.astype(np.float64))
        pointer = float(np.degrees(np.arctan2((np.sin(ar) * w).sum(),
                                              (np.cos(ar) * w).sum())) % 360.0)
        conf = peak / max(1.0, float(pmax.mean()))     # 峰相对内带整体的尖锐度

    # ---- 条: 外带, 每角度均亮, 减外带中位; 先挖指针角窗, 再按宽度+颜色筛 ----
    bv = bright[geom.bar_band]
    bmean = np.bincount(geom.bar_ai, weights=bv, minlength=360) / np.maximum(geom.bar_cnt, 1.0)
    base_out = float(np.median(bmean[geom.bar_cnt > 0]))
    bsal = np.maximum(0.0, bmean - base_out)

    active = bsal >= P["bar_min_sal"]
    if pointer is not None:                            # 挖掉指针那一窄角(根治"指针被当成条")
        gex = P["ptr_glow"] + P["ptr_excise_pad"]
        d = (np.arange(360) - int(round(pointer))) % 360
        active &= ~((d <= gex) | (d >= 360 - gex))

    yellow, blue = _band_colors(bgr, geom, P)
    sectors = []
    for start, length in circular_runs(active):
        if not (P["bar_min_arc"] <= length <= P["bar_max_arc"]):
            continue
        ar = (start + np.arange(length)) % 360
        ny, nb = int(yellow[ar].sum()), int(blue[ar].sum())
        if ny >= 0.5 * length:
            color = "yellow"
        elif nb >= 0.5 * length:
            color = "blue"
        else:
            continue                                   # 不饱和 = 指针残辉/噪声, 丢
        sectors.append({"center": (start + length / 2.0) % 360.0, "len": float(length),
                        "color": color, "str": float(bsal[ar].sum())})
    sectors.sort(key=lambda s: -s["str"])
    sectors = sectors[:P["max_sectors"]]
    return {"pointer": pointer, "conf": conf, "sectors": sectors, "base_in": base_in}


# ----------------------------- 可视化 / 离线自检 -----------------------------
def draw(roi, geom, P, d, extra=""):
    ov = roi.copy()
    c = (int(geom.cxl), int(geom.cyl))
    for r in (P["ptr_in"], P["ptr_out"], P["bar_in"], P["bar_out"]):
        cv2.circle(ov, c, r, (55, 55, 55), 1)
    Rbar = int((P["bar_in"] + P["bar_out"]) / 2)
    for s in d["sectors"]:
        col = (0, 255, 255) if s["color"] == "yellow" else (255, 160, 0)
        a0, a1 = s["center"] - s["len"] / 2, s["center"] + s["len"] / 2
        cv2.ellipse(ov, c, (Rbar, Rbar), 0, a0, a1, col, 4)
    if d["pointer"] is not None:
        a = np.deg2rad(d["pointer"])
        cv2.line(ov, c, (int(geom.cxl + P["bar_out"] * np.cos(a)),
                         int(geom.cyl + P["bar_out"] * np.sin(a))), (0, 0, 255), 2)
    tag = (f"ptr={'-' if d['pointer'] is None else int(d['pointer'])} "
           f"conf={d['conf']:.1f} nSec={len(d['sectors'])} {extra}")
    cv2.putText(ov, tag, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    return ov


def _load_run(run_dir, center_arg):
    """读 capture(frames.json) 或 botrec(rec.json)。
    返回 (frames_meta, 帧内圆心(cx,cy))。botrec 的帧已是 ROI, 圆心要减去 region 偏移。"""
    p = os.path.join(run_dir, "rec.json")
    if os.path.exists(p):                              # botrec: 帧是预裁的 ROI
        meta = json.load(open(p, encoding="utf-8"))
        cx, cy = meta["center"]
        reg = meta.get("region", {})
        return meta["frames"], (cx - reg.get("left", 0), cy - reg.get("top", 0))
    p = os.path.join(run_dir, "frames.json")
    if os.path.exists(p):                              # capture: 全屏帧, 圆心用绝对值
        meta = json.load(open(p, encoding="utf-8"))
        reg = meta.get("region", {})
        cx, cy = (int(v) for v in center_arg.split(","))
        return meta["frames"], (cx - reg.get("left", 0), cy - reg.get("top", 0))
    raise SystemExit(f"{run_dir} 下没有 frames.json / rec.json")


def main():
    ap = argparse.ArgumentParser(description="轮盘撬锁 检测器 离线自检")
    ap.add_argument("run_dir", help="captures/run_* 或 captures/botrec_* 目录")
    ap.add_argument("--center", type=str, default="956,700", help="圆心 x,y(默认真机采集值)")
    ap.add_argument("--every", type=int, default=15, help="每隔几帧存一张标注图")
    args = ap.parse_args()

    run_dir = args.run_dir
    if not os.path.isdir(run_dir):
        run_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), run_dir)
    frames, (cx, cy) = _load_run(run_dir, args.center)
    P = dict(DEFAULT_P)
    geom = Geom(cx, cy, P)

    out = os.path.join(run_dir, "_check")
    os.makedirs(out, exist_ok=True)
    rows = []
    for i, f in enumerate(frames):
        img = cv2.imread(os.path.join(run_dir, f["file"]))
        if img is None:
            continue
        roi = geom.crop(img)
        d = detect(roi, geom, P)
        rows.append((i, f.get("t", i), d))
        if i % args.every == 0:
            cv2.imwrite(os.path.join(out, f"chk_{i:04d}.jpg"), draw(roi, geom, P, d),
                        [cv2.IMWRITE_JPEG_QUALITY, 88])

    # ---- 健康报告 ----
    seen = [d for _, _, d in rows if d["pointer"] is not None]
    # 关键健康指标: 「条落在指针角窗内」的帧数 —— 新版应≈0(旧版正是这里出错)
    leak = 0
    jumps = 0
    prev = None
    ny = nb = 0
    for i, t, d in rows:
        for s in d["sectors"]:
            if s["color"] == "yellow":
                ny += 1
            else:
                nb += 1
            if d["pointer"] is not None and abs(ang_diff(s["center"], d["pointer"])) <= P["ptr_glow"]:
                leak += 1
        if d["pointer"] is not None:
            if prev is not None and (t - prev[0]) < 0.1 and abs(ang_diff(d["pointer"], prev[1])) > 15:
                jumps += 1
            prev = (t, d["pointer"])

    with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as fp:
        def p(s):
            print(s); fp.write(s + "\n")
        p(f"=== detector 自检 {os.path.basename(run_dir)} (圆心={cx},{cy}) ===")
        p(f"帧={len(rows)} 指针可见={len(seen)} ({100*len(seen)/max(1,len(rows)):.0f}%)")
        p(f"条检出: 黄={ny} 蓝={nb} (累计跨帧)")
        p(f"★指针角速度跳变(>15°/帧)={jumps}  <- 越少越好(0=指针识别全程平滑)")
        p(f"★『条落在指针角窗内』泄漏={leak}  <- 必须≈0(>0 说明指针又被当成条了)")
        p(f"标注图 chk_*.jpg / report.txt 已存 {out}")


if __name__ == "__main__":
    main()
