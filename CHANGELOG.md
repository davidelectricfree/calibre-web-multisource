# Changelog

All notable changes to Calibre-Web MultiSource.

## [2026-08-10] 架构复盘 Roadmap

### P2 统一代理策略
- **Unified** 代理选择：生产搜索路径统一使用 `get_proxies()`（纯 TCP 端口探测），移除 OL/GB 中的 `probe_best_proxy()` HTTP 延迟探测调用
- **Added** `PROXY_DIAGNOSTIC_ENABLED = False` 标志到 `proxy_manager.py` — HTTP 探测默认关闭，规避 cw_advocate 代理禁令；设为 True 可启用延迟诊断
- **Removed** 死代码：OL/GB 源文件中未使用的 `_get_proxies()` 包装函数
- **Simplified** OL/GB `_get_best_proxies()` → 直接委托给 `proxy_manager.get_proxies()`（模块级 60s 缓存已足够）
- **Removed** OL/GB 各自的 `_proxies_cache` / `_proxies_cache_name` / `_proxies_cache_checked_at` 类属性 — 缓存由 proxy_manager 统一管理
- **Updated** 测试: OL/GB 委托验证 + `get_proxies()` 60s 缓存 TTL 测试

### P1 性能语义验证
- **Removed** `SOURCE_TIMEOUT = 4` 死代码 — 从未被引用，阶段 1 真正超时由 `SEARCH_BUDGET_SECONDS` 控制
- **Documented** 源 timeout 与预算的关系：源内部 timeout (8-10s) 作为二级安全网 >= 预算值 (6s)
- **Added** `SourceTimeoutHealthTests` (2 项): 验证死代码已清除 + 已知源 timeout >= 预算

### P0 正确性修复
- **Fixed** `MultiSource.py` 缺少 `import os` — 豆瓣封面 Cookie 读取路径触发 `NameError`
- **Fixed** 搜索预算被 `ThreadPoolExecutor` context manager 绕过 — `_query_all_sources()` 和 `_query_isbn_cascade()` 移除 `with` context manager，改用显式 `try/finally pool.shutdown(wait=False)`，确保慢源不阻塞返回
- **Fixed** 单元测试 `test_search_returns_raw_records_when_merge_fails` — 补 `circuit_breaker = None`（Phase 4 新增字段未设置）
- **Added** 25 项单元测试（13 → 25），含 3 个新测试类：
  - `CoverProxyTests`: 封面请求参数生成 + 域名检测（4 项）
  - `CircuitBreakerTests`: 熔断器 skip/cooldown/half_open 全状态（5 项）
  - `SearchBudgetTests`: 搜索预算边界保护（3 项）

### 文档
- 新增中文版 `ROADMAP.md`，记录性能优化后的架构判断和后续执行优先级。
- 更新 `README.md`，增加当前状态和 roadmap 入口。
- 更新 `docs/performance-optimization-plan.md`，增加复盘备注并链接到 roadmap。

### 复盘结论记录
- `MultiSource.py` 需要补 `import os`，否则豆瓣封面 Cookie 分支可能触发 `NameError`。 ✅ 已修复
- 当前单元测试基线有 1 个失败，应在继续改行为前恢复测试全绿。 ✅ 已修复
- 搜索预算行为需要回归测试，证明慢源不会绕过配置的等待预算。 ✅ 已修复 + 测试验证
- 生产代理探测策略需要统一：正常搜索路径使用 TCP 探测；HTTP 探测如保留，应只作为诊断能力。

## [2026-08-09] Performance Optimization Sprint

### Fix: Douban Cover Download
- **Fixed** Douban cover images intermittently failing (0-byte or broken image)
- Added `_get_cover_request_params()`: domestic covers bypass global proxy
- Douban covers now use direct connection (`proxies={"http": None, "https": None}`)
- Added `Referer: https://book.douban.com/` header (anti-hotlinking prevention)
- Added Cookie from `douban_cookie.txt` for authenticated cover access
- Applied to both cover proxy route and `hack_cover_proxy` monkey patch
- Reference: fugary/calibre-douban cover download approach

### Phase 4: Source Circuit Breaker
- **Added** `source_health.py` — CircuitBreaker with closed/open/half_open states
- **Added** `SOURCE_CIRCUIT_BREAKER_ENABLED` config (default: True)
- **Added** `SOURCE_FAILURE_THRESHOLD` = 3 (consecutive failures)
- **Added** `SOURCE_COOLDOWN_SECONDS` = 300 (5 min)
- Failed sources auto-skipped after 3 consecutive failures, retry after 5 min
- Budget timeouts (not_done) NOT counted as failures
- Rollback: set `SOURCE_CIRCUIT_BREAKER_ENABLED = False`

### Phase 5: Cascade & Enrichment Limiting
- **Added** `CASCADE_MAX_RECORDS` = 3 (only cascade top 3 ISBNs)
- **Fixed** `CASCADE_WAIT=15` class attribute bug — now uses module-level `CASCADE_TIMEOUT=5`
- **Changed** `WEREAD_DETAIL_LIMIT`: 5 → 3
- Cascade 60-100s reduced to ≤5s
- In-memory cache skipped — low single-user usage, near-zero cache hit rate

### Phase 2: Query Classification & Source Tiers
- **Added** `ZH_SKIP_OPENLIBRARY` config (default: True)
- **Added** `ZH_SKIP_GOOGLEBOOKS` config (default: True)
- Chinese searches skip OpenLibrary (always 0 results, 4-31s wasted)
- Chinese searches skip Google Books (limited coverage, 2-9s wasted)
- English/ISBN searches unchanged — all sources active
- `_query_all_sources()` accepts explicit sources list

### Phase 1: Search Budget
- **Added** `SEARCH_BUDGET_SECONDS` = 6s global timeout
- **Added** `FAST_RESULT_MIN_COUNT` = 5 early merge threshold
- **Added** `SOURCE_RETRY_ENABLED` = False (disable auto-retry)
- **Changed** `SOURCE_TIMEOUT` from 8s → 4s
- `_query_all_sources()` uses `futures.wait()` instead of `as_completed()`
- Unfinished sources at budget exhaustion are logged and skipped

### Phase 0: Performance Baseline (Logging)
- Added request IDs, per-source timing, cascade/merge duration, result counts
- 16 searches across 11 queries: cascade is 80% of latency bottleneck

## Initial Release (2026-08-02)

### Features
- Five-source aggregation: 豆瓣, 当当, 微信读书, Open Library, Google Books
- Three-tier matching: ISBN exact → fingerprint → Levenshtein fuzzy
- ISBN cascade: extract ISBNs from title search results, query OL/GB by ISBN
- Per-field source tracking
- Cover proxy (anti-hotlink for 豆瓣, Google Books, OpenLibrary)
- API key hot-reload (no container restart needed)
- Smart proxy selection: TCP health probe, xray↔clash↔direct auto-failover
- Douban pagination support
- NLC (国家图书馆) source (disabled by default)
- CLC (中图分类号) parser
