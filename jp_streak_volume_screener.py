"""
日本株スクリーナー: 直近N日連騰(前日比プラス+陽線) かつ 出来高が連騰前の中央値から乖離

データ源: yfinance (Yahoo Finance / 無料・APIキー不要)
保存先 : DuckDB (全置換でシンプルに運用)

抽出条件 (冒頭の設定で変更可):
  1. 最新日から連続して「前日比プラス」かつ「陽線(終値>始値)」が続く日数(連騰長)を数える。
     連騰長が STREAK_MIN 日以上の銘柄を対象にする。
  2. 出来高乖離。基準は「連騰初日の前日以前 BASE_WINDOW 日」の出来高中央値(連騰長に追従)。
     連騰中の出来高は基準の計算に含めない(混入で中央値が膨らむのを防ぐ)。
     - VOL_MODE = "all" : 連騰各日すべて >= 基準中央値 × 閾値 (厳しめ・既定)
     - VOL_MODE = "avg" : 連騰中の出来高平均 >= 基準中央値 × 閾値 (緩め)
     閾値は連騰長で変える。STREAK_TOP日以上は VOL_MULT、それ未満(STREAK_MIN以上)は VOL_MULT_SHORT。
  3. 流動性フィルタ(任意)。連騰前の平常時の出来高中央値が MIN_VOLUME 株「以下」、
     または売買代金中央値が MIN_TURNOVER 円「以下」の銘柄を除外する。0で無効。

ランキングは2段階(tier)。STREAK_TOP日以上(tier1)を上位に固定し、それ未満で
出来高乖離が大きいもの(tier2)を下位にランクインさせる。各tier内は複合スコア順。
複合スコアは出来高の勢いと価格の勢いをそれぞれ母集団内のパーセンタイル順位にして加重平均する。
レポートには連騰日数・上場区分(プライム/スタンダード/グロース)と対象データ期間を明記する。

使い方:
  pip install yfinance duckdb pandas openpyxl xlrd schedule requests html2image jpholiday
  python jp_streak_volume_screener.py            # 出来高版を1回実行(タスクスケジューラ向き・推奨)
  python jp_streak_volume_screener.py --metric=turnover  # 売買代金版で実行(出力は *_turnover.*)
  python jp_streak_volume_screener.py --no-fetch # 取得せず既存DBで抽出のみ(--metric併用可)
  python jp_streak_volume_screener.py --daemon    # 常駐し平日20:00に自動実行
  ※ 出来高版と売買代金版はDBを共有するので、毎晩両方回す場合は
    1本目は通常実行、2本目は --no-fetch --metric=turnover で取得を省ける。
  ※ requests/html2image はDiscord通知用、jpholidayは祝日スキップ用。いずれも任意。
    html2image は画像化にChrome/Chromium本体が必要(未導入ならテキストのみ通知)。

Discord通知の設定: WebhookURLは環境変数 DISCORD_WEBHOOK_URL から読む(ソースに書かない)。
  Windows(コマンドプロンプト): setx DISCORD_WEBHOOK_URL "https://discord.com/api/webhooks/..."
  設定後はターミナルを開き直すと反映される。未設定なら通知はスキップされる。

自動化(推奨): Windowsタスクスケジューラで毎日20:00に引数なし実行を登録する。
  プログラム : python
  引数       : C:\\...\\jp_streak_volume_screener.py
  開始        : このスクリプトのあるフォルダ
常駐させたい場合は --daemon を使う(PCを起動したままにする必要がある)。

注意: yfinance(Yahoo)は大引け直後に当日終値が反映されないことがある。20:00時点で
  当日分が未反映だと前営業日基準のレポートになるため、本スクリプトは最新データ日が
  当日でない場合にレポート上部と通知に注意書きを出す(基準日も明記する)。
"""

import os
import time
import sys
import subprocess
from dataclasses import dataclass
import duckdb
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from pathlib import Path

# ============================================================
# 設定
# ============================================================
STREAK_MIN  = 2        # ランクインする最小連騰日数(これ未満は除外)
STREAK_TOP  = 3        # この連騰日数以上を上位tier(tier1)に置く。未満かつMIN以上はtier2
BASE_WINDOW = 10       # 出来高基準中央値の参照期間。連騰初日の前日以前のこの日数で計算
VOL_MULT    = 1.3      # tier1(STREAK_TOP日以上)の出来高乖離の閾値 (基準中央値 × この倍率「以上」)
VOL_MULT_SHORT = 2.0   # tier2(2日連騰など短い連騰)の足切り。明確に厳しくして初動だけ救済する
VOL_MODE    = "all"    # "all"=連騰各日すべてで判定(厳しめ) / "avg"=平均で判定(緩め)
MIN_VOLUME  = 10000    # 流動性フィルタ: 連騰前の平常時出来高(中央値)がこの株数「以下」を除外。0で無効
MIN_TURNOVER = 0       # 流動性フィルタ(代金): 連騰前の平常時売買代金中央値[円]がこの値「以下」を除外。0で無効
                       # ※株数より売買代金の方が銘柄横断で公平。効かせるなら 1e8(1億円)程度を推奨
FETCH_PERIOD = "6mo"   # yfinanceで取得する期間。連騰+基準中央値判定に十分な長さ
CHUNK_SIZE  = 100      # yf.download を一度に投げる銘柄数(レート制限対策)
SLEEP_SEC   = 1.0      # チャンク間の待機秒(IPブロック回避)
DB_LOCK_RETRIES  = 5   # DBが他プロセス(アプリの取得等)にロックされている時の再試行回数
DB_LOCK_WAIT_SEC = 30  # 再試行の間隔(秒)

# ランキング: 出来高の勢い(min_ratio) と 価格の勢い(連騰中の上昇率) を
# それぞれ母集団内のパーセンタイル順位に変換し、加重平均してスコア化する。
# 生の値だと出来高(倍率)が上昇率(%)を支配するため、順位に揃えてから合成する。
W_VOL  = 0.5           # 出来高の勢いの重み
W_RISE = 0.5           # 価格の勢いの重み

DB_PATH      = os.path.join("data", "prices.duckdb")
UNIVERSE_CSV = "tickers.csv"   # コード列を持つCSVがあればこれを優先して使う
# ============================================================
# 指標切替: "volume"=出来高(株数) / "turnover"=売買代金(終値×出来高, 円)
# コマンドラインで --metric=turnover を付けると売買代金版として動く(設定より優先)。
# 売買代金は株価上昇自体が代金を押し上げるため出来高より通りやすい。閾値は別に持つ。
# ============================================================
METRIC = "volume"
TURNOVER_VOL_MULT       = 1.5   # 売買代金版のtier1閾値(出来高版のVOL_MULTに相当)
TURNOVER_VOL_MULT_SHORT = 2.5   # 売買代金版のtier2足切り(同VOL_MULT_SHORTに相当)


@dataclass(frozen=True)
class ScreenConfig:
    """スクリーニング設定。既定値はモジュール冒頭の定数(import時点の値)。
    frozen(ハッシュ可能)にしておくとアプリ側でキャッシュキーとして使える。"""
    metric: str = METRIC
    streak_min: int = STREAK_MIN
    streak_top: int = STREAK_TOP
    base_window: int = BASE_WINDOW
    vol_mult: float = VOL_MULT
    vol_mult_short: float = VOL_MULT_SHORT
    turnover_vol_mult: float = TURNOVER_VOL_MULT
    turnover_vol_mult_short: float = TURNOVER_VOL_MULT_SHORT
    vol_mode: str = VOL_MODE
    min_volume: float = MIN_VOLUME
    min_turnover: float = MIN_TURNOVER
    w_vol: float = W_VOL
    w_rise: float = W_RISE


def config_from_globals():
    """モジュール定数(--metric反映後)から ScreenConfig を組み立てる。CLI経路用。"""
    return ScreenConfig(
        metric=METRIC, streak_min=STREAK_MIN, streak_top=STREAK_TOP,
        base_window=BASE_WINDOW, vol_mult=VOL_MULT, vol_mult_short=VOL_MULT_SHORT,
        turnover_vol_mult=TURNOVER_VOL_MULT, turnover_vol_mult_short=TURNOVER_VOL_MULT_SHORT,
        vol_mode=VOL_MODE, min_volume=MIN_VOLUME, min_turnover=MIN_TURNOVER,
        w_vol=W_VOL, w_rise=W_RISE,
    )

OUTPUT_CSV   = "screen_result.csv"
OUTPUT_HTML  = "screen_result.html"
OUTPUT_PNG   = "screen_result.png"

# Discord通知用WebhookURL。ソースに直書きせず環境変数から読む(秘密情報のため)。
# 未設定なら通知はスキップされる。設定方法は冒頭docstring参照。
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# JPX「東証上場銘柄一覧」Excel (内国株式のコード取得用)
JPX_XLS_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"


# ============================================================
# 1. 銘柄ユニバース
# ============================================================
def get_universe():
    """(code -> 銘柄名) の dict と (code -> 上場区分) の dict を返す。
    tickers.csv があればそれを使い、無ければJPXのExcelから内国株式を取得する。"""
    if os.path.exists(UNIVERSE_CSV):
        df = pd.read_csv(UNIVERSE_CSV, dtype=str)
        code_col = [c for c in df.columns if "code" in c.lower() or "コード" in c][0]
        name_col = next((c for c in df.columns if "name" in c.lower() or "銘柄" in c), None)
        market_col = next((c for c in df.columns if "市場" in c or "market" in c.lower()), None)
        codes = df[code_col].str.strip()
        names = df[name_col].str.strip() if name_col else codes
        markets = df[market_col].str.strip() if market_col else pd.Series([""]*len(codes), index=codes.index)
        print(f"[universe] {UNIVERSE_CSV} から {len(codes)} 銘柄")
        return dict(zip(codes, names)), dict(zip(codes, markets))

    print("[universe] JPX Excel をダウンロード中...")
    df = pd.read_excel(JPX_XLS_URL, dtype=str)
    # 列名は「コード」「銘柄名」「市場・商品区分」
    df = df[df["市場・商品区分"].str.contains("内国株式", na=False)]
    codes = df["コード"].str.strip()
    names = df["銘柄名"].str.strip()
    # "プライム（内国株式）" -> "プライム"
    markets = df["市場・商品区分"].str.replace(r"[（(].*$", "", regex=True).str.strip()
    print(f"[universe] 内国株式 {len(codes)} 銘柄")
    return dict(zip(codes, names)), dict(zip(codes, markets))


# ============================================================
# 2. 株価取得 -> DuckDB 保存
# ============================================================
def _is_lock_error(e):
    """DuckDBのファイルロック競合による接続失敗かどうかを判定する。
    OS由来の文言はロケールで変わる(日本語Windowsは「使用中」)ため複数パターンで見る。"""
    msg = str(e).lower()
    return ("lock" in msg or "being used" in msg
            or "already open" in msg or "使用中" in msg)


def connect_db_with_retry(path=None, retries=DB_LOCK_RETRIES, wait_sec=DB_LOCK_WAIT_SEC):
    """read-write接続を返す。他プロセスがロック中なら待って再試行し、
    最終的に失敗したらログとDiscordに通知して例外を上げる。"""
    path = path or DB_PATH
    last = None
    for attempt in range(1, retries + 1):
        try:
            return duckdb.connect(path)
        except duckdb.Error as e:
            if not _is_lock_error(e):
                raise
            last = e
            print(f"[db] DBがロック中で接続できません({attempt}/{retries}回目)。"
                  f"{wait_sec}秒後に再試行します: {e}")
            if attempt < retries:
                time.sleep(wait_sec)
    msg = (f"DBロックが解除されず接続を断念しました"
           f"({retries}回試行/約{retries * wait_sec // 60}分待機): {last}")
    print(f"[error] {msg}")
    send_discord_error(msg)
    raise last


def fetch_to_db(codes, con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS prices(
            code VARCHAR, date DATE, open DOUBLE, close DOUBLE, volume BIGINT,
            PRIMARY KEY (code, date)
        )
    """)

    # 既存データの最新日付を取得して、差分ダウンロードにする（通信量の節約・高速化）
    max_date_val = con.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    today = datetime.now().date()
    
    fetch_kwargs = {
        "interval": "1d",
        "group_by": "ticker",
        "auto_adjust": True,
        "threads": True,
        "progress": False
    }

    if max_date_val:
        # 直近の休場日や、yfinanceの過去データ修正をカバーするため、5日前から取得する
        start_date = max_date_val - timedelta(days=5)
        # もし長期間実行されておらず6ヶ月以上空いている場合は全期間を取り直す
        if (today - start_date).days >= 180:
            fetch_kwargs["period"] = FETCH_PERIOD
            print(f"[fetch] 最終更新が古いため、全期間({FETCH_PERIOD})を再取得します")
        else:
            fetch_kwargs["start"] = start_date.strftime("%Y-%m-%d")
            print(f"[fetch] 差分取得: {fetch_kwargs['start']} 以降のデータのみ取得します")
    else:
        fetch_kwargs["period"] = FETCH_PERIOD
        print(f"[fetch] 新規取得: 全期間({FETCH_PERIOD})を取得します")

    # 大引け前に実行された場合、yfinanceは当日の「ザラ場途中の値」を返す。
    # 不完全なバーを保存するとランキング・通知が誤った値で生成されるため、
    # 16時前の実行では当日分を保存対象から除外する(夜の定時実行では当日分が入る)。
    skip_today = datetime.now().hour < 16
    if skip_today:
        print(f"[fetch] 大引け前のため当日({today})分は保存しません(確定値は夜の実行で取得)")

    tickers = [c + ".T" for c in codes]
    total = len(tickers)
    for i in range(0, total, CHUNK_SIZE):
        chunk = tickers[i:i + CHUNK_SIZE]
        print(f"[fetch] {i + 1}-{i + len(chunk)} / {total}")
        try:
            data = yf.download(chunk, **fetch_kwargs)
        except Exception as e:
            print(f"  取得失敗(チャンクスキップ): {e}")
            continue

        recs = []
        for tk in chunk:
            code = tk[:-2]  # ".T" を除去
            try:
                # 単一銘柄だと階層が無い場合があるので両対応
                sub = data[tk] if isinstance(data.columns, pd.MultiIndex) else data
            except KeyError:
                continue
            sub = sub.dropna(subset=["Close", "Open", "Volume"])
            for d, row in sub.iterrows():
                if skip_today and d.date() >= today:
                    continue
                recs.append((code, d.date(), float(row["Open"]),
                             float(row["Close"]), int(row["Volume"])))

        if recs:
            con.executemany(
                "INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?)", recs
            )
        time.sleep(SLEEP_SEC)

    n = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    print(f"[fetch] 保存レコード数: {n}")


# ============================================================
# 3. スクリーニング (検証済みSQL)
# ============================================================
def metric_cfg(cfg=None):
    """指標(出来高/売買代金)に応じた式・閾値・表示ラベルを返す。
    cfg省略時はモジュール定数から組み立てる(既存呼び出しの互換維持)。"""
    if cfg is None:
        cfg = config_from_globals()
    if cfg.metric == "turnover":
        return {
            "expr": "(f.close * f.volume)",
            "mult": cfg.turnover_vol_mult,
            "mult_short": cfg.turnover_vol_mult_short,
            "label": "売買代金",
            "unit": "百万円",
            "div": 1e6,   # 表示用除数(円→百万円)
            "suffix": "_turnover",
        }
    return {
        "expr": "f.volume",
        "mult": cfg.vol_mult,
        "mult_short": cfg.vol_mult_short,
        "label": "出来高",
        "unit": "株",
        "div": 1,
        "suffix": "",
    }


def screen(con, cfg=None):
    if cfg is None:
        cfg = config_from_globals()
    mc = metric_cfg(cfg)
    sql = f"""
    WITH daily AS (
      SELECT code, date, open, close, volume,
             LAG(close) OVER (PARTITION BY code ORDER BY date) AS prev_close,
             ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
      FROM prices
    ),
    flags AS (
      SELECT *, (close > prev_close AND close > open) AS up_day FROM daily
    ),
    slen AS (
      -- 連騰長: 最新日(rn=1)から連続して up_day が続く日数。
      -- up_day=false または NULL(データ端)が最初に現れる rn の1つ手前まで。
      SELECT code,
        COALESCE(MIN(CASE WHEN up_day THEN NULL ELSE rn END) - 1, 0) AS streak_len
      FROM flags
      GROUP BY code
    ),
    metrics AS (
      -- 連騰中(rn<=streak_len)と基準期間(連騰初日の前日以前BASE_WINDOW日)を連騰長に合わせて集計。
      -- 乖離判定の指標は {mc['label']}。流動性フィルタ用に出来高・売買代金の基準中央値も別に持つ。
      SELECT f.code, s.streak_len,
        AVG(CASE WHEN f.rn <= s.streak_len THEN {mc['expr']} END) AS avg_m,
        MIN(CASE WHEN f.rn <= s.streak_len THEN {mc['expr']} END) AS min_m,
        MEDIAN({mc['expr']}) FILTER (
          WHERE f.rn BETWEEN s.streak_len + 1 AND s.streak_len + {cfg.base_window}
        ) AS base_med,
        MEDIAN(f.volume) FILTER (
          WHERE f.rn BETWEEN s.streak_len + 1 AND s.streak_len + {cfg.base_window}
        ) AS base_vol_med,
        MEDIAN(f.close * f.volume) FILTER (
          WHERE f.rn BETWEEN s.streak_len + 1 AND s.streak_len + {cfg.base_window}
        ) AS base_turnover,
        MAX(CASE WHEN f.rn = 1 THEN f.date END)  AS last_date,
        MAX(CASE WHEN f.rn = 1 THEN f.close END) AS last_close,
        MAX(CASE WHEN f.rn = s.streak_len + 1 THEN f.close END) AS pre_close
      FROM flags f
      JOIN slen s USING (code)
      WHERE s.streak_len >= {cfg.streak_min}
      GROUP BY f.code, s.streak_len
    ),
    filtered AS (
      SELECT *,
        round((last_close - pre_close) / pre_close * 100, 2) AS rise_pct,
        round(avg_m / base_med, 2) AS avg_ratio,
        round(min_m / base_med, 2) AS min_ratio,
        CASE WHEN streak_len >= {cfg.streak_top} THEN 1 ELSE 2 END AS tier
      FROM metrics
      WHERE base_med IS NOT NULL AND base_med > 0
        AND pre_close IS NOT NULL AND pre_close > 0
        AND ({cfg.min_volume} <= 0 OR base_vol_med > {cfg.min_volume})
        AND ({cfg.min_turnover} <= 0 OR base_turnover > {cfg.min_turnover})
        AND (
          -- tier1 (STREAK_TOP日以上): 通常閾値
          ( streak_len >= {cfg.streak_top} AND (
              ('{cfg.vol_mode}' = 'avg' AND avg_m >= base_med * {mc['mult']})
              OR ('{cfg.vol_mode}' = 'all' AND min_m >= base_med * {mc['mult']}) ) )
          OR
          -- tier2 (STREAK_MIN以上STREAK_TOP未満): 厳しい閾値
          ( streak_len < {cfg.streak_top} AND (
              ('{cfg.vol_mode}' = 'avg' AND avg_m >= base_med * {mc['mult_short']})
              OR ('{cfg.vol_mode}' = 'all' AND min_m >= base_med * {mc['mult_short']}) ) )
        )
    ),
    ranked AS (
      SELECT *,
        PERCENT_RANK() OVER (ORDER BY min_ratio) AS pr_vol,
        PERCENT_RANK() OVER (ORDER BY rise_pct)  AS pr_rise
      FROM filtered
    )
    SELECT
      ROW_NUMBER() OVER (
        ORDER BY tier ASC,
                 ({cfg.w_vol} * pr_vol + {cfg.w_rise} * pr_rise) DESC,
                 rise_pct DESC, min_ratio DESC
      ) AS rank,
      code, streak_len, tier, last_date, last_close, rise_pct,
      CAST(avg_m AS BIGINT) AS avg_vol3,
      CAST(min_m AS BIGINT) AS min_vol3,
      CAST(base_med AS BIGINT) AS base_med,
      avg_ratio, min_ratio,
      round({cfg.w_vol} * pr_vol + {cfg.w_rise} * pr_rise, 3) AS score
    FROM ranked
    ORDER BY rank
    """
    return con.execute(sql).fetchdf()


# ============================================================
# 4. HTMLレポート生成
# ============================================================
def generate_html(res, data_period, run_time, stale_note=""):
    """スクリーニング結果を美しく見やすいモダンなHTMLレポートとして出力する。
    stale_note: 最新データが当日でない場合などの注意書き(空なら非表示)。"""
    n = len(res)
    base_date = str(res["last_date"].iloc[0]) if n > 0 else "N/A"
    mcfg = metric_cfg()

    def _fmt_m(v):
        """指標値の表示。売買代金は百万円に換算して小数1桁、出来高は整数。"""
        if mcfg["div"] > 1:
            return f"{v / mcfg['div']:,.1f}"
        return f"{int(v):,}"

    def _market_cls(m):
        m = str(m)
        if "プライム" in m: return "market-prime"
        if "スタンダード" in m: return "market-standard"
        if "グロース" in m: return "market-growth"
        return ""

    def _score_cls(s):
        if s >= 0.8: return "score-high"
        if s >= 0.5: return "score-mid"
        return "score-low"

    rows = []
    for i, (_, r) in enumerate(res.iterrows()):
        mc = _market_cls(r.get("market", ""))
        sc = _score_cls(r["score"])
        
        rank = int(r["rank"])
        if rank == 1:
            rank_html = '<div class="rank-badge rank-1">1</div>'
        elif rank == 2:
            rank_html = '<div class="rank-badge rank-2">2</div>'
        elif rank == 3:
            rank_html = '<div class="rank-badge rank-3">3</div>'
        else:
            rank_html = f'<div class="rank-badge rank-other">{rank}</div>'

        slen = int(r["streak_len"])
        streak_cls = "streak-top" if int(r["tier"]) == 1 else "streak-short"
        streak_html = f'<span class="streak {streak_cls}">{slen}日連騰</span>'

        rows.append(
            f'<tr>'
            f'<td>{rank_html}</td>'
            f'<td class="code">{r["code"]}</td>'
            f'<td class="name">{r["name"]}</td>'
            f'<td><span class="market {mc}">{r.get("market", "")}</span></td>'
            f'<td>{streak_html}</td>'
            f'<td class="num">{r["last_close"]:,.0f}</td>'
            f'<td class="num rise"><span class="trend-up">▲</span> {r["rise_pct"]:.2f}%</td>'
            f'<td class="num">{_fmt_m(r["avg_vol3"])}</td>'
            f'<td class="num">{_fmt_m(r["min_vol3"])}</td>'
            f'<td class="num" style="color:var(--text-muted)">{_fmt_m(r["base_med"])}</td>'
            f'<td class="num highlight-col">{r["avg_ratio"]:.2f}x</td>'
            f'<td class="num highlight-col">{r["min_ratio"]:.2f}x</td>'
            f'<td class="num"><div class="score-badge {sc}">{r["score"]:.3f}</div></td>'
            f'</tr>'
        )
    if n == 0:
        rows = ['<tr><td colspan="13" style="text-align:center;padding:80px;color:var(--text-muted)">該当する銘柄はありませんでした</td></tr>']
    rows_html = "\n".join(rows)

    dp_from = data_period.get("from", "N/A")
    dp_to   = data_period.get("to", "N/A")
    dp_codes = data_period.get("codes", 0)

    stale_html = (
        f'<div class="stale-banner">⚠️ {stale_note}</div>' if stale_note else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>連騰・{mcfg['label']}スクリーナー | {base_date}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Noto+Sans+JP:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg-main: #09090b;
  --bg-card: #18181b;
  --text-main: #f4f4f5;
  --text-muted: #a1a1aa;
  --border: rgba(255, 255, 255, 0.08);
  --accent-gradient: linear-gradient(135deg, #60a5fa, #c084fc);
  --positive: #10b981;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: 'Inter', 'Noto Sans JP', sans-serif;
  background: var(--bg-main); color: var(--text-main);
  min-height: 100vh; padding-bottom: 60px;
  background-image: 
    radial-gradient(circle at 15% 0%, rgba(59, 130, 246, 0.12), transparent 25%),
    radial-gradient(circle at 85% 30%, rgba(139, 92, 246, 0.08), transparent 25%);
  background-attachment: fixed;
}}
.container {{ max-width: 100%; margin: 0 auto; padding: 24px 12px; }}

.header-glass {{
  background: rgba(24, 24, 27, 0.6);
  backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border); border-radius: 24px;
  padding: 40px; margin-bottom: 32px;
  box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
}}
h1 {{
  font-size: 32px; font-weight: 700; margin-bottom: 32px; letter-spacing: -0.5px;
}}
h1 span {{
  background: var(--accent-gradient);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}}

.grid-stats {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px;
}}
.stat-card {{
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid var(--border); border-radius: 16px; padding: 20px 24px;
  transition: transform 0.2s, background 0.2s;
}}
.stat-card:hover {{ transform: translateY(-2px); background: rgba(255, 255, 255, 0.04); }}
.stat-label {{ font-size: 13px; color: var(--text-muted); font-weight: 500; margin-bottom: 8px; }}
.stat-value {{ font-size: 22px; font-weight: 600; color: #fff; font-family: 'JetBrains Mono', 'Inter', monospace; }}

.table-container {{
  background: var(--bg-card);
  border: 1px solid var(--border); border-radius: 24px;
  overflow: hidden;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.2);
}}
.table-scroll {{ overflow-x: auto; padding-bottom: 8px; }}
.table-scroll::-webkit-scrollbar {{ height: 10px; }}
.table-scroll::-webkit-scrollbar-track {{ background: rgba(255,255,255,0.02); border-radius: 8px; margin: 0 16px; }}
.table-scroll::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.15); border-radius: 8px; }}
.table-scroll::-webkit-scrollbar-thumb:hover {{ background: rgba(255,255,255,0.25); }}
table {{ width: 100%; border-collapse: collapse; text-align: left; }}
thead th {{
  background: rgba(255, 255, 255, 0.02);
  padding: 14px 12px;
  font-size: 11px; font-weight: 600; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.5px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}}
tbody tr {{
  border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  transition: background 0.2s;
}}
tbody tr:hover {{ background: rgba(255, 255, 255, 0.04); }}
tbody td {{ padding: 10px 12px; font-size: 13px; white-space: nowrap; vertical-align: middle; }}
.num {{ font-family: 'JetBrains Mono', monospace; text-align: right; }}
thead th.num {{ text-align: right; }}

.rank-badge {{
  display: inline-flex; align-items: center; justify-content: center;
  width: 32px; height: 32px; border-radius: 50%;
  font-weight: 700; font-size: 14px; font-family: 'JetBrains Mono', monospace;
}}
.rank-1 {{ background: linear-gradient(135deg, #fef08a, #f59e0b); color: #713f12; box-shadow: 0 0 20px rgba(245,158,11,0.3); }}
.rank-2 {{ background: linear-gradient(135deg, #e2e8f0, #94a3b8); color: #0f172a; }}
.rank-3 {{ background: linear-gradient(135deg, #fed7aa, #b45309); color: #451a03; }}
.rank-other {{ background: rgba(255,255,255,0.05); color: var(--text-muted); }}

.code {{ font-family: 'JetBrains Mono', monospace; color: #c084fc; font-weight: 600; letter-spacing: 0.5px; }}
.name {{ font-weight: 600; color: #f4f4f5; }}
.trend-up {{ color: var(--positive); margin-right: 4px; font-size: 10px; }}
.rise {{ color: var(--positive); font-weight: 600; }}
.highlight-col {{ color: #60a5fa; font-weight: 600; }}

.market {{ font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 6px; letter-spacing: 0.5px; display: inline-block; }}
.market-prime {{ background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.2); }}
.market-standard {{ background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.2); }}
.market-growth {{ background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.2); }}

.streak {{ font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 6px; display: inline-block; white-space: nowrap; }}
.streak-top {{ background: rgba(96, 165, 250, 0.18); color: #93c5fd; border: 1px solid rgba(96, 165, 250, 0.3); }}
.streak-short {{ background: rgba(161, 161, 170, 0.12); color: #d4d4d8; border: 1px solid rgba(161, 161, 170, 0.25); }}

.score-badge {{
  display: inline-block; padding: 6px 14px; border-radius: 20px; font-weight: 600; font-size: 13px;
}}
.score-high {{ background: rgba(16, 185, 129, 0.15); color: #34d399; }}
.score-mid {{ background: rgba(245, 158, 11, 0.15); color: #fbbf24; }}
.score-low {{ background: rgba(255, 255, 255, 0.05); color: var(--text-muted); }}

footer {{ text-align: center; margin-top: 40px; font-size: 13px; color: #52525b; }}

.stale-banner {{
  background: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.3);
  color: #fbbf24; border-radius: 14px; padding: 14px 20px; margin-bottom: 20px;
  font-size: 14px; font-weight: 500;
}}
</style>
</head>
<body>
<div class="container">
  
  {stale_html}
  <div class="header-glass">
    <h1>\U0001f4c8 <span>連騰・{mcfg['label']}スクリーナー</span></h1>
    <div class="grid-stats">
      <div class="stat-card">
        <div class="stat-label">条件(連騰日数 / {mcfg['label']}閾値)</div>
        <div class="stat-value" style="font-size:16px; margin-top:6px;">tier1: {STREAK_TOP}日↑×{mcfg['mult']} / tier2: {STREAK_MIN}日×{mcfg['mult_short']}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">{mcfg['label']}判定モード</div>
        <div class="stat-value" style="color: #c084fc">{VOL_MODE.upper()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">ヒット銘柄数</div>
        <div class="stat-value">{n} <span style="font-size:14px;color:var(--text-muted);font-weight:400">銘柄</span></div>
      </div>
      <div class="stat-card">
        <div class="stat-label">基準日</div>
        <div class="stat-value">{base_date}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">データ取得期間</div>
        <div class="stat-value" style="font-size:16px; margin-top:6px;">{dp_from} ~ {dp_to}</div>
      </div>
    </div>
  </div>

  <div class="table-container">
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Rank</th>
            <th>コード</th>
            <th>銘柄名</th>
            <th>市場区分</th>
            <th>連騰</th>
            <th class="num">終値</th>
            <th class="num">上昇率</th>
            <th class="num">平均{mcfg['label']}(連騰中, {mcfg['unit']})</th>
            <th class="num">最小{mcfg['label']}(連騰中, {mcfg['unit']})</th>
            <th class="num">基準中央値({BASE_WINDOW}日間, {mcfg['unit']})</th>
            <th class="num">平均倍率</th>
            <th class="num">最小倍率</th>
            <th class="num">総合スコア</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </div>
  </div>

  <footer>生成: {run_time} | jp_streak_volume_screener.py</footer>

</div>
</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"出力: {OUTPUT_HTML}")


# ============================================================
# 5. Discord通知
# ============================================================
def send_discord_notification(res, run_time, stale_note="", data_date=None):
    if not DISCORD_WEBHOOK_URL:
        print("[Discord] DISCORD_WEBHOOK_URL 未設定のため通知をスキップします")
        return

    # 多重送信防止: 同じデータ基準日(data_date)は1回だけ送る（1日に複数回走っても重複しない）。
    # 新しいデータ日になれば送る。強制送信は環境変数 DISCORD_FORCE_NOTIFY=1。
    marker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", ".discord_last_sent")
    force = os.environ.get("DISCORD_FORCE_NOTIFY", "") in ("1", "true", "True")
    if data_date is not None and not force:
        try:
            if os.path.exists(marker):
                with open(marker, encoding="utf-8") as fmk:
                    if fmk.read().strip() == str(data_date):
                        print(f"[Discord] {data_date} は既に通知済みのため送信をスキップ（多重送信防止）")
                        return
        except OSError:
            pass

    n = len(res)
    note_line = f"\n⚠️ {stale_note}" if stale_note else ""

    if n == 0:
        payload = {
            "username": f"連騰{metric_cfg()['label']}スクリーナーBot",
            "content": f"**📈 連騰・出来高スクリーニング ({run_time})**{note_line}\n今回は条件に合致する銘柄はありませんでした。"
        }
    else:
        # 埋め込みメッセージ (Embed) を作成して見やすくする
        embed = {
            "title": f"📈 連騰・{metric_cfg()['label']}ランキング (上位20銘柄)",
            "description": f"**抽出日時**: {run_time}{note_line}\n**該当銘柄数**: {n} 件\n※より詳細な全データは、添付の「画像」をタップして拡大してご確認ください。",
            "color": 6345210,  # アクセントカラー (青紫色)
            "fields": []
        }

        medals = ["🥇", "🥈", "🥉"]
        # DiscordのEmbedは最大25フィールドまでなので、上位20件に制限
        for i, r in res.head(20).iterrows():
            rank = int(r['rank'])
            medal = medals[rank - 1] if rank <= 3 else "🔹"
            slen = int(r['streak_len'])
            tier_tag = "🔥" if int(r['tier']) == 1 else "✨"

            field = {
                "name": f"{medal} {rank}位 {r['code']} {r['name']} ({r.get('market', '')})",
                "value": f"{tier_tag} {slen}日連騰\n📈 上昇率: **+{r['rise_pct']:.2f}%**\n📊 {metric_cfg()['label']}: **{r['avg_ratio']:.1f}倍** (平均)",
                "inline": False
            }
            embed["fields"].append(field)

        payload = {
            "username": f"連騰{metric_cfg()['label']}スクリーナーBot",
            "embeds": [embed]
        }

    try:
        import requests
        import json
        # 1. HTMLを画像(PNG)に変換する(Chrome/Chromium本体が必要)
        png_path = None
        try:
            from html2image import Html2Image
            hti = Html2Image()
            # テーブル全体が収まるように十分なサイズを指定
            hti.screenshot(html_file=OUTPUT_HTML, save_as=OUTPUT_PNG, size=(1400, 1800))
            png_path = OUTPUT_PNG
        except Exception as e:
            print(f"[Discord] 画像化に失敗しました。テキストのみで送信します: {e}")

        # 2. Discordに送信するファイルを準備
        files_to_send = {}
        f_png = None
        if png_path and os.path.exists(png_path):
            f_png = open(png_path, "rb")
            files_to_send["file1"] = (png_path, f_png)

        # 3. WebhookへPOST
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            data={"payload_json": json.dumps(payload)},
            files=files_to_send if files_to_send else None
        )

        # 4. ファイルを閉じる
        if f_png:
            f_png.close()

        if resp.status_code in (200, 204):
            print("[Discord] 通知を送信しました")
            if data_date is not None:  # 送信成功時のみ基準日を記録（失敗時は次回再送）
                try:
                    os.makedirs(os.path.dirname(marker), exist_ok=True)
                    with open(marker, "w", encoding="utf-8") as fmk:
                        fmk.write(str(data_date))
                except OSError:
                    pass
        else:
            print(f"[Discord] 通知に失敗しました (ステータス: {resp.status_code})")
    except ImportError:
        print("[Discord] requests が未導入のため通知をスキップ: pip install requests")
    except Exception as e:
        print(f"[Discord] 通知処理中にエラーが発生しました: {e}")

def send_discord_error(text):
    """エラー文をDiscordに通知する(Webhook未設定・送信失敗時は何もしない)。"""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        import requests
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"username": "連騰スクリーナーBot", "content": f"🚨 {text}"},
            timeout=10,
        )
    except Exception as e:
        print(f"[Discord] エラー通知の送信に失敗: {e}")


# ============================================================
# 6. 実行
# ============================================================
def run_once(do_fetch=True):
    """1回だけスクリーニングを実行する。"""
    os.makedirs("data", exist_ok=True)
    names, markets = get_universe()

    con = connect_db_with_retry()
    if do_fetch:
        fetch_to_db(list(names.keys()), con)
    else:
        print("[fetch] スキップ(--no-fetch)。既存DBで抽出します")

    # データ期間の取得
    period = con.execute(
        "SELECT MIN(date), MAX(date), COUNT(DISTINCT code) FROM prices"
    ).fetchone()
    data_period = {"from": period[0], "to": period[1], "codes": period[2]}

    # 鮮度チェック: 最新データ日が当日でなければ注意書きを作る
    # (yfinanceは大引け直後に当日終値が反映されないことがあるため)
    stale_note = ""
    latest = period[1]
    today = datetime.now().date()
    if latest is not None and latest != today:
        stale_note = (
            f"最新データは {latest} 時点です(本日 {today} 分は未反映の可能性)。"
            f"ランキングはこの基準日のデータで算出しています。"
        )
        print(f"[warn] {stale_note}")

    cfg = config_from_globals()
    res = screen(con, cfg)
    res["name"] = res["code"].map(names)
    res["market"] = res["code"].map(markets).fillna("")
    res = res[["rank", "code", "name", "market", "streak_len", "tier",
               "last_date", "last_close", "rise_pct",
               "avg_vol3", "min_vol3", "base_med", "avg_ratio", "min_ratio", "score"]]

    mc = metric_cfg(cfg)
    n_tier1 = int((res["tier"] == 1).sum()) if len(res) else 0
    n_tier2 = int((res["tier"] == 2).sum()) if len(res) else 0
    print(f"\n=== 該当 {len(res)} 銘柄 "
          f"(指標: {mc['label']} / tier1: {STREAK_TOP}日以上×{mc['mult']} = {n_tier1}件 / "
          f"tier2: {STREAK_MIN}日×{mc['mult_short']} = {n_tier2}件 / 判定[{VOL_MODE}]) "
          f"tier→スコア降順 ===")
    pd.set_option("display.max_rows", None)
    print(res.to_string(index=False))
    res.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n出力: {OUTPUT_CSV}")

    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    generate_html(res, data_period, run_time, stale_note)

    send_discord_notification(res, run_time, stale_note, data_date=latest)
    con.close()

    # ▼ 日次処理の末尾フック: theme-flow（テーマ別資金フロー）の当日レポートを生成する。
    #   スクリーナーDBを閉じた後に実行（theme-flow が読み取りATTACHするためロック衝突を避ける）。
    #   データ未着・休日はスキップ。theme-flow 側の失敗はログのみで握りつぶし、本体は止めない。
    trigger_theme_flow(latest, today)


# ============================================================
# 6.5 theme-flow 連携（日次処理の末尾フック）
# ============================================================
def trigger_theme_flow(latest_date, today):
    """連騰スクリーナーの日次処理末尾から theme-flow の当日レポートを生成する。

    手順:
      1. (本関数の呼び出し時点で)スクリーナーDB更新は完了済み
      2. theme-flow の fetch_prices_yf で当日分を取り込み（連騰DB全量＋不足のみyfinance）
      3. daily_report で「全ユニバースが揃った最新営業日」基準の3段構成HTMLを生成

    ガード:
      - データ未着・休日（スクリーナー最新データ日 != 当日）はスキップ。
      - theme-flow は独立venvのサブプロセスとして実行（環境分離）。
      - いかなる失敗もログのみ（例外を投げない）。スクリーナー本体は決して止めない。
      - 生成HTMLのパスをログ出力する。
    """
    try:
        # --- 休日・データ未着ガード ---
        if latest_date is None or latest_date != today:
            print(f"[theme-flow] 当日({today})の四本値が未着/休日のためスキップ（最新={latest_date}）")
            return

        tf_dir = Path(__file__).resolve().parent.parent / "資金流入テーマ検出システム" / "theme-flow"
        if not tf_dir.exists():
            print(f"[theme-flow] ディレクトリ未検出のためスキップ: {tf_dir}")
            return

        # theme-flow 専用venvのPython（無ければ現在のPythonにフォールバック）
        venv_py = tf_dir / ".venv" / "Scripts" / "python.exe"
        py = str(venv_py) if venv_py.exists() else sys.executable

        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"  # 子プロセスの日本語出力をUTF-8で安定させる

        def _run(module, timeout):
            return subprocess.run(
                [py, "-X", "utf8", "-m", module],
                cwd=str(tf_dir), env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
            )

        # (2) 当日分の取り込み
        r1 = _run("src.ingest.fetch_prices_yf", timeout=600)
        if r1.returncode != 0:
            print("[theme-flow] fetch_prices_yf 失敗のため中断（本体は継続）。stderr末尾:\n"
                  + (r1.stderr or "")[-1000:])
            return
        print("[theme-flow] fetch_prices_yf 完了（当日分の取り込みOK）")

        # (3) 3段構成レポート生成（as_of は theme-flow 側で『全ユニバースが揃った最新営業日』に解決）
        r2 = _run("src.report.daily_report", timeout=600)
        if r2.returncode != 0:
            print("[theme-flow] daily_report 失敗（本体は継続）。stderr末尾:\n"
                  + (r2.stderr or "")[-1000:])
            return

        # 生成HTMLのパスをログへ（daily_report は「出力: ....html」を標準出力に出す）。
        # Discord送信の成否行もここで透過させる(捨てると通知失敗に気づけないため)。
        html_path = None
        for line in (r2.stdout or "").splitlines():
            s = line.strip()
            if s.startswith("出力:") and s.endswith(".html"):
                html_path = s.split("出力:", 1)[1].strip()
            elif "Discord" in s:
                print(s)
        if html_path:
            print(f"[theme-flow] レポート生成完了 → {html_path}")
        else:
            print("[theme-flow] レポート生成完了（HTMLパスをログから抽出できませんでした）")

    except subprocess.TimeoutExpired as e:
        print(f"[theme-flow] タイムアウトのためスキップ（本体は継続）: {e}")
    except Exception as e:
        print(f"[theme-flow] 生成中にエラー（ログのみ・本体は継続）: {e}")


def apply_metric_from_argv():
    """--metric=turnover / --turnover で売買代金版に切り替え、出力ファイル名を分ける。"""
    global METRIC, OUTPUT_CSV, OUTPUT_HTML, OUTPUT_PNG
    for a in sys.argv[1:]:
        if a == "--turnover" or a == "--metric=turnover":
            METRIC = "turnover"
        elif a == "--metric=volume":
            METRIC = "volume"
    mc = metric_cfg()
    sfx = mc["suffix"]
    if sfx:
        OUTPUT_CSV  = "screen_result_turnover.csv"
        OUTPUT_HTML = "screen_result_turnover.html"
        OUTPUT_PNG  = "screen_result_turnover.png"
    print(f"[metric] 指標: {mc['label']} (閾値 tier1×{mc['mult']} / tier2×{mc['mult_short']})")


def main():
    apply_metric_from_argv()
    if "--daemon" in sys.argv:
        try:
            import schedule as sch
        except ImportError:
            print("[error] schedule パッケージが必要です: pip install schedule")
            sys.exit(1)
        print("[daemon] 平日 20:00 に自動実行します(日本の祝日はスキップ)。Ctrl+C で終了。")
        def _job():
            # 日本の祝日(市場休場日)はスキップ。jpholiday未導入なら祝日も実行する。
            try:
                import jpholiday
                if jpholiday.is_holiday(datetime.now().date()):
                    print("[daemon] 本日は祝日(休場)のためスキップします。")
                    return
            except ImportError:
                pass
            try:
                run_once()
            except Exception as e:
                print(f"[daemon] 実行中にエラー: {e}")
        for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
            getattr(sch.every(), day).at("20:00").do(_job)
        try:
            while True:
                sch.run_pending()
                time.sleep(60)
        except KeyboardInterrupt:
            print("\n[daemon] 終了します。")
    else:
        run_once(do_fetch="--no-fetch" not in sys.argv)


if __name__ == "__main__":
    main()
