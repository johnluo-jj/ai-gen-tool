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

# —— 实机 bot ——
python unlock.py --calibrate              # 标定圆心/半径 -> config.json(换分辨率/窗口要重标; 同机可跳过, 默认值即真值)
python unlock.py --probe                  # 抓一帧自检几何对齐, 存 _probe.jpg, 不点击
python unlock.py --debug                  # 实机运行 + 识别画面窗口
python unlock.py                          # 正式跑; F8 开/暂停, F9 退出

# —— 离线验证(改检测/命中逻辑后必跑)——
python detector.py captures/run_xxxx      # 逐帧检测自检 -> _check/(标注图 + 健康报告)
python unlock.py --sim captures/run_xxxx  # 在录好的帧上跑完整命中逻辑 -> _sim/(每次点击标注图 + 命中/误点统计)

# —— 采集新测试帧 ——
python capture.py [--seconds N] [--select]   # 高速连拍屏幕 -> captures/run_*/(帧 + frames.json)
```

**没有测试框架 / lint / 构建步骤。** 正确性靠「采集真机帧 → `detector.py`/`unlock.py --sim` 离线在帧上验证 → 实机 `--probe`/`--debug` 目视」这条回路保证。改了检测/命中逻辑，**先在已有 `captures/run_*` 帧上 `--sim` 回归**（看「命中/误点」数）再上实机。

## dial-lock 架构

数据流：`capture.py` 采帧 → `detector.py` 逐帧检测 → `unlock.py` 跨帧决策(记住条)并实机点击。

```
capture.py ─> captures/run_*/(帧+frames.json)
                       │
            detector.py: detect()  无状态逐帧检测(指针角 + 条)
                       │  ┌─ detector.py <dir>            离线逐帧自检 -> _check/
                       ▼  │
            unlock.py: SpeedEstimator + BarTracker + decide()  有状态命中决策
                          ├─ unlock.py --sim <dir>        离线跑完整命中逻辑 -> _sim/
                          └─ unlock.py                    实机截屏循环 + 左键点击
```

- **`detector.py` 是检测的唯一真相源**，无状态纯函数。`unlock.py` 顶部 `from detector import Geom, detect, draw, DEFAULT_P, ang_diff` 复用，离线/在线走同一份检测 —— 改检测只改 `detector.py`。
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

## Git 与产物

- `capture.py --push` 会**自动 `git add/commit/push`**（把真机帧发出去）。注意这与「默认禁止写类 git 命令」的全局准则冲突：除非用户明确要采集并推送，否则不要主动用 `--push`。
- `captures/run_*`、`captures/botrec_*` 是**采集/录制的真机帧**（测试数据/ground truth）；其下 `_check/`、`_sim/` 是离线验证生成的标注图。这些可能体积很大；改代码时不要手动编辑这些目录的内容。
- 该游戏带排行榜，自动点击属第三方辅助、**可能违反 ToS 有封号风险**，仓库定位为个人离线学习用途。
