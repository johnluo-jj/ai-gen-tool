#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
"破窀之靴 / 扔靴子" 自动辅助（全自动托管）

这游戏其实是打砖块(Arkanoid/Breakout)的换皮：
  - 底部「推车」= 挡板，A/D 左右移动；
  - 「靴子」= 球，空格扔出/发球，靴子竖直射出后到处弹；
  - 顶部金砖 = 砖墙，靴子撞到就碎(得分)；
  - 目标 = 保持靴子不落地(别让它从挡板下方掉出去)。

辅助思路(和 dial-lock 同构：截屏→视觉识别→自动操作)：
  每帧抓屏 → 找「靴子(球)」和「推车(挡板)」 → 用球的位置+速度预测它落到挡板线时的 X
  (沿途按左右墙壁反射) → 按住 A/D 把挡板挪到落点下方 → 球停着不动(在挡板上候发)就按空格发球。

识别(全部用相对稳的视觉特征，见 README)：
  - 球：靴子带**青色拖尾发光**(playfield 里独一份的青色)→ 主特征；棕色靴身 + 帧间运动作兜底。
  - 挡板：推车顶部那条**高饱和红色横杠**→ 给出挡板中心 X、宽度、反弹 Y 线。

模式：
  python auto_boot.py --calibrate   # 框出 playfield 区域 + 点左墙/右墙/挡板线 -> config.json
  python auto_boot.py --snapshot     # 抓一帧跑识别, 存掩膜+标注图核对(识别为空时先跑这个)
  python auto_boot.py --debug        # 实时可视化(球/挡板/预测落点/当前按键)
  python auto_boot.py                # 正式运行: F8 开始/暂停, F9 退出

⚠ 自动按键属第三方辅助, 可能违反游戏 ToS、有封号风险; 仅供个人离线练手/学习, 勿冲公开榜。
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
import mss

# 截屏坐标与屏幕真实像素一致(免 Windows DPI 缩放导致区域错位)
if sys.platform == "win32":
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
try:  # GBK 控制台编不了 ⚠/✅ 等符号会崩, 改成遇到就替换而非报错
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE_DIR, "config.json")

# 默认配置(--calibrate 会填 region/wall_left/wall_right/paddle_y)
DEFAULT_CFG = {
    "region": None,            # 抓屏区域(屏幕坐标) {left,top,width,height}
    "wall_left": None,         # 左反弹墙 X(相对 region)
    "wall_right": None,        # 右反弹墙 X(相对 region)
    "paddle_y": None,          # 反弹 Y 线(挡板红条顶部, 相对 region)
    "ceiling": 0,              # 球活动区上沿 Y(相对 region; 此上忽略, 防顶栏误检)
    "ball": {
        # 青色拖尾(HSV: H 0-179): 这是球独有的发光, 最可靠
        "cyan_h": [80, 105], "cyan_s": 70, "cyan_v": 110,
        # 棕色靴身: 兜底(砖也偏橙, 故仅在下层腔配合运动用)
        "brown_h": [8, 28], "brown_s": 70, "brown_v": 60,
        "min_area": 8,         # 球团最小面积(px), 滤噪点
        "motion_thresh": 18,   # 帧差阈值, 用运动兜底找球
    },
    "paddle": {
        # 推车顶部红横杠(红色 H 在 0 和 180 两端)
        "red_h1": [0, 8], "red_h2": [168, 179], "red_s": 110, "red_v": 110,
        "min_area": 40,
        "band": 60,            # 在 paddle_y 下方多少 px 内找红条
    },
    "control": {
        "deadzone": 12,            # 挡板中心离落点 < 此值就不动, 防抖
        "predict": True,          # True=按速度预测落点; False=只跟球当前 X
        "serve_idle_frames": 14,  # 连续多少帧没看到动球就判定"在候发"->发球
        "serve_interval": 0.6,    # 两次发球最小间隔(秒)
        "keys": {"left": "left", "right": "right", "serve": "space"},
        "max_fps": 0,             # 帧率上限(0=不限); 跟球快时建议不限
        "vel_smooth": 3,          # 速度用最近几帧位移平滑
    },
}


# ----------------------------- 基础工具 -----------------------------
def deep_merge(base, override):
    """把 override 合进 base 的副本(用于 默认配置 + 文件配置)。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))  # 深拷贝默认
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg = deep_merge(cfg, json.load(f))
    return cfg


def save_cfg(cfg):
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def imwrite_u(path, img):
    """cv2.imwrite 在非 ASCII 路径(本目录是中文)会失败, 改用 imencode+tofile。"""
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if ok:
        buf.tofile(path)
    return ok


def grab(sct, region):
    """抓一帧 region -> BGR ndarray。"""
    return np.asarray(sct.grab(region))[:, :, :3].copy()


def play_box(cfg, shape):
    """返回 (x0,y0,x1,y1): 球活动区(墙之间, ceiling 到 挡板线下方一点)。"""
    H, W = shape[:2]
    x0 = cfg["wall_left"] if cfg["wall_left"] is not None else 0
    x1 = cfg["wall_right"] if cfg["wall_right"] is not None else W
    y0 = cfg.get("ceiling", 0) or 0
    py = cfg["paddle_y"] if cfg["paddle_y"] is not None else H
    y1 = min(H, py + 8)       # 球可低到挡板线稍下
    return int(x0), int(y0), int(x1), int(y1)


# ----------------------------- 识别 -----------------------------
def detect_ball(frame, cfg, prev_gray=None, gray=None):
    """找球(靴子)。返回 (cx, cy, area, mask) 或 None。
    只认「在动的球」: 真球带青色拖尾且在移动 -> cyan∩运动。**静止的青色**(开局/过关时候发的
    青色瞄准线、停在车上的靴子、青色装饰)被运动门挡掉 -> 返回 None, 交给发球逻辑按空格。
    这样既不会把静止瞄准线当球(导致永不发球), 也不会锁错目标(锁在静止线上而非真球)。
    仅首帧(无前一帧、无运动信息)退回纯青色兜底。全部限制在 play_box 内。"""
    bb = cfg["ball"]
    x0, y0, x1, y1 = play_box(cfg, frame.shape)
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return None
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

    cyan = cv2.inRange(hsv, (bb["cyan_h"][0], bb["cyan_s"], bb["cyan_v"]),
                       (bb["cyan_h"][1], 255, 255))
    brown = cv2.inRange(hsv, (bb["brown_h"][0], bb["brown_s"], bb["brown_v"]),
                        (bb["brown_h"][1], 255, 255))

    motion = None
    if prev_gray is not None and gray is not None:
        d = cv2.absdiff(gray[y0:y1, x0:x1], prev_gray[y0:y1, x0:x1])
        motion = (d > bb["motion_thresh"]).astype(np.uint8) * 255

    # 只取「在动的青色」: 真球在移动 -> cyan∩motion; 静止青色(候发瞄准线/装饰)无运动 -> 被挡掉。
    # 有运动信息却找不到"在动的球" => 判定无球(返回 None), 让发球逻辑去按空格。
    thr = bb["min_area"] * 255
    if motion is not None:
        mdil = cv2.dilate(motion, np.ones((7, 7), np.uint8))
        cm = cv2.bitwise_and(cyan, mdil)
        if int(cm.sum()) >= thr:
            mask = cm                                  # 在动的青色拖尾 = 真球
        else:
            bm = cv2.bitwise_and(brown, mdil)
            if int(bm.sum()) >= thr:
                mask = bm                              # 兜底: 在动的棕色靴身
            else:
                return None                            # 没有在动的球 -> 候发/空场
    else:
        mask = cyan if int(cyan.sum()) >= thr else brown   # 仅首帧无运动信息时退回纯色

    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    area = cv2.contourArea(c)
    if area < bb["min_area"]:
        return None
    M = cv2.moments(c)
    if M["m00"] == 0:
        return None
    cx = int(M["m10"] / M["m00"]) + x0
    cy = int(M["m01"] / M["m00"]) + y0
    full = np.zeros(frame.shape[:2], np.uint8)
    full[y0:y1, x0:x1] = mask
    return cx, cy, area, full


def detect_paddle(frame, cfg, last_cx=None):
    """找推车(挡板)的红横杠。返回 (cx, half_w, mask) 或 None(找不到则沿用 last_cx)。
    用「墙之间、paddle_y 下方一条带」内所有红像素的质心定中心：比"最大轮廓"稳——
    红条被中央装饰/靴子切成两段时取最大段会偏心; ←/→ 箭头对称, 也不影响质心。"""
    pp = cfg["paddle"]
    H, W = frame.shape[:2]
    py = cfg["paddle_y"] if cfg["paddle_y"] is not None else int(H * 0.85)
    yA = max(0, py - 12)
    yB = min(H, py + pp["band"])
    xL = cfg["wall_left"] if cfg["wall_left"] is not None else 0
    xR = cfg["wall_right"] if cfg["wall_right"] is not None else W
    roi = frame[yA:yB, xL:xR]
    if roi.size == 0:
        return None
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (pp["red_h1"][0], pp["red_s"], pp["red_v"]), (pp["red_h1"][1], 255, 255))
    m2 = cv2.inRange(hsv, (pp["red_h2"][0], pp["red_s"], pp["red_v"]), (pp["red_h2"][1], 255, 255))
    mask = cv2.bitwise_or(m1, m2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))   # 去红色噪点
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 11), np.uint8))  # 连横向缝
    # 取「最大连通红块」的质心: 车顶红条是带内最大的连通红色, 自动甩开两侧角落的零碎红装饰
    num, _, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
    if num <= 1:
        return None
    i = 1 + int(stats[1:, cv2.CC_STAT_AREA].argmax())
    if stats[i, cv2.CC_STAT_AREA] < pp["min_area"]:
        return None
    cx = xL + int(cents[i][0])
    half_w = int(stats[i, cv2.CC_STAT_WIDTH] // 2)
    full = np.zeros(frame.shape[:2], np.uint8)
    full[yA:yB, xL:xR] = mask
    return cx, half_w, full


def predict_landing(x, y, vx, vy, left, right, paddle_y):
    """球(x,y)以速度(vx,vy)运动, 预测落到 paddle_y 时的 X(沿左右墙反射)。
    vy<=0(上行)时返回当前 x(先跟着等它回头)。"""
    span = max(1, right - left)
    if vy <= 1e-3:
        ux = x
    else:
        t = (paddle_y - y) / vy
        ux = x + vx * t
    # 折叠反射到 [left,right]
    p = (ux - left) % (2 * span)
    if p < 0:
        p += 2 * span
    if p > span:
        p = 2 * span - p
    return left + p


# ----------------------------- 标定 -----------------------------
def select_region(sct):
    """抓主屏拖框选 playfield 区域; 返回屏幕坐标 dict 或 None。"""
    mon = sct.monitors[1]
    frame = np.asarray(sct.grab(mon))[:, :, :3]
    H, W = frame.shape[:2]
    scale = min(1.0, 0.9 * mon["height"] / H, 0.9 * mon["width"] / W)
    disp = cv2.resize(frame, (int(W * scale), int(H * scale)))
    win = "select PLAYFIELD: drag box (curtain-to-curtain, ceiling to below cart), ENTER=ok, c=cancel"
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
    return {"left": int(mon["left"] + x / scale), "top": int(mon["top"] + y / scale),
            "width": int(w / scale), "height": int(h / scale)}


def calibrate():
    sct = mss.mss()
    cfg = load_cfg()
    print("【标定】先把游戏切到屏幕最前、露出 playfield。3 秒后抓主屏拖框...")
    for s in range(3, 0, -1):
        print(f"\r  {s}", end=""); time.sleep(1)
    print()
    region = select_region(sct)
    if region is None:
        sys.exit("未选择区域, 已取消。")
    cfg["region"] = region
    frame = grab(sct, region)

    # 预探一下红横杠, 画出来帮你点挡板线
    guess = detect_paddle(frame, deep_merge(cfg, {"paddle_y": int(frame.shape[0] * 0.5)}))
    disp = frame.copy()
    if guess is not None:
        ys, xs = np.where(guess[2] > 0)
        if len(ys):
            cv2.rectangle(disp, (xs.min(), ys.min()), (xs.max(), ys.max()), (0, 255, 0), 2)

    clicks = []
    win = "calib: click 1=LEFT wall  2=RIGHT wall  3=PADDLE top line(on red bar). ENTER=save r=redo"

    def on_mouse(ev, mx, my, flags, _):
        if ev == cv2.EVENT_LBUTTONDOWN and len(clicks) < 3:
            clicks.append((mx, my))

    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    labels = ["LEFT wall", "RIGHT wall", "PADDLE line"]
    while True:
        show = disp.copy()
        for i, (mx, my) in enumerate(clicks):
            col = (0, 200, 255)
            if i < 2:  # 墙: 竖线
                cv2.line(show, (mx, 0), (mx, show.shape[0]), col, 1)
            else:      # 挡板线: 横线
                cv2.line(show, (0, my), (show.shape[1], my), (255, 0, 255), 1)
            cv2.circle(show, (mx, my), 4, col, -1)
        tip = "done, ENTER=save" if len(clicks) == 3 else f"click {labels[len(clicks)]}"
        cv2.putText(show, tip, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.imshow(win, show)
        k = cv2.waitKey(20) & 0xFF
        if k in (13, 10) and len(clicks) == 3:
            break
        if k == ord("r"):
            clicks.clear()
        if k == 27:
            cv2.destroyAllWindows()
            sys.exit("已取消。")
    cv2.destroyAllWindows()
    cv2.waitKey(1)

    cfg["wall_left"] = int(clicks[0][0])
    cfg["wall_right"] = int(clicks[1][0])
    cfg["paddle_y"] = int(clicks[2][1])
    if cfg["wall_left"] > cfg["wall_right"]:
        cfg["wall_left"], cfg["wall_right"] = cfg["wall_right"], cfg["wall_left"]
    save_cfg(cfg)
    print(f"已保存 config.json:\n  region={region}\n  wall_left={cfg['wall_left']} "
          f"wall_right={cfg['wall_right']} paddle_y={cfg['paddle_y']}")
    print("下一步: python auto_boot.py --snapshot  核对球/挡板识别。")


# ----------------------------- 可视化叠加 -----------------------------
def annotate(frame, cfg, ball, paddle, target):
    out = frame.copy()
    x0, y0, x1, y1 = play_box(cfg, frame.shape)
    cv2.rectangle(out, (x0, y0), (x1, y1), (80, 80, 80), 1)
    if cfg["paddle_y"] is not None:
        cv2.line(out, (0, cfg["paddle_y"]), (out.shape[1], cfg["paddle_y"]), (160, 160, 0), 1)
    if ball is not None:
        cx, cy = ball[0], ball[1]
        cv2.circle(out, (cx, cy), 9, (255, 255, 0), 2)
    if paddle is not None:
        cx, hw = paddle[0], paddle[1]
        py = cfg["paddle_y"] or int(frame.shape[0] * 0.85)
        cv2.rectangle(out, (cx - hw, py), (cx + hw, py + 16), (0, 0, 255), 2)
    if target is not None:
        cv2.line(out, (int(target), 0), (int(target), out.shape[0]), (0, 255, 0), 1)
    return out


# ----------------------------- 抓帧诊断 -----------------------------
def snapshot():
    cfg = load_cfg()
    if cfg["region"] is None:
        sys.exit("还没标定, 先跑: python auto_boot.py --calibrate")
    sct = mss.mss()
    print("5 秒内切到游戏、让靴子正在飞(有动球才好看青色拖尾)...")
    for s in range(5, 0, -1):
        print(f"\r  {s}", end=""); time.sleep(1)
    print()
    f0 = grab(sct, cfg["region"])
    time.sleep(0.05)
    f1 = grab(sct, cfg["region"])
    g0 = cv2.cvtColor(f0, cv2.COLOR_BGR2GRAY)
    g1 = cv2.cvtColor(f1, cv2.COLOR_BGR2GRAY)

    ball = detect_ball(f1, cfg, prev_gray=g0, gray=g1)
    paddle = detect_paddle(f1, cfg)
    imwrite_u(os.path.join(BASE_DIR, "_snap_raw.png"), f1)
    if ball is not None:
        imwrite_u(os.path.join(BASE_DIR, "_snap_ball.png"), ball[3])
        print(f"球: 中心=({ball[0]},{ball[1]}) 面积={ball[2]:.0f}")
    else:
        print("球: 未检出 (调小 ball.cyan_s/cyan_v 或 brown_*; 确认抓帧时靴子在飞)")
    if paddle is not None:
        imwrite_u(os.path.join(BASE_DIR, "_snap_paddle.png"), paddle[2])
        print(f"挡板: 中心X={paddle[0]} 半宽={paddle[1]}")
    else:
        print("挡板: 未检出 (调小 paddle.red_s/red_v; 确认 paddle_y 压在红横杠上)")
    imwrite_u(os.path.join(BASE_DIR, "_snap_annot.png"),
              annotate(f1, cfg, ball, paddle, ball[0] if ball else None))
    print("已存 _snap_raw/_snap_ball/_snap_paddle/_snap_annot.png -> 核对识别。")


# ----------------------------- 主循环(debug & run 共用) -----------------------------
def run(visualize=False):
    cfg = load_cfg()
    if cfg["region"] is None or cfg["paddle_y"] is None:
        sys.exit("还没标定, 先跑: python auto_boot.py --calibrate")
    ctl = cfg["control"]
    keys = ctl["keys"]

    press = None
    if not visualize:
        import pydirectinput
        pydirectinput.PAUSE = 0
        pydirectinput.FAILSAFE = False
        press = pydirectinput

    active = {"on": visualize, "quit": False}  # debug 模式直接开; run 模式等 F8
    held = {"key": None}

    def set_move(direction):
        """direction: 'left'/'right'/None。按住对应键, 切换时先松旧键。"""
        want = keys[direction] if direction else None
        if held["key"] == want:
            return
        if held["key"] is not None and press:
            press.keyUp(held["key"])
        if want is not None and press:
            press.keyDown(want)
        held["key"] = want

    if not visualize:
        try:
            import keyboard
            keyboard.add_hotkey("f8", lambda: active.update(on=not active["on"]) or
                                print("\n▶ 运行中" if active["on"] else "\n⏸ 暂停"))
            keyboard.add_hotkey("f9", lambda: active.update(quit=True))
            print("把鼠标点进游戏窗口 -> F8 开始/暂停, F9 退出。")
        except Exception as e:
            print(f"(keyboard 不可用, 无法用热键; {e}) 将直接运行。")
            active["on"] = True

    sct = mss.mss()
    region = cfg["region"]
    left = cfg["wall_left"] if cfg["wall_left"] is not None else 0
    right = cfg["wall_right"] if cfg["wall_right"] is not None else region["width"]
    paddle_y = cfg["paddle_y"]
    deadzone = ctl["deadzone"]
    min_dt = (1.0 / ctl["max_fps"]) if ctl["max_fps"] > 0 else 0.0

    hist = deque(maxlen=max(2, ctl["vel_smooth"] + 1))  # (t, cx, cy)
    prev_gray = None
    last_paddle_cx = None
    idle = 0
    last_serve = 0.0
    last_log = time.perf_counter()
    frames = 0
    next_t = time.perf_counter()

    try:
        while not active["quit"]:
            now = time.perf_counter()
            if min_dt and now < next_t:
                continue
            next_t = now + min_dt

            if not active["on"]:
                set_move(None)
                time.sleep(0.02)
                continue

            frame = grab(sct, region)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            ball = detect_ball(frame, cfg, prev_gray=prev_gray, gray=gray)
            paddle = detect_paddle(frame, cfg, last_paddle_cx)
            prev_gray = gray
            frames += 1

            paddle_cx = paddle[0] if paddle else last_paddle_cx
            if paddle:
                last_paddle_cx = paddle[0]

            target = None
            if ball is not None:
                cx, cy = ball[0], ball[1]
                hist.append((now, cx, cy))
                vx = vy = 0.0
                if len(hist) >= 2:
                    t0, x_0, y_0 = hist[0]
                    dt = now - t0
                    if dt > 1e-4:
                        vx = (cx - x_0) / dt
                        vy = (cy - y_0) / dt
                if ctl["predict"]:
                    target = predict_landing(cx, cy, vx, vy, left, right, paddle_y)
                else:
                    target = cx
                idle = 0
            else:
                idle += 1
                hist.clear()

            # 控车: 把挡板中心挪到 target
            if target is not None and paddle_cx is not None:
                err = target - paddle_cx
                if err > deadzone:
                    set_move("right")
                elif err < -deadzone:
                    set_move("left")
                else:
                    set_move(None)
            else:
                set_move(None)

            # 候发: 连续多帧没动球 -> 按空格发球(限速)
            if idle >= ctl["serve_idle_frames"] and (now - last_serve) >= ctl["serve_interval"]:
                if press:
                    press.press(keys["serve"])
                last_serve = now
                idle = 0

            if now - last_log >= 0.5:
                fps = frames / max(1e-6, now - last_log)
                bs = "Y" if ball else "-"
                print(f"\rfps={fps:5.1f} ball={bs} paddle={paddle_cx} "
                      f"target={int(target) if target is not None else '-'} key={held['key']}   ", end="")
                frames = 0
                last_log = now

            if visualize:
                out = annotate(frame, cfg, ball, paddle, target)
                cv2.imshow("auto_boot --debug (q=quit)", out)
                if (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                    break
    finally:
        set_move(None)  # 收尾务必松开按住的键
        if visualize:
            cv2.destroyAllWindows()
        print("\n已停止。")


def main():
    ap = argparse.ArgumentParser(description="扔靴子(打砖块) 自动辅助")
    ap.add_argument("--calibrate", action="store_true", help="标定: 框区域+点墙/挡板线")
    ap.add_argument("--snapshot", action="store_true", help="抓一帧跑识别, 存掩膜核对")
    ap.add_argument("--debug", action="store_true", help="实时可视化(不按键)")
    args = ap.parse_args()
    if args.calibrate:
        calibrate()
    elif args.snapshot:
        snapshot()
    elif args.debug:
        run(visualize=True)
    else:
        run(visualize=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
