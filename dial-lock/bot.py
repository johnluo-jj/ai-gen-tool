#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
轮盘撬锁 自动 bot (重构版) —— 检测逻辑复用 detect2.py(已在真机帧离线验证)。

实测机制(见 detect2.py 注释)：指针=内层最亮径向团(颜色无关,变红也能认);
黄/蓝条=外层切向弧;落空整屏闪红有惩罚。所以策略：**只在 高置信 且 指针(含延迟提前量)
落入已分类的黄/蓝条角区 时才点**;离目标远按住右键加速,临近松开保命中。

用法(PowerShell, 建议管理员)：
  python bot.py --probe     # 抓一帧自检: 看圆心/各层环是否对齐圆盘, 存 _probe.jpg
  python bot.py --debug     # 显示识别画面跑
  python bot.py             # 正式跑; F8 开/暂停, F9 退出
几何默认取自全屏采集的真值; 若移动了游戏窗口, 改 bot_config.json 的 center 或重采集。
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

from detect2 import Geom, detect, DEFAULT_P, ang_diff

if sys.platform == "win32":
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
try:    # GBK 控制台编不了 ⚠/▶/✅ 等符号会崩, 改成遇到就替换而非报错(中文照常)
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_config.json")

DEFAULT_CFG = {
    "center": [956, 700],          # 圆心(取自全屏采集真值; 换窗口/分辨率需改)
    "r_max": 210,                  # ROI 半径(到外圈亮环外一点)
    "det": dict(DEFAULT_P),        # 检测参数(内/外层半径、阈值等, 见 detect2.DEFAULT_P)
    "latency": 0.04,               # 点击延迟提前量(秒): 总点早->调小, 总点晚->调大
    "min_click_interval": 0.18,    # 两次点击最小间隔(秒)
    "click_hold": 0.05,            # 左键按住时长(秒): 瞬间点击游戏常吞掉, 按住~50ms才认; 不响应可调到0.08
    "conf_min": 4.0,               # 命中判定的指针置信下限: 太高会让大量帧不可点(实跑conf常掉到8以下); 看状态行cf分布精调
    "boost": {"enabled": True, "release_deg": 30.0},  # 离目标>此角->按住右键加速
    "click_pos": None,             # 左键坐标[x,y]; null=原地点(不动光标)
}


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        cfg.update(user)
        cfg["det"] = {**DEFAULT_P, **user.get("det", {})}   # det 子项补全默认
        cfg["boost"] = {**DEFAULT_CFG["boost"], **user.get("boost", {})}
    else:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"已生成默认配置 -> {CFG_PATH}")
    return cfg


class Grabber:
    def __init__(self):
        self.sct = mss.mss()

    def grab(self, region):
        return np.asarray(self.sct.grab(region))[:, :, :3]


def make_region(cfg):
    """根据 center/r_max 算 ROI 截取区域, 与 Geom 对齐(直接抓ROI, 不抓全屏更快)。"""
    cx, cy = cfg["center"]
    geom = Geom(cx, cy, cfg["r_max"])
    region = {"left": geom.left, "top": geom.top, "width": geom.size, "height": geom.size}
    return geom, region


def draw_debug(roi, geom, cfg, d, omega, should_click, boosting):
    ov = roi.copy()
    P = cfg["det"]
    c = (int(geom.cxl), int(geom.cyl))
    for r in (P["ptr_in"], P["ptr_out"], P["bar_in"], P["bar_out"]):
        cv2.circle(ov, c, r, (60, 60, 60), 1)
    Rbar = int((P["bar_in"] + P["bar_out"]) / 2)
    for s in d["sectors"]:
        if s["color"] == "yellow":
            col = (0, 255, 255)
        elif s["color"] == "blue":
            col = (255, 150, 0)
        else:
            col = (150, 150, 150)
        if should_click:
            col = (0, 220, 0)        # 命中决策=绿(别和游戏"点错=红"混淆)
        a0, a1 = s["center"] - s["len"] / 2, s["center"] + s["len"] / 2
        cv2.ellipse(ov, c, (Rbar, Rbar), 0, a0, a1, col, 4)
    if d["pointer"] is not None:
        a = np.deg2rad(d["pointer"])
        cv2.line(ov, c, (int(geom.cxl + P["bar_out"] * np.cos(a)), int(geom.cyl + P["bar_out"] * np.sin(a))),
                 (0, 0, 255), 2)
    cv2.putText(ov, f"ptr={'-' if d['pointer'] is None else int(d['pointer'])} conf={d['ptr_conf']:.1f} "
                    f"w={omega:6.0f}/s nSec={len(d['sectors'])} boost={'Y' if boosting else 'n'}",
                (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)
    if should_click:
        cv2.putText(ov, "CLICK", (6, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 220, 0), 2, cv2.LINE_AA)
    return ov


def probe(cfg):
    """抓一帧, 跑检测, 存叠加图供人工核对圆心/各层是否对齐, 不点击。"""
    geom, region = make_region(cfg)
    g = Grabber()
    for s in range(3, 0, -1):
        print(f"\r  {s} 秒后抓一帧自检(切到游戏、露出圆盘) ...", end="")
        time.sleep(1)
    print()
    roi = g.grab(region)
    d = detect(roi, geom, cfg["det"])
    ov = draw_debug(roi, geom, cfg, d, 0.0, False, False)
    out = os.path.join(os.path.dirname(CFG_PATH), "_probe.jpg")
    cv2.imwrite(out, ov, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print(f"画面均值={roi.mean():.1f}  ptr={d['pointer']} conf={d['ptr_conf']:.1f} "
          f"sectors={[(s['color'], int(s['center']), int(s['len'])) for s in d['sectors']]}")
    print(f"已存 {out}: 红线=指针, 黄/蓝弧=条, 灰圈=各层半径。核对圆心是否在表盘中心、灰圈是否压在轨道上。")
    if roi.mean() < 8:
        print("⚠ 画面接近全黑: 游戏多半是独占全屏(mss截不到), 请切窗口化/无边框。")


def run(cfg, debug=False, record_secs=0):
    import pydirectinput
    import keyboard
    pydirectinput.PAUSE = 0
    pydirectinput.FAILSAFE = False

    geom, region = make_region(cfg)
    g = Grabber()
    P = cfg["det"]
    print(f"圆心={cfg['center']} ROI={region['width']}x{region['height']} "
          f"指针层=[{P['ptr_in']},{P['ptr_out']}] 条层=[{P['bar_in']},{P['bar_out']}]")

    state = {"running": False, "quit": False}

    def toggle():
        state["running"] = not state["running"]
        print(f"\n{'▶ 运行中' if state['running'] else '⏸ 已暂停'}")

    keyboard.add_hotkey("f8", toggle)
    keyboard.add_hotkey("f9", lambda: state.update(quit=True))

    rec_imgs, rec_meta, rec_t0 = [], [], None
    if record_secs:
        print(f"【录制模式】倒计时后自动开跑并录制 {record_secs}s(边跑边点边录), 录完自动推送给作者。F9可提前停。")
        for s in range(3, 0, -1):
            print(f"\r  {s} 秒后开始(切到游戏、露出转盘、确保那局在转) ...", end="")
            time.sleep(1)
        print()
        state["running"] = True
    else:
        print("就绪: 鼠标放进游戏窗口空白处 -> F8 开始/暂停, F9 退出。(F8无反应=用管理员跑)")

    prev_angle, prev_t, omega = None, None, 0.0
    last_click, clicks = 0.0, 0
    boosting = False

    def set_boost(on):
        nonlocal boosting
        if on != boosting:
            (pydirectinput.mouseDown if on else pydirectinput.mouseUp)(button="right")
            boosting = on

    last_log, frames, ptr_frames, hit_frames = time.perf_counter(), 0, 0, 0
    conf_hist = []
    try:
        while not state["quit"]:
            now = time.perf_counter()
            if not state["running"]:
                set_boost(False)
                if debug and (cv2.waitKey(1) & 0xFF == 27):
                    break
                time.sleep(0.02)
                continue

            if record_secs:
                if rec_t0 is None:
                    rec_t0 = now
                elif now - rec_t0 >= record_secs:
                    break

            frames += 1
            did_click = False
            roi = g.grab(region)
            d = detect(roi, geom, P)
            pointer, conf, sectors = d["pointer"], d["ptr_conf"], d["sectors"]

            dt = (now - prev_t) if prev_t is not None else 0.016
            # 追踪omega: 只要看得到指针且非闪红就更新(低分帧也用以保持角速度连续)
            track_ok = pointer is not None and not d["flash"]
            if track_ok:
                if prev_angle is not None and 0 < dt < 0.2:
                    w = ang_diff(pointer, prev_angle) / dt
                    omega = 0.6 * w + 0.4 * omega
                prev_angle, prev_t = pointer, now
                conf_hist.append(conf)
            else:
                prev_angle, prev_t, omega = None, None, 0.0

            clickable = track_ok and conf >= cfg["conf_min"]   # 命中判定另设更严的conf门槛
            if clickable:
                ptr_frames += 1

            # 决策: nearest(给加速判距)用所有track_ok帧算; 命中(should_click)才要求clickable
            should_click, nearest = False, None
            if track_ok:
                predicted = pointer + omega * cfg["latency"]
                guard = 0.5 * abs(omega) * dt
                for s in sectors:
                    if s["color"] not in ("yellow", "blue"):
                        continue
                    e = max(0.0, abs(ang_diff(s["center"], pointer)) - s["len"] / 2.0)
                    nearest = e if nearest is None else min(nearest, e)
                    if clickable and abs(ang_diff(predicted, s["center"])) <= s["len"] / 2.0 + guard:
                        should_click = True
            if should_click:
                hit_frames += 1

            bc = cfg["boost"]
            set_boost(bool(bc["enabled"] and nearest is not None and nearest > bc["release_deg"]))

            if should_click and (now - last_click) >= cfg["min_click_interval"]:
                set_boost(False)
                if cfg["click_pos"]:
                    pydirectinput.moveTo(cfg["click_pos"][0], cfg["click_pos"][1])
                # 按下->按住->抬起: 瞬间down+up游戏会吞, 必须保持按下几十ms才被识别
                pydirectinput.mouseDown(button="left")
                time.sleep(cfg["click_hold"])
                pydirectinput.mouseUp(button="left")
                last_click, clicks, did_click = now, clicks + 1, True

            if record_secs:
                rec_imgs.append(roi.copy())
                rec_meta.append({"t": round(now - rec_t0, 4),
                                 "ptr": None if pointer is None else round(pointer, 1),
                                 "omega": round(omega, 1), "conf": round(conf, 2),
                                 "flash": bool(d["flash"]),
                                 "sectors": [{"c": round(s["center"], 1), "len": round(s["len"], 1),
                                              "color": s["color"]} for s in sectors],
                                 "hit": bool(should_click), "clicked": did_click,
                                 "boost": bool(boosting)})

            if now - last_log > 1.0:
                fps = frames / (now - last_log)
                seenp = 100 * ptr_frames / max(1, frames)
                cs = sorted(conf_hist) or [0.0]
                cf = lambda q: cs[min(len(cs) - 1, int(q * len(cs)))]
                # cf(mn/md/mx)=conf最小/中位/最大; seen=可点帧占比; hits/s=本秒命中判定次数
                print(f"\rfps={fps:4.0f} ptr={'-' if pointer is None else f'{pointer:5.0f}'} "
                      f"cf(mn/md/mx)={cf(0):.0f}/{cf(.5):.0f}/{cf(.99):.0f} seen={seenp:3.0f}% "
                      f"w={omega:6.0f}/s nSec={len(sectors)} hits/s={hit_frames} "
                      f"clicks={clicks} boost={'Y' if boosting else 'n'}   ", end="")
                last_log, frames, ptr_frames, hit_frames = now, 0, 0, 0
                conf_hist = []

            if debug:
                cv2.imshow("bot-debug", draw_debug(roi, geom, cfg, d, omega, should_click, boosting))
                if cv2.waitKey(1) & 0xFF == 27:
                    break
    finally:
        set_boost(False)
        keyboard.clear_all_hotkeys()
        if debug:
            cv2.destroyAllWindows()
        if record_secs and rec_imgs:
            save_recording(rec_imgs, rec_meta, region, cfg)
        print("\n已退出。")


def save_recording(imgs, meta, region, cfg):
    """存录制帧+每帧检测决策到 captures/botrec_*, 并自动 git 推送(便于作者离线复现误点)。"""
    import subprocess
    base = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(base, "captures", "botrec_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(out, exist_ok=True)
    print(f"\n保存录制 {len(imgs)} 帧 -> {out} ...")
    for i, (img, m) in enumerate(zip(imgs, meta)):
        m["i"], m["file"] = i, f"f{i:04d}.jpg"
        cv2.imwrite(os.path.join(out, m["file"]), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    with open(os.path.join(out, "rec.json"), "w", encoding="utf-8") as f:
        json.dump({"region": region, "center": cfg["center"], "r_max": cfg["r_max"],
                   "det": cfg["det"], "latency": cfg["latency"], "conf_min": cfg["conf_min"],
                   "count": len(imgs), "frames": meta}, f, ensure_ascii=False, indent=2)
    print("已写 rec.json。自动提交+推送 ...")
    ok = True
    for cmd in (["git", "add", out],
                ["git", "commit", "-m", f"bot实机录制 {os.path.basename(out)}"],
                ["git", "push"]):
        r = subprocess.run(cmd, cwd=base, capture_output=True, text=True)
        txt = (r.stdout + r.stderr).strip()
        if txt:
            print("  " + txt.replace("\n", "\n  "))
        if r.returncode != 0:
            ok = False
            break
    print("✅ 已推送, 跟作者说“录好了”。" if ok else
          f"⚠ 自动推送失败(见上)。请手动: git add {os.path.relpath(out, base)} ; git commit -m 录制 ; git push")


def main():
    ap = argparse.ArgumentParser(description="轮盘撬锁 自动 bot (重构版)")
    ap.add_argument("--probe", action="store_true", help="抓一帧自检几何对齐, 不点击")
    ap.add_argument("--debug", action="store_true", help="运行时显示识别画面")
    ap.add_argument("--record", nargs="?", type=int, const=12, default=0, metavar="SECS",
                    help="录制模式: 自动跑并录制SECS秒(默认12)原始帧+检测决策, 录完自动推送给作者")
    args = ap.parse_args()
    cfg = load_cfg()
    if args.probe:
        probe(cfg)
    else:
        run(cfg, debug=args.debug, record_secs=args.record)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
