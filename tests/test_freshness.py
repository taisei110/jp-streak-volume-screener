"""データ鮮度判定(screener_core.latest_trading_day)の検証。

「直近の、データが揃っているはずの営業日」を、反映待ち時刻と土日を考慮して
正しく返すかを、固定した日時で確認する。
"""
import datetime as dt

from screener_core import FRESH_CUTOFF_HOUR, latest_trading_day


def test_weekday_before_cutoff_returns_previous_day():
    # 水曜の朝(反映待ち時刻より前) → 前営業日(火)を返す
    now = dt.datetime(2026, 6, 24, 10, 0)  # Wed
    assert latest_trading_day(now) == dt.date(2026, 6, 23)  # Tue


def test_weekday_after_cutoff_returns_same_day():
    # 水曜の夜(反映待ち時刻より後) → 当日を返す
    now = dt.datetime(2026, 6, 24, FRESH_CUTOFF_HOUR + 1, 0)
    assert latest_trading_day(now) == dt.date(2026, 6, 24)


def test_weekend_skips_back_to_friday():
    # 日曜の夜 → 直近営業日の金曜まで遡る
    now = dt.datetime(2026, 6, 28, 22, 0)  # Sun
    assert latest_trading_day(now) == dt.date(2026, 6, 26)  # Fri


def test_saturday_returns_friday():
    now = dt.datetime(2026, 6, 27, 23, 0)  # Sat
    assert latest_trading_day(now) == dt.date(2026, 6, 26)  # Fri
