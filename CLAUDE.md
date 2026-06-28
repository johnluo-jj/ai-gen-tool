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

python unlock.py --calibrate              # 标定圆心/半径 -> config.json(换分辨率/窗口要重标; 同机可跳过, 默认值即真值)
python unlock.py --probe                  # 抓一帧自检几何对齐, 存 _probe.jpg, 不点击
python unlock.py --debug                  # 实机运行 + 识别画面窗口
python unlock.py                          # 正式跑; F8 开/暂停, F9 退出
```

**没有测试框架 / lint / 构建步骤，也没有离线验证。** 只有实机两条路：`--probe` 看几何对齐、`--debug` 看实时识别画面(黄/蓝弧=条、绿点=已登记条、绿字 CLICK=点击决策)。改了检测/命中逻辑只能实机目视核对。

## dial-lock 架构

仅两个文件：`detector.py`(无状态逐帧检测) + `unlock.py`(跨帧决策"记住条"并实机点击)。

```
mss 截 ROI ─> detector.py: detect()  无状态逐帧检测(指针角 + 条)
                   │
                   ▼
           unlock.py: SpeedEstimator + BarTracker + decide()  有状态命中决策
                          └─ run()  实机截屏循环 + 左键点击 (calibrate/probe/debug 同文件)
```

- **`detector.py` 是检测的唯一真相源**，无状态纯库(不单独运行)。`unlock.py` 顶部 `from detector import Geom, detect, draw, DEFAULT_P, ang_diff` 复用 —— 改检测只改 `detector.py`。
- **检测靠「半径形状」分指针/条，不靠颜色**（指针和蓝条都偏蓝，颜色分不开）：指针=**径向流光**，在**内半带** `[ptr_in,ptr_out]`(此处条还暗)找最亮峰；条=**切向弧**，在**外半带** `[bar_in,bar_out]` 找，**先挖掉指针那一窄角**再按宽度+饱和色(明确黄/明确蓝)筛，不饱和的(指针残辉)丢弃。参数在 `detector.DEFAULT_P`，每键带注释。半径按外轨道半径 `R` 的固定比例(`unlock.BAND_FRAC`)缩放，故 `--calibrate` 只需点轨道外缘。
- **`unlock.py` 命中决策(关键，旧版屡错就错在这)**：实测**条是固定的、只有指针在转**。挖掉指针后那一帧条会被一起挖没——恰好在该点的瞬间瞎了。故 `BarTracker` **记住条**：指针不在条上时干净登记条的位置/颜色，指针扫进**已登记**的条就点，哪怕这帧条被指针盖住。只点 `now-last_seen<=fresh` 的「新鲜」条，避免对已消失的**幽灵条**误点（这是反复出现的 false-click 根因）。角速度用 **0.2s 基线 + 物理封顶** 估(防微小 dt 尖刺)；命中=指针(+`latency` 提前量)落入条角区；点完给该条 `spent` 冷却防重复点。
- **精度优先**：游戏「落空(点空)会被短暂禁用」，故宁可少点不乱点；`boost`(右键加速)默认**关**（开了指针变快、窄条可能被帧间跨过而误点/漏点）。
- **几何默认即真值**：`config.json`(或缺省) 的 `center=(956,700)`/`R=182` 取自真机采集；`--probe` 目视确认各带环压在轨道上。

## 全脚本通用约定

- **GBK 控制台兜底**：每个脚本开头都有 `sys.stdout.reconfigure(errors="replace")`。Windows GBK 控制台打印 `⚠/▶/✅` 等符号会崩，靠这个替换而非报错。新脚本沿用此模式。
- **DPI 感知**：`ctypes.windll.user32.SetProcessDPIAware()` 让截屏坐标与鼠标坐标一致，截屏类脚本必须有。
- **游戏需窗口化/无边框**：独占全屏下 `mss` 截到黑屏（脚本会用画面均值<8 提示）。点击/热键不生效时需**管理员**运行（游戏以管理员运行时尤其）。
- **热键统一 F8 开/暂停、F9 退出**。
- **角度约定**：`arctan2(dy,dx)` 得 0–359°；角差一律用 `ang_diff` 归一到 (-180,180]。
- **JSON 配置惯例**：脚本内 `DEFAULT_*` 字典为默认值，读配置时 `{**默认, **用户}` 合并补全缺省键；改默认改脚本里的字典，调参改 `*.json`。

## 产物与定位

- 运行 `--probe` 会生成 `_probe.jpg`（几何自检叠加图）；`config.json` 由 `--calibrate` 写入。这些是运行产物，不必手编辑。
- `game-*.png` 是游戏画面参考截图（介绍页 + 转盘），用于理解视觉元素。
- 该游戏带排行榜，自动点击属第三方辅助、**可能违反 ToS 有封号风险**，仓库定位为个人离线学习用途。
