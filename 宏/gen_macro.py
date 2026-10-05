"""生成 Razer Synapse 可导入的「连续按下-松开」按键宏 XML。

格式照抄雷云导出的原始文件 `L 0.03秒.xml`(UTF-8 无 BOM、CRLF、末尾无换行)。
文件名里的秒数 = 一次「按下→下一次按下」的周期; 雷云里选循环模式即可一直按。

模拟真人敲键, 而不是把周期对半分:
  - 按住时长(dwell)和周期无关, 真人点按一般 60~130ms, 用对数正态采样;
  - 周期带约 3% 的抖动, 且前后相关(AR(1) 慢漂移, 真人节奏是慢慢快/慢慢慢);
  - 松开时长 = 周期 - 按住时长;
  - 最后把整组周期之和校正到 次数 x 周期, 保证循环跑久了平均频率与文件名一致。

用法: python gen_macro.py            # 按 TARGETS 重新生成
      python gen_macro.py --seed 1   # 固定随机种子, 可复现
"""
import os
import re
import sys
import uuid
import random
import argparse

sys.stdout.reconfigure(errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))

# Makecode 是 Windows 虚拟键码(VK, 字母键即大写 ASCII), 不是扫描码。
# 依据: 雷云原始导出 `L 0.03秒.xml` 里 L=76=VK_L; 填扫描码(V=47)导入后不识别。
VK = {"V": 86, "L": 76}

# 文件名 -> (按键, 周期秒)
TARGETS = {
    "V 0.06秒": ("V", 0.06),
    "V 1.0秒": ("V", 1.0),
    "V 1.9秒": ("V", 1.9),
    "V 2.3秒": ("V", 2.3),
    "V 3.0秒": ("V", 3.0),
}

CYCLES = 20        # 每个宏里按几次(同原始文件), 循环模式下重复播放
PERIOD_CV = 0.03   # 周期抖动(标准差/周期)
DRIFT = 0.5        # AR(1) 相关系数: 0=每次独立, 越大节奏越"连贯"
MIN_MS = 5         # 雷云单段延迟下限(原始文件最小 0.005)


def dwell_params(period):
    """按住时长的 (中位数ms, 对数标准差, 下限ms, 上限ms)。快速连点时手指按得更短。"""
    if period < 0.2:
        # 连打: 按住约占周期的 40%, 但不低于 MIN_MS
        med = period * 1000 * 0.4
        return med, 0.15, max(MIN_MS, med * 0.6), med * 1.5
    return 90, 0.2, 55, 150


def gen_timings(period, rng):
    """返回 [(按住ms, 松开ms), ...], 周期之和精确等于 CYCLES*period。"""
    p_ms = period * 1000
    med, sig, lo, hi = dwell_params(period)

    # 周期: AR(1) 抖动, 平稳方差 = (cv*p)^2
    sd = PERIOD_CV * p_ms
    e = 0.0
    periods = []
    for _ in range(CYCLES):
        e = DRIFT * e + rng.gauss(0, sd * (1 - DRIFT ** 2) ** 0.5)
        periods.append(p_ms + e)
    # 校正均值, 再取整到 ms, 余数随机摊到各次上
    shift = p_ms - sum(periods) / CYCLES
    periods = [round(x + shift) for x in periods]
    rest = round(p_ms * CYCLES) - sum(periods)
    for _ in range(abs(rest)):
        periods[rng.randrange(CYCLES)] += 1 if rest > 0 else -1

    out = []
    for per in periods:
        dw = min(hi, max(lo, rng.lognormvariate(0, sig) * med))
        dw = round(dw)
        gap = per - dw
        if gap < MIN_MS:          # 极端情况: 挤掉按住时长保证松开够长
            dw, gap = per - MIN_MS, MIN_MS
        out.append((dw, gap))
    return out


KEY_EVENT = """      <MacroEvent>
         <Type>1</Type>
         <Id>{id}</Id>
         <KeyEvent>
            <Makecode>{code}</Makecode>
            <State>{state}</State>
         </KeyEvent>
         <selected>false</selected>
         <isPairing>false</isPairing>
      </MacroEvent>"""

DELAY_EVENT = """      <MacroEvent>
         <Type>0</Type>
         <Number>{sec:.3f}</Number>
         <selected>false</selected>
      </MacroEvent>"""

HEAD = """<Macro>
   <Name>{name}</Name>
   <MacroEvents>
      <MacroEvent>
         <Type>actionBar</Type>
         <recordProfile>
            <mmtSetting>0</mmtSetting>
         </recordProfile>
         <selected>false</selected>
      </MacroEvent>"""

TAIL = """   </MacroEvents>
   <DelaySetting>0</DelaySetting>
   <Guid>{guid}</Guid>
   <Version>4</Version>
   <MouseMoveType>none</MouseMoveType>
</Macro>"""


def build_xml(name, code, timings, guid):
    parts = [HEAD.format(name=name)]
    for i, (dw, gap) in enumerate(timings):
        parts.append(KEY_EVENT.format(id=i, code=code, state=0))   # 0=按下
        parts.append(DELAY_EVENT.format(sec=dw / 1000))
        parts.append(KEY_EVENT.format(id=i, code=code, state=1))   # 1=松开
        parts.append(DELAY_EVENT.format(sec=gap / 1000))
    parts.append(TAIL.format(guid=guid))
    return "\n".join(parts).replace("\n", "\r\n")


def old_guid(path):
    """沿用已有文件的 Guid, 重新导入时雷云识别为同一个宏; 没有就新建。"""
    if os.path.exists(path):
        m = re.search(r"<Guid>([^<]+)</Guid>", open(path, encoding="utf-8").read())
        if m:
            return m.group(1)
    return str(uuid.uuid4())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    for name, (key, period) in TARGETS.items():
        path = os.path.join(HERE, name + ".xml")
        t = gen_timings(period, rng)
        xml = build_xml(name, VK[key], t, old_guid(path))
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(xml)
        dws = [d for d, _ in t]
        pers = [d + g for d, g in t]
        print(f"{name}: 键={key}(VK{VK[key]}) 平均周期 {sum(pers)/len(pers):.1f}ms "
              f"[{min(pers)}~{max(pers)}]  按住 {min(dws)}~{max(dws)}ms")


if __name__ == "__main__":
    main()
