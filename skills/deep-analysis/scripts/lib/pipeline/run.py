"""pipeline.run · 编排入口 · collect → score → synthesize.

**v3.0.0 默认路径**：`run.py <ticker>` 默认走这里。`UZI_LEGACY=1` 才走 legacy.

用法：
    from lib.pipeline.run import run_pipeline
    report_path = run_pipeline("300470.SZ")
"""
from __future__ import annotations

import json
from pathlib import Path

from .collect import collect as pipeline_collect
from .score import score_from_cache
from .synthesize import synthesize_and_render


def run_pipeline(ticker: str, resume: bool = True) -> str:
    """完整管道入口（v3.0.0 主干）.

    1. pipeline.collect · 用 22 BaseFetcher adapter 并发抓数据（max_workers=6）
    2. 写 .cache/<ticker>/raw_data.json（与 legacy schema 兼容）
    3. pipeline.score_from_cache · 直接调 rrt 纯函数（score_dimensions / generate_panel /
       generate_synthesis）· 不再调 stage1（stage1 会重新 collect）
    4. pipeline.synthesize_and_render · 调 stage2（stage2 只读 cache 不 collect · OK）

    Phase 6c 升级：score 阶段解耦 legacy stage1 · 不再重复 collect · 省 5-10 min/股.
    """
    # v3.0.0 · pre-flight guards · 不兼容场景抛异常让 run.py fallback legacy（legacy 有完整解析）
    _preflight_guards(ticker)

    print(f"🚀 [pipeline.run] collect · {ticker}")
    raw_previous = _load_cache(ticker) if resume else {}
    raw_dict = pipeline_collect(ticker, raw_previous=raw_previous, max_workers=6)

    # 组装 legacy 兼容 raw_data.json（dimensions + 顶层溢出字段）
    raw_data_compatible = {
        "ticker": ticker,
        "dimensions": {k: v for k, v in raw_dict.items()
                       if k not in ("fund_managers", "similar_stocks")},
    }
    for k in ("fund_managers", "similar_stocks"):
        if k in raw_dict:
            raw_data_compatible[k] = raw_dict[k]

    _write_cache(ticker, raw_data_compatible)

    # Task 1.5 · 机构级财务建模 (Dims 20-22) · 补回 v3.0 pipeline 遗漏的 legacy stage1 step.
    # 缺这步 institutional_modeling (DCF/LBO/IC memo/target_price/BCG) 全是 None.
    _attach_institutional_modeling(ticker, raw_data_compatible)

    # pipeline.score_from_cache · 直接调 rrt.score_dimensions/generate_panel/generate_synthesis
    # 不再走 rrt.stage1（stage1 会重新 collect · 浪费时间）
    score_from_cache(ticker)
    return synthesize_and_render(ticker)


def _attach_institutional_modeling(ticker: str, raw: dict) -> None:
    """补跑 legacy stage1 Task 1.5 (Dims 20-22) · 填 raw["dimensions"] + 重写 cache.

    包含：
      - 20_valuation_models: DCF + LBO + Comps
      - 21_research_workflow: 首次覆盖评级 + 目标价 + upside
      - 22_deep_methods: IC memo + BCG position + 行业吸引力

    pipeline 路径默认不跑这一步 · 导致 synthesis.institutional_modeling 字段全 None.
    本函数完全照搬 run_real_test.stage1 第 574-583 行的代码.
    """
    from compute_deep_methods import compute_dim_20, compute_dim_21, compute_dim_22
    from lib.stock_features import extract_features

    dims = raw.setdefault("dimensions", {})
    features_pre = extract_features(raw, dims)
    _normalize_yi_units(features_pre)
    dims["20_valuation_models"] = compute_dim_20(features_pre, raw)
    d20 = dims["20_valuation_models"]["data"]
    dims["21_research_workflow"] = compute_dim_21(features_pre, raw, d20)
    d21 = dims["21_research_workflow"]["data"]
    dims["22_deep_methods"] = compute_dim_22(features_pre, raw, d20, d21)

    s20 = d20.get("summary", {})
    s21 = d21.get("summary", {})
    s22 = dims["22_deep_methods"]["data"].get("summary", {})
    print(f"🏛  [pipeline.run] Task 1.5 机构级建模")
    print(f"   DCF: ¥{s20.get('dcf_intrinsic')} · 安全边际 {s20.get('dcf_safety_margin_pct')}% · {s20.get('dcf_verdict')}")
    print(f"   LBO: IRR {s20.get('lbo_irr_pct')}% · {s20.get('lbo_verdict')}")
    print(f"   首次覆盖: {s21.get('rec_rating')} · TP ¥{s21.get('target_price')} ({s21.get('upside_pct')}%)")
    print(f"   IC Memo: {s22.get('ic_recommendation')}")

    _write_cache(ticker, raw)  # 重写含 d20/21/22 的 raw_data.json


def _normalize_yi_units(features: dict) -> None:
    """修 features 里 `_yi` 后缀字段单位错的兜底.

    extract_features 假设 raw 的 market_cap 是 "X亿" 字符串，实际上 v3.0
    fetcher 返回的是元数值 (e.g., 33,439,595,383)。replace("亿","") 不起作用，
    market_cap_yi 字面值就是元 · 然后 shares_outstanding_yi = mcap/price
    也跟着错（应是亿股，实际是股）· 导致 compute_dcf 内 intrinsic_per_share
    被压成 0.

    A 股最大市值约 4 万亿元 ≈ 4e4 亿，所以 >1e6 必然是元单位.
    """
    mc = features.get("market_cap_yi") or 0
    if mc > 1e6:  # 元单位 → 转亿
        features["market_cap_yi"] = round(mc / 1e8, 3)
    cc = features.get("circulating_cap_yi") or 0
    if cc > 1e6:
        features["circulating_cap_yi"] = round(cc / 1e8, 3)
    # shares_outstanding_yi 由 extract_features 用错单位的 market_cap_yi 派生
    # · 修完 market_cap_yi 后必须重算
    px = features.get("price") or 0
    if px > 0 and features["market_cap_yi"] > 0:
        features["shares_outstanding_yi"] = round(features["market_cap_yi"] / px, 3)


def _preflight_guards(ticker: str) -> None:
    """v3.0.0 · pipeline 不覆盖的场景 · 抛异常让 run.py 回退 legacy.

    抛 ValueError 触发 fallback（不 crash · run.py catch 后走 legacy stage1 能正常处理）.
    """
    from lib.market_router import is_chinese_name, parse_ticker, classify_security_type

    # 1. 中文名 · 由 legacy stage1 的 resolve_chinese_name_rich 处理
    if is_chinese_name(ticker):
        raise ValueError(
            f"pipeline: 中文名 {ticker!r} 需 legacy 解析 · fallback"
        )

    # 2. ETF / LOF / 可转债 · legacy stage1 有完整 guidance
    try:
        ti = parse_ticker(ticker)
        if ti.market == "A":
            sec_type = classify_security_type(ti.code)
            if sec_type in ("etf", "lof", "convertible_bond", "index"):
                raise ValueError(
                    f"pipeline: {sec_type} 证券类型需 legacy 处理 · fallback"
                )
    except ValueError:
        raise  # 重新抛 · 让 run.py fallback
    except Exception:
        pass  # 其他异常（parse 失败）· 让 pipeline 自己尝试 · 失败后再 fallback


def _load_cache(ticker: str) -> dict:
    """读已有 raw_data.json · 用于 resume."""
    from lib.market_router import parse_ticker
    ti = parse_ticker(ticker)
    import run_real_test as rrt
    cache_path = Path(rrt.__file__).parent / ".cache" / ti.full / "raw_data.json"
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cache(ticker: str, raw: dict) -> None:
    """写 raw_data.json · 让 legacy stage1 的 resume 能复用.

    `default=str` 兜底 fetcher 偶尔返回的 datetime.date / datetime / Decimal 等
    非 JSON 原生类型 · 防止 score_from_cache 后续读不到 raw_data.json.
    """
    from lib.market_router import parse_ticker
    ti = parse_ticker(ticker)
    import run_real_test as rrt
    cache_dir = Path(rrt.__file__).parent / ".cache" / ti.full
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "raw_data.json"
    cache_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"✅ [pipeline.run] raw_data.json 已写 · 进入 scoring 段（v3.0 纯函数编排）")
