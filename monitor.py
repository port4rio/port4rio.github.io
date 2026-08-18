import os
import json
import datetime
import time
import re
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

# ==========================================
# 0. APIと日本時間（JST）の設定
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("エラー: GEMINI_API_KEYが設定されていません。")
    exit(1)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.5-flash-lite')

JST = datetime.timezone(datetime.timedelta(hours=9), 'JST')
today = datetime.datetime.now(JST) # 本番用（現在時刻）に戻しました
today_str_tdnet = today.strftime('%Y%m%d')
today_patterns = [f"{today.month}/{today.day}", today.strftime('%m/%d')]
print(f"【処理開始】日本時間: {today.strftime('%Y-%m-%d %H:%M:%S')}")

# ==========================================
# 1. 決算スケジュールの取得と保存
# ==========================================
print("トレーダーズ・ウェブから決算スケジュールを取得中...")
headers = {'User-Agent': 'Mozilla/5.0'}

schedule_data = {}
todays_codes = []

try:
    # ▼ 1ページ目と2ページ目を順番に処理するためのループ
    for page in [1, 2]:
        target_url = f"https://www.traders.co.jp/market_jp/earnings_calendar/all/all_ex_etf/{page}"
        print(f" - {page}ページ目を取得中: {target_url}")
        
        response = requests.get(target_url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        current_date = None
        for tr in soup.find_all('tr'):
            text = tr.get_text(separator=' ', strip=True)
            d_match = re.search(r'\b(\d{1,2}/\d{1,2})\b', text)
            if d_match:
                current_date = d_match.group(1)
                
            c_match = re.search(r'\b([1-9][0-9A-Z]{3})\b', text)
            t_match = re.search(r'\b(\d{1,2}:\d{2})\b', text)
            
            if c_match and current_date:
                code = c_match.group(1)
                time_str = t_match.group(1) if t_match else "-"
                schedule_data[code] = {"date": current_date, "time": time_str}
                
                if current_date in today_patterns:
                    todays_codes.append(code)
        
        # ▼ 【超重要】相手サーバーへの負荷を下げるため、次のページにいく前に1秒待機
        time.sleep(1)
            
    todays_codes = list(set(todays_codes))
    with open("schedule.json", "w", encoding="utf-8") as f:
        json.dump(schedule_data, f, ensure_ascii=False, indent=2)
    print(f"スケジュール取得完了: 全 {len(schedule_data)} 件 (うち本日発表 {len(todays_codes)} 件)")
except Exception as e:
    print(f"スケジュール取得エラー: {e}")

# ==========================================
# ★一時的な手動レスキュー設定（終わったら消す）
# ==========================================
todays_codes = ["6327", "7532"]
today_str_tdnet = "20260818"  # ← ここを「実際の決算発表日」の数字8桁に書き換えて（例は8月18日）

# ==========================================
# 2. TDnetから対象銘柄のPDFURLを抽出
# ==========================================
def get_tdnet_pdfs(target_codes):
    if not target_codes:
        return []
        
    print("TDnetの本日分ページを巡回中...")
    base_url = "https://www.release.tdnet.info/inbs/"
    found_pdfs = []
    
    for page_num in range(1, 18):
        page_str = str(page_num).zfill(3)
        tdnet_url = f"https://www.release.tdnet.info/inbs/I_list_{page_str}_{today_str_tdnet}.html"
        
        try:
            response = requests.get(tdnet_url, timeout=10)
            response.encoding = response.apparent_encoding
            if response.status_code != 200:
                break
                
            soup = BeautifulSoup(response.text, 'html.parser')
            trs = soup.find_all('tr')
            if not trs:
                break
                
            for tr in trs:
                row_text = tr.get_text()
                matched_code = next((c for c in target_codes if c in row_text), None)
                
                # ▼ 弾きたいノイズのリストを定義
                exclude_words = ["訂正", "レビュー", "補足", "お知らせ"]
                
                # ▼ 「決算短信」が含まれ、かつ除外リストの言葉が1つも入っていない場合のみ処理
                if matched_code and "決算短信" in row_text and not any(word in row_text for word in exclude_words):
                    pdf_link = tr.find('a', href=re.compile(r'\.pdf$'))
                    if pdf_link:
                        found_pdfs.append({
                            "code": matched_code,
                            "title": pdf_link.get_text(strip=True) or "決算短信",
                            "pdf_url": base_url + pdf_link['href']
                        })
            print(f" - ページ {page_str} をチェック完了")
        except:
            break
            
    print(f"合計 {len(found_pdfs)} 件の対象PDFを発見しました。")
    return found_pdfs

# ==========================================
# 3. PDF抽出＆Geminiによる要約（PDF直接読み込み特化版）
# ==========================================
def summarize_pdfs(pdf_list):
    news_data = []
    request_count = 0
    start_time = time.time()
    
    for item in pdf_list:
        print(f"\n[{item['code']}] 処理開始...")
        try:
            current_time = time.time()
            if current_time - start_time > 60:
                request_count = 0
                start_time = time.time()
                
            if request_count >= 14:
                time.sleep(60 - (current_time - start_time) + 1)
                request_count = 0
                start_time = time.time()

            print(f"  └ 1. TDnetからPDFをダウンロード中...")
            res = requests.get(item['pdf_url'], timeout=20)
            
            print(f"  └ 2. PDFをAI用に準備中...")
            doc_part = {
                "mime_type": "application/pdf",
                "data": res.content
            }
            
            # ▼ Secretから秘伝のタレ（プロンプト）を読み込み、PDFとセットにする
            base_prompt = os.environ.get("SECRET_PROMPT", "要約を作成してください。")
            request_contents = [base_prompt, doc_part]

            print(f"  └ 3. Geminiへ要約をリクエスト中...")
            max_retries = 3
            ai_response = None
            for attempt in range(max_retries):
                try:
                    ai_response = model.generate_content(request_contents)
                    break 
                except Exception as api_error:
                    if "429" in str(api_error) and attempt < max_retries - 1:
                        print(f"    ⚠️ API制限を検知。30秒待機して再試行します... ({attempt+1}/{max_retries})")
                        time.sleep(30)
                    else:
                        raise api_error
            
            request_count += 1
            
            ai_text = ai_response.text.strip() if ai_response else ""
            lines = [line for line in ai_text.split('\n') if line.strip()]
            
            if len(lines) >= 2:
                catchy_title = lines[0].replace('1行目:', '').replace('*', '').strip()
                summary_text = '\n'.join(lines[1:]).replace('2行目以降:', '').strip()
            else:
                catchy_title = "要約のフォーマットエラー→"
                summary_text = ai_text

            news_data.append({
                "date": today.strftime('%Y-%m-%d'),
                "code": item["code"],
                "title": catchy_title,
                "summary": summary_text,
                "url": item["pdf_url"]
            })
            print(f"＞ [{item['code']}] ✨要約完了！")
            time.sleep(2)
            
        except requests.exceptions.Timeout:
            print(f"＞ ❌エラー ({item['code']}): TDnetの応答がありません（タイムアウト）")
        except Exception as e:
            print(f"＞ ❌エラー ({item['code']}): {e}")
            
    return news_data

# ==========================================
# 4. メイン処理 (過去ニュースの維持 + 重複スキップ + マージ)
# ==========================================
if __name__ == "__main__":
    pdfs_to_process = get_tdnet_pdfs(todays_codes)
    
    # ① 既存のニュースを読み込む（90日前まで保持）
    existing_news = []
    if os.path.exists("ai_news.json"):
        with open("ai_news.json", "r", encoding="utf-8") as f:
            try: existing_news = json.load(f)
            except: pass

    limit_date = (today - datetime.timedelta(days=99)).strftime('%Y-%m-%d')
    existing_news = [n for n in existing_news if n['date'] >= limit_date]

    # ② 今日すでに処理済みの銘柄コードをリストアップ（同日の二重取得防止）
    today_str_json = today.strftime('%Y-%m-%d')
    processed_codes_today = {n['code'] for n in existing_news if n['date'] == today_str_json}
    
    # ③ まだ処理していない新しいPDFだけを抽出
    unprocessed_pdfs = [p for p in pdfs_to_process if p['code'] not in processed_codes_today]
    
    print(f"本日発見されたPDF: {len(pdfs_to_process)}件")
    print(f"すでに処理済みでスキップ: {len(pdfs_to_process) - len(unprocessed_pdfs)}件")
    print(f"今回新しくAI要約する件数: {len(unprocessed_pdfs)}件")

    new_news = []
    if unprocessed_pdfs:
        new_news = summarize_pdfs(unprocessed_pdfs)
        
    # ④ 既存データと新規データを合体させる（過去の1Q・2Qも残るようURLで重複排除）
    combined_news = new_news + existing_news
    
    final_news = []
    seen_urls = set()
    for n in combined_news:
        if n['url'] not in seen_urls:
            final_news.append(n)
            seen_urls.add(n['url'])
            
    # 日付の降順（新しい順）で安定ソート
    final_news.sort(key=lambda x: x['date'], reverse=True)
    
    with open("ai_news.json", "w", encoding="utf-8") as f:
        json.dump(final_news, f, ensure_ascii=False, indent=2)
    print("ai_news.json の更新が完了しました！")
