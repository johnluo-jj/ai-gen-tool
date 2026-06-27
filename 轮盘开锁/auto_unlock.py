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
    "blue":   [[[92, 90, 120], [125, 255, 255]]],    # 蓝指针
    "bonus":  [[[92, 90, 120], [125, 255, 255]]],    # 蓝加时条(默认同蓝指针；建议标定单独取色)
    "yellow": [[[18, 110, 130], [34, 255, 255]]],    # 黄高亮条
}

DEFAULTS = {
    "latency": 0.05,            # 总延迟(秒)：截屏+处理+系统点击；命中率主要靠它调
    "min_click_interval": 0.16, # 两次点击最小间隔(秒)，防抖
    "fire_base_tol": 2.5,       # 瞄准弧条中心的基础角度容差(度)
    "pointer_guard_deg": 8,     # 找蓝加时条时排除指针±该角度，避免蓝针被当成加时条
    "pointer_min_px": 8,        # 指针被认定所需最少像素
    "bar_min_px": 5,            # 某角度上被算作"条"所需最少像素
    "bar_min_arc": 4,           # 弧条最小角宽(度)：放小以便识别"逐渐变细"的条
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


def fit_circle(points):
    """最小二乘(代数法)拟合圆，返回 (cx, cy, R)。多点拟合可抹平单点点击误差。"""
    p = np.asarray(points, dtype=np.float64)
    x, y = p[:, 0], p[:, 1]
    A = np.column_stack([2 * x, 2 * y, np.ones(len(x))])
    sol, *_ = np.linalg.lstsq(A, x * x + y * y, rcond=None)
    a, b, c = sol
    return float(a), float(b), float(np.sqrt(max(c + a * a + b * b, 0.0)))


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
def resolve_colors(cfg):
    """整理颜色：bonus 缺省时回退到 blue(老配置不必重标也能用)。"""
    c = dict(cfg.get("colors", {}))
    if "bonus" not in c:
        c["bonus"] = c.get("blue", DEFAULT_COLORS["bonus"])
    for k in DEFAULT_COLORS:
        c.setdefault(k, DEFAULT_COLORS[k])
    return c


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
        "colors": resolve_colors(cfg),
        "cfg": cfg,
    }


def color_mask(hsv, ranges, band_u8):
    mask = None
    for lo, hi in ranges:
        m = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
        mask = m if mask is None else cv2.bitwise_or(mask, m)
    return cv2.bitwise_and(mask, band_u8)


def bars_from_mask(angle_int, mask, ctx, exclude_angle=None, exclude_half=0.0):
    a = angle_int[mask > 0]
    if a.size == 0:
        return []
    hist = np.bincount(a, minlength=360).astype(np.int64)
    if exclude_angle is not None and exclude_half > 0:   # 抹掉指针附近，避免蓝针被当成条
        c, h = int(round(exclude_angle)) % 360, int(exclude_half)
        hist[(np.arange(c - h, c + h + 1) % 360)] = 0
    active = hist >= ctx["cfg"]["bar_min_px"]
    bars = []
    for start, length in circular_runs(active):
        if ctx["cfg"]["bar_min_arc"] <= length < 350:   # 太窄=噪点；太宽=误检整圈
            bars.append({"center": (start + length / 2.0) % 360.0, "len": float(length)})
    return bars


def detect(frame_bgr, ctx):
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    cols = ctx["colors"]

    # 指针：蓝色 ∩ 内圈带
    pmask = color_mask(hsv, cols["blue"], ctx["inner_u8"])
    psel = pmask > 0
    pointer = circular_mean_deg(ctx["angle"][psel]) if int(psel.sum()) >= ctx["cfg"]["pointer_min_px"] else None

    # 蓝加时条：外圈带，并排除指针附近(蓝针穿过外圈会留细线)
    bonus = bars_from_mask(ctx["angle_int"], color_mask(hsv, cols["bonus"], ctx["outer_u8"]), ctx,
                           exclude_angle=pointer, exclude_half=ctx["cfg"]["pointer_guard_deg"])
    # 黄高亮条：外圈带(与蓝色不冲突，无需排除)
    score = bars_from_mask(ctx["angle_int"], color_mask(hsv, cols["yellow"], ctx["outer_u8"]), ctx)
    return pointer, score, bonus


# ----------------------------- 决策 -----------------------------
def edge_distance(pointer, bar):
    """指针到弧条最近边缘的角距(已在条内则为 0)"""
    return max(0.0, abs(ang_diff(bar["center"], pointer)) - bar["len"] / 2.0)


def choose_and_decide(pointer, omega, dt, score, bonus, cfg):
    """瞄准弧条"中心"：当(含延迟提前量的)预测点扫到中心时点击。蓝色加时条优先。
    瞄中心而非整条，可抗"弧长逐渐变细"——边缘会缩、中心不动。
    返回 (should_click, nearest_edge_dist)。"""
    predicted = pointer + omega * cfg["latency"]
    fire_tol = max(cfg["fire_base_tol"], 0.6 * abs(omega) * dt)   # 至少覆盖一帧角位移，避免跨过中心

    should_click = False
    for bar in bonus + score:                       # 蓝条优先(放前面)
        err = ang_diff(predicted, bar["center"])                       # 预测点相对中心
        approaching = (omega * ang_diff(bar["center"], pointer)) > 0   # 中心在前进方向上
        if abs(err) <= fire_tol and (approaching or abs(err) <= cfg["fire_base_tol"]):
            should_click = True
            break

    nearest = min((edge_distance(pointer, b) for b in bonus + score), default=None)
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
                dt = (now - prev_t) if prev_t is not None else 0.016
                if prev_angle is not None and dt > 0:
                    w = ang_diff(pointer, prev_angle) / dt
                    omega = 0.5 * w + 0.5 * omega           # 轻度平滑，反向时也能较快跟上
                prev_angle, prev_t = pointer, now

                should_click, nearest = choose_and_decide(pointer, omega, dt, score, bonus, cfg)

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

    def grab_check(wait):
        for s in range(wait, 0, -1):
            print(f"\r  {s} 秒后抓取主屏画面 ...   ", end="")
            time.sleep(1)
        print()
        f = g.grab(mon)
        if float(f.mean()) < 8.0:
            print("⚠ 抓到的画面接近全黑：游戏很可能是『独占全屏』(mss 截不到)。"
                  "请改成『窗口化 / 无边框窗口』模式，再在标定窗口里按 r 重抓。")
        return f

    print("标定：先把游戏切到前台，让画面出现蓝指针 + 黄/蓝弧条。")
    frame = grab_check(3)

    H, W = frame.shape[:2]
    scale = min(1.0, 1100.0 / max(W, H))
    disp_w, disp_h = int(W * scale), int(H * scale)

    # ring 步多点拟合圆(抹平点击误差)；color 步单点取色(位置无所谓，只采颜色)
    steps = [
        {"key": "outer",  "mode": "ring",  "tip": "沿[外环]边缘点 4~6 个点，按 n 下一步"},
        {"key": "inner",  "mode": "ring",  "tip": "沿[内环]边缘点 4~6 个点，按 n 下一步"},
        {"key": "blue",   "mode": "color", "tip": "点[蓝色指针]取色 (s 跳过)"},
        {"key": "bonus",  "mode": "color", "tip": "点[蓝色加时条]取色 (s 跳过)"},
        {"key": "yellow", "mode": "color", "tip": "点[黄色高亮条]取色 (s 跳过)"},
    ]
    picks = {}
    idx = [0]
    hsv_full = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    def to_orig(mx, my):
        return int(mx / scale + mon["left"]), int(my / scale + mon["top"])

    def to_disp(ax, ay):
        return int((ax - mon["left"]) * scale), int((ay - mon["top"]) * scale)

    def on_mouse(event, mx, my, flags, _):
        if event != cv2.EVENT_LBUTTONDOWN or idx[0] >= len(steps):
            return
        st = steps[idx[0]]
        if st["mode"] == "ring":
            picks.setdefault(st["key"], []).append(to_orig(mx, my))   # 累积，不自动前进
        else:
            iy, ix = int(my / scale), int(mx / scale)
            patch = hsv_full[max(0, iy - 2):iy + 3, max(0, ix - 2):ix + 3].reshape(-1, 3)
            med = np.median(patch, axis=0)
            picks[st["key"]] = build_hsv_ranges(med[0], med[1], med[2])
            print(f"  {st['key']} 采样 HSV={med.astype(int).tolist()}")
            idx[0] += 1

    win = "calibrate (左键点击; n 下一步; s 跳过取色; r 重抓; Enter 保存; Esc 取消)"

    def open_win():
        cv2.namedWindow(win)
        try:
            cv2.setWindowProperty(win, cv2.WND_PROP_TOPMOST, 1)   # 置顶，避免被游戏挡住
        except Exception:
            pass
        cv2.moveWindow(win, 0, 0)
        cv2.setMouseCallback(win, on_mouse)

    open_win()
    print("标定窗口已弹出并置顶；若仍被游戏挡住，Alt+Tab 切到 'calibrate' 窗口。画面是冻结的，可慢慢点。")
    print("外环/内环各点 4~6 个点(越多越准)，会实时画出拟合圆；贴住环线后按 n 进入下一步。")

    def ring_ok():
        return len(picks.get("outer", [])) >= 3 and len(picks.get("inner", [])) >= 3

    while True:
        disp = cv2.resize(frame, (disp_w, disp_h))
        done = idx[0] >= len(steps)
        # 画已点的环点 + 拟合圆预览
        for key, color in (("outer", (0, 200, 255)), ("inner", (0, 255, 120))):
            pts = picks.get(key, [])
            for ax, ay in pts:
                cv2.circle(disp, to_disp(ax, ay), 3, color, -1)
            if len(pts) >= 3:
                try:
                    a, b, R = fit_circle(pts)
                    cv2.circle(disp, to_disp(a, b), int(R * scale), color, 1)
                except Exception:
                    pass
        if done:
            tip = "完成: Enter 保存 / r 重抓 / Esc 取消"
        else:
            st = steps[idx[0]]
            cnt = f"  已点 {len(picks.get(st['key'], []))} 个" if st["mode"] == "ring" else ""
            tip = f"[{idx[0]+1}/{len(steps)}] {st['tip']}{cnt}"
        cv2.putText(disp, tip, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.imshow(win, disp)

        k = cv2.waitKey(20) & 0xFF
        if k == ord("n") and not done:
            if steps[idx[0]]["mode"] == "ring" and len(picks.get(steps[idx[0]]["key"], [])) < 3:
                print("！该环至少点 3 个点。")
            else:
                idx[0] += 1
        elif k == ord("s") and not done and steps[idx[0]]["mode"] == "color":
            print(f"  跳过 {steps[idx[0]]['key']}")
            idx[0] += 1
        elif k == ord("r"):
            cv2.destroyWindow(win)        # 先关掉标定窗口，免得把它自己截进去
            cv2.waitKey(1)
            frame = grab_check(2)
            hsv_full = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            picks.clear()
            idx[0] = 0
            open_win()
        elif k == 13:    # Enter
            if ring_ok():
                break
            print("！外环/内环 各至少点 3 个点。")
        elif k == 27:    # Esc
            cv2.destroyAllWindows()
            sys.exit("已取消标定。")

    cv2.destroyAllWindows()

    # 外/内环各自拟合圆 -> 取两圆心均值为公共圆心 -> 半径用到该圆心的平均距离
    oa, ob, _ = fit_circle(picks["outer"])
    ia, ib, _ = fit_circle(picks["inner"])
    cx, cy = (oa + ia) / 2.0, (ob + ib) / 2.0

    def radii(pts):
        p = np.asarray(pts, dtype=np.float64)
        return np.hypot(p[:, 0] - cx, p[:, 1] - cy)

    ro, ri = radii(picks["outer"]), radii(picks["inner"])
    r_out, r_in = sorted((ro.mean(), ri.mean()))

    cfg = dict(DEFAULTS)
    cfg["center"] = [int(round(cx)), int(round(cy))]
    cfg["r_inner"] = max(1, int(round(r_in)))
    cfg["r_outer"] = int(round(r_out))
    # DEFAULT_COLORS 只含 blue/bonus/yellow，不含 outer/inner，故天然排除环点
    colors = {k: picks.get(k, DEFAULT_COLORS[k]) for k in DEFAULT_COLORS}
    if "bonus" not in picks and "blue" in picks:
        colors["bonus"] = picks["blue"]        # 没单独取蓝加时条，则沿用蓝指针的蓝
    cfg["colors"] = colors
    save_config(cfg)
    print(f"圆心=({cfg['center'][0]},{cfg['center'][1]})  "
          f"外环 R={ro.mean():.1f}±{ro.std():.1f}  内环 R={ri.mean():.1f}±{ri.std():.1f}  "
          f"(±越小越准)")
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
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
