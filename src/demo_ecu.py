# -*- coding: utf-8 -*-
"""
最小闭环：让虚拟 ECU 在虚拟总线上跑一遍逼近场景，看状态机怎么迁移。

这一步不测任何东西，只确认「激励 → ECU → 响应」这条链路是通的。

运行：.venv/Scripts/python.exe src/demo_ecu.py
"""
import can
import cantools

import ecu_sim
from ecu_sim import AebEcu, STATE_NAME

DBC = 'adas.dbc'
STIM_CH = 'adas_stim'      # 测试框架 → ECU
CMD_CH = 'adas_cmd'        # ECU → 测试框架

# 逼近场景：(车速 km/h, 距离 m, 相对速度 m/s)。相对速度为负 = 正在接近
SCENARIO = [
    (80.0, 100.0, -10.0),   # TTC 10.0   → OFF
    (80.0,  30.0, -10.0),   # TTC  3.0   → OFF（刚好在解除边界）
    (80.0,  25.0, -10.0),   # TTC  2.5   → ARMED（踩在预警阈值上）
    (80.0,  20.0, -10.0),   # TTC  2.0   → ARMED
    (80.0,  14.0, -10.0),   # TTC  1.4   → ACTIVE（BUG#3 在这里触发：车速 > 60）
    (80.0,  10.0, -10.0),   # TTC  1.0   → ACTIVE，紧急值
    ( 0.5,  10.0, -10.0),   # 刹停了     → 应回 ARMED，实测会掉到 OFF（BUG#2）
    ( 0.5,  10.0, -10.0),   # 再来一帧
]


def main():
    db = cantools.database.load_file(DBC, database_format='dbc')
    msg_vcu = db.get_message_by_name('VCU_Status')
    msg_obj = db.get_message_by_name('ADAS_Obj')
    msg_cmd = db.get_message_by_name('AEB_Cmd')

    bench_tx = can.Bus(interface='virtual', channel=STIM_CH)
    bench_rx = can.Bus(interface='virtual', channel=CMD_CH)
    ecu_rx = can.Bus(interface='virtual', channel=STIM_CH)
    ecu_tx = can.Bus(interface='virtual', channel=CMD_CH)

    ecu = AebEcu(ecu_rx, ecu_tx, db)

    print('=' * 78)
    print('最小闭环：激励 → 虚拟 ECU → 响应')
    print('=' * 78)
    print(f'{"步":>3}  {"车速":>7}  {"距离":>7}  {"TTC":>6}  '
          f'{"迁移":>16}  {"减速度":>8}  预警')
    print('-' * 78)

    for i, (speed, dist, rel) in enumerate(SCENARIO, 1):
        bench_tx.send(can.Message(arbitration_id=msg_vcu.frame_id,
                                  data=msg_vcu.encode({'VehicleSpeed': speed, 'YawRate': 0.0}),
                                  is_extended_id=False))
        bench_tx.send(can.Message(arbitration_id=msg_obj.frame_id,
                                  data=msg_obj.encode({'ObjDistance': dist,
                                                       'RelSpeed': rel,
                                                       'ObjValid': 1}),
                                  is_extended_id=False))

        prev, st, warn, decel = ecu.step()

        # 从总线上读回 ECU 真正发出来的帧，而不是信任返回值
        frame = bench_rx.recv(timeout=1.0)
        assert frame is not None, '总线上没收到 AEB_Cmd'
        # cantools 默认会把 DBC 里 VAL_ 定义的枚举解成字符串（'OFF'），
        # 断言要的是数字，所以 decode_choices=False
        got = msg_cmd.decode(bytes(frame.data), decode_choices=False)

        ttc = ecu_sim.ttc_of(dist, rel)
        ttc_txt = f'{ttc:.2f}' if ttc is not None else '--'
        flag = '' if (st == prev or st in ecu_sim.LEGAL_TRANSITIONS[prev]) else '  ← 非法迁移'
        print(f'{i:>3}  {speed:>7.1f}  {dist:>7.1f}  {ttc_txt:>6}  '
              f'{STATE_NAME[prev]:>7} → {STATE_NAME[st]:<7}  '
              f'{got["DecelReq"]:>8.2f}  {got["AEB_Warning"]}{flag}')

        if got['AEB_State'] != st:
            print(f'      [DEBUG] 原始帧 {bytes(frame.data).hex(" ")}  '
                  f'解码 {got}  期望 state={st} decel={decel}')
        assert got['AEB_State'] == st, '总线上的状态和 ECU 内部状态不一致'
        assert abs(got['DecelReq'] - decel) < 0.011, '总线上的减速度对不上'

    for b in (bench_tx, bench_rx, ecu_rx, ecu_tx):
        b.shutdown()

    print('-' * 78)
    print('✅ 链路通：激励帧被 ECU 吃进去，AEB_Cmd 从总线回到测试端')


if __name__ == '__main__':
    main()