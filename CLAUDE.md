# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 本仓库代码、注释、README、commit 均为中文，本文件也用中文以保持一致。

## 仓库概览

`ai-gen-tool` 是「AI 辅助开发的小游戏自动化工具」合集，每个子目录是一个独立工具。当前包含：

- **`dial-lock/`** —「轮盘撬锁」全自动 bot（截屏 → CV 识别指针/高亮条 → 自动点击撬锁）。**仓库里唯一成型的代码项目**，下文架构主要讲它。
- **`扔靴子/`** — 仅有游戏截图与介绍图（`截图*.png`、`扔靴子介绍.png`、`_zoom/`），**还没有代码**，处于看图分析、规划阶段的新工具。

根目录 `README.md` 是占位符；真正的使用文档在 `dial-lock/README.md`。

## 环境与常用命令

Windows + PowerShell + Python（无虚拟环境约定，直接系统 Python）。所有命令在对应子目录下运行。

```powershell
cd dial-lock
pip install -r requirements.txt          # 依赖: numpy opencv-python mss pydirectinput keyboard

# —— 当前在用的实机 bot（detect2 检测 + bot 驱动）——
python bot.py --probe                     # 抓一帧自检几何对齐(圆心/各层环), 存 _probe.jpg, 不点击
python bot.py --debug                     # 显示识别画面运行
python bot.py                             # 正式跑; F8 开/暂停, F9 退出
python bot.py --record [SECS]             # 录制 SECS 秒(默认12)原始帧+检测决策, 录完自动 git push

# —— 数据驱动的离线验证回路(改检测逻辑时用)——
python capture.py [--seconds N] [--select] [--push]   # 高速连拍屏幕 -> captures/run_*/
python analyze.py captures/run_YYYYmmdd_HHMMSS         # 离线分析: 拟合圆心/角速度/条 -> _analysis/
python detect2.py captures/run_YYYYmmdd_HHMMSS         # 在采集帧上跑检测器 -> _detect2/(标注图+det.csv+report.txt)
python detect2.py captures/run_* --profile            # 只打印径向显著度剖面, 用于定内/外层半径

# —— 旧版一体 bot(自带检测, 见下「两代 bot」)——
python auto_unlock.py --calibrate         # 标定圆环几何 -> config.json
python auto_unlock.py --snapshot          # 抓一帧诊断识别
python auto_unlock.py [--debug]           # 运行; F8 开/暂停, F9 退出
```

**没有测试框架 / lint / 构建步骤。** 正确性靠「采集真机帧 → 离线在帧上验证检测 → 实机 `--probe`/`--debug` 目视核对」这条回路保证，不是单元测试。改了检测逻辑要先用 `python detect2.py <采集目录>` 在已有 `captures/run_*` 帧上回归，再上实机。

## dial-lock 架构

整套是从「采集真机数据 → 离线分析 → 定检测器 → 上实机」一步步重构出来的数据驱动流水线，理解时按这个数据流看：

```
capture.py ──> captures/run_*/(帧+frames.json) ──┬─> analyze.py  ──> _analysis/(拟合圆心/角速度)
                                                  └─> detect2.py  ──> _detect2/(逐帧检测验证)
                                                          │
                                          (detect/Geom/DEFAULT_P/ang_diff)
                                                          │
                                                          ▼
                                                       bot.py ──> 实机截屏循环 + 点击/右键加速
```

- **`detect2.py` 是检测的唯一真相源**。`bot.py` 顶部 `from detect2 import Geom, detect, DEFAULT_P, ang_diff` 直接复用，离线(`detect2.py` 跑采集帧)和在线(`bot.py` 跑屏幕)走完全相同的检测代码 —— 改检测只改 `detect2.py` 一处。
- **检测核心思路(颜色无关、按半径分层)**：圆环分两层 —— 内层 `[ptr_in,ptr_out]` 只有指针、外层 `[bar_in,bar_out]` 才有黄/蓝条。指针 = 内层每角度显著度的峰（取「最亮通道」做显著度，故指针变红也认）；条 = 外层显著弧、再按均色分黄/蓝，并剔掉「针自身的窄蓝辉光」。整环变亮(落空闪红) → 内层基底高 → 低置信不点。这些经验值全在 `detect2.DEFAULT_P` 里，每个键都有注释说明为何取该范围（如指针层为何收到最内圈 `[104,118]` —— 避开会偷峰的金条）。
- **`bot.py` 的实机策略**：用 `detect` 拿指针角+条；跨帧追踪角速度 `omega`(带物理封顶防微小 dt 除法尖刺)；`predicted = pointer + omega*latency` 预测落点，落入某条角区(加 `guard` 帧间余量)且置信达标才点；离目标远按住右键加速、临近松开保命中。左键用「按下→`click_hold`→抬起」而非瞬间点击（游戏会吞瞬间点击）。
- **几何是真值、非每次标定**：`bot_config.json` 的 `center` 取自全屏采集的拟合圆心(956,700)；换窗口/分辨率要重采集或改配置。`bot.py --probe` 用来目视确认圆心和各层环压在真实轨道上。

### 两代 bot（重要，别改错文件）

| | 新版（**当前在用**） | 旧版（保留） |
|---|---|---|
| 入口 | `bot.py` | `auto_unlock.py` |
| 检测 | 复用 `detect2.py`（半径分层、数据验证过） | 自带检测（单层「相对底环偏离」成廓，指针=最亮发光团） |
| 配置 | `bot_config.json` | `config.json` |
| 标定 | 几何取采集真值，`--probe` 核对 | `--calibrate` 手点圆环拟合 |
| 文档 | 本文件 | `dial-lock/README.md` |

近期提交都在新版（`bot.py`/`detect2.py`）。涉及检测/实机逻辑默认改新版那条线；`auto_unlock.py` 是早期一体方案，除非明确要动旧版否则不碰。

## 全脚本通用约定

- **GBK 控制台兜底**：每个脚本开头都有 `sys.stdout.reconfigure(errors="replace")`。Windows GBK 控制台打印 `⚠/▶/✅` 等符号会崩，靠这个替换而非报错。新脚本沿用此模式。
- **DPI 感知**：`ctypes.windll.user32.SetProcessDPIAware()` 让截屏坐标与鼠标坐标一致，截屏类脚本必须有。
- **游戏需窗口化/无边框**：独占全屏下 `mss` 截到黑屏（脚本会用画面均值<8 提示）。点击/热键不生效时需**管理员**运行（游戏以管理员运行时尤其）。
- **热键统一 F8 开/暂停、F9 退出**。
- **角度约定**：`arctan2(dy,dx)` 得 0–359°；角差一律用 `ang_diff` 归一到 (-180,180]。
- **JSON 配置惯例**：脚本内 `DEFAULT_*` 字典为默认值，读配置时 `{**默认, **用户}` 合并补全缺省键；改默认改脚本里的字典，调参改 `*.json`。

## Git 与产物

- `capture.py --push`、`bot.py --record` 会**自动 `git add/commit/push`**（把真机帧/录制发给作者离线复现）。注意这与「默认禁止写类 git 命令」的全局准则冲突：除非用户明确要采集/录制并推送，否则不要主动触发这两个开关。
- `captures/run_*`、`botrec_*`、各 `_analysis/`/`_detect2/`/`_zoom/` 是**生成产物**（采集帧、分析图、标注图），可能体积很大；改代码时不要手动编辑这些目录的内容。
- 该游戏带排行榜，自动点击属第三方辅助、**可能违反 ToS 有封号风险**，仓库定位为个人离线学习用途。
