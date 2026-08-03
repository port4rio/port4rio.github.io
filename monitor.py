import os
import json
import datetime
import time
import re
import requests
from bs4 import BeautifulSoup
import io
from pypdf import PdfReader, PdfWriter
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

# 確実に日本時間（JST）で今日の日付を取得する
JST = datetime.timezone(datetime.timedelta(hours=9), 'JST')
# today = datetime.datetime.now(JST)
today = datetime.datetime(2026, 8, 3, tzinfo=JST)
today_str_tdnet = today.strftime('%Y%m%d')
today_patterns = [f"{today.month}/{today.day}", today.strftime('%m/%d')]
print(f"【処理開始】日本時間: {today.strftime('%Y-%m-%d %H:%M:%S')}")

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
                
    todays_codes = list(set(todays_codes))
    with open("schedule.json", "w", encoding="utf-8") as f:
        json.dump(schedule_data, f, ensure_ascii=False, indent=2)
    print(f"スケジュール取得完了: 全 {len(schedule_data)} 件 (うち本日発表 {len(todays_codes)} 件)")
except Exception as e:
    print(f"スケジュール取得エラー: {e}")

# ==========================================
# 2. TDnetから対象銘柄のPDFURLを抽出 (複数ページ対応)
# ==========================================
def get_tdnet_pdfs(target_codes):
    if not target_codes:
        return []
        
    print("TDnetの本日分ページを巡回中...")
    base_url = "https://www.release.tdnet.info/inbs/"
    found_pdfs = []
    
    # 001から005ページ（最大500件）までパトロールする
    for page_num in range(1, 6):
        page_str = str(page_num).zfill(3) # 001, 002...
        tdnet_url = f"https://www.release.tdnet.info/inbs/I_list_{page_str}_{today_str_tdnet}.html"
        
        try:
            response = requests.get(tdnet_url)
            response.encoding = response.apparent_encoding
            
            # ページが存在しなければループを終了
            if response.status_code != 200:
                break
                
            soup = BeautifulSoup(response.text, 'html.parser')
            trs = soup.find_all('tr')
            if not trs:
                break
                
            for tr in trs:
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
            print(f" - ページ {page_str} をチェック完了")
        except:
            break
            
    print(f"合計 {len(found_pdfs)} 件の対象PDFを発見しました。")
    return found_pdfs

# ==========================================
# 3. PDF抽出＆Geminiによる要約（OCR直接読み込み対応版）
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
            
            print(f"  └ 2. PDFからテキストを抽出中...")
            reader = PdfReader(io.BytesIO(res.content))
            text = "".join([reader.pages[i].extract_text() for i in range(min(2, len(reader.pages)))])
            
            # Geminiに渡すデータの中身を準備
            request_contents = []
            
            if not text.strip():
                print(f"  └ ⚠️ テキスト抽出不能。AIの視覚機能(OCR)でPDFを直接読み込みます。")
                
                # 最初の2ページだけを切り出して新しいPDFデータを作成（節約と高速化のため）
                writer = PdfWriter()
                for i in range(min(2, len(reader.pages))):
                    writer.add_page(reader.pages[i])
                
                short_pdf_stream = io.BytesIO()
                writer.write(short_pdf_stream)
                pdf_bytes = short_pdf_stream.getvalue()
                
                # Geminiに「これはPDFファイルだよ」と教えてバイナリデータを渡す
                doc_part = {
                    "mime_type": "application/pdf",
                    "data": pdf_bytes
                }
                
                prompt = """以下の決算短信（画像）の冒頭部分を読んで、個人投資家向けに要約を作成してください。
挨拶や前置きは一切不要です。必ず以下のフォーマット通りに出力してください。

1行目: 業績の印象がパッとわかる魅力的な15文字程度の見出し。好調なら末尾に「⤴」、苦戦なら「⤵」、横ばいなら「→」をつけてください。
2行目以降: 以下の3点を簡潔な箇条書き（・から始める）でまとめてください。
・売上と利益の増減
・なぜその業績になったのか（市場背景や要因）
・来期の見通し
"""
                request_contents = [prompt, doc_part]
            
            else:
                # テキストが抽出できた場合は今まで通りテキストで渡す
                prompt = f"""以下の決算短信から、個人投資家向けに要約を作成してください。
挨拶や前置きは一切不要です。必ず以下のフォーマット通りに出力してください。

1行目: 業績の印象がパッとわかる魅力的な15文字程度の見出し。好調なら末尾に「⤴」、苦戦なら「⤵」、横ばいなら「→」をつけてください。
2行目以降: 以下の3点を簡潔な箇条書き（・から始める）でまとめてください。
・売上と利益の増減
・なぜその業績になったのか（市場背景や要因）
・来期の見通し

【テキスト】
{text}
"""
                request_contents = [prompt]

            print(f"  └ 3. Geminiへ要約をリクエスト中...")
            max_retries = 3
            ai_response = None
            for attempt in range(max_retries):
                try:
                    # テキストまたはPDFデータをGeminiに送信
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
# 4. メイン処理 (過去ニュースの維持 + マージ)
# ==========================================
if __name__ == "__main__":
    pdfs_to_process = get_tdnet_pdfs(todays_codes)
    
    existing_news = []
    if os.path.exists("ai_news.json"):
        with open("ai_news.json", "r", encoding="utf-8") as f:
            try: existing_news = json.load(f)
            except: pass

    seven_days_ago = (today - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
    existing_news = [n for n in existing_news if n['date'] >= seven_days_ago]

    new_news = []
    if pdfs_to_process:
        new_news = summarize_pdfs(pdfs_to_process)
        
    news_dict = { n['code']: n for n in existing_news }
    for n in new_news:
        news_dict[n['code']] = n
        
    final_news = list(news_dict.values())
    final_news.reverse()
    final_news.sort(key=lambda x: x['date'], reverse=True)
    
    with open("ai_news.json", "w", encoding="utf-8") as f:
        json.dump(final_news, f, ensure_ascii=False, indent=2)
    print("ai_news.json の更新が完了しました！")
