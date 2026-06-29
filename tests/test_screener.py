"""スクリーニングSQLの中核ロジックを合成データで検証する。

連騰(前日比プラス+陽線)の検出・出来高乖離の判定・流動性フィルタ・tier判定が
意図通り動くことを、外部ネットワークやyfinanceに依存せず確認する。
"""
import datetime as dt

import duckdb
import pytest

import jp_streak_volume_screener as core


def _make_db(rows):
    """(code, date, open, close, volume) のリストからインメモリDBを作る。"""
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE prices(code VARCHAR, date DATE, open DOUBLE, "
        "close DOUBLE, volume BIGINT, PRIMARY KEY(code,date))"
    )
    con.executemany("INSERT INTO prices VALUES (?,?,?,?,?)", rows)
    return con


def _series(code, base_days, base_vol, streak):
    """基準期間(横ばいの非陽線)＋末尾の連騰を持つ時系列を組み立てる。

    streak: [(open, close, volume), ...] を新しい順ではなく古い順で渡す。
    """
    rows = []
    d = dt.date(2026, 1, 5)
    # 基準期間: close<open(陰線)で up_day=false にし、連騰に含まれないようにする
    for _ in range(base_days):
        rows.append((code, d, 100.0, 99.0, base_vol))
        d += dt.timedelta(days=1)
    for op, cl, vol in streak:
        rows.append((code, d, op, cl, vol))
        d += dt.timedelta(days=1)
    return rows


def test_detects_streak_with_volume_surge():
    # 3日連騰 + 出来高3倍 → tier1でヒットするはず
    rows = _series(
        "AAAA", base_days=15, base_vol=20000,
        streak=[(99.5, 101.0, 60000), (101.0, 103.0, 60000), (103.0, 106.0, 60000)],
    )
    con = _make_db(rows)
    res = core.screen(con, core.ScreenConfig())
    con.close()

    assert "AAAA" in set(res["code"]), "連騰+出来高急増の銘柄が検出されない"
    row = res[res["code"] == "AAAA"].iloc[0]
    assert int(row["streak_len"]) == 3
    assert int(row["tier"]) == 1
    assert row["min_ratio"] == pytest.approx(3.0, abs=0.01)  # 60000/20000
    assert row["rise_pct"] > 0


def test_no_streak_is_excluded():
    # 連騰していない(下落基調)銘柄は除外される
    rows = []
    d = dt.date(2026, 1, 5)
    for _ in range(20):
        rows.append(("DOWN", d, 100.0, 98.0, 50000))  # 毎日陰線
        d += dt.timedelta(days=1)
    con = _make_db(rows)
    res = core.screen(con, core.ScreenConfig())
    con.close()
    assert "DOWN" not in set(res["code"])


def test_liquidity_filter_excludes_thin_names():
    # 基準出来高が MIN_VOLUME 以下の薄商い銘柄は、連騰していても除外される
    rows = _series(
        "THIN", base_days=15, base_vol=500,  # 既定 MIN_VOLUME=10000 を下回る
        streak=[(99.5, 101.0, 1500), (101.0, 103.0, 1500), (103.0, 106.0, 1500)],
    )
    con = _make_db(rows)
    res = core.screen(con, core.ScreenConfig())  # 既定で min_volume=10000
    con.close()
    assert "THIN" not in set(res["code"]), "流動性フィルタが効いていない"


def test_volume_below_threshold_not_a_hit():
    # 連騰はしているが出来高が基準とほぼ同水準 → 乖離不足で非ヒット
    rows = _series(
        "FLAT", base_days=15, base_vol=20000,
        streak=[(99.5, 101.0, 21000), (101.0, 103.0, 21000), (103.0, 106.0, 21000)],
    )
    con = _make_db(rows)
    res = core.screen(con, core.ScreenConfig())  # VOL_MULT=1.3, min_ratio≈1.05
    con.close()
    assert "FLAT" not in set(res["code"])
