# Original figure provenance / 原图来源

[English home](../../README.md) · [中文首页](../../README_zh.md) · [Machine-readable index / 机器可读索引](index.json)

These eight figures are exact copies of the project's archived images. Each PNG and SVG hash matches the archived figure sidecar and its asset manifest. The source result is identified by SHA-256. Original labels, axes and plotted values are preserved. English explanations below translate their meaning; the archive's private environment and source paths are retained locally.

八张图均逐字节复制自项目归档；PNG、SVG 哈希与原图元数据及资产清单一致，并记录来源结果哈希。保留原标签、坐标和数值，下方提供英文释义。原始环境信息与来源绝对路径留在本地。

A local demonstration explains a method on a particular input. The `effects` figure is an archived reanalysis of historical experiments. These roles differ from a newly executed experiment or an independent replication.

本地演示解释特定输入上的方法；`effects` 是历史实验的既有复算图。二者分别标明来源身份。

| Figure / 原图 | Read the axes / 解读 | Record type / 身份 |
|---|---|---|
| [QUBO coefficients](qubo.png) · [SVG](qubo.svg) | Symmetric matrix for the six-asset B01_L0.1 demonstration; x0–x5 follow the archived asset order.<br>六资产 B01_L0.1 本地演示的对称 QUBO 矩阵；x0–x5 沿用归档资产顺序。 | `LOCAL_DEMONSTRATION` |
| [Objective components](objective_components.png) · [SVG](objective_components.svg) | Twenty feasible baskets sorted by objective; solid = return contribution, dashed = risk, dotted = cost. The horizontal axis is basket rank.<br>20个可行组合按目标排序；实线为收益项、虚线为风险项、点线为费用项，横轴是组合排序序号。 | `LOCAL_DEMONSTRATION` |
| [Calculated probabilities and sampled frequencies](probabilities.png) · [SVG](probabilities.svg) | B01_L0.1, fixed qas_00, gamma 0.6, beta 0.25, 1024 local shots; filled bars = calculated probabilities, outlined bars = sampled frequencies.<br>B01_L0.1、固定 qas_00、gamma 0.6、beta 0.25、1024次本地采样；实心柱为计算概率、空心柱为采样频率。 | `LOCAL_DEMONSTRATION` |
| [Probabilities of feasible three-holding baskets](feasible_probabilities.png) · [SVG](feasible_probabilities.svg) | The twenty six-bit states with three ones; x-axis indexes feasible baskets, not all 64 binary integers. Bars retain original probability mass.<br>六比特中恰有三个1的20个状态；横轴为可行组合序号，不是64个二进制整数；柱高保留原概率质量。 | `LOCAL_DEMONSTRATION` |
| [Observed candidate quality and proxy predictions](candidate_scores.png) · [SVG](candidate_scores.svg) | Synthetic local integration: six candidates, four training candidates and two held out. Filled bars = observed objective gap; hatched bars = ridge-proxy prediction.<br>合成输入本地集成案例：六候选、四训练与二留出；实心柱为观测目标差距，斜线柱为岭回归代理预测。 | `LOCAL_NORMALIZED_INPUT` |
| [Training and held-out candidate predictions](proxy_fit.png) · [SVG](proxy_fit.svg) | Same synthetic integration; circles = training, squares = held out, dashed diagonal = exact agreement. Nearly constant predictions miss the best held-out objective.<br>同一合成集成案例；圆圈为训练、方框为留出，虚线对角线表示完全一致。近似常数预测未准确识别最好的留出目标。 | `LOCAL_NORMALIZED_INPUT` |
| [Recorded objective-evaluation trace](optimization.png) · [SVG](optimization.svg) | Same synthetic integration; x-axis is actual evaluation index, y-axis expected objective. Temporary rises are optimizer evaluations, not training epochs.<br>同一合成集成案例；横轴为实际求值序号，纵轴为期望目标。暂时上升是优化器求值过程，不是训练轮次。 | `LOCAL_NORMALIZED_INPUT` |
| [Effects and intervals for 15 primary comparisons](effects.png) · [SVG](effects.svg) | Archived statistical reanalysis, with separate MAE, CVaR95 and objective-gap panels. Numbered comparisons are mapped below; task units remain distinct.<br>历史结果的统计复算图，MAE、CVaR95、目标差距分面。下表对应比较编号；任务单位仍须分别解释。 | `EXISTING_RESULTS_REANALYSIS` |

## Numbered comparisons in the effects figure / 效应图编号

Negative differences favor the first method for these loss/gap endpoints. Intervals belong to the archived resampling procedure. E02 and E03 have different targets and scales even though both report MAE; E05 and E06 also use different objective endpoints. Horizontal position compares a method with its reference within a row, not the scientific importance of different experiments.

这些损失或差距端点中，负差值有利于前一方法；区间来自原重采样程序。E02、E03虽都报告MAE，目标与尺度不同；E05、E06的目标端点也不同。横坐标用于解读行内方法对照，不用于比较不同实验的重要性。

| Number / 编号 | Experiment | Exact comparison key | Effect | Archived 95% interval |
|---|---|---|---:|---|
| 01 | [E02](../../evidence/E02.json) | `selected_vs_fixed` | 0 | [0, 0] |
| 02 | [E02](../../evidence/E02.json) | `selected_vs_classical` | -0.000583038093 | [-0.000917219743, -0.000271262164] |
| 03 | [E03](../../evidence/E03.json) | `local_message_vs_validation_classical` | 8.83367415e-05 | [4.09374425e-05, 0.000148670295] |
| 04 | [E03](../../evidence/E03.json) | `directional_vs_self` | 7.53994398e-05 | [3.54658488e-05, 0.000127352189] |
| 05 | [E03](../../evidence/E03.json) | `directional_vs_reverse` | 0.000101498152 | [5.43043197e-05, 0.000157357324] |
| 06 | [E03](../../evidence/E03.json) | `directional_vs_random` | -3.57592053e-05 | [-9.94154319e-05, 3.01643165e-05] |
| 07 | [E04](../../evidence/E04.json) | `quantum_mean_vs_classical_mean_cvar` | 0.00941191824 | [0.00597644431, 0.0133643372] |
| 08 | [E04](../../evidence/E04.json) | `factor_vs_shrinkage_cvar` | 0.00141333025 | [-0.000161962603, 0.0033556581] |
| 09 | [E04](../../evidence/E04.json) | `quantum_risk_vs_factor_cvar` | -0.00158073643 | [-0.00384989301, 0.000296848167] |
| 10 | [E07](../../evidence/E07.json) | `integrated_vs_classical_cvar` | 0.00941191824 | [0.00597644431, 0.0133643372] |
| 11 | [E07](../../evidence/E07.json) | `integrated_vs_fixed_map_cvar` | 0 | [0, 0] |
| 12 | [E05](../../evidence/E05.json) | `qaoa_vs_uniform` | -0.00162763866 | [-0.00205064417, -0.00126686787] |
| 13 | [E05](../../evidence/E05.json) | `qaoa_vs_local` | 0.00243163113 | [0.00191941915, 0.00297519674] |
| 14 | [E06](../../evidence/E06.json) | `noise_qas_vs_fixed` | -8.77088188e-05 | [-0.00030463501, 0.000168568945] |
| 15 | [E06](../../evidence/E06.json) | `noise_qas_vs_resource` | 0.000228037759 | [0.000123524635, 0.000334479349] |

## How to cite / 如何引用

Cite the project, figure ID, execution mode and source-result SHA-256 in `index.json`. Keep the original caption and describe whether a statement concerns this demonstration, an archived experiment or a future research question. Team-original figures follow the documentation license [CC BY-NC-SA 4.0](../../LICENSE-DOCS.txt); third-party data rights remain governed by the [evidence notice](../../evidence/DATA_NOTICE.md).

引用项目、图ID、执行模式和索引内的来源结果哈希；保留原图说明，明确结论对应演示、历史实验还是未来研究。团队原创图遵循文档许可，第三方数据权利遵循证据声明。
