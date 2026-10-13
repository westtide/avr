# AVR Array1 修复：实现与验证

最终源码基于本地分支 `fix/array1-sort-classification`，原始提交
`b562948cfee17502dd1583ec3ac3be9d47f44eec`。原 `checkers/avr` 源码和五个二进制
SHA 均已复核保持不变；现有 Pono、rIC3、AVR 实验没有重启。

## 根因与修改

AVR 的 Array `get_size()` 是元素位宽，不能用它判断整个表达式是否 Boolean。
新增 `is_boolean()`，只接受 `bvtype && size == 1`；不改变 `get_size()`、
数组 Sort、索引域、状态数或 BTOR2 输入。

| 修改位置 | 目的 |
|---|---|
| vwn/dpa/reach 的 `avr_word_netlist.h/.cpp` | 统一标量 Boolean 判定；约束 Boolean 简化，避免把 Array1 当 BV1 |
| `reach_core.h` | EUF/UFBV Mapper 的 BOOL_VAR 只接收标量 BV1 |
| `reach_core.cpp`, `reach_coi.cpp`, `reach_solve.cpp`, `reach_sa.cpp`, `reach_evaluate.cpp` | 控制、关系和模型值路径按 Sort 分类；Array 不走 Boolean 取值 |
| `reach_y2.cpp`, `reach_bt.cpp` | Array1 进入完整数组模型值路径；保持初始化 payload 声明宽度；BT 严格按其 API 的默认零/`*` 默认值恢复完整值 |
| `reach_util.cpp` | 回灌恢复完整 dense payload 前导零；ArrayConst literal 不做父变量推广；扫描 I/P/T/assume 的可达 Sort，识别仅输入数组；数组输入保留原符号和约束 |
| `reach_cegar.cpp` | 零步 SAT witness 从同一已确认公式刷新 concrete 模型，编码实际合取根；新 retrieval key 阻止旧抽象值进入反例 |
| `reach_cex.cpp` | 按整个数组索引域恢复 witness payload，保持既有地址/元素顺序 |
| `avr` | 前端软栈下限 64 MiB；已有输出目录报错；保存运行文件，避免删除历史输出 |

dense payload 保留已有协议：高地址元素在前，低地址元素在后。元素宽度的
初始化值表示广播；完整域宽度的 payload 表示逐地址内容，两者不能混淆。
所有相关移位、乘法、枚举检查上限；完整 payload 超过 INT_MAX 明确报
`AVR backend unsupported:`，不允许溢出后跳过初始化或生成不完整数组。

没有修改 C2BTOR、控制流、LBE、Slice、内联或内存模型。历史失败重跑使用逐字节
相同的 `proof_full.btor2`，验证 query/model/map/log SHA，随后冻结输入和工具。

## 构建与运行

最终隔离构建记录：
`c2btor_vs_cpv/avr_array1_repair_20261010T043414.588092Z/build_v5_20261010T045857.260790Z/`。
使用 `/usr/bin/gcc` 和 `/usr/bin/g++`，Y2/Y2BT 各自独立 source/object 目录，
依赖只读复用原 `checkers/avr/deps`。只构建明确的二进制 target；没有执行会删除
对象文件的 `make all`。完整 source.patch、95 项源码 SHA、driver SHA、命令和日志保留。

本 fork 的 `build/bin` 链接到最终编译目录，默认后端仍是 `y2bt`。
必须在 fork 目录运行，且 `-o` 指定新的输出位置，例如：

```sh
python3 avr.py --bt /absolute/model.btor2 -o /absolute/new-output-directory \
  --backend y2bt --timeout 300 --memout 15360
```

上述是工具调用示例。批量评测另外使用有界 runner，为每个进程组落实 1 CPU、
15 GiB 地址空间/组 RSS、300 秒、禁 core dump 和 1 GiB 单文件限制。

## 已完成验证

- 33 个有限 BTOR2 fixtures 均通过官方 parser。
- 最终 Y2BT：33/33，16 UNSAT、17 SAT；最终 Y2：同样 33/33。
- 两后端均与新版 Pono 的同模型结果一致；原版 AVR 同套样例仅 6/33 严格匹配。
- 3 个显式宽域限制样例按预期 Unsupported；不返回未经支持的 SAT/UNSAT。
- Y2BT 的 17 个 SAT：4 个原 witness 直接重放成功；另外 13 个原文件触发旧
  btorsim SIGSEGV。独立检查每个显式数组初值与模型既定初始化一致后，在新副本
  只省略冗余的已初始化数组初值，13 个副本全部重放成功。没有改变输入、
  未初始化数组、其他帧内容或模型；原 witness/hash/exit-11 完整保留。
- 同一大模型的原版前端探针：8 MiB 栈 SIGSEGV；64 MiB 栈成功，RSS 均约 2 GiB。
- fork 默认 binary 路径 smoke：历史失败 case59/mixed512 成功 UNSAT，0.44 秒。

原版的仅打印 avr-v 后在输出反例时崩溃不能算完整通过；测试 harness 的 strict
result 始终优先记录 f_err/assert/segfault，再独立记录打印答案和 witness 状态。

## 历史失败集

快照为 881 个已完成失败单元，441 个源案例：mixed512 441、mixed2048 440。
877 个旧 `add_gate_constraint` Array1 assertion，4 个前端 segmentation fault。
资源限制和普通无结论记录没有混入这一失败定义。

最终 v5 全集重跑目录：
`c2btor_vs_cpv/avr_array1_repair_20261010T043414.588092Z/failed_full_v5_20261010T050348.408527Z/`。
24 路，CPU96–107/116–127，每单元1核15GiB300s，默认不启用 witness，保留历史算法参数。
CSV 逐行落盘。开始时仍在运行，因此本文件不宣称 881 已全部验证完毕；完成后的
`summary.json` 和 exact-query crosschecker 对照是全量结论的依据。

full query 同时含源属性、memory-validity/model-limit 守卫，raw SAT 不能直接记作
源程序 UNSAFE；本轮不计算 source label match。

## 限制与参考

现有 dense 数组模型值协议仍枚举整个索引域，成本为
O(2^indexBits × elementBits)，解决类型错误不意味着大数组求解性能已经改善。
64 MiB 栈和 witness 模型刷新属于资源/验证开销变化，性能比较要单列。
本轮构建、回归覆盖 Y2BT/Y2，没有承诺其他 SMT backend 或导出路径都已修复。
测试 runner 依赖本工作区的冻结资源 helper，尚未打包成可独立安装的测试框架。

参考用户给的 HWMCC2025 分支与提交 03bc7f6：后者主要改变日志和结果输出，
不是 Array1 类型修复；本次没有整体合并分支或升级 AVR 依赖。

另对 exact-query 参考审计发现：case12 的旧 Pono kind 在常量数组不支持警告后
仍给 UNSAT。相关 SMT 子查询会返回 UNKNOWN，而旧 kind 缺少已有 UNKNOWN
处理修复；该 UNSAT 不作为可信证明。新版 SAT 暂无完整 witness，不能凭
裸 SAT 判断冲突赢家。该既有 Pono 冲突与新 AVR 结果分开记录。
