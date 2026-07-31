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

print("株価の取得と前日比の計算を開始します...")

for ticker in tickers:
    try:
        stock = yf.Ticker(ticker)
        # 休場日（土日祝など）を考慮して、過去5日分のデータを取得する
        hist = stock.history(period="5d")
        
        # 最低でも2日分（今日と前回）のデータがあるか確認
        if len(hist) >= 2:
            current_price = float(hist['Close'].iloc[-1]) # 最新の終値
            previous_close = float(hist['Close'].iloc[-2]) # 1つ前の終値
            
            # 前日比（円）と増減率（％）を計算
            change_amount = current_price - previous_close
            change_percent = (change_amount / previous_close) * 100
            
            code = ticker.replace(".T", "")
            
            # 保存するデータを「価格だけ」から「詳細データ」にパワーアップ
            prices_data[code] = {
                "price": round(current_price, 2),
                "previous_close": round(previous_close, 2),
                "change_amount": round(change_amount, 2),
                "change_percent": round(change_percent, 2)
            }
            print(f"{code}: 現在値 {current_price:.2f}円 (前日比 {change_amount:+.2f}円 / {change_percent:+.2f}%)")
        else:
            print(f"{ticker} のデータが十分に取得できませんでした。")
            
        time.sleep(1)
    except Exception as e:
        print(f"{ticker} の取得に失敗しました: {e}")

with open("prices.json", "w", encoding="utf-8") as f:
    json.dump(prices_data, f, ensure_ascii=False, indent=4)

print("すべての処理が完了し、prices.json を作成しました！")
