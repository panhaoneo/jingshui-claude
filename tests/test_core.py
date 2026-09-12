import numpy as np
import pandas as pd
import pytest

from jingshui.adjust import forward_factors
from jingshui.fundamentals import a_metrics, c_metrics, growth_decelerating, single_quarters, yoy
from jingshui.market import evaluate_index
from jingshui.pipeline import load_config
from jingshui.scoring import score_a, score_c, score_n, score_stock
from jingshui.sectors import score_sectors
from jingshui.signals import buy_point

CFG = load_config("config.yaml")


# ---------------- 复权 ----------------
def test_forward_adjust_bonus_split_is_continuous():
    dates = pd.bdate_range("2026-01-05", periods=6)
    raw = [10.0, 10.2, 10.4, 5.3, 5.4, 5.5]  # 第 4 天 10 送 10
    k = pd.DataFrame({"thscode": "A.SZ", "date": dates, "close": raw})
    ev = pd.DataFrame({"thscode": ["A.SZ"], "ex_date": [dates[3]], "d": [0.0], "s": [1.0], "r": [0.0], "p": [0.0]})
    f = forward_factors(k, ev)
    adj = k["close"] * f
    assert f.iloc[-1] == pytest.approx(1.0)
    assert adj.iloc[2] == pytest.approx(5.2)  # 10.4 / 2
    assert adj.iloc[3] / adj.iloc[2] - 1 == pytest.approx(5.3 / 5.2 - 1)


def test_forward_adjust_cash_dividend():
    dates = pd.bdate_range("2026-01-05", periods=3)
    k = pd.DataFrame({"thscode": "B.SH", "date": dates, "close": [20.0, 20.0, 19.0]})
    ev = pd.DataFrame({"thscode": ["B.SH"], "ex_date": [dates[2]], "d": [1.0], "s": [0.0], "r": [0.0], "p": [0.0]})
    f = forward_factors(k, ev)
    assert (k["close"] * f).iloc[1] == pytest.approx(19.0)  # 除息 1 元后价格连续


def test_forward_adjust_no_events():
    k = pd.DataFrame({"thscode": "C.SZ", "date": pd.bdate_range("2026-01-05", periods=3), "close": [1.0, 2.0, 3.0]})
    assert list(forward_factors(k, None)) == [1.0, 1.0, 1.0]


# ---------------- 财务口径 ----------------
def _q(fy, q, rev, np_, rep):
    return {"fiscal_year": fy, "quarter": q, "period_end": f"{fy}-{q * 3:02d}-28", "report_date": rep,
            "revenue": rev, "parent_np": np_}


QUARTERLY = [
    _q(2025, 1, 100, 10, "2025-04-20"), _q(2025, 2, 220, 24, "2025-08-20"),
    _q(2025, 3, 350, 40, "2025-10-20"), _q(2025, 4, 500, 60, "2026-03-30"),
    _q(2026, 1, 150, 16, "2026-04-20"), _q(2026, 2, 330, 38, "2026-08-20"),
]


def test_single_quarter_derivation():
    sq = single_quarters(QUARTERLY, pd.Timestamp("2026-09-01"))
    last = sq.iloc[-1]
    assert (last.fy, last.q) == (2026, 2)
    assert last.revenue == 180 and last.np == 22  # 330-150, 38-16
    q4 = sq[(sq.fy == 2025) & (sq.q == 4)].iloc[0]
    assert q4.np == 20  # 年报累计 60 − 三季报累计 40


def test_yoy_against_same_quarter_last_year():
    c = c_metrics({"quarterly": QUARTERLY}, pd.Timestamp("2026-09-01"))
    assert c["period"] == "2026Q2"
    assert c["np_yoy"] == pytest.approx(22 / 14 - 1)  # 去年 Q2 单季 24-10=14
    assert c["prev_np_yoy"] == pytest.approx(16 / 10 - 1)


def test_point_in_time_excludes_unreleased_report():
    c = c_metrics({"quarterly": QUARTERLY}, pd.Timestamp("2026-08-01"))
    assert c["period"] == "2026Q1"  # 半年报 8/20 才披露


def test_yoy_negative_base_is_none():
    assert yoy(10, -5) is None and yoy(None, 5) is None and yoy(15, 10) == pytest.approx(0.5)


def test_a_metrics_roe_and_growth():
    fin = {"annual": [{"fiscal_year": y, "quarter": 4, "parent_np": v, "report_date": f"{y + 1}-04-01"}
                      for y, v in [(2021, 10), (2022, 13), (2023, 17), (2024, 22), (2025, 28)]],
           "balance": [{"fiscal_year": 2024, "equity": 100, "report_date": "2025-04-01"},
                       {"fiscal_year": 2025, "equity": 120, "report_date": "2026-04-01"}]}
    a = a_metrics(fin, pd.Timestamp("2026-09-01"))
    assert [round(g["yoy"], 3) for g in a["growth"]] == [round(17 / 13 - 1, 3), round(22 / 17 - 1, 3), round(28 / 22 - 1, 3)]
    assert a["roe"] == pytest.approx(28 / 110)


def test_growth_deceleration_trigger():
    c = {"history": [{"np_yoy": 0.9, "rev_yoy": 0.3}, {"np_yoy": 0.5, "rev_yoy": 0.28}, {"np_yoy": 0.2, "rev_yoy": 0.27}]}
    assert growth_decelerating(c, 1 / 3) == {"np_yoy": True, "rev_yoy": False}


# ---------------- 评分 ----------------
def test_c_score_thresholds():
    cfg = CFG["stocks"]["C"]
    base = {"available": True, "rev_yoy": 0.3, "prev_np_yoy": 0.1}
    assert score_c({**base, "np_yoy": 0.20}, cfg)[0] == 0  # <25% 不起分
    s25 = score_c({**base, "np_yoy": 0.25}, cfg)[1]["profit"]
    s50 = score_c({**base, "np_yoy": 0.50}, cfg)[1]["profit"]
    assert s25 == pytest.approx(7.0) and s50 == pytest.approx(14.0)
    assert score_c({**base, "np_yoy": 0.8}, cfg)[0] == pytest.approx(20.0)  # 满分含营收与加速


def test_n_score_steps():
    cfg = CFG["stocks"]["N"]
    assert score_n(0.04, cfg)[0] == 20 and score_n(0.08, cfg)[0] == 10 and score_n(0.12, cfg)[0] == 0


def test_a_requires_growth_years():
    cfg = CFG["stocks"]["A"]
    a = {"available": True, "growth": [{"yoy": 0.3}, {"yoy": 0.4}, {"yoy": 0.5}], "roe": 0.30}
    assert score_a(a, cfg)[0] == pytest.approx(15.0)


def test_pass_requires_nonzero_core_factors():
    stc = CFG["stocks"]
    row = {"dist_high": 0.02, "up_vol_ratio": 2.0, "float_mcap_yi": 100, "rs": 99,
           "sector_rank_pct": 0.1, "org_net_60d": 1e8, "turnover_pct": 99}
    c = {"available": True, "np_yoy": 0.10, "rev_yoy": 0.4, "prev_np_yoy": 0.05}
    a = {"available": True, "growth": [{"yoy": 0.3}] * 3, "roe": 0.3}
    r = score_stock(row, c, a, stc, True)
    assert "C" in r["zero_factors"] and not r["passed"]
    r2 = score_stock(row, {**c, "np_yoy": 0.6}, a, stc, True)
    assert r2["passed"] and r2["total"] >= 70


# ---------------- L0 ----------------
def _index(n=260, drift=0.001, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + drift + rng.normal(0, 0.002, n))
    return pd.DataFrame({"date": pd.bdate_range("2025-06-02", periods=n), "close": close,
                         "high": close * 1.01, "low": close * 0.99, "volume": 1e9})


def test_market_offense_in_clean_uptrend():
    r = evaluate_index(_index(), CFG["market"])
    assert r["state"] == "OFFENSE" and r["dd_count"] == 0


def test_market_distribution_days_counted():
    df = _index()
    for i in range(-20, 0, 4):  # 5 个派发日：放量收跌
        df.loc[df.index[i], "close"] = df["close"].iloc[i - 1] * 0.99
        df.loc[df.index[i], "volume"] = 2e9
    r = evaluate_index(df, CFG["market"])
    assert r["dd_count"] == 5 and r["state"] != "OFFENSE"


def test_market_defense_on_death_cross():
    r = evaluate_index(_index(drift=-0.002), CFG["market"])
    assert r["state"] == "DEFENSE"


# ---------------- L1 ----------------
def test_sector_near_high_required():
    up = _index(drift=0.002)
    down = _index(drift=0.002)
    down.loc[down.index[-30:], "close"] = down["close"].iloc[-31] * 0.8
    t = score_sectors({"A": up, "B": down}, {"A": "强", "B": "弱"}, CFG["sectors"])
    assert t.set_index("code").loc["A", "mainline"] and not t.set_index("code").loc["B", "mainline"]


# ---------------- L3 ----------------
def _stock(pullback=0.12, restart_vol=3.0):
    """上涨 200 日 → 回调 pullback → 横盘 → 最后一日放量上攻。"""
    n_up, n_down, n_flat = 200, 10, 10
    up = 10 * np.cumprod(np.full(n_up, 1.004))
    peak = up[-1]
    down = np.linspace(peak, peak * (1 - pullback), n_down + 1)[1:]
    flat = np.full(n_flat, peak * (1 - pullback) * 1.01)
    close = np.concatenate([up, down, flat, [flat[-1] * 1.04]])
    vol = np.full(len(close), 1e6)
    vol[-1] = restart_vol * 1e6
    idx = pd.bdate_range("2025-09-01", periods=len(close))
    return pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995, "close": close,
                         "volume": vol, "turnover": close * vol}, index=idx)


def test_buy_point_pullback_then_volume_restart():
    r = buy_point(_stock(), CFG["buy"])
    assert r["ok"], r["status"]
    assert r["stops"][0] == pytest.approx(r["entry"] * 0.93, rel=1e-3)


def test_no_buy_without_volume_or_breakout():
    df = _stock(pullback=0.10, restart_vol=1.0)
    df.iloc[-1, df.columns.get_loc("close")] = df["close"].iloc[-2] * 1.001
    df.iloc[-1, df.columns.get_loc("high")] = df["close"].iloc[-1]
    r = buy_point(df, CFG["buy"])
    assert not r["ok"] and "等待起涨确认" in r["status"]


def test_too_deep_pullback_rejected():
    r = buy_point(_stock(pullback=0.35), CFG["buy"])
    assert not r["ok"]


# ---------------- 持仓体检 ----------------
def _holding_df(closes, vols=None):
    idx = pd.bdate_range("2026-01-05", periods=len(closes))
    c = np.asarray(closes, dtype=float)
    v = np.full(len(c), 1e6) if vols is None else np.asarray(vols, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": v,
                         "turnover": c * v}, index=idx)


def test_holding_stop_loss_per_batch():
    from jingshui.signals import check_holding
    df = _holding_df(np.linspace(10, 12, 80).tolist() + [11.0])
    h = {"batches": [{"date": str(df.index[-5].date()), "price": 12.0, "weight": 0.1}]}
    r = check_holding("X.SZ", h, df, None, None, "OFFENSE", CFG)
    assert any("止损" in a for a in r["actions"])  # 11 ≤ 12×0.93


def test_holding_hold_8_weeks_rule():
    from jingshui.signals import check_holding
    closes = [10.0] * 60 + list(np.linspace(10, 12.5, 12))
    df = _holding_df(closes)
    h = {"batches": [{"date": str(df.index[60].date()), "price": 10.0, "weight": 0.1}]}
    r = check_holding("X.SZ", h, df, None, None, "OFFENSE", CFG)
    assert any("8 周" in n for n in r["notes"]) and not r["actions"]


def test_holding_defense_halves():
    from jingshui.signals import check_holding
    df = _holding_df(np.linspace(10, 12, 80).tolist())
    h = {"batches": [{"date": str(df.index[-10].date()), "price": 11.5, "weight": 0.1}]}
    r = check_holding("X.SZ", h, df, None, None, "DEFENSE", CFG)
    assert any("DEFENSE" in a for a in r["actions"])
