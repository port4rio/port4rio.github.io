import os
import json
import datetime
import time
import re
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

# ==========================================
# 0. APIと日本時間（JST）およびモデル優先度の設定
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("エラー: GEMINI_API_KEYが設定されていません。")
    exit(1)

genai.configure(api_key=GEMINI_API_KEY)

# 4段階のモデル優先度リスト
MODEL_PRIORITY_LIST = [
    'gemini-3.8-flash',
    'gemini-3.7-flash',
    'gemini-3.6-flash',
    'gemini-3.5-flash-lite'
]

# 本日上限（429）に達したモデルを記憶するセット（当日の処理中で共有）
EXHAUSTED_MODELS = set()

JST = datetime.timezone(datetime.timedelta(hours=9), 'JST')
today = datetime.datetime.now(JST)
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
    for page in [1, 2]:
        target_url = f"https://www.traders.co.jp/market_jp/earnings_calendar/all/all_ex_etf/{page}"
        print(f" - {page}ページ目を取得中: {target_url}")
        
        response = requests.get(target_url, headers=headers, timeout=8)
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
#todays_codes = ["2590", "9494"]
#today_str_tdnet = "20260827"  # ← ここを「実際の決算発表日」の数字8桁に書き換えて

# ==========================================
# 2. TDnetから対象銘柄のPDFURLを抽出（高速化版）
# ==========================================
def get_tdnet_pdfs(target_codes):
    if not target_codes:
        return []
        
    print(f"TDnetの本日分ページを巡回中... (対象銘柄数: {len(target_codes)}件)")
    base_url = "https://www.release.tdnet.info/inbs/"
    found_pdfs = []
    
    for page_num in range(1, 18):
        # 本日の対象PDFが全て見つかったら即終了（不要なページ巡回をカット）
        if len(found_pdfs) >= len(target_codes):
            print(" ⚡ 全対象銘柄のPDFを発見したため、TDnet巡回を早期終了します。")
            break

        page_str = str(page_num).zfill(3)
        tdnet_url = f"https://www.release.tdnet.info/inbs/I_list_{page_str}_{today_str_tdnet}.html"
        
        try:
            # タイムアウトを10秒→4秒に短縮して応答待ちを高速化
            response = requests.get(tdnet_url, timeout=4)
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
                
                exclude_words = ["訂正", "レビュー", "補足", "お知らせ"]
                
                if matched_code and "決算短信" in row_text and not any(word in row_text for word in exclude_words):
                    pdf_link = tr.find('a', href=re.compile(r'\.pdf$'))
                    if pdf_link:
                        pdf_url = base_url + pdf_link['href']
                        if not any(p['pdf_url'] == pdf_url for p in found_pdfs):
                            found_pdfs.append({
                                "code": matched_code,
                                "title": pdf_link.get_text(strip=True) or "決算短信",
                                "pdf_url": pdf_url
                            })
            print(f" - ページ {page_str} チェック完了 (累計発見: {len(found_pdfs)}件)")
        except Exception:
            # 存在しないページやタイムアウト発生時は即座に巡回終了
            print(f" - ページ {page_str} なし（またはタイムアウト）。巡回を終了します。")
            break
            
    print(f"合計 {len(found_pdfs)} 件の対象PDFを発見しました。")
    return found_pdfs

# ==========================================
# 3. 高速フォールバック対応 Gemini生成関数
# ==========================================
def generate_ai_summary(request_contents):
    """
    4段階の優先モデルリストに沿ってAI要約を試行。
    本日の上限(429)に達したモデルは即座にブラックリスト化し、0秒で次モデルへフォールバックします。
    """
    for current_model_name in MODEL_PRIORITY_LIST:
        # すでに本日上限に達しているモデルは0秒スキップ
        if current_model_name in EXHAUSTED_MODELS:
            continue

        print(f"  🤖 試行中モデル: [{current_model_name}]")
        try:
            active_model = genai.GenerativeModel(current_model_name)
            for attempt in range(2):
                try:
                    response = active_model.generate_content(
                        request_contents,
                        request_options={"timeout": 120} # タイムアウトも2分に短縮
                    )
                    if response and response.text:
                        return response.text.strip(), current_model_name
                except Exception as e:
                    err_msg = str(e)
                    
                    # ▼ 1日上限エラー(Quota exceeded / 429)を検知した場合
                    if "429" in err_msg and ("quota" in err_msg.lower() or "limit: 20" in err_msg.lower()):
                        print(f"    ⛔ [{current_model_name}] 本日の1日上限(20回)に達しました。本処理内では即スキップします。")
                        EXHAUSTED_MODELS.add(current_model_name)
                        break # リトライせず直ちに次のモデルへ切り替え
                    
                    # ▼ 一時的なサーバー混雑・タイムアウト(504)等の場合のみ1回リトライ
                    if ("504" in err_msg or "Deadline" in err_msg or "429" in err_msg) and attempt < 1:
                        print(f"    ⚠️ 一時的エラー。10秒待機後に再試行... ({attempt+1}/2)")
                        time.sleep(10)
                    else:
                        raise e
        except Exception as e:
            if current_model_name not in EXHAUSTED_MODELS:
                print(f"    ⚠️ [{current_model_name}] での処理失敗: {e}")
            if current_model_name != MODEL_PRIORITY_LIST[-1]:
                print(f"    🔄 次の優先モデルにフォールバックします...")
            continue
            
    return None, None

def summarize_pdfs(pdf_list, target_date_str):
    news_data = []
    
    if len(target_date_str) == 8:
        formatted_date = f"{target_date_str[:4]}-{target_date_str[4:6]}-{target_date_str[6:]}"
    else:
        formatted_date = today.strftime('%Y-%m-%d')

    for item in pdf_list:
        print(f"\n[{item['code']}] 処理開始 ({item.get('title', '決算短信')})...")
        try:
            res = requests.get(item['pdf_url'], timeout=15)
            if res.status_code != 200:
                print(f"＞ ❌ [{item['code']}] PDF取得失敗")
                continue

            doc_part = {
                "mime_type": "application/pdf",
                "data": res.content
            }
            
            base_prompt = os.environ.get("SECRET_PROMPT", "要約を作成してください。")
            request_contents = [base_prompt, doc_part]

            ai_text, used_model = generate_ai_summary(request_contents)
            
            if not ai_text:
                print(f"＞ ❌ [{item['code']}] 利用可能な全モデルで要約生成に失敗しました。")
                continue

            print(f"    └ 使用モデル: [{used_model}]")
            lines = [line for line in ai_text.split('\n') if line.strip()]
            
            if len(lines) >= 2:
                catchy_title = lines[0].replace('1行目:', '').replace('*', '').strip()
                summary_text = '\n'.join(lines[1:]).replace('2行目以降:', '').strip()
            else:
                catchy_title = item.get("title", "決算短信")
                summary_text = ai_text

            news_data.append({
                "date": formatted_date,
                "code": item["code"],
                "title": catchy_title,
                "summary": summary_text,
                "url": item["pdf_url"]
            })
            print(f"＞ [{item['code']}] ✨要約完了！")
            time.sleep(1) # 間隔も1秒に短縮
            
        except Exception as e:
            print(f"＞ ❌エラー ({item['code']}): {e}")
            
    return news_data

# ==========================================
# 4. メイン処理 (過去ニュースの維持 + 安全な重複チェック)
# ==========================================
if __name__ == "__main__":
    pdfs_to_process = get_tdnet_pdfs(todays_codes)
    
    existing_news = []
    if os.path.exists("ai_news.json"):
        with open("ai_news.json", "r", encoding="utf-8") as f:
            try: 
                raw_json = json.load(f)
                if isinstance(raw_json, list):
                    existing_news = raw_json
            except: pass

    limit_date = (today - datetime.timedelta(days=99)).strftime('%Y-%m-%d')
    existing_news = [n for n in existing_news if isinstance(n, dict) and n.get('date', '') >= limit_date]

    processed_urls_today = {n.get('url') or n.get('pdf_url', '') for n in existing_news}
    unprocessed_pdfs = [p for p in pdfs_to_process if p.get('pdf_url') not in processed_urls_today]
    
    print(f"本日発見されたPDF: {len(pdfs_to_process)}件")
    print(f"すでに処理済みでスキップ: {len(pdfs_to_process) - len(unprocessed_pdfs)}件")
    print(f"今回新しくAI要約する件数: {len(unprocessed_pdfs)}件")

    new_news = []
    if unprocessed_pdfs:
        new_news = summarize_pdfs(unprocessed_pdfs, today_str_tdnet)
        
    combined_news = new_news + existing_news
    
    final_news = []
    seen_urls = set()
    for n in combined_news:
        if isinstance(n, dict):
            url = n.get('url') or n.get('pdf_url', '')
            if url and url not in seen_urls:
                final_news.append(n)
                seen_urls.add(url)
            
    final_news.sort(key=lambda x: x.get('date', ''), reverse=True)
    
    with open("ai_news.json", "w", encoding="utf-8") as f:
        json.dump(final_news, f, ensure_ascii=False, indent=2)
    print("ai_news.json の更新が完了しました！")
