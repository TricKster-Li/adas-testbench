# -*- coding: utf-8 -*-
"""
环境自检 —— 搭项目之前先确认两件事能跑：

  1. python-can 的 virtual 接口能不能在同一进程内让两个 Bus 实例互通
  2. cantools 能不能解析 DBC 字符串，并完成信号的编码 / 解码

两个都过，才往下写 ECU 和测试框架。

运行：.venv/Scripts/python.exe smoke_test.py
"""
import can
import cantools

CHANNEL = 'adas_smoke'

# 最小 DBC：一条报文，两个信号
#   BO_ 256 = 0x100，DLC 8，发送节点 VCU
#   SG_ 起始位|长度@字节序符号 (factor,offset) [min|max] "单位" 接收节点
DBC_SAMPLE = '''
VERSION ""

NS_ :

BS_:

BU_: VCU ADAS

BO_ 256 VCU_Status: 8 VCU
 SG_ VehicleSpeed : 0|16@1+ (0.01,0) [0|250] "km/h" ADAS
 SG_ YawRate : 16|16@1- (0.01,0) [-100|100] "deg/s" ADAS

CM_ SG_ 256 VehicleSpeed "本车车速";
'''


def check_virtual_bus():
    """两个 Bus 实例挂在同一个虚拟通道上互发。"""
    tx = can.Bus(interface='virtual', channel=CHANNEL)
    rx = can.Bus(interface='virtual', channel=CHANNEL)

    frame = can.Message(
        arbitration_id=0x100,
        data=[0x11, 0x22, 0x33, 0x44, 0x00, 0x00, 0x00, 0x00],
        is_extended_id=False,
    )
    tx.send(frame)
    got = rx.recv(timeout=2.0)

    tx.shutdown()
    rx.shutdown()

    assert got is not None, '2 秒内没收到帧 —— 虚拟通道没通'
    assert got.arbitration_id == 0x100, f'ID 不对：{hex(got.arbitration_id)}'
    assert bytes(got.data[:4]) == bytes([0x11, 0x22, 0x33, 0x44]), '数据域不对'
    return got


def check_dbc_roundtrip():
    """DBC 解析 → 编码 → 解码，物理值要能原样回来。"""
    db = cantools.database.load_string(DBC_SAMPLE, database_format='dbc')
    msg = db.get_message_by_name('VCU_Status')

    phys = {'VehicleSpeed': 72.5, 'YawRate': -3.2}
    payload = msg.encode(phys)
    decoded = msg.decode(bytes(payload))
    return msg, bytes(payload), decoded


if __name__ == '__main__':
    print('=' * 58)
    print('环境自检')
    print('=' * 58)

    print('\n[1/2] 虚拟 CAN 总线')
    f = check_virtual_bus()
    print(f'      ✅ 收到 0x{f.arbitration_id:X}  dlc={f.dlc}  data={bytes(f.data).hex(" ")}')

    print('\n[2/2] cantools DBC 编解码')
    msg, payload, decoded = check_dbc_roundtrip()
    print(f'      ✅ 报文 {msg.name} (id=0x{msg.frame_id:X}, {msg.length} 字节)')
    print(f'         编码后 : {payload.hex(" ")}')
    print(f'         解码回 : VehicleSpeed={decoded["VehicleSpeed"]} km/h, '
          f'YawRate={decoded["YawRate"]} deg/s')
    assert abs(decoded['VehicleSpeed'] - 72.5) < 1e-6
    assert abs(decoded['YawRate'] - (-3.2)) < 1e-6

    print('\n' + '=' * 58)
    print('✅ 两项全过，环境可用')
    print('=' * 58)