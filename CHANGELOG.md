# Changelog

All notable changes to Calibre-Web MultiSource.

## [2026-08-09] Performance Optimization Sprint

### Phase 4: Source Circuit Breaker
- **Added** `source_health.py` — CircuitBreaker with closed/open/half_open states
- **Added** `SOURCE_CIRCUIT_BREAKER_ENABLED` config (default: True)
- **Added** `SOURCE_FAILURE_THRESHOLD` = 3 (consecutive failures)
- **Added** `SOURCE_COOLDOWN_SECONDS` = 300 (5 min)
- Failed sources auto-skipped after 3 consecutive failures, retry after 5 min
- Budget timeouts (not_done) NOT counted as failures
- Rollback: set `SOURCE_CIRCUIT_BREAKER_ENABLED = False`

### Phase 3: (Skipped)
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
