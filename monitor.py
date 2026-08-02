import os
import json
import datetime
import time
import re
import requests
from bs4 import BeautifulSoup
import io
from pypdf import PdfReader
import google.generativeai as genai

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("エラー: GEMINI_API_KEYが設定されていません。")
    exit(1)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash')

today = datetime.datetime.now()
today_str_tdnet = today.strftime('%Y%m%d')
# Traders Webでの今日の日付表記 (例: 8/2 または 08/02)
today_patterns = [f"{today.month}/{today.day}", today.strftime('%m/%d')]

# ==========================================
# 1. 決算スケジュールの取得と保存
# ==========================================
print("トレーダーズ・ウェブから決算スケジュールを取得中...")
url = "https://www.traders.co.jp/market_jp/earnings_calendar"
headers = {'User-Agent': 'Mozilla/5.0'}

schedule_data = {}
todays_codes = []

try:
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.content, 'html.parser')
    
    current_date = None
    for tr in soup.find_all('tr'):
        text = tr.get_text(separator=' ', strip=True)
        
        # 8/4 のような日付を探す
        d_match = re.search(r'\b(\d{1,2}/\d{1,2})\b', text)
        if d_match:
            current_date = d_match.group(1)
            
        c_match = re.search(r'\b([1-9][0-9A-Z]{3})\b', text)
        t_match = re.search(r'\b(\d{1,2}:\d{2})\b', text)
        
        if c_match and current_date:
            code = c_match.group(1)
            time_str = t_match.group(1) if t_match else "-"
            schedule_data[code] = {"date": current_date, "time": time_str}
            
            # もし日付が「今日」なら、TDnet監視対象に追加
            if current_date in today_patterns:
                todays_codes.append(code)
                
    todays_codes = list(set(todays_codes))
    
    # schedule.json として保存（画面側で読み込む用）
    with open("schedule.json", "w", encoding="utf-8") as f:
        json.dump(schedule_data, f, ensure_ascii=False, indent=2)
    print(f"スケジュール取得完了: 全 {len(schedule_data)} 件 (うち本日発表 {len(todays_codes)} 件)")
    
except Exception as e:
    print(f"スケジュール取得エラー: {e}")

# ==========================================
# 2. TDnetから対象銘柄のPDFURLを抽出
# ==========================================
def get_tdnet_pdfs(target_codes):
    if not target_codes:
        return []
        
    print("TDnetの本日分ページを巡回中...")
    tdnet_url = f"https://www.release.tdnet.info/inbs/I_list_001_{today_str_tdnet}.html"
    base_url = "https://www.release.tdnet.info/inbs/"
    
    found_pdfs = []
    try:
        response = requests.get(tdnet_url)
        response.encoding = response.apparent_encoding
        if response.status_code != 200:
            return []
            
        soup = BeautifulSoup(response.text, 'html.parser')
        for tr in soup.find_all('tr'):
            row_text = tr.get_text()
            matched_code = next((c for c in target_codes if c in row_text), None)
            
            if matched_code and "決算短信" in row_text:
                pdf_link = tr.find('a', href=re.compile(r'\.pdf$'))
                if pdf_link:
                    found_pdfs.append({
                        "code": matched_code,
                        "title": pdf_link.get_text(strip=True) or "決算短信",
                        "pdf_url": base_url + pdf_link['href']
                    })
    except:
        pass
    return found_pdfs

# ==========================================
# 3. PDF抽出＆Geminiによる要約（制限対策版）
# ==========================================
def summarize_pdfs(pdf_list):
    news_data = []
    request_count = 0
    start_time = time.time()
    
    for item in pdf_list:
        print(f"[{item['code']}] を要約中...")
        try:
            current_time = time.time()
            if current_time - start_time > 60:
                request_count = 0
                start_time = time.time()
                
            if request_count >= 14:
                time.sleep(60 - (current_time - start_time) + 1)
                request_count = 0
                start_time = time.time()

            res = requests.get(item['pdf_url'])
            reader = PdfReader(io.BytesIO(res.content))
            text = "".join([reader.pages[i].extract_text() for i in range(min(2, len(reader.pages)))])
                
            prompt = f"以下の決算短信冒頭から、個人投資家向けに重要なポイント(売上・利益の増減や来期見通しなど)を3つの簡潔な箇条書きで要約して。\n{text}"
            
            ai_response = model.generate_content(prompt)
            request_count += 1
            
            news_data.append({
                "date": today.strftime('%Y-%m-%d'),
                "code": item["code"],
                "title": item["title"],
                "summary": ai_response.text.strip(),
                "url": item["pdf_url"]
            })
            time.sleep(2)
        except Exception as e:
            print(f"エラー ({item['code']}): {e}")
    return news_data

# ==========================================
# 4. メイン処理 (過去ニュースの維持 + マージ)
# ==========================================
if __name__ == "__main__":
    pdfs_to_process = get_tdnet_pdfs(todays_codes)
    
    # 既存のニュースを読み込む
    existing_news = []
    if os.path.exists("ai_news.json"):
        with open("ai_news.json", "r", encoding="utf-8") as f:
            try: existing_news = json.load(f)
            except: pass

    # 過去7日間より古いニュースは削除
    seven_days_ago = (today - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
    existing_news = [n for n in existing_news if n['date'] >= seven_days_ago]

    # 新しい要約があれば取得
    new_news = []
    if pdfs_to_process:
        new_news = summarize_pdfs(pdfs_to_process)
        
    # 古いニュースと新しいニュースを合体 (同じ銘柄コードがあれば最新のもので上書き)
    news_dict = { n['code']: n for n in existing_news }
    for n in new_news:
        news_dict[n['code']] = n
        
    final_news = list(news_dict.values())
    final_news.sort(key=lambda x: x['date'], reverse=True) # 日付の新しい順に並び替え
    
    with open("ai_news.json", "w", encoding="utf-8") as f:
        json.dump(final_news, f, ensure_ascii=False, indent=2)
    print("ai_news.json の更新が完了しました！")
