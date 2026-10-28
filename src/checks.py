# -*- coding: utf-8 -*-
"""
断言层 —— 四类独立检查。

设计原则：每类只看自己那一个维度，互不依赖。
缺陷被检出时，能立刻定位到"哪一条规则被违反"，
而不是笼统地说"这步没过"。

四类：
    1. RANGE       值域     —— 判据直接读 DBC 的 [min|max]
    2. TRANSITION  状态机   —— 对照合法迁移表
    3. CYCLE       周期     —— 相邻帧间隔
    4. BEHAVIOUR   行为     —— 给定输入，输出是否落在期望窗口内
"""
from dataclasses import dataclass, field

EPS = 1e-9


@dataclass
class Finding:
    kind: str                      # RANGE / TRANSITION / CYCLE / BEHAVIOUR
    message: str
    detail: dict = field(default_factory=dict)

    def __str__(self):
        return f'[{self.kind}] {self.message}'


def _state_name(v, names):
    return names.get(v, str(v)) if names else str(v)


def _resolve_state(v, names):
    """用例里可以写 'ACTIVE'，也可以写 2。"""
    if isinstance(v, str):
        inv = {name.upper(): code for code, name in (names or {}).items()}
        return inv.get(v.upper(), v)
    return v


# ── 1. 值域 ────────────────────────────────────────────────
def check_range(db, msg_name, decoded, signals=None):
    """
    按 DBC 里每个信号的 [min|max] 判定。

    注意：DBC 的范围不是注释，是验收标准 —— 这里直接读它。
    想改判据就改 DBC，不要在代码里另写一套魔法数字。
    """
    msg = db.get_message_by_name(msg_name)
    out = []
    for sig in msg.signals:
        if signals and sig.name not in signals:
            continue
        if sig.name not in decoded:
            continue
        val = decoded[sig.name]
        if not isinstance(val, (int, float)):
            continue
        unit = sig.unit or ''
        if sig.minimum is not None and val < sig.minimum - EPS:
            out.append(Finding('RANGE',
                f'{msg_name}.{sig.name} = {val} 低于下限 {sig.minimum} {unit}'.strip(),
                {'signal': sig.name, 'value': val,
                 'min': sig.minimum, 'max': sig.maximum}))
        if sig.maximum is not None and val > sig.maximum + EPS:
            out.append(Finding('RANGE',
                f'{msg_name}.{sig.name} = {val} 超出上限 {sig.maximum} {unit}'.strip(),
                {'signal': sig.name, 'value': val,
                 'min': sig.minimum, 'max': sig.maximum}))
    return out


# ── 2. 状态迁移 ────────────────────────────────────────────
def check_transition(prev, nxt, legal, names=None):
    """
    对照合法迁移表判定。

    坑：状态不变（next == prev）是合法的，迁移表只列"能搬到哪些新状态"，
    直接拿 `nxt in legal[prev]` 判会把 OFF→OFF 报成非法。
    """
    if nxt == prev:
        return []
    if nxt in legal.get(prev, set()):
        return []
    return [Finding('TRANSITION',
        f'非法状态迁移 {_state_name(prev, names)} → {_state_name(nxt, names)}',
        {'prev': prev, 'next': nxt,
         'allowed': sorted(legal.get(prev, set()))})]


# ── 3. 周期超时 ────────────────────────────────────────────
def check_cycle(timestamps, expected_ms, limit_ms=None, label='帧'):
    """timestamps 为单调递增的秒级时间戳。at 最小间隔不足不报，只报超时。"""
    if len(timestamps) < 2:
        return []
    limit = limit_ms if limit_ms is not None else expected_ms * 1.5
    out = []
    for i in range(1, len(timestamps)):
        gap_ms = (timestamps[i] - timestamps[i - 1]) * 1000.0
        if gap_ms > limit:
            out.append(Finding('CYCLE',
                f'第 {i} 帧间隔 {gap_ms:.1f} ms，超过上限 {limit:.1f} ms'
                f'（期望周期 {expected_ms} ms）',
                {'index': i, 'gap_ms': round(gap_ms, 2),
                 'limit_ms': limit, 'expected_ms': expected_ms}))
    return out


# ── 4. 行为 ────────────────────────────────────────────────
def check_behaviour(actual, expect, names=None):
    """
    actual: 从总线解码回来的响应 dict
    expect: {'state': 'ACTIVE', 'warning': 1, 'decel_min': -9, 'decel_max': -6}
    """
    out = []
    if actual is None:
        return [Finding('BEHAVIOUR', '总线上没有收到响应帧')]

    if 'state' in expect:
        want = _resolve_state(expect['state'], names)
        got = actual.get('AEB_State')
        if got != want:
            out.append(Finding('BEHAVIOUR',
                f'AEB_State 期望 {_state_name(want, names)}，'
                f'实际 {_state_name(got, names)}',
                {'expected': want, 'actual': got}))

    if 'warning' in expect:
        got = actual.get('AEB_Warning')
        if got != expect['warning']:
            out.append(Finding('BEHAVIOUR',
                f'AEB_Warning 期望 {expect["warning"]}，实际 {got}',
                {'expected': expect['warning'], 'actual': got}))

    if 'decel_min' in expect or 'decel_max' in expect:
        lo = expect.get('decel_min', -1e9)
        hi = expect.get('decel_max', 1e9)
        got = actual.get('DecelReq')
        if got is None or not (lo - EPS <= got <= hi + EPS):
            out.append(Finding('BEHAVIOUR',
                f'DecelReq 期望落在 [{lo}, {hi}]，实际 {got}',
                {'expected': [lo, hi], 'actual': got}))

    return out