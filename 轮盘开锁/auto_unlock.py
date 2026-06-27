#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
轮盘撬锁 自动辅助 (B 方案 / 全自动)

原理：截屏 -> 识别蓝色指针角度与 黄色高亮条/蓝色加时条 的角度区间 ->
指针(算上点击延迟提前量)压到目标上时自动点左键撬锁；
离目标远时按住右键加速逼近，临近时松开右键以保证命中。

元素说明：
  - 指针 = 蓝针(从表盘中心指向外圈)
  - 黄色弧条 = 高亮条(撬中得分)
  - 蓝色弧条 = 加时条(撬中得分且加时间)  —— 与指针同为蓝色，靠半径分层区分
  - 红色小箭头 = 鼠标光标，游戏自绘，直接忽略；bot 不移动鼠标，请自行把光标停在不影响识别的区域

控制：
  python auto_unlock.py --calibrate   先标定(圆心/半径/颜色)，生成 config.json
  python auto_unlock.py                运行；F8 开/暂停，F9 退出，--debug 显示识别画面

注意：客户端游戏请用「无边框窗口/窗口化」模式(独占全屏可能截到黑屏)；
若点击不生效，请用管理员身份运行本脚本(游戏以管理员运行时尤其需要)。
"""

import os
import sys
import json
import time
import argparse
import ctypes

import numpy as np
import cv2
import mss

# Windows 下让截屏坐标与鼠标坐标一致(避免 DPI 缩放错位)
if sys.platform == "win32":
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# 颜色默认值(标定跳过时兜底；HSV，H:0-179)。指针与加时条共用 blue。
DEFAULT_COLORS = {
    "blue":   [[[92, 90, 120], [125, 255, 255]]],    # 蓝指针 + 蓝加时条
    "yellow": [[[18, 110, 130], [34, 255, 255]]],    # 黄高亮条
}

DEFAULTS = {
    "latency": 0.05,            # 总延迟(秒)：截屏+处理+系统点击；命中率主要靠它调
    "min_click_interval": 0.16, # 两次点击最小间隔(秒)，防抖
    "hit_margin_deg": 5.0,      # 命中判定角度容差
    "pointer_min_px": 8,        # 指针被认定所需最少像素
    "bar_min_px": 5,            # 某角度上被算作"条"所需最少像素
    "bar_min_arc": 10,          # 弧条最小角宽(度)：用来滤掉"蓝针"在外圈留下的细线
    "pointer_band": 0.55,       # 指针检测带：内圈 [r_in, r_in+该比例*环宽]
    "bar_band": 0.60,           # 弧条检测带：外圈 末尾该比例*环宽
    "click_pos": None,          # 左键点击坐标[x,y]；null=原地点击(不移动光标，光标由你自己停好)
    "boost": {"enabled": True, "release_deg": 22.0},  # 自适应加速：离目标>该角度则按住右键
}


# ----------------------------- 角度/几何 小工具 -----------------------------
def ang_diff(a, b):
    """a-b 归一化到 (-180, 180]"""
    return (a - b + 180.0) % 360.0 - 180.0


def circular_mean_deg(angles_deg):
    r = np.deg2rad(angles_deg)
    s = np.sin(r).sum()
    c = np.cos(r).sum()
    if s == 0 and c == 0:
        return None
    return float(np.rad2deg(np.arctan2(s, c)) % 360.0)


def circular_runs(active):
    """长度 360 布尔数组里的连续 True 段(处理跨 0/360)，返回 [(start, length), ...]"""
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


def build_hsv_ranges(h, s, v, dh=10, ds=80, dv=80):
    """由采样 HSV 中位数构造 inRange 范围(蓝色一般不跨界；保留通用红色跨 0/180 逻辑)"""
    h = int(h)
    s_lo, v_lo = max(int(s) - ds, 60), max(int(v) - dv, 60)
    lo1, hi1 = h - dh, h + dh
    out = []
    if lo1 < 0:
        out.append([[0, s_lo, v_lo], [hi1, 255, 255]])
        out.append([[180 + lo1, s_lo, v_lo], [179, 255, 255]])
    elif hi1 > 179:
        out.append([[lo1, s_lo, v_lo], [179, 255, 255]])
        out.append([[0, s_lo, v_lo], [hi1 - 180, 255, 255]])
    else:
        out.append([[lo1, s_lo, v_lo], [hi1, 255, 255]])
    return out


# ----------------------------- 配置读写 -----------------------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit("未找到 config.json，请先运行：python auto_unlock.py --calibrate")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"已保存配置 -> {CONFIG_PATH}")


# ----------------------------- 截屏 -----------------------------
class Grabber:
    def __init__(self):
        self.sct = mss.mss()

    def grab(self, region):
        img = np.asarray(self.sct.grab(region))   # BGRA
        return img[:, :, :3]                       # -> BGR

    def primary(self):
        return self.sct.monitors[1]                # 主显示器(含 left/top 偏移)


# ----------------------------- 检测上下文(ROI 内预计算) -----------------------------
def make_context(cfg):
    cx, cy = cfg["center"]
    r_in, r_out = cfg["r_inner"], cfg["r_outer"]
    depth = max(1, r_out - r_in)
    margin = 8
    left = int(cx - r_out - margin)
    top = int(cy - r_out - margin)
    size = int(2 * (r_out + margin))
    region = {"left": left, "top": top, "width": size, "height": size}

    cxl, cyl = cx - left, cy - top
    yy, xx = np.mgrid[0:size, 0:size]
    dx = xx - cxl
    dy = yy - cyl
    radius = np.sqrt(dx * dx + dy * dy)
    angle = (np.degrees(np.arctan2(dy, dx)) % 360.0)

    inner = (radius >= r_in) & (radius <= r_in + cfg["pointer_band"] * depth)   # 指针带
    outer = (radius >= r_out - cfg["bar_band"] * depth) & (radius <= r_out)     # 弧条带

    return {
        "region": region,
        "angle": angle,
        "angle_int": angle.astype(np.int32),
        "inner_u8": (inner.astype(np.uint8) * 255),
        "outer_u8": (outer.astype(np.uint8) * 255),
        "colors": {k: cfg.get("colors", {}).get(k, DEFAULT_COLORS[k]) for k in DEFAULT_COLORS},
        "cfg": cfg,
    }


def color_mask(hsv, ranges, band_u8):
    mask = None
    for lo, hi in ranges:
        m = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
        mask = m if mask is None else cv2.bitwise_or(mask, m)
    return cv2.bitwise_and(mask, band_u8)


def bars_from_mask(angle_int, mask, ctx):
    a = angle_int[mask > 0]
    if a.size == 0:
        return []
    hist = np.bincount(a, minlength=360)
    active = hist >= ctx["cfg"]["bar_min_px"]
    bars = []
    for start, length in circular_runs(active):
        if ctx["cfg"]["bar_min_arc"] <= length < 350:   # 太窄=噪点/蓝针；太宽=误检整圈
            bars.append({"center": (start + length / 2.0) % 360.0, "len": float(length)})
    return bars


def detect(frame_bgr, ctx):
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    blue, yellow = ctx["colors"]["blue"], ctx["colors"]["yellow"]

    # 指针：蓝色 ∩ 内圈带
    pmask = color_mask(hsv, blue, ctx["inner_u8"])
    psel = pmask > 0
    pointer = circular_mean_deg(ctx["angle"][psel]) if int(psel.sum()) >= ctx["cfg"]["pointer_min_px"] else None

    # 弧条：在外圈带上找(蓝=加时条 / 黄=高亮条)；蓝针在外圈的细线会被 bar_min_arc 滤掉
    bonus = bars_from_mask(ctx["angle_int"], color_mask(hsv, blue, ctx["outer_u8"]), ctx)
    score = bars_from_mask(ctx["angle_int"], color_mask(hsv, yellow, ctx["outer_u8"]), ctx)
    return pointer, score, bonus


# ----------------------------- 决策 -----------------------------
def edge_distance(pointer, bar):
    """指针到弧条最近边缘的角距(已在条内则为 0)"""
    return max(0.0, abs(ang_diff(bar["center"], pointer)) - bar["len"] / 2.0)


def choose_and_decide(pointer, omega, score, bonus, cfg):
    """返回 (should_click, nearest_edge_dist)。蓝色加时条优先。"""
    predicted = pointer + omega * cfg["latency"]
    margin = cfg["hit_margin_deg"]

    should_click = False
    for bar in bonus + score:           # 蓝条优先(放前面)，命中即触发
        if abs(ang_diff(predicted, bar["center"])) <= bar["len"] / 2.0 + margin:
            should_click = True
            break

    all_bars = bonus + score
    nearest = min((edge_distance(pointer, b) for b in all_bars), default=None)
    return should_click, nearest


# ----------------------------- 运行循环 -----------------------------
def run(cfg, debug=False):
    import pydirectinput
    import keyboard
    pydirectinput.PAUSE = 0
    pydirectinput.FAILSAFE = False

    ctx = make_context(cfg)
    g = Grabber()

    click_pos = cfg["click_pos"]   # None=原地点击(光标由你自己停在不影响区，bot 不动鼠标)

    state = {"running": False, "quit": False}
    keyboard.add_hotkey("f8", lambda: state.update(running=not state["running"]))
    keyboard.add_hotkey("f9", lambda: state.update(quit=True))

    print("就绪。把鼠标放到游戏窗口内 -> 按 F8 开始/暂停，F9 退出。")
    if debug:
        print("debug 窗口已开，可据此微调 config.json 的颜色/半径带/阈值。")

    prev_angle, prev_t = None, None
    omega = 0.0
    last_click = 0.0
    boosting = False
    boost_cfg = cfg["boost"]

    def set_boost(on):
        nonlocal boosting
        if on != boosting:
            (pydirectinput.mouseDown if on else pydirectinput.mouseUp)(button="right")
            boosting = on

    last_log = 0.0
    try:
        while not state["quit"]:
            now = time.perf_counter()

            if not state["running"]:
                set_boost(False)
                if debug and (cv2.waitKey(1) & 0xFF == 27):
                    break
                time.sleep(0.02)
                continue

            frame = g.grab(ctx["region"])
            pointer, score, bonus = detect(frame, ctx)

            if pointer is not None:
                if prev_angle is not None and prev_t is not None:
                    dt = now - prev_t
                    if dt > 0:
                        w = ang_diff(pointer, prev_angle) / dt
                        omega = 0.5 * w + 0.5 * omega       # 轻度平滑，反向时也能较快跟上
                prev_angle, prev_t = pointer, now

                should_click, nearest = choose_and_decide(pointer, omega, score, bonus, cfg)

                if boost_cfg["enabled"] and nearest is not None and nearest > boost_cfg["release_deg"]:
                    set_boost(True)        # 离目标远 -> 加速
                else:
                    set_boost(False)       # 临近/无目标 -> 松开保精度

                if should_click and (now - last_click) >= cfg["min_click_interval"]:
                    set_boost(False)                       # 点击瞬间确保不在加速
                    if click_pos:
                        pydirectinput.click(click_pos[0], click_pos[1], button="left")
                    else:
                        pydirectinput.click(button="left")  # 原地点击，不移动光标
                    last_click = now
            else:
                prev_angle, prev_t = None, None
                set_boost(False)

            if now - last_log > 1.0:
                last_log = now
                print(f"\rptr={'-' if pointer is None else f'{pointer:6.1f}'} "
                      f"w={omega:7.1f}/s bonus={len(bonus)} score={len(score)} "
                      f"boost={'Y' if boosting else 'n'}   ", end="")

            if debug:
                cv2.imshow("auto_unlock-debug", draw_debug(frame, ctx, pointer, score, bonus, boosting))
                if cv2.waitKey(1) & 0xFF == 27:
                    break
    finally:
        set_boost(False)
        keyboard.clear_all_hotkeys()
        if debug:
            cv2.destroyAllWindows()
        print("\n已退出。")


def draw_debug(frame, ctx, pointer, score, bonus, boosting):
    img = frame.copy()
    cfg = ctx["cfg"]
    cx, cy = cfg["center"]
    l, t = ctx["region"]["left"], ctx["region"]["top"]
    cxl, cyl = int(cx - l), int(cy - t)
    cv2.circle(img, (cxl, cyl), int(cfg["r_inner"]), (90, 90, 90), 1)
    cv2.circle(img, (cxl, cyl), int(cfg["r_outer"]), (90, 90, 90), 1)
    R = int((cfg["r_inner"] + cfg["r_outer"]) / 2)

    def put_arc(bars, color):
        for b in bars:
            a = np.deg2rad(b["center"])
            cv2.circle(img, (int(cxl + R * np.cos(a)), int(cyl + R * np.sin(a))), 5, color, -1)

    put_arc(score, (0, 255, 255))     # 黄
    put_arc(bonus, (255, 200, 0))     # 蓝(加时条)
    if pointer is not None:
        a = np.deg2rad(pointer)
        cv2.line(img, (cxl, cyl),
                 (int(cxl + cfg["r_outer"] * np.cos(a)), int(cyl + cfg["r_outer"] * np.sin(a))),
                 (255, 120, 0), 2)     # 蓝指针
    cv2.putText(img, f"BOOST {'ON' if boosting else 'off'}", (8, 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    return img


# ----------------------------- 标定 -----------------------------
def calibrate():
    g = Grabber()
    mon = g.primary()
    print("标定：3 秒后抓取主屏画面，请先把游戏切到前台、并让画面里出现蓝指针+黄/蓝弧条 ...")
    time.sleep(3)
    frame = g.grab(mon)

    H, W = frame.shape[:2]
    scale = min(1.0, 1100.0 / max(W, H))
    disp_w, disp_h = int(W * scale), int(H * scale)

    steps = [
        ("点 [表盘圆心]", "center"),
        ("点 [外环边缘]", "r_out"),
        ("点 [内环边缘]", "r_in"),
        ("点 [蓝色指针] (没有可按 s 跳过)", "blue"),
        ("点 [黄色高亮条] (没有可按 s 跳过)", "yellow"),
    ]
    picks = {}
    idx = [0]
    hsv_full = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    def to_orig(mx, my):
        return int(mx / scale + mon["left"]), int(my / scale + mon["top"])

    def on_mouse(event, mx, my, flags, _):
        if event != cv2.EVENT_LBUTTONDOWN or idx[0] >= len(steps):
            return
        key = steps[idx[0]][1]
        if key in ("center", "r_out", "r_in"):
            picks[key] = to_orig(mx, my)
        else:
            iy, ix = int(my / scale), int(mx / scale)
            patch = hsv_full[max(0, iy - 2):iy + 3, max(0, ix - 2):ix + 3].reshape(-1, 3)
            med = np.median(patch, axis=0)
            picks[key] = build_hsv_ranges(med[0], med[1], med[2])
            print(f"  {key} 采样 HSV={med.astype(int).tolist()}")
        idx[0] += 1

    win = "calibrate (左键依次点击; s 跳过当前; r 重抓; Enter 保存; Esc 取消)"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)

    while True:
        disp = cv2.resize(frame, (disp_w, disp_h))
        tip = (f"[{idx[0]+1}/{len(steps)}] {steps[idx[0]][0]}"
               if idx[0] < len(steps) else "完成: Enter 保存 / r 重抓 / Esc 取消")
        cv2.putText(disp, tip, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.imshow(win, disp)
        k = cv2.waitKey(20) & 0xFF
        if k == ord("s") and idx[0] < len(steps):
            print(f"  跳过 {steps[idx[0]][1]}")
            idx[0] += 1
        elif k == ord("r"):
            print("重抓画面 ...")
            time.sleep(2)
            frame = g.grab(mon)
            hsv_full = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            picks.clear()
            idx[0] = 0
        elif k == 13:    # Enter
            if all(j in picks for j in ("center", "r_out", "r_in")):
                break
            print("！圆心/外环/内环 必须全部点击。")
        elif k == 27:    # Esc
            cv2.destroyAllWindows()
            sys.exit("已取消标定。")

    cv2.destroyAllWindows()

    cx, cy = picks["center"]
    r_out = int(round(np.hypot(picks["r_out"][0] - cx, picks["r_out"][1] - cy)))
    r_in = int(round(np.hypot(picks["r_in"][0] - cx, picks["r_in"][1] - cy)))
    r_in, r_out = sorted((r_in, r_out))

    cfg = dict(DEFAULTS)
    cfg["center"] = [int(cx), int(cy)]
    cfg["r_inner"] = max(1, r_in)
    cfg["r_outer"] = r_out
    cfg["colors"] = {k: picks.get(k, DEFAULT_COLORS[k]) for k in DEFAULT_COLORS}
    save_config(cfg)
    print(f"圆心=({cx},{cy}) 内径={r_in} 外径={r_out}")
    print("建议先用 --debug 跑一遍看识别准不准：python auto_unlock.py --debug")


# ----------------------------- 入口 -----------------------------
def main():
    ap = argparse.ArgumentParser(description="轮盘撬锁 自动辅助")
    ap.add_argument("--calibrate", action="store_true", help="标定圆心/半径/颜色")
    ap.add_argument("--debug", action="store_true", help="运行时显示识别画面")
    args = ap.parse_args()

    if args.calibrate:
        calibrate()
    else:
        run(load_config(), debug=args.debug)


if __name__ == "__main__":
    main()
