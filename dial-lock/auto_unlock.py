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

DEFAULTS = {
    "latency": 0.05,            # 总延迟(秒)：截屏+处理+系统点击；命中率主要靠它调
    "min_click_interval": 0.16, # 两次点击最小间隔(秒)，防抖
    "arc_salience": 22,         # 显著阈值：环上像素相对底环的"亮+彩"偏离≥此值算"有东西"(抗褪色)
    "bar_min_px": 5,            # 某角度上算"有东西"所需最少像素
    "pointer_max_arc": 6,       # 指针/扇形分界(度)：≤此值的细段=指针，>此值=扇形(实测指针恒定4~5°，留+1°余量)
    "bar_max_arc": 35,          # 扇形角宽上限(度)：扇形基本不超过~30°，更宽的判为非扇形(误检/反光)
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

    track = (radius >= r_in) & (radius <= r_out)        # 整条环轨道(指针 + 扇形都在这)

    return {
        "region": region,
        "angle": angle,
        "angle_int": angle.astype(np.int32),
        "track_bool": track,
        "cfg": cfg,
    }


def detect(frame_bgr, ctx):
    """把环带里所有"显著径向段"切出来，按角宽分类：
      细段(≤pointer_max_arc) = 指针；宽段(pointer_max_arc~bar_max_arc) = 扇形(无死区)。
    指针落在某扇形角度内即命中；指针并入扇形时没有独立细段(决策里按"已在扇形内"处理)。
    返回 (pointer, sectors)。"""
    cfg = ctx["cfg"]
    lab = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2Lab)
    L = lab[:, :, 0].astype(np.float32)
    A = lab[:, :, 1].astype(np.float32)
    Bc = lab[:, :, 2].astype(np.float32)
    diag = {}

    pointer = None
    sectors = []
    tr = ctx["track_bool"]
    if tr.any():
        Lt, At, Bt = L[tr], A[tr], Bc[tr]
        sal = np.maximum(0.0, Lt - np.median(Lt)) + np.sqrt((At - np.median(At)) ** 2 + (Bt - np.median(Bt)) ** 2)
        sel = sal >= cfg["arc_salience"]
        diag["trkMaxSal"] = int(sal.max())                  # 环上最大显著度(看 arc_salience 该多少)
        diag["salPx"] = int(sel.sum())                      # 超过 arc_salience 的像素数
        if sel.any():
            cnt = np.bincount(ctx["angle_int"][tr][sel], minlength=360)
            best_ptr_px = -1
            widths = []
            for start, length in circular_runs(cnt >= cfg["bar_min_px"]):
                widths.append(int(length))
                center = (start + length / 2.0) % 360.0
                if length <= cfg["pointer_max_arc"]:                          # 细 -> 指针(取像素最多的细段)
                    px = int(cnt[(np.arange(start, start + length) % 360)].sum())
                    if px > best_ptr_px:
                        best_ptr_px, pointer = px, center
                elif length <= cfg["bar_max_arc"]:                            # 宽 -> 扇形(下界=pointer_max_arc，无死区)
                    sectors.append({"center": center, "len": float(length)})
            diag["runW"] = sorted(widths, reverse=True)     # 所有段的角宽(看 pointer_max_arc 该多少)
    diag["ptr"] = "-" if pointer is None else int(pointer)
    diag["nSec"] = len(sectors)
    ctx["diag"] = diag
    return pointer, sectors


# ----------------------------- 决策 -----------------------------
def edge_distance(pointer, sector):
    """指针到扇形最近边缘的角距(已在扇形内则为 0)"""
    return max(0.0, abs(ang_diff(sector["center"], pointer)) - sector["len"] / 2.0)


def choose_and_decide(pointer, omega, dt, sectors, cfg):
    """命中即点，不分黄/蓝、不排优先级。返回 (should_click, nearest_edge_dist)。
    - 指针有独立细段：其(含延迟提前量)落入某扇形角度区间就点；
    - 指针没切出独立细段：说明已并入某扇形(指针很细、被宽段吞掉)，有扇形即判命中。"""
    if pointer is None:
        return len(sectors) > 0, None

    predicted = pointer + omega * cfg["latency"]
    guard = 0.5 * abs(omega) * dt                # 高速时半帧角位移，避免两帧间跨过窄扇形而漏判
    should_click = False
    nearest = None
    for s in sectors:
        if abs(ang_diff(predicted, s["center"])) <= s["len"] / 2.0 + guard:
            should_click = True
        e = edge_distance(pointer, s)
        nearest = e if nearest is None else min(nearest, e)
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

    def toggle():
        state["running"] = not state["running"]
        print(f"\n{'▶ 运行中' if state['running'] else '⏸ 已暂停'}")

    keyboard.add_hotkey("f8", toggle)
    keyboard.add_hotkey("f9", lambda: state.update(quit=True))

    print("就绪。把鼠标放到游戏窗口内 -> 按 F8 开始/暂停，F9 退出。")
    print("（按 F8 若没看到 “▶ 运行中”，多半是热键没被收到：请用管理员身份运行本脚本。）")
    if debug:
        print("debug：控制台会多打 runW/trkMaxSal/salPx/ptr/nSec，用于据实调阈值。")

    prev_angle, prev_t = None, None
    omega = 0.0
    last_click = 0.0
    clicks = 0
    boosting = False
    boost_cfg = cfg["boost"]

    def set_boost(on):
        nonlocal boosting
        if on != boosting:
            (pydirectinput.mouseDown if on else pydirectinput.mouseUp)(button="right")
            boosting = on

    last_log, frames = 0.0, 0
    try:
        while not state["quit"]:
            now = time.perf_counter()

            if not state["running"]:
                set_boost(False)
                if debug and (cv2.waitKey(1) & 0xFF == 27):
                    break
                time.sleep(0.02)
                continue

            frames += 1
            frame = g.grab(ctx["region"])
            pointer, sectors = detect(frame, ctx)

            dt = (now - prev_t) if prev_t is not None else 0.016
            if pointer is not None:
                if prev_angle is not None and dt > 0:
                    w = ang_diff(pointer, prev_angle) / dt
                    omega = 0.5 * w + 0.5 * omega           # 轻度平滑，反向时也能较快跟上
                prev_angle, prev_t = pointer, now
            else:
                prev_angle, prev_t, omega = None, None, 0.0  # 指针并入扇形/丢失：不更新速度

            should_click, nearest = choose_and_decide(pointer, omega, dt, sectors, cfg)

            if boost_cfg["enabled"] and nearest is not None and nearest > boost_cfg["release_deg"]:
                set_boost(True)            # 离目标远 -> 加速
            else:
                set_boost(False)           # 临近/无目标 -> 松开保精度

            if should_click and (now - last_click) >= cfg["min_click_interval"]:
                set_boost(False)                           # 点击瞬间确保不在加速
                if click_pos:
                    pydirectinput.click(click_pos[0], click_pos[1], button="left")
                else:
                    pydirectinput.click(button="left")      # 原地点击，不移动光标
                last_click = now
                clicks += 1

            if now - last_log > 1.0:
                fps = frames / (now - last_log)
                last_log, frames = now, 0
                msg = (f"\rfps={fps:4.0f} ptr={'-' if pointer is None else f'{pointer:6.1f}'} "
                       f"w={omega:7.1f}/s nSec={len(sectors)} hit={'Y' if should_click else 'n'} "
                       f"clicks={clicks} boost={'Y' if boosting else 'n'}")
                if debug:
                    msg += "  " + " ".join(f"{k}={v}" for k, v in ctx.get("diag", {}).items())
                print(msg + "   ", end="")

            if debug:
                cv2.imshow("auto_unlock-debug", draw_debug(frame, ctx, pointer, sectors, boosting, should_click))
                if cv2.waitKey(1) & 0xFF == 27:
                    break
    finally:
        set_boost(False)
        keyboard.clear_all_hotkeys()
        if debug:
            cv2.destroyAllWindows()
        print("\n已退出。")


def draw_debug(frame, ctx, pointer, sectors, boosting, should_click):
    img = frame.copy()
    cfg = ctx["cfg"]
    cx, cy = cfg["center"]
    l, t = ctx["region"]["left"], ctx["region"]["top"]
    cxl, cyl = int(cx - l), int(cy - t)
    cv2.circle(img, (cxl, cyl), int(cfg["r_inner"]), (90, 90, 90), 1)
    cv2.circle(img, (cxl, cyl), int(cfg["r_outer"]), (90, 90, 90), 1)
    R = int((cfg["r_inner"] + cfg["r_outer"]) / 2)

    # 命中(should_click)时扇形画红，否则黄。
    # 注意：指针压到扇形上时多半会并入扇形(pointer=None)——那一刻正是命中，
    # 所以按 should_click 上色(它已处理"pointer=None+有扇形=命中")，而不是看指针是否还独立可见。
    for s in sectors:
        color = (0, 0, 255) if should_click else (0, 220, 220)
        a0, a1 = s["center"] - s["len"] / 2.0, s["center"] + s["len"] / 2.0
        cv2.ellipse(img, (cxl, cyl), (R, R), 0, a0, a1, color, 3)
    if pointer is not None:           # 指针(细线)；并入扇形时无独立指针，不画线
        a = np.deg2rad(pointer)
        cv2.line(img, (cxl, cyl),
                 (int(cxl + cfg["r_outer"] * np.cos(a)), int(cyl + cfg["r_outer"] * np.sin(a))),
                 (255, 120, 0), 2)

    ptxt = "merged/None" if pointer is None else f"{pointer:.0f}"
    cv2.putText(img, f"ptr={ptxt}  nSec={len(sectors)}  boost={'on' if boosting else 'off'}",
                (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 1, cv2.LINE_AA)
    if should_click:                  # 真实点击决策：命中即大字提示
        cv2.putText(img, "CLICK", (8, 54), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2, cv2.LINE_AA)
    diag = ctx.get("diag", {})        # 诊断值叠到画面底部，方便直接截图给我看
    if diag:
        h = img.shape[0]
        cv2.putText(img, f"runW={diag.get('runW')}", (8, h - 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(img, f"trkMaxSal={diag.get('trkMaxSal')}  salPx={diag.get('salPx')}", (8, h - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
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

    print("标定：先把游戏切到前台，露出表盘(画面里有没有弧条都行，本步只标圆环几何)。")
    frame = grab_check(3)

    H, W = frame.shape[:2]
    scale = min(1.0, 0.92 * mon["height"] / H, 0.92 * mon["width"] / W)   # 放大到接近全屏，刻度更大
    disp_w, disp_h = int(W * scale), int(H * scale)

    # 只标几何：外/内环各多点拟合圆(抹平点击误差)。颜色不再标定(运行时 Lab 自适应判别)
    steps = [
        {"key": "outer", "tip": "沿[外环]边缘点 4~6 个点，按 n 下一步"},
        {"key": "inner", "tip": "沿[内环]边缘点 4~6 个点，按 n 下一步"},
    ]
    picks = {}
    idx = [0]

    def to_orig(mx, my):
        return int(mx / scale + mon["left"]), int(my / scale + mon["top"])

    def to_disp(ax, ay):
        return int((ax - mon["left"]) * scale), int((ay - mon["top"]) * scale)

    def on_mouse(event, mx, my, flags, _):
        if event != cv2.EVENT_LBUTTONDOWN or idx[0] >= len(steps):
            return
        picks.setdefault(steps[idx[0]]["key"], []).append(to_orig(mx, my))   # 累积，不自动前进

    win = "calibrate (左键点击环边; n 下一步; r 重抓; Enter 保存; Esc 取消)"

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
                cv2.circle(disp, to_disp(ax, ay), 2, color, -1)
            if len(pts) >= 3:
                try:
                    a, b, R = fit_circle(pts)
                    cv2.circle(disp, to_disp(a, b), int(R * scale), color, 1, cv2.LINE_AA)  # 1px 抗锯齿细线
                except Exception:
                    pass
        if done:
            tip = "完成: Enter 保存 / r 重抓 / Esc 取消"
        else:
            st = steps[idx[0]]
            tip = f"[{idx[0]+1}/{len(steps)}] {st['tip']}  已点 {len(picks.get(st['key'], []))} 个"
        cv2.putText(disp, tip, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.imshow(win, disp)

        k = cv2.waitKey(20) & 0xFF
        if k == ord("n") and not done:
            if len(picks.get(steps[idx[0]]["key"], [])) < 3:
                print("！该环至少点 3 个点。")
            else:
                idx[0] += 1
        elif k == ord("r"):
            cv2.destroyWindow(win)        # 先关掉标定窗口，免得把它自己截进去
            cv2.waitKey(1)
            frame = grab_check(2)
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
    save_config(cfg)
    print(f"圆心=({cfg['center'][0]},{cfg['center'][1]})  "
          f"外环 R={ro.mean():.1f}±{ro.std():.1f}  内环 R={ri.mean():.1f}±{ri.std():.1f}  "
          f"(±越小越准)")
    print("建议先用 --debug 跑一遍看识别准不准：python auto_unlock.py --debug")


# ----------------------------- 入口 -----------------------------
def main():
    ap = argparse.ArgumentParser(description="轮盘撬锁 自动辅助")
    ap.add_argument("--calibrate", action="store_true", help="标定表盘圆环几何(只点环)")
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
