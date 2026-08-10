"""
Phase 5 基准测试 — 与 Phase 0 相同 query 集，对比优化后效果
"""
import sys
import time
sys.path.insert(0, '/app/calibre-web/cps/metadata_provider')

from MultiSource import MultiSource

QUERIES = [
    ("三体", "zh-hot"),
    ("Python Programming", "en"),
    ("9787544270878", "isbn"),
    ("原则", "zh"),
    ("三体", "zh-hot"),
    ("活着", "zh-hot"),
    ("明朝那些事儿", "zh-hot"),
    ("深入理解计算机系统", "zh-tech"),
    ("百年孤独", "zh-lit"),
    ("Design Patterns", "en-tech"),
    ("The Great Gatsby", "en-lit"),
    ("9787111633224", "isbn"),
    ("9787506365437", "isbn"),
    ("三体", "zh-repeat"),
]

import importlib
config = importlib.import_module('MultiSource')

plugin = MultiSource()
print(f"Plugin loaded. Sources: {[s.SOURCE_NAME for s in plugin.sources]}")
print(f"Config: BUDGET={config.SEARCH_BUDGET_SECONDS}s"
      f" SOURCE_TO={config.SOURCE_TIMEOUT}s"
      f" RETRY={config.SOURCE_RETRY_ENABLED}"
      f" ZH_SKIP_OL={config.ZH_SKIP_OPENLIBRARY}"
      f" ZH_SKIP_GB={config.ZH_SKIP_GOOGLEBOOKS}"
      f" CB={config.SOURCE_CIRCUIT_BREAKER_ENABLED}"
      f" CASCADE_MAX={config.CASCADE_MAX_RECORDS}"
)
print()

print(f"{'#':>2} | {'Query':<25} | {'Type':<8} | {'Total':>7} | {'Results':>7}")
print("-" * 60)

for i, (query, qtype) in enumerate(QUERIES, 1):
    t0 = time.time()
    try:
        results = plugin.search(query)
        elapsed = time.time() - t0
        count = len(results) if results else 0
        print(f"{i:>2} | {query:<25} | {qtype:<8} | {elapsed:>6.2f}s | {count:>7}")
    except Exception as e:
        elapsed = time.time() - t0
        print(f"{i:>2} | {query:<25} | {qtype:<8} | {elapsed:>6.2f}s | ERROR: {e}")

print()
print("Done. Check container logs for per-source timing breakdown.")
