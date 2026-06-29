"""
連騰スクリーナー Streamlitローカルアプリ(計画書 screener_app_plan.md 準拠)。

起動: run_app.bat をダブルクリック(どこからでも可)、
      またはこのフォルダ(連騰)で  streamlit run app.py

設計メモ:
- 株価取得は「データ取得」ボタンを押したときだけ実行する。起動時・つまみ変更では走らない。
- DuckDB接続は保持せず、操作のたびに開いて閉じる。接続を持ちっぱなしにすると
  ファイルロックで夜間の自動取得(タスクスケジューラ/--daemon)が書き込めなくなるため。
  スクリーニングは read_only 接続、取得時のみ read-write 接続を短時間だけ開く。
- screen() の結果は (設定, DB最新日) をキーに st.cache_data でキャッシュする。
  取得後はDB最新日が変わるのでキャッシュキーも自然に変わり、さらに明示的にクリアもする。
- 見た目はCLIのHTMLレポート(screen_result.html)とデザイン言語を揃えている
  (ダーク基調・ガラスカード・ブルー→パープルのアクセント・Noto Sans JP/JetBrains Mono)。
"""

import html as _html
import os
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import jp_streak_volume_screener as core
from screener_core import (
    FRESH_CUTOFF_HOUR,
    HAS_JPHOLIDAY,
    db_latest_date,
    is_data_fresh,
    latest_trading_day,
)

st.set_page_config(page_title="連騰スクリーナー", page_icon="📈", layout="wide")

# ============================================================
# デモ用フォールバック
# 実データDB(data/prices.duckdb)が無い環境(Streamlit Cloud等の公開デモ)では、
# リポジトリ同梱のサンプルデータ(sample_data/)で動作させる。
# ローカル(実DBあり)では作動しないため、通常運用・夜間自動取得には一切影響しない。
# ============================================================
_APP_DIR = Path(__file__).resolve().parent
_SAMPLE_DB = _APP_DIR / "sample_data" / "prices.duckdb"
_SAMPLE_TICKERS = _APP_DIR / "sample_data" / "tickers.csv"
DEMO_MODE = (not os.path.exists(core.DB_PATH)) and _SAMPLE_DB.exists()
if DEMO_MODE:
    core.DB_PATH = str(_SAMPLE_DB)          # サンプル株価DBを参照
    core.UNIVERSE_CSV = str(_SAMPLE_TICKERS)  # JPX取得の代わりに同梱の銘柄名CSVを使う

# ============================================================
# テーマCSS (CLIレポートと同じデザイン言語)
# ============================================================
THEME_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {
  --bg-main: #09090b; --bg-card: #18181b;
  --text-main: #f4f4f5; --text-muted: #a1a1aa;
  --line: rgba(255,255,255,0.08);
  --accent-a: #60a5fa; --accent-b: #c084fc;
  --positive: #10b981;
  --font: 'Noto Sans JP', sans-serif;
}
.stApp {
  background-color: var(--bg-main);
  background-image:
    radial-gradient(circle at 15% 0%, rgba(59,130,246,.13), transparent 30%),
    radial-gradient(circle at 85% 15%, rgba(139,92,246,.10), transparent 30%);
  background-attachment: fixed;
}
.stApp, .stApp p, .stApp label, .stApp h1, .stApp h2, .stApp h3,
[data-testid="stSidebar"] * , [data-testid="stMarkdownContainer"] {
  font-family: 'Noto Sans JP', sans-serif;
}
[data-testid="stSidebar"] {
  background: rgba(24,24,27,.94);
  border-right: 1px solid var(--line);
}
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
  font-size: 15px; letter-spacing: .5px;
}
button[data-testid="stBaseButton-primary"] {
  background: linear-gradient(135deg, #3b82f6, #8b5cf6);
  border: none; font-weight: 700; letter-spacing: 1px;
  box-shadow: 0 4px 18px rgba(96,165,250,.25);
}
button[data-testid="stBaseButton-primary"]:hover { filter: brightness(1.15); }

/* ---- ヒーローヘッダー ---- */
.hero {
  background: rgba(24,24,27,.6);
  border: 1px solid var(--line); border-radius: 20px;
  padding: 22px 28px; margin-bottom: 14px;
  backdrop-filter: blur(14px);
}
.hero h1 { font-size: 26px; font-weight: 700; margin: 0 0 6px 0; letter-spacing: -0.5px; }
.hero h1 .grad {
  background: linear-gradient(135deg, var(--accent-a), var(--accent-b));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.hero .sub { color: var(--text-muted); font-size: 13px; font-family: 'JetBrains Mono', monospace; }
.chip {
  display: inline-block; padding: 3px 12px; border-radius: 999px;
  font-size: 12.5px; font-weight: 700; margin-left: 10px; vertical-align: 2px;
}
.chip-fresh { background: rgba(16,185,129,.15); color: #34d399; border: 1px solid rgba(16,185,129,.3); }
.chip-stale { background: rgba(245,158,11,.15); color: #fbbf24; border: 1px solid rgba(245,158,11,.35); }

/* ---- 統計カード ---- */
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 4px 0 14px 0; }
.stat-card {
  background: rgba(255,255,255,.025); border: 1px solid var(--line);
  border-radius: 14px; padding: 14px 18px;
}
.stat-card .lbl { font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
.stat-card .val { font-size: 21px; font-weight: 600; font-family: 'JetBrains Mono', monospace; }
.stat-card .val small { font-size: 12px; color: var(--text-muted); font-weight: 400; }

/* ---- デモ表示 ---- */
.demo-pill {
  display: inline-block; padding: 7px 14px; border-radius: 999px;
  font-size: 13px; font-weight: 700; letter-spacing: .5px; text-align: center;
  background: linear-gradient(135deg, rgba(192,132,252,.2), rgba(96,165,250,.2));
  border: 1px solid rgba(192,132,252,.4); color: #d8b4fe; width: 100%;
}
.demo-banner {
  background: rgba(192,132,252,.08); border: 1px solid rgba(192,132,252,.3);
  color: #d8b4fe; border-radius: 12px; padding: 11px 16px; margin: 2px 0 12px 0;
  font-size: 13.5px; line-height: 1.6;
}
.demo-banner b { color: #e9d5ff; }

/* ---- 注意バナー ---- */
.stale-banner {
  background: rgba(245,158,11,.10); border: 1px solid rgba(245,158,11,.3);
  color: #fbbf24; border-radius: 12px; padding: 10px 16px; margin-bottom: 12px;
  font-size: 13.5px;
}

/* ---- ランキング表 ---- */
.tbl-wrap {
  background: var(--bg-card); border: 1px solid var(--line); border-radius: 16px;
  overflow: auto; max-height: 660px;
  box-shadow: 0 10px 40px rgba(0,0,0,.25);
}
table.rk { width: 100%; border-collapse: collapse; }
table.rk thead th {
  position: sticky; top: 0; z-index: 2;
  background: #1f1f23; padding: 11px 12px;
  font-size: 11px; font-weight: 600; color: var(--text-muted);
  letter-spacing: .6px; text-align: left; white-space: nowrap;
  border-bottom: 1px solid var(--line);
}
table.rk thead th.num { text-align: right; }
table.rk tbody td { padding: 9px 12px; font-size: 13.5px; white-space: nowrap; border-bottom: 1px solid rgba(255,255,255,.035); }
table.rk tbody tr:hover { background: rgba(96,165,250,.06); }
table.rk .num { font-family: 'JetBrains Mono', monospace; text-align: right; font-size: 13px; }

.rkb {
  display: inline-flex; align-items: center; justify-content: center;
  width: 28px; height: 28px; border-radius: 50%;
  font-weight: 700; font-size: 13px; font-family: 'JetBrains Mono', monospace;
}
.rkb-1 { background: linear-gradient(135deg,#fef08a,#f59e0b); color: #713f12; }
.rkb-2 { background: linear-gradient(135deg,#e2e8f0,#94a3b8); color: #0f172a; }
.rkb-3 { background: linear-gradient(135deg,#fed7aa,#b45309); color: #451a03; }
.rkb-x { background: rgba(255,255,255,.05); color: var(--text-muted); }

.code { font-family: 'JetBrains Mono', monospace; color: var(--accent-b); font-weight: 600; }
.name { font-weight: 600; }
.rise { color: var(--positive); font-weight: 600; }

.mk { font-size: 11.5px; font-weight: 600; padding: 3px 9px; border-radius: 6px; display: inline-block; }
.mk-p { background: rgba(59,130,246,.15); color: #60a5fa; border: 1px solid rgba(59,130,246,.25); }
.mk-s { background: rgba(16,185,129,.15); color: #34d399; border: 1px solid rgba(16,185,129,.25); }
.mk-g { background: rgba(245,158,11,.15); color: #fbbf24; border: 1px solid rgba(245,158,11,.25); }

.stk { font-size: 11.5px; font-weight: 700; padding: 3px 9px; border-radius: 6px; display: inline-block; }
.stk-1 { background: rgba(96,165,250,.18); color: #93c5fd; border: 1px solid rgba(96,165,250,.3); }
.stk-2 { background: rgba(161,161,170,.12); color: #d4d4d8; border: 1px solid rgba(161,161,170,.25); }

.scorebar { display: inline-flex; align-items: center; gap: 8px; min-width: 130px; justify-content: flex-end; }
.scorebar .bar { width: 70px; height: 6px; border-radius: 4px; background: rgba(255,255,255,.07); overflow: hidden; }
.scorebar .fill { height: 100%; border-radius: 4px; background: linear-gradient(90deg, var(--accent-a), var(--accent-b)); }
.scorebar .v { font-family: 'JetBrains Mono', monospace; font-size: 12.5px; }

/* ---- テーマ資金フロー ---- */
.ts { font-size: 11.5px; font-weight: 700; padding: 3px 9px; border-radius: 6px; display: inline-block; }
.ts-ign   { background: rgba(192,132,252,.18); color: #d8b4fe; border: 1px solid rgba(192,132,252,.35); }
.ts-watch { background: rgba(59,130,246,.15);  color: #60a5fa; border: 1px solid rgba(59,130,246,.25); }
.ts-stall { background: rgba(245,158,11,.15);  color: #fbbf24; border: 1px solid rgba(245,158,11,.3); }
.ts-out   { background: rgba(255,255,255,.05); color: var(--text-muted); border: 1px solid var(--line); }
.sg-s1   { background: rgba(192,132,252,.18); color: #d8b4fe; border: 1px solid rgba(192,132,252,.35); }
.sg-s2   { background: rgba(16,185,129,.15);  color: #34d399; border: 1px solid rgba(16,185,129,.25); }
.sg-none { background: rgba(255,255,255,.04); color: var(--text-muted); border: 1px solid var(--line); }
.theme-name { font-weight: 600; }
.warn-mini { color: #fbbf24; font-size: 12px; }
.contrib {
  font-size: 12px; color: var(--text-muted); font-family: 'JetBrains Mono', monospace;
  max-width: 360px; overflow: hidden; text-overflow: ellipsis; display: inline-block; vertical-align: middle;
}
.cf-neg .fill { background: rgba(161,161,170,.5) !important; }
</style>
"""
st.markdown(THEME_CSS, unsafe_allow_html=True)


# ============================================================
# データアクセス(接続は短命に保つ)
# ============================================================
def db_exists():
    return os.path.exists(core.DB_PATH)


def read_db_status():
    """(最新日, データ開始日, 銘柄数)。DBが無ければ (None, None, 0)。"""
    if not db_exists():
        return None, None, 0
    con = duckdb.connect(core.DB_PATH, read_only=True)
    try:
        row = con.execute(
            "SELECT MAX(date), MIN(date), COUNT(DISTINCT code) FROM prices"
        ).fetchone()
        return row[0], row[1], row[2]
    except duckdb.Error:
        return None, None, 0
    finally:
        con.close()


@st.cache_data(ttl=24 * 3600, show_spinner="銘柄リストを読み込み中...")
def load_universe():
    """銘柄ユニバース(code->名前, code->市場)。JPXのExcel取得が走るため24hキャッシュ。
    株価データの取得ではないので、起動時に読んでも計画書の制約には抵触しない。"""
    names, markets = core.get_universe()
    return names, markets


@st.cache_data(show_spinner=False)
def run_screen(cfg: core.ScreenConfig, db_stamp: str) -> pd.DataFrame:
    """スクリーニング実行。db_stamp(DB最新日)をキーに含め、取得後に自動で再計算させる。"""
    con = duckdb.connect(core.DB_PATH, read_only=True)
    try:
        return core.screen(con, cfg)
    finally:
        con.close()


def do_fetch(codes):
    """read-write接続を取得の間だけ開いて株価を更新する。"""
    os.makedirs(os.path.dirname(core.DB_PATH), exist_ok=True)
    con = duckdb.connect(core.DB_PATH)
    try:
        core.fetch_to_db(codes, con)
    finally:
        con.close()


# ============================================================
# サイドバー(調整つまみ)— 変更しても取得は走らず screen() だけ再実行される
# ============================================================
def build_config_from_sidebar() -> core.ScreenConfig:
    sb = st.sidebar
    sb.header("⚙️ スクリーニング条件")

    metric_label = sb.segmented_control(
        "指標", ["出来高", "売買代金"], default="出来高",
        help="売買代金 = 終値×出来高。株価上昇自体が代金を押し上げるため出来高より通りやすい",
    ) or "出来高"
    metric = "volume" if metric_label == "出来高" else "turnover"

    vol_mode = sb.segmented_control(
        "判定モード", ["all", "avg"], default=core.VOL_MODE,
        help="all=連騰各日すべてで判定(厳しめ) / avg=平均で判定(緩め)",
    ) or core.VOL_MODE

    streak_min = sb.slider(
        "最小連騰日数 (STREAK_MIN)", 1, 10, core.STREAK_MIN,
        help="これ未満の連騰は除外")
    streak_top = sb.slider(
        "上位tierの連騰日数 (STREAK_TOP)", streak_min, 10,
        max(core.STREAK_TOP, streak_min),
        help="この日数以上を tier1(上位固定)に置く")

    if metric == "volume":
        sb.subheader("出来高 閾値")
        vol_mult = sb.slider("tier1 倍率", 1.0, 5.0, core.VOL_MULT, 0.1)
        vol_mult_short = sb.slider("tier2 倍率(短連騰の足切り)", 1.0, 10.0, core.VOL_MULT_SHORT, 0.1)
        turnover_mult = core.TURNOVER_VOL_MULT
        turnover_mult_short = core.TURNOVER_VOL_MULT_SHORT
    else:
        sb.subheader("売買代金 閾値")
        turnover_mult = sb.slider("tier1 倍率", 1.0, 5.0, core.TURNOVER_VOL_MULT, 0.1)
        turnover_mult_short = sb.slider("tier2 倍率(短連騰の足切り)", 1.0, 10.0,
                                        core.TURNOVER_VOL_MULT_SHORT, 0.1)
        vol_mult = core.VOL_MULT
        vol_mult_short = core.VOL_MULT_SHORT

    base_window = sb.slider(
        "基準中央値の参照期間 (BASE_WINDOW)", 5, 30, core.BASE_WINDOW,
        help="連騰初日の前日以前この日数の中央値を基準にする")

    sb.subheader("流動性フィルタ (0で無効)")
    min_volume = sb.number_input(
        "平常時出来高の下限 (株)", min_value=0, value=int(core.MIN_VOLUME), step=10000,
        help="連騰前の出来高中央値がこの値以下の銘柄を除外")
    min_turnover_oku = sb.number_input(
        "平常時売買代金の下限 (億円)", min_value=0.0,
        value=float(core.MIN_TURNOVER) / 1e8, step=0.5,
        help="連騰前の売買代金中央値がこの値以下の銘柄を除外。1億円程度を推奨")

    sb.subheader("ランキング重み")
    # 合計1の制約を保つため、出来高側の重みだけを操作し W_RISE は自動算出する
    w_vol = sb.slider("出来高の勢い W_VOL", 0.0, 1.0, core.W_VOL, 0.05)
    w_rise = round(1.0 - w_vol, 2)
    sb.caption(f"価格の勢い W_RISE = **{w_rise}**(合計1になるよう自動設定)")

    return core.ScreenConfig(
        metric=metric, streak_min=streak_min, streak_top=streak_top,
        base_window=base_window, vol_mult=vol_mult, vol_mult_short=vol_mult_short,
        turnover_vol_mult=turnover_mult, turnover_vol_mult_short=turnover_mult_short,
        vol_mode=vol_mode, min_volume=min_volume, min_turnover=min_turnover_oku * 1e8,
        w_vol=w_vol, w_rise=w_rise,
    )


# ============================================================
# ヘッダー: データ状態 + 取得ボタン(計画書5-2, 5-3)
# ============================================================
def render_header():
    latest, oldest, n_codes = read_db_status()
    now = datetime.now()
    target = latest_trading_day(now)

    col_status, col_btn = st.columns([5, 1], vertical_alignment="center")
    with col_status:
        if latest is None:
            chip = '<span class="chip chip-stale">DBなし</span>'
            sub = "「データ取得」ボタンで株価データベースを作成してください。"
        else:
            fresh = latest >= target
            chip = ('<span class="chip chip-fresh">最新</span>' if fresh
                    else '<span class="chip chip-stale">要更新</span>')
            sub = (f"最終データ日 {latest} ｜ 直近営業日 {target}"
                   f"(反映待ち {FRESH_CUTOFF_HOUR}:00 基準) ｜ "
                   f"収録 {oldest} ~ {latest} ｜ {n_codes} 銘柄")
        st.markdown(
            f'<div class="hero"><h1><span class="grad">連騰スクリーナー</span>{chip}</h1>'
            f'<div class="sub">{sub}</div></div>',
            unsafe_allow_html=True,
        )
        if not HAS_JPHOLIDAY:
            st.caption("jpholiday 未導入のため祝日判定は曜日のみで行っています。")

    with col_btn:
        if DEMO_MODE:
            st.markdown('<div class="demo-pill">🧪 DEMO</div>', unsafe_allow_html=True)
            fetch_clicked = False
        else:
            fetch_clicked = st.button("🔄 データ取得", type="primary", width="stretch")

    if DEMO_MODE:
        st.markdown(
            '<div class="demo-banner">🧪 これは公開デモです。GitHub同梱の'
            '<b>サンプル株価データ(主要444銘柄)</b>で動作しています。'
            '実運用版は東証全約3,800銘柄を毎晩自動取得し、Discordへ通知します。'
            'サイドバーのつまみを動かすと結果がリアルタイムに更新されます。</div>',
            unsafe_allow_html=True,
        )

    # 取得はこのボタンを押したときだけ走る(起動時・つまみ変更では走らない)
    if fetch_clicked:
        if db_exists():
            con = duckdb.connect(core.DB_PATH, read_only=True)
            try:
                fresh, db_date, target = is_data_fresh(con, now)
            finally:
                con.close()
        else:
            fresh, db_date = False, None

        if fresh:
            st.info(f"既に最新のデータです(最終取得日: {db_date})")
        else:
            names, _ = load_universe()
            try:
                with st.spinner(f"{len(names)} 銘柄の株価を取得中... (数分かかります)"):
                    do_fetch(list(names.keys()))
            except duckdb.Error as e:
                if core._is_lock_error(e):
                    st.error("データベースが他のプロセス(夜間の自動取得など)に使用中のため"
                             "取得できませんでした。しばらく待ってから再度お試しください。")
                    return
                raise
            new_latest = None
            if db_exists():
                con = duckdb.connect(core.DB_PATH, read_only=True)
                try:
                    new_latest = db_latest_date(con)
                finally:
                    con.close()
            run_screen.clear()  # 取得後はスクリーニング結果のキャッシュを無効化
            st.success(f"{len(names)} 銘柄を更新しました(最終取得日: {new_latest})")
            st.rerun()  # 状態表示と結果表を新データで描き直す


# ============================================================
# 結果表示(計画書5-2)
# ============================================================
_MARKET_CLS = {"プライム": "mk-p", "スタンダード": "mk-s", "グロース": "mk-g"}

_SORTS = {
    "総合順位": ("rank", True),
    "上昇率が高い順": ("rise_pct", False),
    "最小倍率が高い順": ("min_ratio", False),
    "平均倍率が高い順": ("avg_ratio", False),
    "連騰日数が長い順": ("streak_len", False),
}


def _fmt_m(v, mc):
    return f"{v / 1e6:,.1f}百万円" if mc["div"] > 1 else f"{int(v):,}株"


def _table_html(res, mc):
    rows = []
    for _, r in res.iterrows():
        rank = int(r["rank"])
        rkb_cls = f"rkb-{rank}" if rank <= 3 else "rkb-x"
        mk_cls = next((c for k, c in _MARKET_CLS.items() if k in str(r["market"])), "mk-g")
        stk_cls = "stk-1" if int(r["tier"]) == 1 else "stk-2"
        name = _html.escape(str(r["name"]))
        market = _html.escape(str(r["market"]))
        tip = (f"連騰中の平均{mc['label']} {_fmt_m(r['avg_vol3'], mc)} / "
               f"最小 {_fmt_m(r['min_vol3'], mc)} / 基準中央値 {_fmt_m(r['base_med'], mc)}")
        score = float(r["score"])
        rows.append(
            f'<tr title="{tip}">'
            f'<td><span class="rkb {rkb_cls}">{rank}</span></td>'
            f'<td class="code">{r["code"]}</td>'
            f'<td class="name">{name}</td>'
            f'<td><span class="mk {mk_cls}">{market}</span></td>'
            f'<td><span class="stk {stk_cls}">{int(r["streak_len"])}日連騰</span></td>'
            f'<td class="num">{r["last_close"]:,.0f}</td>'
            f'<td class="num rise">▲ {r["rise_pct"]:.2f}%</td>'
            f'<td class="num">{r["avg_ratio"]:.2f}x</td>'
            f'<td class="num">{r["min_ratio"]:.2f}x</td>'
            f'<td class="num"><span class="scorebar">'
            f'<span class="bar"><span class="fill" style="width:{score * 100:.0f}%;display:block"></span></span>'
            f'<span class="v">{score:.3f}</span></span></td>'
            f'</tr>'
        )
    head = (
        '<tr><th>RANK</th><th>コード</th><th>銘柄名</th><th>市場</th><th>連騰</th>'
        '<th class="num">終値</th><th class="num">上昇率</th>'
        '<th class="num">平均倍率</th><th class="num">最小倍率</th><th class="num">総合スコア</th></tr>'
    )
    return f'<div class="tbl-wrap"><table class="rk"><thead>{head}</thead><tbody>{"".join(rows)}</tbody></table></div>'


def render_results(cfg: core.ScreenConfig):
    latest, _, _ = read_db_status()
    if latest is None:
        return

    names, markets = load_universe()
    res = run_screen(cfg, str(latest))
    res["name"] = res["code"].map(names).fillna(res["code"])
    res["market"] = res["code"].map(markets).fillna("")

    mc = core.metric_cfg(cfg)
    n = len(res)
    n_tier1 = int((res["tier"] == 1).sum()) if n else 0
    n_tier2 = int((res["tier"] == 2).sum()) if n else 0

    st.markdown(
        '<div class="stat-row">'
        f'<div class="stat-card"><div class="lbl">ヒット銘柄数</div><div class="val">{n} <small>銘柄</small></div></div>'
        f'<div class="stat-card"><div class="lbl">tier1 ｜ {cfg.streak_top}日以上 × {mc["mult"]}</div><div class="val">{n_tier1} <small>件</small></div></div>'
        f'<div class="stat-card"><div class="lbl">tier2 ｜ {cfg.streak_min}日~ × {mc["mult_short"]}</div><div class="val">{n_tier2} <small>件</small></div></div>'
        f'<div class="stat-card"><div class="lbl">指標 ｜ 判定モード</div><div class="val" style="font-size:17px">{mc["label"]} ｜ {cfg.vol_mode}</div></div>'
        '</div>',
        unsafe_allow_html=True,
    )

    # 鮮度の注意書き(CLIのstale_note相当)
    today = datetime.now().date()
    if latest != today:
        st.markdown(
            f'<div class="stale-banner">⚠️ 最新データは {latest} 時点です'
            f'(本日 {today} 分は未反映の可能性)。ランキングはこの基準日のデータで算出しています。</div>',
            unsafe_allow_html=True,
        )

    if n == 0:
        st.info("条件に合致する銘柄はありませんでした。つまみを緩めて再検索してください。")
        return

    col_q, col_sort, col_dl = st.columns([3, 2, 2], vertical_alignment="bottom")
    with col_q:
        query = st.text_input("絞り込み", placeholder="コード・銘柄名で絞り込み...",
                              label_visibility="collapsed")
    with col_sort:
        sort_key = st.selectbox("表示順", list(_SORTS.keys()), label_visibility="collapsed")
    with col_dl:
        full = res[["rank", "code", "name", "market", "streak_len", "tier",
                    "last_date", "last_close", "rise_pct",
                    "avg_vol3", "min_vol3", "base_med", "avg_ratio", "min_ratio", "score"]]
        st.download_button(
            "📥 CSVダウンロード",
            data=full.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"screen_result_{cfg.metric}_{latest}.csv",
            mime="text/csv",
            width="stretch",
        )

    view = res
    if query:
        q = query.strip()
        view = res[res["code"].str.contains(q, case=False, na=False)
                   | res["name"].str.contains(q, case=False, na=False)]
    col, asc = _SORTS[sort_key]
    view = view.sort_values(col, ascending=asc)

    if len(view) == 0:
        st.info(f"「{query}」に一致する銘柄はありません。")
        return
    if query:
        st.caption(f"絞り込み: {len(view)} / {n} 銘柄")

    st.markdown(_table_html(view, mc), unsafe_allow_html=True)
    st.caption("行にマウスを乗せると平常時の出来高/売買代金(基準中央値)が見られます。"
               "倍率 = 連騰中の値 ÷ 連騰前の基準中央値。")


# ============================================================
# テーマ資金フロー ページ
# (theme-flow の夜間レポートCSV/flow.duckdb を読み取り専用で参照する。
#  集計の再実行はしない=theme-flow venv には依存しない。接続・ファイルは短命に開閉)
# ============================================================
THEME_FLOW_DATA = (Path(__file__).resolve().parent.parent
                   / "資金流入テーマ検出システム" / "theme-flow" / "data")


def list_theme_reports():
    """利用可能な日次レポートを [(日付文字列, Path), ...] 新しい順で返す。"""
    if not THEME_FLOW_DATA.exists():
        return []
    files = sorted(THEME_FLOW_DATA.glob("daily_report_*.csv"), reverse=True)
    return [(f.stem.replace("daily_report_", ""), f) for f in files]


@st.cache_data(show_spinner=False)
def load_theme_report(path_str: str, mtime: float) -> pd.DataFrame:
    """日次レポートCSVを読む。mtimeをキーに含め、夜間ジョブの上書き後に自動で再読込させる。"""
    return pd.read_csv(path_str, encoding="utf-8-sig")


@st.cache_data(show_spinner=False)
def load_theme_history(stamp: tuple) -> pd.DataFrame:
    """全daily_reportから確信度の推移を組み立てる。
    (flow.duckdb の daily_metrics は手動検証時のみ追記され日次の履歴にならないため、
     夜間ジョブと同一パラメータで出力されるレポートCSV群を履歴ソースにする)"""
    frames = []
    for d, f in list_theme_reports():
        df = pd.read_csv(f, encoding="utf-8-sig", usecols=["theme", "confidence"])
        df["date"] = d
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["date", "theme", "confidence"])
    return pd.concat(frames).sort_values("date")


def _status_chip(r):
    status = str(r["status"]) if pd.notna(r["status"]) else ""
    ign = str(r["ignition"]) if pd.notna(r["ignition"]) else ""
    if ign:  # 初動/再点火はその日のイベントとして最優先表示
        return f'<span class="ts ts-ign">⚡ {_html.escape(ign)}</span>'
    if status == "初動":
        return '<span class="ts ts-ign">⚡ 初動</span>'
    if status == "監視中":
        return '<span class="ts ts-watch">監視中</span>'
    if status == "失速":
        return '<span class="ts ts-stall">失速</span>'
    return '<span class="ts ts-out">圏外</span>'


def _stage_chip(stage):
    s = str(stage) if pd.notna(stage) else "—"
    if "S1" in s:
        return f'<span class="ts sg-s1">{_html.escape(s)}</span>'
    if "S2" in s:
        return f'<span class="ts sg-s2">{_html.escape(s)}</span>'
    return '<span class="ts sg-none">—</span>'


def _theme_table_html(df: pd.DataFrame) -> str:
    cmin, cmax = float(df["confidence"].min()), float(df["confidence"].max())
    span = (cmax - cmin) or 1.0
    rows = []
    for _, r in df.iterrows():
        rank = int(r["rank"])
        rkb_cls = f"rkb-{rank}" if rank <= 3 else "rkb-x"
        conf = float(r["confidence"])
        width = (conf - cmin) / span * 100
        neg_cls = "" if conf >= 0 else " cf-neg"
        name = _html.escape(str(r["theme"]))
        warn = ' <span class="warn-mini" title="構成銘柄が少なくz値が暴れやすいテーマ">⚠小母数</span>' \
            if bool(r.get("small_universe")) else ""
        floor = "✓" if bool(r.get("floor_pass")) else "—"
        contrib_full = _html.escape(str(r.get("top_contributors", "") or ""))
        tip = _html.escape(
            f"参加率 {float(r['participation_rate']) * 100:.1f}% / 集中度 {float(r['concentration']):.2f} / "
            f"初動登録 {r.get('registered_since', '—')} / 直近点火 {r.get('last_ignition', '—')}"
        )
        rows.append(
            f'<tr title="{tip}">'
            f'<td><span class="rkb {rkb_cls}">{rank}</span></td>'
            f'<td class="theme-name">{name}{warn}</td>'
            f'<td>{_status_chip(r)}</td>'
            f'<td>{_stage_chip(r["stage"])}</td>'
            f'<td class="num"><span class="scorebar{neg_cls}">'
            f'<span class="bar"><span class="fill" style="width:{width:.0f}%;display:block"></span></span>'
            f'<span class="v">{conf:+.2f}</span></span></td>'
            f'<td class="num">{int(r["participants"])}/{int(r["eligible_members"])}社</td>'
            f'<td class="num">{float(r["turnover_anomaly"]):+.2f}</td>'
            f'<td class="num">{float(r["mkt_rel_z"]):+.2f}</td>'
            f'<td class="num">{float(r["breadth_z"]):+.2f}</td>'
            f'<td style="text-align:center">{floor}</td>'
            f'<td><span class="contrib" title="{contrib_full}">{contrib_full}</span></td>'
            f'</tr>'
        )
    head = (
        '<tr><th>RANK</th><th>テーマ</th><th>状態</th><th>ステージ</th>'
        '<th class="num">確信度</th><th class="num">参加</th>'
        '<th class="num">代金z</th><th class="num">市場相対z</th><th class="num">広がりz</th>'
        '<th>床</th><th>主な寄与銘柄(売買代金)</th></tr>'
    )
    return (f'<div class="tbl-wrap"><table class="rk"><thead>{head}</thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def render_theme_page():
    sb = st.sidebar
    sb.header("🌊 テーマ資金フロー")

    reports = list_theme_reports()
    if not reports:
        st.markdown(
            '<div class="hero"><h1><span class="grad">テーマ資金フロー</span></h1>'
            '<div class="sub">日次レポートが見つかりません</div></div>',
            unsafe_allow_html=True)
        st.info(f"theme-flow のレポート(daily_report_*.csv)が見つかりません: {THEME_FLOW_DATA}\n\n"
                "夜間ジョブ(20:00)実行後に生成されます。")
        return

    dates = [d for d, _ in reports]
    sel = sb.selectbox("基準日(レポート)", dates, index=0,
                       help="theme-flow の夜間レポートを日付で切り替え。過去日のリプレイ閲覧も可能")
    status_filter = sb.segmented_control(
        "状態で絞り込み", ["すべて", "監視中", "失速"], default="すべて") or "すべて"
    floor_only = sb.checkbox("床通過テーマのみ", value=False,
                             help="参加3社以上などの最低条件(床)を満たしたテーマだけを表示")
    sb.caption("このページは閲覧専用です。集計は夜間ジョブ(連騰スクリーナー20:00 → theme-flow)が自動更新します。")

    path = dict(reports)[sel]
    df = load_theme_report(str(path), os.path.getmtime(path))

    # --- ヘッダー ---
    is_latest = (sel == dates[0])
    chip = ('<span class="chip chip-fresh">最新レポート</span>' if is_latest
            else '<span class="chip chip-stale">過去日</span>')
    st.markdown(
        f'<div class="hero"><h1><span class="grad">テーマ資金フロー</span>{chip}</h1>'
        f'<div class="sub">基準日 {sel} ｜ {len(df)} テーマ ｜ '
        f'資金流入の「広がり」をテーマ単位で監視(theme-flow 日次レポート)</div></div>',
        unsafe_allow_html=True)

    if is_latest:
        target = latest_trading_day(datetime.now())
        if str(target) > sel:
            st.markdown(
                f'<div class="stale-banner">⚠️ 最新レポートは {sel} 時点です(直近営業日 {target} 分は未生成)。'
                f'夜間ジョブ実行後に更新されます。</div>', unsafe_allow_html=True)

    # --- 統計カード ---
    ign = df["ignition"].notna() | (df["status"] == "初動")
    n_ign = int(ign.sum())
    n_s2 = int(df["stage"].astype(str).str.contains("S2", na=False).sum())
    n_stall = int((df["status"] == "失速").sum())
    n_floor = int(df["floor_pass"].fillna(False).sum())
    st.markdown(
        '<div class="stat-row">'
        f'<div class="stat-card"><div class="lbl">⚡ 本日の初動/再点火</div><div class="val">{n_ign} <small>件</small></div></div>'
        f'<div class="stat-card"><div class="lbl">継続点火中 (S2)</div><div class="val">{n_s2} <small>テーマ</small></div></div>'
        f'<div class="stat-card"><div class="lbl">失速シグナル</div><div class="val">{n_stall} <small>テーマ</small></div></div>'
        f'<div class="stat-card"><div class="lbl">床通過 / 全テーマ</div><div class="val">{n_floor} <small>/ {len(df)}</small></div></div>'
        '</div>', unsafe_allow_html=True)

    # --- 絞り込み + ダウンロード ---
    view = df
    if status_filter != "すべて":
        view = view[view["status"] == status_filter]
    if floor_only:
        view = view[view["floor_pass"].fillna(False)]

    col_info, col_dl = st.columns([5, 2], vertical_alignment="center")
    with col_info:
        if len(view) != len(df):
            st.caption(f"絞り込み: {len(view)} / {len(df)} テーマ")
    with col_dl:
        st.download_button(
            "📥 CSVダウンロード", data=df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"theme_flow_{sel}.csv", mime="text/csv", width="stretch")

    if len(view) == 0:
        st.info("条件に一致するテーマはありません。")
    else:
        st.markdown(_theme_table_html(view), unsafe_allow_html=True)
        st.caption("確信度 = 広がりz・市場相対z・代金z・加速度の等加重和 ×(1−集中度)。"
                   "全成分は自テーマの過去分布で正規化。行にマウスを乗せると参加率・集中度・点火履歴が見られます。")

    # --- 確信度の推移 ---
    with st.expander("📈 確信度の推移(テーマ別)"):
        try:
            stamp = tuple((str(f), os.path.getmtime(f)) for _, f in reports)
            hist = load_theme_history(stamp)
        except Exception:
            hist = None
        if hist is None or hist.empty:
            st.caption("推移データ(日次レポートCSV)を読み込めませんでした。")
        else:
            default_themes = df.sort_values("rank")["theme"].head(5).tolist()
            picked = st.multiselect("表示テーマ", sorted(hist["theme"].unique()),
                                    default=[t for t in default_themes
                                             if t in set(hist["theme"])])
            if picked:
                pivot = (hist[hist["theme"].isin(picked)]
                         .pivot_table(index="date", columns="theme", values="confidence"))
                st.line_chart(pivot, height=320)
                st.caption(f"記録日数: {hist['date'].nunique()} 日分(夜間ジョブ実行日のみ蓄積)")

    # --- 元のHTMLレポート ---
    html_file = THEME_FLOW_DATA / f"daily_report_{sel}.html"
    if html_file.exists():
        with st.expander("🗒 詳細HTMLレポート(3段構成・theme-flow生成)"):
            components.html(html_file.read_text(encoding="utf-8"), height=900, scrolling=True)


# ============================================================
# エントリポイント(ページ切り替え)
# ============================================================
PAGES = ["📈 連騰スクリーナー", "🌊 テーマ資金フロー"]


def main():
    page = st.sidebar.radio("ページ", PAGES, label_visibility="collapsed")
    st.sidebar.markdown("---")
    if page == PAGES[0]:
        cfg = build_config_from_sidebar()
        render_header()
        render_results(cfg)
    else:
        render_theme_page()


main()
