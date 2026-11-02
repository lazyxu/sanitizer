# LLVM GPU Sanitizer：自研真实GPU技术设计

**文档v2.9 · 2026-11-02｜会议评审稿；自研目标Checker尚未实现/实测，GPU_NOT_RUN。**

本仓库研究自研LLVM GPU后端、设备检查库与UMD/KMD如何共同实现内存检查。当前详细比较六条软件路线：**NV-R、B1、B2、NV-C、cuCatch、CuSafe**；Intel设备ASan作为B2产品对照。核心分类仍为两家主要厂商、三种参考配置、两层基础空间判定；工程包B/C/D/E/F不等于五种独立算法。

## 评审阅读入口

| 评审问题 | 首选内容 |
|---|---|
| 六条路线原理、效果/盲区、约束、性能与开发量差在哪里？ | [核心方案比较](docs/core-design-comparison.md) |
| 选哪条路线、具体怎样实现、工作怎么拆？ | [主研究报告](docs/llvm-gpu-sanitizer-research.md)：六路线细节在既有§10～12，预算§16、验收§19、待决项§22 |
| Compiler/Device/UMD/KMD的正确代码和契约是什么？ | [唯一组件ABI与伪代码](docs/real-gpu-components-implementation.md) |
| 厂商版本、命令与支持条件是什么？ | [S01 NVIDIA/AMD指南](docs/S01-vendor-implementation-guide.md)、[AMD XNACK/Shadow适配](docs/amd-gpu-asan-hardware-fallback.md)、[Intel2026.2专题](docs/intel-oneapi-device-sanitizers.md) |
| 论文数字能否横比、证据是否完整？ | [论文取证](docs/research-notes/papers.md)、[版本化厂商登记](docs/research-notes/vendors_sources.json) |
| 还有哪些平台事实需确认？ | [部署准入审计](docs/hardware-deployability-audit.md)、[预算原始JSON](docs/estimates-v1.8.json) |

[统一导航](docs/README.md)列出各文档职责。v2.9直接重写/合并原文，没有新增独立方案对比专题；v1.8预算数字未改。历史判断见[架构笔记](docs/research-notes/architecture.md)，阶段状态和修订分别见[进度](docs/research-progress.md)、[历史](docs/revision-history.md)。

## 样例、验证与证据

[S01～S28](samples/README.md)各有CUDA/HIP源码、错误/合法对照和唯一详细规范；[NVIDIA官方独立回归组](tests/upstream/nvidia/README.md)另保留上游代码和许可。源码存在、编译成功和具体设备检出是不同状态，不能从CPU测试推断GPU性能或能力。

- [CPU静态CI](.github/workflows/sample-structure.yml)：结构、Python/Bash语法、[本地Markdown链接/锚点](scripts/validate_markdown_links.py)、预算与报告分类。
- [GPU工作流](.github/workflows/gpu-hardware.yml)及[Runner说明](docs/gpu-actions-ci.md)：需要实际GPU及受支持工具链；未运行/Skipped记GPU_NOT_RUN。
- [审计快照](docs/validation-report.json)：绑定主稿Hash与结构计数，真实检查结论以对应Commit的Actions为准。

## 本地命令

仓库scripts内**8 个自有**Bash/Python脚本支持-v/--verbose，用于显示实际命令或读取路径；参数不改变覆盖或结果判定。

~~~bash
# CPU检查，不编译或执行GPU Kernel
python3 scripts/validate_layout.py
python3 scripts/validate_markdown_links.py
python3 scripts/validate_docs.py
python3 -m unittest discover -s tests -v

# 仅在具备对应GPU、驱动和容器的环境运行
bash scripts/ci-gpu.sh cuda safe-all -v
./scripts/setup-docker.sh cuda --enter -v
python3 scripts/run_upstream_nvidia.py --case all --strict --verbose
~~~

## 推荐实施顺序

公共对象/队列/报告P → **B2 Global检查与S01/S02** → **D1原对象Bounds** → 按需受控D2 → 有真实闭源/JIT缺口时C/E。B1为经论证的当前区间替代；cuCatch/CuSafe用于评估具体元数据和布局机制，不能把论文原型当已完成移植。F仅保留真实硬件依赖和历史预算边界。
