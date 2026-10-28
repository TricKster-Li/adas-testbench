# -*- coding: utf-8 -*-
"""
生成测试报告。

输入：outputs/results.json       （test_runner.py 产出）
输出：outputs/report.html        （自包含，图片内嵌，可直接发人）
      outputs/waveform.png       （演示场景波形，讲解用）

波形图跑的是一个专门设计的逼近场景，把三个缺陷在同一条时间轴上暴露出来。

运行：.venv/Scripts/python.exe src/report.py
"""
import base64
import io
import json
import os
from datetime import datetime

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import cantools

from test_runner import Bench
from ecu_sim import STATE_NAME, CFG

matplotlib.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
matplotlib.rcParams['axes.unicode_minus'] = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBC_PATH = os.path.join(ROOT, 'adas.dbc')
OUT_DIR = os.path.join(ROOT, 'outputs')

# 演示场景：(车速 km/h, 距离 m)，相对速度固定 -10 m/s
# 设计意图：TTC 依次穿过 2.5 → 1.5 → 3.0 三条阈值线，
#           并在末尾停车，让三个缺陷落在同一张图上。
DEMO = [
    (80.0, 100.0),   # TTC 10.0
    (80.0,  50.0),   # 5.0
    (80.0,  30.0),   # 3.0
    (80.0,  25.0),   # 2.5  预警边界
    (80.0,  20.0),   # 2.0
    (80.0,  15.0),   # 1.5  制动边界 ← BUG#1 在这里
    (80.0,  14.0),   # 1.4  触发制动 ← BUG#3 越界
    (80.0,  10.0),   # 1.0
    (80.0,   5.0),   # 0.5
    (80.0,   0.5),   # 0.05
    ( 0.2,   0.5),   # 停车    ← BUG#2 非法迁移
]

DEFECTS = [
    {
        'id': 'BUG#1',
        'title': '制动边界判定漏触发',
        'where': 'ecu_sim.py · ARMED 分支',
        'cause': 'TTC 与制动阈值比较用了 `<`，应为 `<=`',
        'symptom': 'TTC 恰好等于 1.5 s 时停留在 ARMED，不进入 ACTIVE',
        'cases': ['BHV-006'],
        'checks': ['BEHAVIOUR'],
        'severity': '严重',
    },
    {
        'id': 'BUG#2',
        'title': '非法状态迁移',
        'where': 'ecu_sim.py · ACTIVE 分支',
        'cause': '停车后直接跳回 OFF，跳过 ARMED',
        'symptom': 'ACTIVE → OFF 迁移不在合法迁移表内，预警标志同时被清零',
        'cases': ['TRN-004'],
        'checks': ['TRANSITION', 'BEHAVIOUR'],
        'severity': '严重',
    },
    {
        'id': 'BUG#3',
        'title': '减速度请求越界',
        'where': 'ecu_sim.py · ARMED / ACTIVE 分支',
        'cause': '车速超过 60 km/h 时输出 -12.5 m/s²，超出 DBC 上限 -12',
        'symptom': '高速工况下 DecelReq 超出信号定义范围，下游可能饱和或拒收',
        'cases': ['BHV-012', 'RNG-004'],
        'checks': ['RANGE', 'BEHAVIOUR'],
        'severity': '严重',
    },
]


def run_demo(db):
    """跑演示场景，录下每一帧的输入与响应。"""
    bench = Bench(db)
    ecu = bench.new_ecu()
    rows = []
    for i, (speed, dist) in enumerate(DEMO):
        bench.stimulate({'speed': speed, 'distance': dist, 'rel_speed': -10})
        prev, st, warn, decel = ecu.step()
        got, _ = bench.read_cmd()
        rows.append({
            'i': i,
            'speed': speed,
            'distance': dist,
            'ttc': dist / 10.0,
            'prev': prev,
            'state': st,
            'decel': got['DecelReq'] if got else None,
        })
    bench.shutdown()
    return rows


def plot_waveform(rows, png_path):
    x = [r['i'] for r in rows]
    ttc = [r['ttc'] for r in rows]
    state = [r['state'] for r in rows]
    decel = [r['decel'] if r['decel'] is not None else 0.0 for r in rows]

    fig, axes = plt.subplots(3, 1, figsize=(11, 8.2), sharex=True,
                             gridspec_kw={'height_ratios': [1.15, 0.85, 0.85]})
    fig.suptitle('AEB 功能验证 · 演示场景波形（车速 80 km/h，相对速度 -10 m/s）',
                 fontsize=13, fontweight='bold', color='#14415C')

    # ── 1. TTC ──
    ax = axes[0]
    ax.plot(x, ttc, '-o', color='#14415C', lw=2, ms=5, label='TTC (s)')
    for y, label, color in ((CFG['ttc_warn'], '预警阈值 2.5s', '#B07A1E'),
                            (CFG['ttc_brake'], '制动阈值 1.5s', '#B4453C'),
                            (CFG['ttc_clear'], '解除阈值 3.0s', '#5A8A5A')):
        ax.axhline(y, ls='--', lw=1.1, color=color, alpha=.85)
        ax.text(len(x) - .6, y, label, fontsize=8.5, color=color,
                va='bottom', ha='right')
    ax.set_ylabel('TTC (s)')
    ax.set_ylim(-0.5, 11.5)
    ax.grid(alpha=.25)
    ax.legend(loc='upper right', fontsize=8.5)

    # 标注 BUG#1：TTC=1.5 这一步
    i1 = next(i for i, r in enumerate(rows) if abs(r['ttc'] - 1.5) < 1e-6)
    ax.annotate('BUG#1 边界未触发', xy=(i1, 1.5), xytext=(i1 - 2.6, 6.2),
                fontsize=9, color='#B4453C', fontweight='bold',
                arrowprops=dict(arrowstyle='->', color='#B4453C', lw=1.2))

    # ── 2. 状态 ──
    ax = axes[1]
    ax.step(x, state, where='mid', color='#14415C', lw=2)
    ax.scatter(x, state, s=26, color='#14415C', zorder=3)
    ax.set_yticks([0, 1, 2, 3])
    ax.set_yticklabels([STATE_NAME[k] for k in (0, 1, 2, 3)], fontsize=8.5)
    ax.set_ylim(-0.4, 3.4)
    ax.set_ylabel('AEB_State')
    ax.grid(alpha=.25)

    i2 = len(rows) - 1
    ax.annotate('BUG#2 非法迁移\nACTIVE → OFF', xy=(i2, 0), xytext=(i2 - 4.2, 2.35),
                fontsize=9, color='#B4453C', fontweight='bold',
                arrowprops=dict(arrowstyle='->', color='#B4453C', lw=1.2))

    # ── 3. 减速度 ──
    ax = axes[2]
    ax.step(x, decel, where='mid', color='#2F6E8F', lw=2)
    ax.scatter(x, decel, s=26, color='#2F6E8F', zorder=3)
    ax.axhline(-12, ls='--', lw=1.1, color='#B4453C', alpha=.85)
    ax.text(len(x) - .6, -12, 'DBC 下限 -12 m/s²', fontsize=8.5,
            color='#B4453C', va='top', ha='right')
    ax.axhline(0, ls=':', lw=.9, color='#889')
    ax.set_ylabel('DecelReq (m/s²)')
    ax.set_xlabel('测试帧序号')
    ax.set_ylim(-14.2, 1.2)
    ax.grid(alpha=.25)

    i3 = next(i for i, r in enumerate(rows)
              if r['decel'] is not None and r['decel'] < -12)
    ax.annotate('BUG#3 越界 -12.5', xy=(i3, -12.5), xytext=(i3 - 1.4, -4.4),
                fontsize=9, color='#B4453C', fontweight='bold',
                arrowprops=dict(arrowstyle='->', color='#B4453C', lw=1.2))

    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(png_path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#eef1f4;
     color:#21252b;font-size:13.5px;line-height:1.62;padding:26px 18px}
.page{max-width:1020px;margin:0 auto;background:#fff;padding:34px 38px 30px;
      box-shadow:0 3px 22px rgba(0,0,0,.10);border-radius:3px}
h1{font-size:22px;color:#14415C;letter-spacing:1px}
.sub{color:#6b7680;font-size:12.5px;margin-top:5px}
hr{border:0;border-top:2px solid #14415C;margin:14px 0 20px}
h2{font-size:14.5px;color:#14415C;letter-spacing:1.2px;margin:24px 0 10px;
   padding-bottom:4px;border-bottom:1px solid #cfd8dd}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:6px}
.card{flex:1;min-width:118px;background:#f7f9fa;border-left:3px solid #14415C;
      padding:10px 13px;border-radius:2px}
.card .v{font-size:24px;font-weight:700;color:#14415C;line-height:1.15}
.card .k{font-size:11.5px;color:#6b7680;margin-top:2px}
.card.bad{border-left-color:#B4453C}
.card.bad .v{color:#B4453C}
table{width:100%;border-collapse:collapse;font-size:12.4px;margin-top:6px}
th{background:#f2f5f7;color:#33424d;font-weight:600;text-align:left;
   padding:7px 9px;border-bottom:1.5px solid #cfd8dd;font-size:12px}
td{padding:6px 9px;border-bottom:1px solid #eceff2;vertical-align:top}
tr.fail td{background:#fdf4f3}
td.id{font-family:Consolas,monospace;font-size:11.8px;color:#14415C;white-space:nowrap}
.pass{color:#3C7A4E;font-weight:600}
.failtag{color:#B4453C;font-weight:600}
.kind{display:inline-block;font-size:10.5px;background:#eaf0f4;color:#14415C;
      border-radius:3px;padding:0 5px;margin-right:4px}
.kind.bad{background:#fbebe9;color:#B4453C}
.defect{border:1px solid #e3e8ec;border-left:4px solid #B4453C;border-radius:2px;
        padding:11px 14px;margin-bottom:10px;background:#fdfcfc}
.defect .t{font-weight:700;color:#B4453C;font-size:13.5px}
.defect .row{color:#4a5560;font-size:12.5px;margin-top:3px}
.defect .row b{color:#21252b;font-weight:600}
img{width:100%;border:1px solid #e3e8ec;border-radius:2px;margin-top:6px}
.note{background:#f7f9fa;border-left:3px solid #5A8A5A;padding:9px 13px;
      font-size:12.4px;color:#46535e;margin-top:12px}
.foot{margin-top:24px;padding-top:10px;border-top:1px solid #eceff2;
      color:#93a1ac;font-size:11.5px;display:flex;justify-content:space-between}
"""


def render_html(data, img_b64, out_path):
    s = data['summary']
    results = data['results']
    rate = s['pass'] / s['total'] * 100 if s['total'] else 0

    # 缺陷卡片
    defect_html = []
    for d in DEFECTS:
        cases = '、'.join(f"<span class='id'>{c}</span>" for c in d['cases'])
        kinds = ' '.join(f"<span class='kind bad'>{k}</span>" for k in d['checks'])
        defect_html.append(f"""
      <div class="defect">
        <div class="t">{d['id']} · {d['title']}　<span style="font-weight:400;color:#6b7680;font-size:12px">严重等级：{d['severity']}</span></div>
        <div class="row"><b>位置：</b>{d['where']}</div>
        <div class="row"><b>根因：</b>{d['cause']}</div>
        <div class="row"><b>现象：</b>{d['symptom']}</div>
        <div class="row"><b>检出：</b>{cases}　{kinds}</div>
      </div>""")
    defect_html = '\n'.join(defect_html)

    # 用例明细
    rows = []
    for r in results:
        cls = 'fail' if not r['passed'] else ''
        mark = ('<span class="pass">PASS</span>' if r['passed']
                else '<span class="failtag">FAIL</span>')
        kinds = sorted({f['kind'] for f in r['findings']})
        kind_html = ' '.join(f"<span class='kind bad'>{k}</span>" for k in kinds) or '—'
        detail = '；'.join(f['message'] for f in r['findings']) or ''
        rows.append(f"""
        <tr class="{cls}">
          <td class="id">{r['id']}</td>
          <td>{r['category']}</td>
          <td>{mark}</td>
          <td>{r['name']}{f'<div style="color:#B4453C;font-size:11.8px;margin-top:2px">{detail}</div>' if detail else ''}</td>
          <td>{kind_html}</td>
        </tr>""")
    rows = '\n'.join(rows)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>AEB 功能测试报告</title><style>{CSS}</style></head>
<body><div class="page">

  <h1>AEB 功能测试报告</h1>
  <div class="sub">
    被测对象：虚拟 AEB ECU（软件在环）　|　测试台：python-can 虚拟 CAN 总线 + cantools DBC 解析<br>
    信号定义：adas.dbc（3 报文 / 8 信号）　|　报告生成：{datetime.now().strftime('%Y-%m-%d %H:%M')}
  </div>
  <hr>

  <div class="cards">
    <div class="card"><div class="v">{s['total']}</div><div class="k">用例总数</div></div>
    <div class="card"><div class="v">{s['pass']}</div><div class="k">通过</div></div>
    <div class="card bad"><div class="v">{s['fail']}</div><div class="k">失败</div></div>
    <div class="card"><div class="v">{rate:.1f}%</div><div class="k">通过率</div></div>
    <div class="card"><div class="v">{s['elapsed_s']}s</div><div class="k">执行耗时</div></div>
  </div>

  <h2>缺陷清单</h2>
  {defect_html}

  <div class="note">
    <b>关于 26 条通过用例：</b>如果 30 条全部失败，只能说明测试写得不对；26 条通过、4 条失败，
    说明这套断言层既有检出能力（抓到了 3 个缺陷），也有判别力（不对正确行为误报）。
    测试有效性由这两侧共同证明。
  </div>

  <h2>演示场景波形</h2>
  <div class="sub" style="margin-bottom:6px">
    车速 80 km/h，相对速度 -10 m/s，目标从 100 m 逼近直至车辆停住。三个缺陷落在同一条时间轴上。
  </div>
  <img src="data:image/png;base64,{img_b64}" alt="AEB 演示场景波形">

  <h2>用例明细</h2>
  <table>
    <thead><tr><th style="width:88px">用例</th><th style="width:96px">类别</th>
    <th style="width:56px">结果</th><th>名称 / 失败原因</th><th style="width:150px">断言</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>

  <div class="foot">
    <span>ADAS 测试台 · 四类断言：RANGE / TRANSITION / CYCLE / BEHAVIOUR</span>
    <span>DUT 中的 BUG#1~#3 为演示用故意植入缺陷</span>
  </div>

</div></body></html>"""

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return len(html)


def main():
    db = cantools.database.load_file(DBC_PATH, database_format='dbc')
    with open(os.path.join(OUT_DIR, 'results.json'), encoding='utf-8') as f:
        data = json.load(f)

    os.makedirs(OUT_DIR, exist_ok=True)
    png = os.path.join(OUT_DIR, 'waveform.png')
    rows = run_demo(db)
    plot_waveform(rows, png)
    print(f'波形图已生成：{png}')

    with open(png, 'rb') as f:
        img_b64 = base64.b64encode(f.read()).decode()

    html_path = os.path.join(OUT_DIR, 'report.html')
    size = render_html(data, img_b64, html_path)
    print(f'报告已生成：{html_path}  ({size/1024:.1f} KB)')

    s = data['summary']
    print(f"\n汇总：{s['total']} 条用例，PASS {s['pass']}，FAIL {s['fail']}")


if __name__ == '__main__':
    main()