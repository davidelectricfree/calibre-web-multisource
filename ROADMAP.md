# Roadmap

本文档基于 2026-08-10 架构复盘，复盘对象是性能优化阶段完成后的 Calibre-Web MultiSource 元数据聚合插件。

## 当前架构判断

当前架构适合这个插件的生产使用场景：单用户 Calibre-Web、NAS 部署、多个外部元数据源不稳定、同时需要优先保证中文书籍元数据质量。

项目不建议推倒重写。现有边界基本合理：

- `MultiSource.py` 是 Calibre-Web metadata provider 入口，负责搜索编排和结果转换。
- `source_*.py` 独立封装每个外部数据源的请求、解析和降级逻辑。
- `book_record.py` 定义插件内部统一数据模型。
- `matcher.py` 负责 ISBN、复合指纹、模糊匹配和字段合并规则。
- `proxy_manager.py` 负责代理路径选择。
- `source_health.py` 负责数据源熔断。

下一阶段重点不是大规模重构，而是修复明确问题、补齐测试和统一运维语义。

## 优先级 0：正确性修复

这些问题应在任何新功能之前处理。

1. 修复豆瓣封面 Cookie 分支

   `MultiSource.py` 的 `_get_cover_request_params()` 使用了 `os.path`，但文件顶部没有 `import os`。当豆瓣封面下载尝试读取 `douban_cookie.txt` 时，可能触发 `NameError`。

2. 恢复绿色测试基线

   当前本地测试结果：

   ```text
   python -m unittest discover -s tests -v
   ```

   结果：

   - 发现 13 个测试。
   - 12 个通过。
   - 1 个失败：`test_search_returns_raw_records_when_merge_fails`。

   该失败看起来是测试桩没有跟上 Phase 4 新增的 `circuit_breaker` 字段。后续修改行为前，应先修复测试脚手架并保持测试全绿。

3. 补齐关键回归测试

   需要增加聚焦测试，覆盖：

   - 豆瓣封面请求参数生成，包括 Cookie 文件读取。
   - merge 失败时回退到原始记录直出。
   - 熔断器跳过、冷却和恢复行为。
   - 一个快源 + 一个慢源时，搜索预算是否能保护整体响应时间。

## 优先级 1：验证性能语义

性能优化阶段已经降低尾延迟风险，但还有两个实现语义需要用测试证明。

1. 验证 6 秒搜索预算

   `_query_all_sources()` 当前使用 `wait(..., timeout=SEARCH_BUDGET_SECONDS)`，并调用 `pool.shutdown(wait=False)`，但外层仍处于 `ThreadPoolExecutor` context manager 中。需要新增回归测试，证明未完成的慢源不会让调用方继续等待超过配置预算。

   如果测试证明 context manager 仍会等待未完成任务，应将线程池改为显式生命周期管理，避免预算被绕过。

2. 梳理源级 timeout 常量

   `MultiSource.py` 中配置了 `SOURCE_TIMEOUT = 4`，但各数据源仍保留自己的 timeout，例如：

   - `DOUBAN_TIMEOUT = 8`
   - `DANGDANG_TIMEOUT = 10`
   - `OL_TIMEOUT = 10`
   - `GB_TIMEOUT = 8`
   - `WEREAD_TIMEOUT = 8`

   下一步应决定：源模块保留本地 timeout，还是接受调度层传入的 timeout。这个重构不要早于搜索预算回归测试。

## 优先级 2：统一代理策略

当前项目存在两套代理选择逻辑：

- `get_proxies()` 使用 TCP 端口探测，并缓存主备代理选择。
- `probe_best_proxy()` 使用 HTTP 请求目标 URL，对 direct / xray / clash 做延迟比较。

这和运维约束“正常搜索路径尽量避免 HTTP 探测，降低触发 Calibre-Web 代理限制的风险”不完全一致。

推荐方向：

1. 生产搜索路径默认使用 TCP 探测。
2. HTTP 延迟探测如果保留，应放到显式诊断函数或开关后面。
3. OpenLibrary / Google Books 等源统一使用同一个生产代理 API。
4. 实现对齐后，同步更新 README、CHANGELOG 和 SKILL 记录。

## 优先级 3：文档同步

行为修复后，需要保持文档和代码一致。

需要同步更新：

- README 当前状态和已知限制。
- CHANGELOG 修复记录和测试覆盖变化。
- SKILL 中关于代理、封面下载、测试状态的说明。
- 如果搜索预算语义发生变化，同步更新性能优化计划文档。

## 暂缓事项

以下方向暂不建议作为下一阶段工作。

1. 全量异步架构

   Calibre-Web metadata provider 接口本身是同步返回结果列表。除非验证 UI 支持增量更新，否则异步内部实现会显著增加复杂度，但收益不明确。

2. 持久化缓存

   Phase 3 跳过是合理的。当前是单用户、低频搜索，重复查询命中率有限。持久化缓存会引入状态、失效策略和文件权限问题。

3. 大规模模块拆分

   `MultiSource.py` 偏大，但目前还不是最大风险。应先补测试，再按真实复杂度决定是否拆出调度器、查询分类器或缓存模块。

4. 继续增加数据源

   当前瓶颈不是数据源数量，而是外部源稳定性、搜索预算语义、代理策略和合并正确性。

## 建议执行顺序

1. 修复 `MultiSource.py` 缺少 `import os` 的问题。
2. 修复当前失败的单元测试，恢复测试全绿。
3. 补充封面下载、fallback、熔断器、搜索预算回归测试。
4. 验证搜索预算是否真正有边界。
5. 将生产代理选择简化为一致策略。
6. 行为验证后，更新 README、CHANGELOG 和 SKILL 记录。
