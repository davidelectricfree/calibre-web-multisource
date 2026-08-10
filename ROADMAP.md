# Roadmap

This roadmap is based on the 2026-08-10 architecture review after the performance optimization phases were completed.

## Current Architecture Assessment

The current architecture is suitable for the plugin's production use case: a single-user Calibre-Web deployment on NAS, aggregating unstable external metadata sources while keeping Chinese metadata quality high.

The project should not be rewritten. The existing boundaries are mostly right:

- `MultiSource.py` is the Calibre-Web provider entry and orchestration layer.
- `source_*.py` modules isolate external source behavior.
- `book_record.py` defines the normalized internal data model.
- `matcher.py` owns deduplication and field merge rules.
- `proxy_manager.py` owns proxy path selection.
- `source_health.py` owns source circuit breaking.

The next work should focus on correctness, tests, and operational clarity instead of large architecture changes.

## Priority 0: Correctness Fixes

These should be handled before any further feature work.

1. Fix Douban cover Cookie branch

   `MultiSource.py` uses `os.path` in `_get_cover_request_params()` but does not import `os`. This can raise `NameError` when Douban cover download tries to read `douban_cookie.txt`.

2. Restore a green test baseline

   Current local result from `python -m unittest discover -s tests -v`:

   - 13 tests discovered
   - 12 passed
   - 1 failed: `test_search_returns_raw_records_when_merge_fails`

   The failing test appears to be out of sync with the Phase 4 `circuit_breaker` field. Fix the test scaffold and keep this suite green before changing behavior.

3. Add missing regression tests

   Add focused tests for:

   - Douban cover request parameter generation, including Cookie file handling.
   - Search fallback when merge fails.
   - Circuit breaker skip and recovery behavior.
   - Search budget behavior with one fast source and one slow source.

## Priority 1: Validate Performance Semantics

The performance sprint reduced the likely tail latency, but two implementation details need explicit verification.

1. Verify the 6-second search budget

   `_query_all_sources()` uses `wait(..., timeout=SEARCH_BUDGET_SECONDS)` and calls `pool.shutdown(wait=False)`, but it is still inside a `ThreadPoolExecutor` context manager. Add a regression test proving an unfinished slow source cannot make the caller wait beyond the configured budget.

   If the test shows the context manager still waits for pending work, replace the `with ThreadPoolExecutor(...)` block with explicit executor lifecycle management.

2. Reconcile source timeout constants

   `SOURCE_TIMEOUT = 4` is configured in `MultiSource.py`, but individual sources still have their own timeout constants such as `DOUBAN_TIMEOUT = 8`, `DANGDANG_TIMEOUT = 10`, `OL_TIMEOUT = 10`, `GB_TIMEOUT = 8`, and `WEREAD_TIMEOUT = 8`.

   Decide whether source modules should keep local timeouts or accept scheduler-provided timeout settings. Do not refactor this until the budget regression test exists.

## Priority 2: Proxy Strategy Cleanup

The project currently has two proxy-selection patterns:

- `get_proxies()` uses TCP port probing and cached primary/backup selection.
- `probe_best_proxy()` uses HTTP requests against a target URL to compare direct/xray/clash latency.

This differs from the operational rule that proxy health detection should avoid HTTP probing in normal search paths.

Recommended direction:

1. Keep TCP probing as the production default.
2. Move HTTP latency probing behind an explicit diagnostic function or flag.
3. Update source modules to use one consistent production proxy API.
4. Update the skill/documentation after the implementation is aligned.

## Priority 3: Documentation Alignment

Keep operational documentation synchronized with the code.

Required updates after fixes:

- README current status and known limitations.
- CHANGELOG entries for bug fixes and test coverage.
- Skill notes for proxy behavior, cover download behavior, and test status.
- Performance plan status if budget semantics change.

## Deferred Work

These are intentionally not recommended for the next iteration.

1. Full async architecture

   Calibre-Web's metadata provider interface is synchronous. Async internals would add complexity without proving that the UI can consume incremental results.

2. Persistent cache

   Phase 3 was skipped for a good reason: single-user usage has low repeated-query volume. Persistent cache adds state, invalidation, and filesystem concerns.

3. Large module extraction

   `MultiSource.py` is somewhat large, but not yet the main risk. Extract modules only after tests cover the current behavior.

4. More data sources

   Current quality bottlenecks are source stability, timeout behavior, and merge correctness, not source count.

## Suggested Execution Order

1. Fix `import os` in `MultiSource.py`.
2. Fix the failing unit test and keep the suite green.
3. Add regression tests for cover download, fallback, circuit breaker, and budget behavior.
4. Validate whether the search budget is truly bounded.
5. Simplify production proxy selection to one consistent strategy.
6. Update README, CHANGELOG, and skill notes after behavior changes are verified.
