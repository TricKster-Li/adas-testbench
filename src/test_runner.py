# -*- coding: utf-8 -*-
"""
测试执行器：读 cases.yaml，驱动虚拟 ECU，跑四类断言，汇总结果。

用法：
    .venv/Scripts/python.exe src/test_runner.py
    .venv/Scripts/python.exe src/test_runner.py --only BHV
    .venv/Scripts/python.exe src/test_runner.py -v          # 打印每步明细

输出：
    控制台表格
    outputs/results.json   （给 report.py 生成报告用）
"""
import argparse
import json
import os
import sys
import time

import can
import cantools
import yaml

from ecu_sim import AebEcu, STATE_NAME, LEGAL_TRANSITIONS, OFF
from checks import (Finding, check_behaviour, check_cycle,
                    check_range, check_transition)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBC_PATH = os.path.join(ROOT, 'adas.dbc')
CASES_PATH = os.path.join(ROOT, 'cases', 'cases.yaml')
OUT_DIR = os.path.join(ROOT, 'outputs')

STIM_CH = 'adas_stim'      # 台架 → DUT
CMD_CH = 'adas_cmd'        # DUT → 台架


class Bench:
    """台架侧：发激励、收响应。和 DUT 只通过总线通信。"""

    def __init__(self, db):
        self.db = db
        self.msg_vcu = db.get_message_by_name('VCU_Status')
        self.msg_obj = db.get_message_by_name('ADAS_Obj')
        self.msg_cmd = db.get_message_by_name('AEB_Cmd')

        self.tx = can.Bus(interface='virtual', channel=STIM_CH)
        self.rx = can.Bus(interface='virtual', channel=CMD_CH)
        self.ecu_rx = can.Bus(interface='virtual', channel=STIM_CH)
        self.ecu_tx = can.Bus(interface='virtual', channel=CMD_CH)

    def new_ecu(self):
        """每条用例换一台干净的 ECU（状态归零）。"""
        self.drain()
        return AebEcu(self.ecu_rx, self.ecu_tx, self.db)

    def drain(self):
        for bus in (self.rx, self.ecu_rx):
            while bus.recv(timeout=0.0) is not None:
                pass

    def stimulate(self, step):
        speed = float(step.get('speed', 0.0))
        distance = float(step.get('distance', 300.0))
        rel = float(step.get('rel_speed', 0.0))
        obj_valid = int(step.get('obj_valid', 1))

        self.tx.send(can.Message(
            arbitration_id=self.msg_vcu.frame_id,
            data=self.msg_vcu.encode({'VehicleSpeed': speed,
                                      'YawRate': float(step.get('yaw_rate', 0.0))}),
            is_extended_id=False))
        self.tx.send(can.Message(
            arbitration_id=self.msg_obj.frame_id,
            data=self.msg_obj.encode({'ObjDistance': distance,
                                      'RelSpeed': rel,
                                      'ObjValid': obj_valid}),
            is_extended_id=False))

    def read_cmd(self, timeout=1.0):
        frame = self.rx.recv(timeout=timeout)
        if frame is None:
            return None, None
        decoded = self.msg_cmd.decode(bytes(frame.data), decode_choices=False)
        return decoded, time.monotonic()

    def shutdown(self):
        for bus in (self.tx, self.rx, self.ecu_rx, self.ecu_tx):
            bus.shutdown()


def _summarize(findings):
    kinds = sorted({f.kind for f in findings})
    return ','.join(kinds) if kinds else '-'


def run_step_case(bench, case, verbose=False):
    """行为 / 迁移 / 值域三类用例：preheat 带到起点，steps 里逐步施加。"""
    ecu = bench.new_ecu()
    findings = []
    trace = []
    prev_state = OFF

    preheat = list(case.get('preheat') or [])
    steps = list(case.get('steps') or [])
    all_steps = preheat + steps
    n_pre = len(preheat)

    for i, step in enumerate(all_steps):
        bench.stimulate(step)
        prev_state, new_state, warn, decel = ecu.step()
        got, ts = bench.read_cmd()

        # 迁移 + 值域：每一步都查
        findings += check_transition(prev_state, new_state, LEGAL_TRANSITIONS, STATE_NAME)
        if got is not None:
            findings += check_range(bench.db, 'AEB_Cmd', got)

        phase = '预热' if i < n_pre else '测试'
        trace.append({
            'phase': phase,
            'input': step,
            'prev': STATE_NAME.get(prev_state),
            'state': STATE_NAME.get(new_state),
            'decel': decel,
            'wire': got,
        })

        # 行为断言只落在最后一个「测试」步
        if i >= n_pre and i == len(all_steps) - 1:
            findings += check_behaviour(got, case.get('expect') or {}, STATE_NAME)

        if verbose:
            print(f'      {phase}[{i - n_pre:>2}] {STATE_NAME[prev_state]:>7} → '
                  f'{STATE_NAME[new_state]:<7} decel={decel:>7.2f} '
                  f'raw={got["DecelReq"] if got else "None"}')

    return findings, trace


def run_cycle_case(bench, case):
    """周期类用例：按固定节奏发帧，人为制造卡顿，看检查器抓不抓得住。"""
    ecu = bench.new_ecu()
    cycle_ms = int(case.get('cycle_ms', 50))
    frames = int(case.get('frames', 20))
    stall_at = case.get('stall_at')
    stall_ms = int(case.get('stall_ms', 0))

    tstamps = []
    for i in range(frames):
        if stall_at is not None and i == stall_at:
            time.sleep(stall_ms / 1000.0)      # 模拟 ECU 卡顿/丢帧

        bench.stimulate({'speed': 50, 'distance': 100, 'rel_speed': -10})
        ecu.step()
        _, ts = bench.read_cmd()
        if ts is not None:
            tstamps.append(ts)

        time.sleep(cycle_ms / 1000.0)

    findings = check_cycle(tstamps, cycle_ms, case.get('limit_ms'))

    want = case.get('expect_finding')
    if want == 'CYCLE' and not findings:
        findings.append(Finding('CYCLE',
            '期望检出周期超时，但检查器没有报出任何超时'))
    if want is None and findings:
        pass   # 多报出来的超时本身就是失败，保留

    trace = [{'phase': 'period', 'frames': frames,
              'gaps_ms': [round((b - a) * 1000, 1) for a, b in zip(tstamps, tstamps[1:])]}]
    return findings, trace


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', help='只跑 id 前缀，例如 BHV / TRN / RNG / CYC')
    ap.add_argument('-v', '--verbose', action='store_true', help='打印每步明细')
    args = ap.parse_args()

    db = cantools.database.load_file(DBC_PATH, database_format='dbc')
    with open(CASES_PATH, encoding='utf-8') as f:
        doc = yaml.safe_load(f)
    cases = doc['cases']
    if args.only:
        cases = [c for c in cases if str(c['id']).upper().startswith(args.only.upper())]

    bench = Bench(db)
    results = []

    print('=' * 92)
    print(f'ADAS 测试台 · 用例执行   共 {len(cases)} 条   DUT = 虚拟 AEB ECU')
    print('=' * 92)
    print(f'{"ID":<10}{"类别":<12}{"结果":<8}{"断言":<14}名称')
    print('-' * 92)

    t0 = time.monotonic()
    for case in cases:
        cat = case.get('category', 'behaviour')
        if cat == 'cycle':
            findings, trace = run_cycle_case(bench, case)
        else:
            findings, trace = run_step_case(bench, case, verbose=args.verbose)

        # 负向用例：用例的目的是「验证检查器能不能抓住故障」，
        # 所以检出指定断言才算通过，而不是没报警才通过。
        want = case.get('expect_finding')
        if want:
            passed = any(f.kind == want for f in findings)
        else:
            passed = len(findings) == 0
        results.append({
            'id': case['id'],
            'name': case.get('name', ''),
            'category': cat,
            'passed': passed,
            'findings': [{'kind': f.kind, 'message': f.message, 'detail': f.detail}
                         for f in findings],
            'trace': trace,
        })

        mark = 'PASS' if passed else 'FAIL'
        print(f'{case["id"]:<10}{cat:<12}{mark:<8}{_summarize(findings):<14}'
              f'{case.get("name", "")}')
        if not passed:
            for f in findings:
                print(f'{"":<10}{"":<12}{"":<8}{"":<14}↳ {f}')

    elapsed = time.monotonic() - t0
    bench.shutdown()

    total = len(results)
    n_pass = sum(1 for r in results if r['passed'])
    n_fail = total - n_pass

    print('-' * 92)
    print(f'合计 {total} 条：PASS {n_pass}   FAIL {n_fail}   耗时 {elapsed:.2f}s')
    print('=' * 92)

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, 'results.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'summary': {'total': total, 'pass': n_pass, 'fail': n_fail,
                               'elapsed_s': round(elapsed, 2),
                               'dut': 'virtual AEB ECU'},
                   'results': results}, f, ensure_ascii=False, indent=2)
    print(f'结果已写入 {out}')
    return 0 if n_fail == 0 else 1


if __name__ == '__main__':
    sys.exit(main())