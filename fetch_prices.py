import yfinance as yf
import json
import time
import datetime
import jpholiday

# --- 祝日・休日判定 ---
today = datetime.date.today()
if jpholiday.is_holiday(today) or today.weekday() >= 5:
    print(f"本日は休日（{today}）のため、株価取得をスキップします。")
    exit()

# --- 以下、株価取得処理 ---
tickers = ["7203.T", "9984.T", "8306.T"]
prices_data = {}

print("株価の取得を開始します...")

for ticker in tickers:
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1d")
        if not hist.empty:
            current_price = float(hist['Close'].iloc[-1])
            code = ticker.replace(".T", "")
            prices_data[code] = current_price
            print(f"{code} の株価を取得しました: {current_price}円")
        time.sleep(1)
    except Exception as e:
        print(f"{ticker} の取得に失敗しました: {e}")

with open("prices.json", "w", encoding="utf-8") as f:
    json.dump(prices_data, f, ensure_ascii=False, indent=4)

print("すべての処理が完了し、prices.json を作成しました！")
