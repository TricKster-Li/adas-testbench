# ADAS 功能测试台 · AEB 功能验证

> 基于**虚拟 CAN 总线**与**虚拟 ECU** 的 ADAS 功能测试环境。
> 零硬件、零商业软件依赖（不需要 CANoe / dSPACE / 任何许可证），在普通 Windows 笔记本上跑得起来。
>
> 用四类独立断言（值域 / 状态迁移 / 周期 / 行为）自动执行 30 条用例，检出被测 ECU 中 3 个故意植入的缺陷。

---

## 项目定位

一套完整的测试流程：

```
定义信号 → 造被测对象 → 主动植入缺陷 → 写用例 → 自动判定 → 出报告
```

被测对象（DUT）与测试台（Bench）是**两条独立的代码路径**，只通过 CAN 总线通信。断言不读 DUT 的返回值，而是从总线上把报文解回来判——否则测的就是自己。

## 架构

```
        激励                        被测对象                   判定
 ┌──────────────┐  adas_stim  ┌──────────────┐  adas_cmd  ┌──────────────┐
 │  Bench 台架   │ ──────────▶ │   AEB ECU    │ ─────────▶ │  Assertions  │
 │ test_runner  │ VCU_Status  │  ecu_sim.py  │  AEB_Cmd   │  checks.py   │
 │ cases.yaml   │ ADAS_Obj    │              │            │  四类检查     │
 └──────────────┘             └──────────────┘            └──────┬───────┘
                                                                  │
                                                          ┌───────▼───────┐
                                                          │    Report     │
                                                          │ HTML + 波形图 │
                                                          └───────────────┘
```

激励通道与响应通道**分开**（`adas_stim` / `adas_cmd`），四个 `can.Bus` 实例。用单通道 + ID 过滤的写法会让台架自己的激励帧回灌进自己的接收队列，时序一乱就不敢信。

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

```bash
# 1. 环境自检：虚拟总线收发 + DBC 编解码
.venv\Scripts\python.exe smoke_test.py

# 2. 打印 DBC 结构（报文 / 信号 / 位域 / 范围）
.venv\Scripts\python.exe src\dbc_report.py

# 3. 最小闭环：激励 → ECU → 响应
.venv\Scripts\python.exe src\demo_ecu.py

# 4. 执行全部 30 条用例
.venv\Scripts\python.exe src\test_runner.py

# 5. 生成 HTML 报告 + 波形图
.venv\Scripts\python.exe src\report.py
```

> Linux / macOS 把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python`。
> 已验证环境：Windows 11 + Python 3.14.7 + python-can 4.6.1 + cantools 45.1.1（全部为纯 Python 轮子，无需编译器）。

## 目录

```
adas_testbench/
├── adas.dbc                  DBC 信号定义（3 报文 / 8 信号）
├── smoke_test.py             环境自检
├── cases/cases.yaml          30 条用例
├── src/
│   ├── dbc_report.py         DBC 结构打印
│   ├── ecu_sim.py            被测 AEB ECU（含 BUG#1~#3）
│   ├── demo_ecu.py           最小闭环
│   ├── checks.py             四类断言
│   ├── test_runner.py        用例执行器
│   └── report.py             HTML 报告 + 波形图
└── outputs/
    ├── results.json          原始执行结果
    ├── report.html           测试报告（自包含，可直发）
    └── waveform.png          演示场景波形
```

## 信号定义

| 报文 | ID | 周期 | 发送方 | 设计理由 |
|---|---|---|---|---|
| `VCU_Status` | 0x100 | 10 ms | VCU | 车速是 AEB 判决输入，周期须与制动控制任务同频 |
| `ADAS_Obj` | 0x110 | 20 ms | ADAS 感知 | 感知帧率天然低于整车 CAN，行业常态 |
| `AEB_Cmd` | 0x120 | 10 ms | AEB ECU | 制动指令必须与整车状态同频，否则下游拿到的是过期命令 |

信号层面几个刻意的选择：

- **`DecelReq`：12 位有符号，factor 0.01** → 可表达范围 -20.48 ~ 20.47 m/s²，覆盖 -12 m/s² 的制动需求且留有余量。若用 8 位，量程只有 ±1.28，根本刹不住车。
- **`AEB_State`：3 位** → 4 个状态（OFF / ARMED / ACTIVE / FAULT）够用，留余量给后续扩展。
- **`ObjValid`：1 位标志** → 目标丢失时置 0，而不是把距离填成 999 这种"魔法值"。
- **DBC 里的 `[min|max]` 不是注释，是断言判据** —— `checks.check_range()` 直接读取它。改判据就改 DBC，不在代码里另写一套魔法数字。

## 状态机

```
OFF ──TTC ≤ 2.5──▶ ARMED ──TTC ≤ 1.5──▶ ACTIVE
 ▲                   │                    │
 └───TTC > 3.0───────┘                    │
 ▲                                        │
 └──────── 危险解除（先回 ARMED）◀─────────┘
```

合法迁移表：

| 当前 | 允许迁移到 |
|---|---|
| OFF | ARMED, FAULT |
| ARMED | OFF, ACTIVE, FAULT |
| ACTIVE | ARMED, FAULT |
| FAULT | OFF |

注意：**状态不变也是合法的**。迁移表只列"能去哪些新状态"，用 `next in legal[prev]` 直接判会把 `OFF → OFF` 误报成非法迁移。

## 四类断言

| 类型 | 检查什么 | 判据来源 |
|---|---|---|
| `RANGE` | 信号值是否越界 | DBC 的 `[min\|max]` |
| `TRANSITION` | 状态迁移是否合法 | 合法迁移表 |
| `CYCLE` | 报文间隔是否超时 | 期望周期 × 容差 |
| `BEHAVIOUR` | 给定输入，输出是否落在期望窗口 | 用例的 `expect` 段 |

四类互不依赖。缺陷被检出时能立刻定位到"哪条规则被违反"，而不是笼统地说"这步没过"。

## 缺陷清单（故意植入）

`src/ecu_sim.py` 里带 `BUG#` 注释的三处是**演示用的靶子**，不是笔误。

| 缺陷 | 根因 | 现象 | 检出用例 | 断言 |
|---|---|---|---|---|
| **BUG#1** 边界漏触发 | TTC 与制动阈值比较用 `<`，应为 `<=` | TTC 恰好 1.5 s 时停在 ARMED，不进 ACTIVE | `BHV-006` | BEHAVIOUR |
| **BUG#2** 非法迁移 | 停车后直接跳 OFF，跳过 ARMED | `ACTIVE → OFF` 不在合法迁移表内，预警标志同时被清零 | `TRN-004` | TRANSITION + BEHAVIOUR |
| **BUG#3** 值域越界 | 车速 > 60 km/h 时输出 -12.5 m/s² | `DecelReq` 超出 DBC 上限 -12，下游可能饱和或拒收 | `BHV-012`、`RNG-004` | RANGE + BEHAVIOUR |

BUG#3 被两条用例从不同维度命中（一条测行为、一条测值域），说明四类断言是交叉验证的。

## 测试结果

```
$ .venv\Scripts\python.exe src\test_runner.py

ADAS 测试台 · 用例执行   共 30 条   DUT = 虚拟 AEB ECU
ID        类别          结果      断言            名称
--------------------------------------------------------------------------------
BHV-006   behaviour    FAIL     BEHAVIOUR       TTC 恰好 1.5s（制动边界），应触发 ACTIVE
                                          ↳ [BEHAVIOUR] AEB_State 期望 ACTIVE，实际 ARMED
TRN-004   transition   FAIL     BEHAVIOUR,TRANSITION  制动中车辆停住，状态应先回 ARMED 保持预警
                                          ↳ [TRANSITION] 非法状态迁移 ACTIVE → OFF
BHV-012   behaviour    FAIL     BEHAVIOUR,RANGE 高速 80km/h 触发制动时，减速度请求仍须落在 DBC 范围内
                                          ↳ [RANGE] AEB_Cmd.DecelReq = -12.5 低于下限 -12 m/s2
...
--------------------------------------------------------------------------------
合计 30 条：PASS 26   FAIL 4   耗时 4.46s
```

**26 条通过比 4 条失败更能说明问题。** 如果 30 条全红，只能说明测试写错了；26 过 4 挂，说明这套断言层既有检出能力（抓住了 3 个缺陷），也有判别力（不对正确行为误报）。

另有 4 条**周期负向用例**（`CYC-001~004`）：故意制造 200 ms 卡顿，期望检查器报警，报出来才算 PASS——这类用例验证的是**检查器本身有没有瞎**。

## 局限（这份成果的边界）

诚实说明，避免过度解读：

- **DUT 是软件模型**，不是真实 ECU，不包含真实控制器的调度抖动、总线负载、EMC 影响；
- **DBC 为自建简化版**：真实项目中 DBC 由整车厂网络团队基于通信矩阵（Communication Matrix）提供，测试侧是使用者；本项目自己写一份，是为了走通「信号定义 → 解析 → 断言」这条链路，并理解字段出错会带来什么后果；
- **场景是构造的**，基于运动学关系生成，不代表真实道路的复杂工况；
- **缺陷是故意植入的**，用于演示检出能力，不代表真实产品的缺陷分布；
- 未覆盖 UDS 诊断、功能安全（ISO 26262）、总线负载率等维度。

这套框架的价值在于**流程完整且可复现**，不在于结论的工程精度。

## 可扩展方向

1. **接真实硬件** —— 用 USB-CAN 分析仪（约 ¥100~200）替换 `virtual` 接口，同一套用例直接打真实 ECU；
2. **UDS 诊断测试** —— 增加 0x22 / 0x2E 读写 DID 的用例，覆盖诊断服务；
3. **DUT 侧丢帧注入** —— 当前周期用例是台架侧卡顿，可改为让 DUT 自己漏发；
4. **复用框架** —— ACC 跟车、LKA 车道保持可用同一套断言层，只需换 DBC 与状态机。

## 技术栈

`Python 3.14` · `python-can` · `cantools` · `PyYAML` · `matplotlib`

---

作者：李佳朋 · 长春工程学院 · 汽车工程学院 · 智能运输工程