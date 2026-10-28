# -*- coding: utf-8 -*-
"""
读 adas.dbc，把里面定义的报文和信号打印成人能看的形式。

用途：
  1. 确认 DBC 语法能被 cantools 正确解析
  2. 写代码前先看清每个信号的 bit 位置、换算系数、取值范围

运行：.venv/Scripts/python.exe src/dbc_report.py
"""
import os
import sys

import cantools

HERE = os.path.dirname(os.path.abspath(__file__))
DBC = os.path.join(os.path.dirname(HERE), 'adas.dbc')


def main():
    db = cantools.database.load_file(DBC, database_format='dbc')

    print('=' * 78)
    print(f'DBC: {os.path.basename(DBC)}   报文 {len(db.messages)} 条   节点 {[n.name for n in db.nodes]}')
    print('=' * 78)

    for msg in db.messages:
        cyc = msg.cycle_time if msg.cycle_time else '-'
        print(f'\n[{msg.name}]  id=0x{msg.frame_id:X} ({msg.frame_id})  '
              f'dlc={msg.length}  发送={",".join(msg.senders)}  周期={cyc}ms')
        print(f'  {"信号":<14}{"起始位":>6}{"长度":>6}  {"字节序":<8}'
              f'{"factor":>8}{"offset":>8}  {"范围":<18}{"单位":<7}接收节点')
        print('  ' + '-' * 74)
        for s in msg.signals:
            order = 'Intel(小端)' if s.byte_order == 'little_endian' else 'Motorola(大端)'
            rng = f'[{s.minimum}, {s.maximum}]'
            print(f'  {s.name:<14}{s.start:>6}{s.length:>6}  {order:<12}'
                  f'{s.scale:>8}{s.offset:>8}  {rng:<18}{s.unit or "-":<7}'
                  f'{",".join(s.receivers)}')

        for s in msg.signals:
            if s.choices:
                pairs = ', '.join(f'{k}={v}' for k, v in s.choices.items())
                print(f'  ↳ 枚举 {s.name}: {pairs}')

    print('\n' + '=' * 78)
    print('✅ DBC 解析通过')
    print('=' * 78)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f'❌ DBC 解析失败: {type(e).__name__}: {e}')
        sys.exit(1)