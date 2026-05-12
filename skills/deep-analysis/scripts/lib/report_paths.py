"""Report directory naming · 统一 `{ticker}_{name}_{date}` 命名规则.

由 assemble_report / inline_assets / render_share_card 三处共用 · 保证写读一致.

设计：
- 写入新报告时调 `build_report_dir(ticker)` · 从 cache 读股票名拼到目录名里
- 读已有报告时调 `find_report_dir(ticker)` · 先试新命名 · 退化 glob 兼容老报告
- name 缺失 / cache 不存在时退化到 `{ticker}_{date}` (老格式)
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def _sanitize_name(name: str) -> str:
    """去掉 path-unsafe 字符 + 限长 · ST 股的 * 会被去掉."""
    if not name:
        return ""
    cleaned = _INVALID_CHARS.sub("", name.strip())
    # 保留中文 / 字母 / 数字 / 横杠 · 长度限 20 字符防超长
    cleaned = re.sub(r"[^\w一-鿿\-]", "", cleaned)
    return cleaned[:20]


def _load_stock_name(ticker: str) -> str:
    """从 .cache/<ticker>/ 读股票名 · 优先 synthesis.json · 退化 raw_data.json."""
    import run_real_test as rrt
    cache_dir = Path(rrt.__file__).parent / ".cache" / ticker
    if not cache_dir.exists():
        return ""

    syn_path = cache_dir / "synthesis.json"
    if syn_path.exists():
        try:
            syn = json.loads(syn_path.read_text(encoding="utf-8"))
            name = syn.get("name") or ""
            if name:
                return _sanitize_name(name)
        except Exception:
            pass

    raw_path = cache_dir / "raw_data.json"
    if raw_path.exists():
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            basic = ((raw.get("dimensions") or {}).get("0_basic") or {}).get("data") or {}
            name = basic.get("name") or basic.get("stock_name") or ""
            if name:
                return _sanitize_name(name)
        except Exception:
            pass

    return ""


def build_report_dir(ticker: str, date: str | None = None) -> Path:
    """写入侧：构造 reports/{ticker}_{name}_{date} · name 缺失退化为 {ticker}_{date}."""
    date = date or datetime.now().strftime("%Y%m%d")
    name = _load_stock_name(ticker)
    if name:
        return Path("reports") / f"{ticker}_{name}_{date}"
    return Path("reports") / f"{ticker}_{date}"


def find_report_dir(ticker: str, date: str | None = None) -> Path:
    """读取侧：找已存在的报告目录.

    顺序：
      1. 新命名 reports/{ticker}_{name}_{date}
      2. 老命名 reports/{ticker}_{date}
      3. glob reports/{ticker}_* 取最新的（兼容跨日 / 已存在的老报告）

    找不到抛 FileNotFoundError.
    """
    date = date or datetime.now().strftime("%Y%m%d")

    # 1. 新命名
    name = _load_stock_name(ticker)
    if name:
        cand = Path("reports") / f"{ticker}_{name}_{date}"
        if cand.exists():
            return cand

    # 2. 老命名
    cand = Path("reports") / f"{ticker}_{date}"
    if cand.exists():
        return cand

    # 3. glob 兜底
    matches = sorted(Path("reports").glob(f"{ticker}_*"))
    if matches:
        return matches[-1]

    raise FileNotFoundError(f"No report dir for {ticker}")
