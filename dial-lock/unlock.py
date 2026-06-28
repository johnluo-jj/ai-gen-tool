#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
轮盘撬锁 自动 bot（全新实现，取代旧 auto_unlock/bot）。

为什么之前「指针落入扇形」总判错、这版怎么修：
  真机里**条是固定的、只有指针在转**(实测每条会在某角度稳定停留几十帧)。旧版无状态、
  每帧重新找条, 一旦指针扫到条上, 为了不把指针误当成条就得把指针那一角挖掉, 结果**恰好在
  该点击的瞬间把条也挖没了** —— 这正是命中判不出来的根因。
  本版改成「**记住条**」：指针不在条上时干净地检测并登记条的位置/颜色(BarTracker),
  指针扫进**已登记**的条时就点 —— 哪怕这一帧条被指针盖住看不见, 也照点。

另外两个旧坑一并修掉：
  • 角速度尖刺：旧版用相邻两帧角差/dt, 微小dt会炸到数千°/s。本版用 0.2s 基线测速 + 物理封顶。
  • 误点惩罚：游戏「落空会暂时无法撬锁」, 故**精度优先**——只点已确认的条, 点完给该条上冷却防重复点。

检测逻辑全部复用 detector.py(已在真机帧离线验证)。

用法(PowerShell, 建议管理员运行)：
  python unlock.py --calibrate                 # 标定圆心/半径 -> config.json(换分辨率/窗口要重标)
  python unlock.py --probe                      # 抓一帧自检几何对齐, 存 _probe.jpg, 不点击
  python unlock.py --sim captures/run_xxxx      # 离线: 在录好的帧上跑完整命中逻辑, 出 _sim/ 报告(不点屏幕)
  python unlock.py --debug                      # 实机+显示识别画面
  python unlock.py                              # 实机正式跑; F8 开/暂停, F9 退出
"""

import os
import sys
import json
import time
import argparse
import ctypes
from collections import deque

import numpy as np
import cv2

from detector import Geom, detect, draw, DEFAULT_P, ang_diff, circular_runs  # noqa: F401

if sys.platform == "win32":
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# 半径分带占外轨道边缘半径 R 的比例(真机 R≈182 时正好得到 detector.DEFAULT_P 的绝对值)
BAND_FRAC = {"ptr_in": 0.58, "ptr_out": 0.66, "bar_in": 0.74, "bar_out": 0.97}

DEFAULT_CFG = {
    "center": [956, 700],          # 圆心(真机采集真值; --calibrate 可重标)
    "R": 182,                      # 外轨道边缘半径
    "latency": 0.05,               # 点击延迟提前量(秒): 总点早->调小, 总点晚->调大
    "min_click_interval": 0.18,    # 两次点击最小间隔(秒)
    "click_hold": 0.05,            # 左键按住时长(秒): 瞬点会被游戏吞, 按住~50ms才认
    "hit_margin": 1.5,             # 命中判定额外角余量(度): 小余量, 过大会在窄条上提前点(易落空)
    "speed_cap": 200.0,            # 角速度封顶(°/s): 针实测最快~130, 超出必是测速尖刺
    "lead_max": 15.0,              # 延迟提前量换算成角度的上限(度)
    "conf_min": 4.0,               # 指针置信下限(峰/均); 低于此不更新命中(防糊帧)
    "boost": {"enabled": False, "release_deg": 35.0},  # 离最近条>此角->按住右键加速(默认关, 稳为先)
    "click_pos": None,             # 左键坐标[x,y]; null=原地点(不动光标)
    "det": {},                     # 覆盖 detector 的检测参数(留空=用默认)
}


# ----------------------------- 配置 / 几何 -----------------------------
def make_P(cfg):
    """由 R 和比例算出检测器半径参数, 叠加用户 det 覆盖。"""
    R = cfg["R"]
    P = dict(DEFAULT_P)
    for k, fr in BAND_FRAC.items():
        P[k] = int(round(fr * R))
    P.update(cfg.get("det", {}))
    return P


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
        cfg["boost"] = {**DEFAULT_CFG["boost"], **cfg.get("boost", {})}
    return cfg


def save_cfg(cfg):
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"已保存 -> {CFG_PATH}")


def fit_circle(points):
    """代数最小二乘拟合圆 -> (cx, cy, R)。多点拟合抹平单点误差。"""
    p = np.asarray(points, np.float64)
    x, y = p[:, 0], p[:, 1]
    A = np.column_stack([2 * x, 2 * y, np.ones(len(x))])
    sol, *_ = np.linalg.lstsq(A, x * x + y * y, rcond=None)
    a, b, c = sol
    return float(a), float(b), float(np.sqrt(max(c + a * a + b * b, 0.0)))


# ----------------------------- 测速(防尖刺) -----------------------------
class SpeedEstimator:
    """用 ~window 秒的角度基线估角速度(°/s), 物理封顶。避免微小dt除法尖刺。"""
    def __init__(self, cap=200.0, window=0.2):
        self.cap, self.window = cap, window
        self.hist = deque()
        self.cum, self.last = None, None

    def update(self, ang, now):
        if ang is None:
            self.hist.clear(); self.cum = self.last = None
            return 0.0
        self.cum = ang if self.last is None else self.cum + ang_diff(ang, self.last)
        self.last = ang
        self.hist.append((now, self.cum))
        while len(self.hist) > 2 and now - self.hist[0][0] > self.window:
            self.hist.popleft()
        t0, a0 = self.hist[0]
        if len(self.hist) >= 3 and now - t0 > 0.03:
            sp = (self.cum - a0) / (now - t0)
            return max(-self.cap, min(self.cap, sp))
        return 0.0


# ----------------------------- 条追踪(核心) -----------------------------
class BarTracker:
    """记住「固定的条」。指针扫过把条挖掉看不见, 但条仍在册, 故能在指针压上条的瞬间命中。

    可点判据用「新鲜度」而非「指针是否盖住」: 真条在指针靠近前一直被检到(最后一次≈0.2s前),
    已消失的「幽灵条」最后一次检到要久得多 —— 故 now-last<=fresh 能干净区分, 避免对幽灵条误点。"""
    def __init__(self, fresh=0.35, stale=1.2, match_tol=12.0, min_confirm=2, spent=0.4):
        self.bars = []
        self.fresh, self.stale = fresh, stale
        self.match_tol, self.min_confirm, self.spent = match_tol, min_confirm, spent

    def _match(self, tb, sectors, used):
        for j, s in enumerate(sectors):
            if used[j] or s["color"] != tb["color"]:
                continue
            if abs(ang_diff(s["center"], tb["center"])) <= self.match_tol:
                return j
        return None

    def update(self, sectors, now):
        used = [False] * len(sectors)
        for tb in self.bars:
            j = self._match(tb, sectors, used)
            if j is not None:                                  # 又检到这条 -> 更新位置/颜色/新鲜度
                used[j] = True
                s = sectors[j]
                tb["center"] = (tb["center"] + 0.5 * ang_diff(s["center"], tb["center"])) % 360.0
                tb["len"] = 0.6 * tb["len"] + 0.4 * s["len"]
                tb["color"] = s["color"]
                tb["seen"] = min(tb["seen"] + 1, 99)
                tb["last"] = now
        for j, s in enumerate(sectors):                        # 没匹配上的检测 = 新条
            if not used[j]:
                self.bars.append({"center": s["center"], "len": s["len"], "color": s["color"],
                                  "seen": 1, "last": now, "spent_until": 0.0})
        self.bars = [tb for tb in self.bars if now - tb["last"] <= self.stale]

    def confirmed(self, now):
        return [tb for tb in self.bars if tb["seen"] >= self.min_confirm
                and now >= tb["spent_until"] and now - tb["last"] <= self.fresh]

    def spend(self, tb, now):
        tb["spent_until"] = now + self.spent                   # 点完冷却, 防对同一条重复点(重点=落空惩罚)


# ----------------------------- 命中决策 -----------------------------
def decide(pointer, speed, tracker, now, last_click, cfg):
    """返回要点的条(dict)或 None。"""
    if pointer is None:
        return None
    lead = max(-cfg["lead_max"], min(cfg["lead_max"], speed * cfg["latency"]))
    pred = pointer + lead
    if now - last_click < cfg["min_click_interval"]:
        return None
    best, best_e = None, 1e9
    for tb in tracker.confirmed(now):
        e = abs(ang_diff(pred, tb["center"]))
        if e <= tb["len"] / 2 + cfg["hit_margin"] and e < best_e:
            best, best_e = tb, e
    return best


def nearest_dist(pointer, tracker, now):
    if pointer is None:
        return None
    ds = [max(0.0, abs(ang_diff(pointer, tb["center"])) - tb["len"] / 2) for tb in tracker.confirmed(now)]
    return min(ds) if ds else None


# ----------------------------- 实机运行 -----------------------------
class Grabber:
    def __init__(self):
        import mss
        self.sct = mss.mss()

    def grab(self, region):
        return np.asarray(self.sct.grab(region))[:, :, :3]


def run(cfg, debug=False):
    import pydirectinput
    import keyboard
    pydirectinput.PAUSE = 0
    pydirectinput.FAILSAFE = False

    cx, cy = cfg["center"]
    P = make_P(cfg)
    geom = Geom(cx, cy, P)
    region = geom.region()
    g = Grabber()
    print(f"圆心=({cx},{cy}) R={cfg['R']} ROI={region['width']}x{region['height']} "
          f"指针带=[{P['ptr_in']},{P['ptr_out']}] 条带=[{P['bar_in']},{P['bar_out']}]")

    state = {"running": False, "quit": False}
    keyboard.add_hotkey("f8", lambda: _toggle(state))
    keyboard.add_hotkey("f9", lambda: state.update(quit=True))
    print("就绪: 鼠标放进游戏窗口空白处 -> F8 开始/暂停, F9 退出。(F8无反应=请用管理员运行)")

    speed_est = SpeedEstimator(cfg["speed_cap"])
    tracker = BarTracker()
    last_click, clicks, boosting = 0.0, 0, False

    def set_boost(on):
        nonlocal boosting
        if on != boosting:
            (pydirectinput.mouseDown if on else pydirectinput.mouseUp)(button="right")
            boosting = on

    last_log, frames, ptr_seen = time.perf_counter(), 0, 0
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
            roi = g.grab(region)
            d = detect(roi, geom, P)
            pointer = d["pointer"] if d["conf"] >= cfg["conf_min"] else None
            if pointer is not None:
                ptr_seen += 1
            speed = speed_est.update(pointer, now)
            tracker.update(d["sectors"], now)

            target = decide(pointer, speed, tracker, now, last_click, cfg)
            if target is not None:
                set_boost(False)
                if cfg["click_pos"]:
                    pydirectinput.moveTo(cfg["click_pos"][0], cfg["click_pos"][1])
                pydirectinput.mouseDown(button="left")
                time.sleep(cfg["click_hold"])
                pydirectinput.mouseUp(button="left")
                tracker.spend(target, now)
                last_click, clicks = now, clicks + 1
            else:
                bc = cfg["boost"]
                nd = nearest_dist(pointer, tracker, now)
                set_boost(bool(bc["enabled"] and nd is not None and nd > bc["release_deg"]))

            if now - last_log > 1.0:
                fps = frames / (now - last_log)
                print(f"\rfps={fps:4.0f} ptr={'-' if pointer is None else f'{pointer:5.0f}'} "
                      f"v={speed:6.0f}/s seen={100*ptr_seen/max(1,frames):3.0f}% "
                      f"bars={len(tracker.confirmed(now))} clicks={clicks} "
                      f"boost={'Y' if boosting else 'n'}   ", end="")
                last_log, frames, ptr_seen = now, 0, 0

            if debug:
                ov = draw(roi, geom, P, d, extra=f"v={speed:.0f} clk={clicks}")
                for tb in tracker.confirmed(now):                # 已登记条画细圈
                    a = np.deg2rad(tb["center"])
                    cv2.circle(ov, (int(geom.cxl + (P["bar_in"] + P["bar_out"]) / 2 * np.cos(a)),
                                    int(geom.cyl + (P["bar_in"] + P["bar_out"]) / 2 * np.sin(a))), 4,
                               (0, 220, 0) if target is tb else (0, 140, 0), -1)
                if target is not None:
                    cv2.putText(ov, "CLICK", (6, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 220, 0), 2)
                cv2.imshow("unlock-debug", ov)
                if cv2.waitKey(1) & 0xFF == 27:
                    break
    finally:
        set_boost(False)
        keyboard.clear_all_hotkeys()
        if debug:
            cv2.destroyAllWindows()
        print(f"\n已退出。共点击 {clicks} 次。")


def _toggle(state):
    state["running"] = not state["running"]
    print(f"\n{'▶ 运行中' if state['running'] else '⏸ 已暂停'}")


# ----------------------------- 标定 -----------------------------
def calibrate(cfg):
    import mss
    sct = mss.mss()
    mon = sct.monitors[1]
    print("3 秒后抓主屏一帧用于标定, 请把游戏切到屏幕最前、露出整个圆盘 ...")
    for s in range(3, 0, -1):
        print(f"\r  {s} ...", end=""); time.sleep(1)
    print()
    full = np.asarray(sct.grab(mon))[:, :, :3]
    H, W = full.shape[:2]
    scale = min(1.0, 0.9 * mon["height"] / H, 0.9 * mon["width"] / W)
    disp0 = cv2.resize(full, (int(W * scale), int(H * scale)))
    pts = []

    def on_mouse(ev, x, y, flags, _):
        if ev == cv2.EVENT_LBUTTONDOWN:
            pts.append((x / scale, y / scale))

    win = "calibrate: 沿『暗轨道外缘』点4~6点(黄蓝条所在那圈的外边), n/Enter=完成 r=重来 Esc=取消"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    while True:
        disp = disp0.copy()
        for p in pts:
            cv2.circle(disp, (int(p[0] * scale), int(p[1] * scale)), 4, (0, 0, 255), -1)
        if len(pts) >= 3:
            cx, cy, R = fit_circle(pts)
            cv2.circle(disp, (int(cx * scale), int(cy * scale)), int(R * scale), (0, 255, 0), 1)
            cv2.circle(disp, (int(cx * scale), int(cy * scale)), 3, (0, 255, 0), -1)
        cv2.imshow(win, disp)
        k = cv2.waitKey(20) & 0xFF
        if k in (13, ord("n")) and len(pts) >= 3:
            break
        if k == ord("r"):
            pts.clear()
        if k == 27:
            cv2.destroyAllWindows()
            print("已取消标定。")
            return
    cv2.destroyAllWindows()
    cx, cy, R = fit_circle(pts)
    cfg["center"] = [int(round(cx)), int(round(cy))]
    cfg["R"] = int(round(R))
    save_cfg(cfg)
    print(f"圆心=({cx:.0f},{cy:.0f}) R={R:.0f}。建议接着 `python unlock.py --probe` 核对各层环是否压在轨道上。")


# ----------------------------- 抓一帧自检 -----------------------------
def probe(cfg):
    import mss
    cx, cy = cfg["center"]
    P = make_P(cfg)
    geom = Geom(cx, cy, P)
    sct = mss.mss()
    for s in range(3, 0, -1):
        print(f"\r  {s} 秒后抓一帧自检(切到游戏、露出圆盘) ...", end=""); time.sleep(1)
    print()
    roi = np.asarray(sct.grab(geom.region()))[:, :, :3]
    d = detect(roi, geom, P)
    out = os.path.join(os.path.dirname(CFG_PATH), "_probe.jpg")
    cv2.imwrite(out, draw(roi, geom, P, d), [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"画面均值={roi.mean():.1f} ptr={d['pointer']} conf={d['conf']:.1f} "
          f"sectors={[(s['color'], int(s['center']), int(s['len'])) for s in d['sectors']]}")
    print(f"已存 {out}: 红线=指针, 黄/蓝弧=条, 灰圈=各层半径。核对圆心在表盘中心、灰圈压在轨道上。")
    if roi.mean() < 8:
        print("⚠ 画面接近全黑: 游戏多半独占全屏(mss截不到), 请切窗口化/无边框。")


# ----------------------------- 离线仿真(在录好的帧上验证命中逻辑) -----------------------------
def sim(cfg, run_dir):
    from detector import _load_run
    if not os.path.isdir(run_dir):
        run_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), run_dir)
    frames, (cx, cy) = _load_run(run_dir, f"{cfg['center'][0]},{cfg['center'][1]}")
    P = make_P(cfg)
    geom = Geom(cx, cy, P)
    speed_est = SpeedEstimator(cfg["speed_cap"])
    tracker = BarTracker()
    out = os.path.join(run_dir, "_sim")
    os.makedirs(out, exist_ok=True)
    last_click, clicks = 0.0, []
    trace = []                                  # (t, pointer) 全程指针轨迹, 用于事后核验命中
    saved = {}
    for i, f in enumerate(frames):
        img = cv2.imread(os.path.join(run_dir, f["file"]))
        if img is None:
            continue
        roi = geom.crop(img)
        t = float(f.get("t", i / 30.0))
        d = detect(roi, geom, P)
        pointer = d["pointer"] if d["conf"] >= cfg["conf_min"] else None
        trace.append((t, pointer))
        speed = speed_est.update(pointer, t)
        tracker.update(d["sectors"], t)
        target = decide(pointer, speed, tracker, t, last_click, cfg)
        if target is not None:
            tracker.spend(target, t)
            last_click = t
            clicks.append({"i": i, "t": t, "ptr": pointer, "c": target["center"],
                           "len": target["len"], "color": target["color"]})
            ov = draw(roi, geom, P, d, extra=f"CLICK {target['color']}")
            cv2.putText(ov, "CLICK", (6, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 220, 0), 2)
            saved[i] = ov

    # 核验: 真正的命中机会 = 指针轨迹在点击时刻±0.15s内确实进入了该条角区[心±半宽]
    def is_real_hit(c):
        for tt, pp in trace:
            if pp is not None and abs(tt - c["t"]) <= 0.15 and abs(ang_diff(pp, c["c"])) <= c["len"] / 2:
                return True
        return False
    good = [c for c in clicks if is_real_hit(c)]
    for c in clicks:
        c["ok"] = is_real_hit(c)
        cv2.imwrite(os.path.join(out, f"click_{c['i']:04d}_{'OK' if c['ok'] else 'MISS'}.jpg"),
                    saved[c["i"]], [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(f"=== sim {os.path.basename(run_dir)} (圆心={cx},{cy}) ===")
    print(f"帧={len(frames)} 点击={len(clicks)} 次, 其中命中(指针确进入条)={len(good)} 误点={len(clicks)-len(good)}")
    for c in clicks:
        print(f"  f{c['i']:4d} t={c['t']:5.2f}: ptr={c['ptr']:6.1f} -> {c['color']:6s}@{c['c']:6.1f} "
              f"len={c['len']:4.1f}  {'命中' if c['ok'] else '★误点(指针没进条)'}")
    print(f"误点应为0。标注图 click_*_OK/MISS.jpg 存 {out}")


def main():
    ap = argparse.ArgumentParser(description="轮盘撬锁 自动 bot(全新实现)")
    ap.add_argument("--calibrate", action="store_true", help="标定圆心/半径 -> config.json")
    ap.add_argument("--probe", action="store_true", help="抓一帧自检几何, 不点击")
    ap.add_argument("--debug", action="store_true", help="实机运行并显示识别画面")
    ap.add_argument("--sim", metavar="DIR", default=None, help="离线: 在录好的帧目录上跑命中逻辑")
    args = ap.parse_args()
    cfg = load_cfg()
    if args.calibrate:
        calibrate(cfg)
    elif args.probe:
        probe(cfg)
    elif args.sim:
        sim(cfg, args.sim)
    else:
        run(cfg, debug=args.debug)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
