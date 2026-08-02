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

# ==========================================
# 1. 初期設定
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("エラー: GEMINI_API_KEYが設定されていません。")
    exit(1)

genai.configure(api_key=GEMINI_API_KEY)
# 最新モデルを指定
model = genai.GenerativeModel('gemini-2.0-flash')

today = datetime.datetime.now()
today_str_tdnet = today.strftime('%Y%m%d')
print(f"【処理開始】本日の日付: {today.strftime('%Y-%m-%d')}")

# ==========================================
# 2. トレーダーズ・ウェブから本日の決算銘柄を取得
# ==========================================
def get_todays_earnings_codes():
    print("トレーダーズ・ウェブから本日の決算銘柄を取得中...")
    url = "https://www.traders.co.jp/market_jp/earnings_calendar"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    target_codes = []
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 【修正】先頭が数字(1-9)、以降3桁が数字または英大文字（130Aなど新コード対応）
        code_pattern = re.compile(r'^([1-9][0-9A-Z]{3})$')
        
        for a_tag in soup.find_all('a'):
            text = a_tag.get_text(strip=True)
            match = code_pattern.match(text)
            if match:
                target_codes.append(match.group(1))
                
        target_codes = list(set(target_codes))
        print(f"取得成功: 本日の決算予定は {len(target_codes)} 銘柄です。")
        return target_codes
        
    except Exception as e:
        print(f"カレンダーの取得に失敗しました: {e}")
        return []

# ==========================================
# 3. TDnetから対象銘柄のPDFURLを抽出
# ==========================================
def get_tdnet_pdfs(target_codes):
    if not target_codes:
        return []
        
    print("TDnetの本日分ページを巡回中...")
    url = f"https://www.release.tdnet.info/inbs/I_list_001_{today_str_tdnet}.html"
    base_url = "https://www.release.tdnet.info/inbs/"
    
    found_pdfs = []
    try:
        response = requests.get(url)
        response.encoding = response.apparent_encoding
        if response.status_code != 200:
            print("本日のTDnet一覧ページがまだ作成されていないか、アクセスできません。")
            return []
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        for tr in soup.find_all('tr'):
            row_text = tr.get_text()
            matched_code = next((code for code in target_codes if code in row_text), None)
            
            if matched_code:
                if "決算短信" in row_text:
                    pdf_link = tr.find('a', href=re.compile(r'\.pdf$'))
                    if pdf_link:
                        full_pdf_url = base_url + pdf_link['href']
                        title = pdf_link.get_text(strip=True) or "決算短信"
                        found_pdfs.append({
                            "code": matched_code,
                            "title": title,
                            "pdf_url": full_pdf_url
                        })
                        
        print(f"TDnetで {len(found_pdfs)} 件の決算短信PDFを発見しました。")
        return found_pdfs
        
    except Exception as e:
        print(f"TDnetの取得に失敗しました: {e}")
        return []

# ==========================================
# 4. PDF抽出＆Geminiによる要約（API制限対策版）
# ==========================================
def summarize_pdfs(pdf_list):
    news_data = []
    request_count = 0
    start_time = time.time()
    
    for item in pdf_list:
        print(f"\n[{item['code']}] のPDFを読み込み・要約中...")
        try:
            # --- 【修正】レートリミット制御（1分間に14リクエストまで） ---
            current_time = time.time()
            elapsed_time = current_time - start_time
            
            if elapsed_time > 60:
                request_count = 0
                start_time = time.time()
                
            if request_count >= 14:
                wait_time = 60 - elapsed_time + 1
                print(f"⚠️ API制限回避のため、{wait_time:.1f}秒待機します...")
                time.sleep(wait_time)
                request_count = 0
                start_time = time.time()
            # -----------------------------------------------------------

            res = requests.get(item['pdf_url'])
            pdf_file = io.BytesIO(res.content)
            reader = PdfReader(pdf_file)
            
            text = ""
            for i in range(min(2, len(reader.pages))):
                text += reader.pages[i].extract_text()
                
            prompt = f"""
            あなたは優秀な株式投資アシスタントです。
            以下の決算短信の冒頭テキストから、個人投資家向けに重要なポイントを3つの箇条書きで要約してください。
            【要件】専門用語は避け、売上・利益の増減や来期の見通しを含める。各箇条書きは簡潔に。
            【テキスト】\n{text}
            """
            
            ai_response = model.generate_content(prompt)
            summary = ai_response.text.strip()
            
            request_count += 1
            
            news_data.append({
                "date": today.strftime('%Y-%m-%d'),
                "code": item["code"],
                "title": item["title"],
                "summary": summary,
                "url": item["pdf_url"]
            })
            print("＞ 要約成功！")
            
            time.sleep(2)
            
        except Exception as e:
            print(f"＞ エラー発生 ({item['code']}): {e}")
            
    return news_data

# ==========================================
# 5. メイン処理・JSON保存
# ==========================================
if __name__ == "__main__":
    codes_to_watch = get_todays_earnings_codes()
    pdfs_to_process = get_tdnet_pdfs(codes_to_watch)
    
    if pdfs_to_process:
        final_news = summarize_pdfs(pdfs_to_process)
        
        if final_news:
            output_file = "ai_news.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(final_news, f, ensure_ascii=False, indent=2)
            print(f"\n【完了】{len(final_news)}件の要約を {output_file} に保存しました！")
    else:
        print("\n本日は処理対象の決算短信が見つかりませんでした。")
        # 空配列で上書き（画面側でのエラー防止）
        with open("ai_news.json", "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
