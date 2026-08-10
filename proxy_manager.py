"""
proxy_manager — 代理健康检测与自动切换

每次搜索前自动检测主代理链路是否通畅，不通则切换备代理。
仅使用底层 TCP socket 探测（不引入 requests/urllib3），
避免触发 calibre-web 内置 cw_advocate 的代理禁令。

用法:
    from .proxy_manager import get_proxies, get_current_proxy_info

    proxies = get_proxies()  # 返回 {"http": url, "https": url} 或 None
    resp = requests.get(url, proxies=proxies, verify=False)
"""
import socket
import time

# ============================================================
# 代理列表（主 → 备）
# ============================================================
PROXY_LIST = [
    ("xray",   "http://192.168.1.249:20172"),   # 主代理：NAS 主机 xray 进程
    ("clash",  "http://192.168.1.249:17890"),   # 备代理：Docker mihomo 容器
]

# ============================================================
# 健康检测配置
# ============================================================
TCP_TIMEOUT = 1              # TCP 端口探测超时（秒）
PROXY_CACHE_SECONDS = 60     # 检测结果缓存时间
PROXY_DIAGNOSTIC_ENABLED = False  # HTTP 延迟探测开关 — 生产默认关闭，避免触发 cw_advocate 代理禁令

# 运行时状态
_active_proxy = None
_proxy_last_check = 0


# ---- 公开 API ----

def get_proxies():
    """
    获取当前可用代理配置。自动检测主备链路。

    仅做 TCP 端口探测 — 不发起任何 HTTP 请求，
    避免触发 calibre-web 内置 cw_advocate 的代理禁令。

    返回:
        {"http": url, "https": url} — 选中代理
        None                         — 所有代理不可用，直连
    """
    global _active_proxy, _proxy_last_check

    now = time.time()
    if _active_proxy is not None and (now - _proxy_last_check) < PROXY_CACHE_SECONDS:
        return {"http": _active_proxy, "https": _active_proxy}

    # 按顺序检测主备 TCP 端口
    for name, url in PROXY_LIST:
        if _check_port(name, url):
            _active_proxy = url
            _proxy_last_check = now
            return {"http": url, "https": url}

    # 都不可用
    _active_proxy = None
    _proxy_last_check = now
    return None


def get_current_proxy_info():
    """返回当前选中代理的描述（用于日志）"""
    if _active_proxy is None:
        return "直连（无代理）"
    for name, url in PROXY_LIST:
        if url == _active_proxy:
            return f"{name} ({url})"
    return f"未知 ({_active_proxy})"


# ---- 内部 ----

def _check_port(name, url):
    """
    纯 TCP 端口探测 — 代理进程是否在监听？

    不使用 requests/urllib3 — 避免触发 cw_advocate 的
    "Proxies cannot be used with Advocate" 错误。
    """
    try:
        host, port = url.replace("http://", "").split(":")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TCP_TIMEOUT)
        result = sock.connect_ex((host, int(port)))
        sock.close()
        return result == 0
    except Exception:
        return False


def probe_best_proxy(target_url="https://openlibrary.org/search.json?q=test&limit=1",
                     timeout=5):
    """测试直连、xray、clash 的延迟，返回最优代理配置。

    仅在 PROXY_DIAGNOSTIC_ENABLED=True 时执行 HTTP 探测。
    生产搜索路径使用 get_proxies() (纯 TCP 探测) 以规避 cw_advocate 代理禁令。
    返回: (proxies_dict, name) 或 (None, "direct")
    """
    if not PROXY_DIAGNOSTIC_ENABLED:
        return (None, "direct")

    import requests
    import time

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }
    _PROBE_PROXY_LIST = [
        ("direct", None),
        ("xray", {"http": "http://192.168.1.249:20172", "https": "http://192.168.1.249:20172"}),
        ("clash", {"http": "http://192.168.1.249:17890", "https": "http://192.168.1.249:17890"}),
    ]

    results = []
    for name, px in _PROBE_PROXY_LIST:
        t0 = time.time()
        try:
            r = requests.get(target_url, params={}, headers=headers,
                             proxies=px, verify=False, timeout=timeout)
            if r.status_code == 200:
                latency = time.time() - t0
                results.append((latency, name, px))
        except Exception:
            pass

    if not results:
        # 全部不通，fallback 到 xray
        return ({"http": "http://192.168.1.249:20172", "https": "http://192.168.1.249:20172"}, "xray")

    # 延迟排序，取最快
    results.sort(key=lambda x: x[0])
    best_latency, best_name, best_px = results[0]

    # 如果直连和最快代理差距在 1s 内，优先直连
    direct_result = next((r for r in results if r[1] == "direct"), None)
    if direct_result and direct_result[0] <= best_latency + 1.0:
        return (None, "direct")

    return (best_px, best_name)
