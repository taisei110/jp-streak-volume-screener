import duckdb

con = duckdb.connect("data/prices.duckdb", read_only=True)

# 全体の最新日付
max_date = con.execute("SELECT MAX(date) FROM prices").fetchone()[0]
print(f"DB内の最新日付: {max_date}")

# 最新日のレコード数
cnt_max = con.execute(f"SELECT COUNT(*) FROM prices WHERE date = '{max_date}'").fetchone()[0]
print(f"{max_date} のレコード数: {cnt_max}")

# 1日前のレコード数
prev = con.execute(f"SELECT MAX(date) FROM prices WHERE date < '{max_date}'").fetchone()[0]
cnt_prev = con.execute(f"SELECT COUNT(*) FROM prices WHERE date = '{prev}'").fetchone()[0]
print(f"{prev} のレコード数: {cnt_prev}")

# 最新日のデータサンプル（open/closeがNULLでないもの）
valid = con.execute(f"SELECT COUNT(*) FROM prices WHERE date = '{max_date}' AND open IS NOT NULL AND close IS NOT NULL").fetchone()[0]
print(f"{max_date} で open/close 有効: {valid} 銘柄")

con.close()
