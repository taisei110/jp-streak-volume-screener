"""
連騰スクリーナー Streamlitアプリ用のデータ鮮度判定(計画書6章)。

既存の jp_streak_volume_screener.py には手を入れず、アプリ専用ロジックを
この薄いモジュールに置く。CLI・daemon・自動取得からは参照されない。
"""

from datetime import datetime, timedelta

# yfinanceは大引け直後に当日終値を反映しないことがあるため、
# この時刻(JST)以降なら当日を「取得対象の営業日」とみなす。
# 夜間ジョブ(20:00)完了後の21時にずらし、20時台にアプリで手動取得した場合と
# ジョブが競合する窓をなくす。
FRESH_CUTOFF_HOUR = 21

try:
    import jpholiday
    HAS_JPHOLIDAY = True
except ImportError:
    jpholiday = None
    HAS_JPHOLIDAY = False


def _is_market_holiday(d):
    """土日または日本の祝日(jpholiday導入時のみ)なら True。"""
    if d.weekday() >= 5:
        return True
    if HAS_JPHOLIDAY and jpholiday.is_holiday(d):
        return True
    return False


def latest_trading_day(now=None):
    """直近の「データが揃っているはずの営業日」を返す。

    - 当日が営業日でも FRESH_CUTOFF_HOUR より前は反映待ちとみなし、前営業日を返す。
    - 土日(およびjpholiday導入時は祝日)は遡ってスキップする。
    """
    if now is None:
        now = datetime.now()
    d = now.date()
    if now.hour < FRESH_CUTOFF_HOUR:
        d -= timedelta(days=1)
    while _is_market_holiday(d):
        d -= timedelta(days=1)
    return d


def db_latest_date(con):
    """DBの最新データ日を返す。テーブル未作成・空なら None。"""
    try:
        return con.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    except Exception:
        return None


def is_data_fresh(con, now=None):
    """(fresh, db_date, target_day) を返す。

    fresh = DBの最新日 >= 直近営業日。DBが空・テーブル無しは「要更新」側に倒す。
    """
    target = latest_trading_day(now)
    db_date = db_latest_date(con)
    if db_date is None:
        return False, None, target
    return db_date >= target, db_date, target
