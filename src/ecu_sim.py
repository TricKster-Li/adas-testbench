# -*- coding: utf-8 -*-
"""
虚拟 AEB ECU —— 本项目的被测对象（DUT, Device Under Test）。

总线行为：
    收  VCU_Status (0x100)  车速
    收  ADAS_Obj   (0x110)  目标距离 / 相对速度 / 目标有效标志
    发  AEB_Cmd    (0x120)  AEB 状态 / 预警标志 / 减速度请求

判决依据 TTC（Time To Collision，碰撞时间）：

        TTC = 距离 / 接近速度
        接近速度 = -RelSpeed          （RelSpeed 为负 = 正在接近）

三级阈值，带迟滞避免状态抖动：
        预警 2.5 s  → 进入 ARMED
        制动 1.5 s  → 进入 ACTIVE
        解除 3.0 s  → 退回 OFF

状态机：

        OFF ──TTC<=2.5──▶ ARMED ──TTC<=1.5──▶ ACTIVE
         ▲                  │                    │
         └──TTC>3.0─────────┘                    │
         ▲                                       │
         └────────── 危险解除（先回 ARMED）◀──────┘

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
  本文件里带 "BUG#" 注释的三处是**故意植入的缺陷**，
  供测试框架检出。它们不是笔误，是本项目的测试靶子。
  对应的缺陷说明见 README 的「缺陷清单」。
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
"""
import can

# 状态取值，与 DBC 的 VAL_ 表一致
OFF, ARMED, ACTIVE, FAULT = 0, 1, 2, 3
STATE_NAME = {OFF: 'OFF', ARMED: 'ARMED', ACTIVE: 'ACTIVE', FAULT: 'FAULT'}

# 合法状态迁移表 —— 测试框架用它判断「非法迁移」
LEGAL_TRANSITIONS = {
    OFF:    {ARMED, FAULT},
    ARMED:  {OFF, ACTIVE, FAULT},
    ACTIVE: {ARMED, FAULT},
    FAULT:  {OFF},
}

CFG = {
    'ttc_warn': 2.5,        # 预警阈值 s
    'ttc_brake': 1.5,       # 制动阈值 s
    'ttc_clear': 3.0,       # 解除阈值 s（迟滞）
    'decel_normal': -6.0,   # 常规制动 m/s²
    'decel_emergency': -9.0,  # 紧急制动 m/s²
    'decel_bug': -12.5,     # BUG#3 用的越界值（DBC 范围 -12~0）
    'speed_stop': 0.5,      # 判定「已停车」的车速 km/h
    'speed_bug': 60.0,      # BUG#3 的触发车速 km/h
}


def ttc_of(distance: float, rel_speed: float):
    """TTC = 距离 / 接近速度。不接近（或正在远离）时返回 None。"""
    closing = -rel_speed
    if closing <= 0.1:
        return None
    return distance / closing


def decide(prev: int, speed: float, obj_valid: bool,
           distance: float, rel_speed: float, cfg: dict = CFG):
    """
    单步判决（纯函数，不碰总线，方便单元测试）。

    返回 (state, warning, decel_request)
    """
    ttc = ttc_of(distance, rel_speed) if obj_valid else None

    # ── 目标丢失 ────────────────────────────────────
    if ttc is None:
        if prev == ACTIVE:
            return ARMED, 1, 0.0     # 制动中目标消失：先回 ARMED，保持预警
        return OFF, 0, 0.0

    # ── 数据合理性（传感器数据不可信 → 故障）────────
    if distance < 0 or distance > 300 or not (-100 <= rel_speed <= 100):
        return FAULT, 0, 0.0

    # ── OFF ─────────────────────────────────────────
    if prev == OFF:
        if ttc <= cfg['ttc_warn']:
            return ARMED, 1, 0.0
        return OFF, 0, 0.0

    # ── ARMED ───────────────────────────────────────
    if prev == ARMED:
        if ttc > cfg['ttc_clear']:
            return OFF, 0, 0.0
        if ttc < cfg['ttc_brake']:                      # BUG#1：应为 <=
            decel = cfg['decel_emergency'] if ttc < 1.0 else cfg['decel_normal']
            if speed > cfg['speed_bug']:                # BUG#3：高速输出越界值
                decel = cfg['decel_bug']
            return ACTIVE, 1, decel
        return ARMED, 1, 0.0

    # ── ACTIVE ──────────────────────────────────────
    if prev == ACTIVE:
        if speed <= cfg['speed_stop']:
            return OFF, 0, 0.0                          # BUG#2：应回 ARMED
        decel = cfg['decel_emergency'] if ttc < 1.0 else cfg['decel_normal']
        if speed > cfg['speed_bug']:                    # BUG#3
            decel = cfg['decel_bug']
        return ACTIVE, 1, decel

    return OFF, 0, 0.0


class AebEcu:
    """
    把 decide() 挂到总线上。

    rx_bus 收激励帧，tx_bus 发 AEB_Cmd。测试框架在另外两个 Bus 实例上
    收发，所以激励通道和响应通道要分开，避免自己的帧回灌进自己的队列。
    """

    def __init__(self, rx_bus, tx_bus, db, cfg: dict = CFG):
        self.rx = rx_bus
        self.tx = tx_bus
        self.cfg = cfg
        self.msg_vcu = db.get_message_by_name('VCU_Status')
        self.msg_obj = db.get_message_by_name('ADAS_Obj')
        self.msg_cmd = db.get_message_by_name('AEB_Cmd')

        # 内部输入缓存（相当于 ECU 的 RAM）
        self.speed = 0.0
        self.distance = 300.0
        self.rel_speed = 0.0
        self.obj_valid = False
        self.state = OFF

    def _on_frame(self, frame: can.Message):
        if frame.arbitration_id == self.msg_vcu.frame_id:
            d = self.msg_vcu.decode(bytes(frame.data))
            self.speed = d['VehicleSpeed']
        elif frame.arbitration_id == self.msg_obj.frame_id:
            d = self.msg_obj.decode(bytes(frame.data))
            self.distance = d['ObjDistance']
            self.rel_speed = d['RelSpeed']
            self.obj_valid = bool(d['ObjValid'])

    def step(self):
        """把激励通道里积压的帧全部吃掉 → 判决 → 发一帧 AEB_Cmd。"""
        while True:
            frame = self.rx.recv(timeout=0.0)
            if frame is None:
                break
            self._on_frame(frame)

        prev = self.state
        st, warn, decel = decide(prev, self.speed, self.obj_valid,
                                 self.distance, self.rel_speed, self.cfg)
        self.state = st

        payload = self.msg_cmd.encode(
            {'AEB_State': st, 'AEB_Warning': warn, 'DecelReq': decel},
            strict=False,      # 允许把越界值写出去 —— BUG#3 需要
        )
        self.tx.send(can.Message(
            arbitration_id=self.msg_cmd.frame_id,
            data=bytes(payload),
            is_extended_id=False,
        ))
        return prev, st, warn, decel