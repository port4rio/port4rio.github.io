import os
import json
import datetime
import time
import re
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

# ==========================================
# 0. APIと設定
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("エラー: GEMINI_API_KEYが設定されていません。")
    exit(1)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.5-flash-lite')

# 画像に記載された主要27社の「発表日」と「銘柄コード」リスト
TARGET_SCHEDULE = [
    {"date": "20260827", "codes": ["2590"]},                 # 予備
    {"date": "20260828", "codes": ["4707", "6118"]},         # 予備
    #{"date": "20260709", "codes": ["9983"]},                 # ファストリ
    #{"date": "20260710", "codes": ["6506"]},                 # 安川電機
    #{"date": "20260724", "codes": ["4519"]},                 # 中外製薬
    #{"date": "20260727", "codes": ["7751"]},                 # キヤノン
    #{"date": "20260729", "codes": ["6501", "6857", "6301"]}, # 日立, アドテスト, コマツ
    #{"date": "20260730", "codes": ["8035", "2914", "4661"]}, # 東エレク, JT, OLC
    #{"date": "20260731", "codes": ["6758", "285A"]},         # ソニー, キオクシア
    #{"date": "20260803", "codes": ["8306", "8058", "7201"]}, # 三菱UFJ, 三菱商事, 日産自
    #{"date": "20260804", "codes": ["7203", "6503", "7011"]}, # トヨタ, 三菱重(7011/6503両対応)
    #{"date": "20260805", "codes": ["6941", "6994", "6363"]}, # 山一電機, 指月電機, 酉島製作所
    #{"date": "20260806", "codes": ["9984", "9432", "7974", "2802", "1662"]}, # SBG, NTT, 任天堂, 味の素, 石油資源
    #{"date": "20260807", "codes": ["6098"]},                 # リクルート
    #{"date": "20260810", "codes": ["6785"]}                  # 鈴木
]

# ==========================================
# 1. TDnet巡回＆PDF抽出
# ==========================================
def fetch_target_pdfs(date_str, target_codes):
    base_url = "https://www.release.tdnet.info/inbs/"
    found_pdfs = []
    print(f"\n📅 【{date_str}】のTDnetを巡回中... (対象: {target_codes})")
    
    for page_num in range(1, 18):
        page_str = str(page_num).zfill(3)
        tdnet_url = f"https://www.release.tdnet.info/inbs/I_list_{page_str}_{date_str}.html"
        
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
                
                exclude_words = ["訂正", "レビュー", "補足", "お知らせ"]
                if matched_code and "決算短信" in row_text and not any(w in row_text for w in exclude_words):
                    pdf_link = tr.find('a', href=re.compile(r'\.pdf$'))
                    if pdf_link:
                        pdf_url = base_url + pdf_link['href']
                        if not any(p['pdf_url'] == pdf_url for p in found_pdfs):
                            found_pdfs.append({
                                "code": matched_code,
                                "date_str": date_str,
                                "title": pdf_link.get_text(strip=True) or "決算短信",
                                "pdf_url": pdf_url
                            })
        except Exception:
            break
            
    print(f"＞ TDnetで {len(found_pdfs)} 件のPDFを発見しました。")
    return found_pdfs

# ==========================================
# 1-B. Yahooファイナンスからの救済抽出（1ヶ月経過用）
# ==========================================
def fetch_pdf_from_yahoo(code, date_str):
    url = f"https://finance.yahoo.co.jp/quote/{code}.T/financials"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # パターン1: リンク(aタグ)から「決算短信」を探す
        for a in soup.find_all('a', href=True):
            text = a.get_text(strip=True)
            if "決算短信" in text:
                href = a['href']
                if href.startswith('/'): 
                    href = "https://finance.yahoo.co.jp" + href
                return {"code": code, "date_str": date_str, "title": text, "pdf_url": href}

        # パターン2: HTML直書きのJSON等からリンクを探す(フォールバック)
        html_text = response.text
        matches = re.findall(r'\{[^}]*"title"\s*:\s*"([^"]*決算短信[^"]*)"\s*,\s*"url"\s*:\s*"([^"]+)"[^}]*\}', html_text)
        if matches:
            title_str, url_str = matches[0]
            try:
                parsed = json.loads(f'{{"title":"{title_str}", "url":"{url_str}"}}')
                title = parsed["title"]
                href = parsed["url"]
            except:
                title = title_str
                href = url_str.replace('\\/', '/')
            return {"code": code, "date_str": date_str, "title": title, "pdf_url": href}
            
    except Exception as e:
        print(f"    ⚠️ Yahooファイナンスの解析エラー ({code}): {e}")
        pass
    
    return None

# ==========================================
# 2. PDFのGemini要約処理
# ==========================================
def summarize_single_pdf(item):
    d_raw = item['date_str']
    formatted_date = f"{d_raw[:4]}-{d_raw[4:6]}-{d_raw[6:]}"
    
    print(f"  └ [{item['code']}] PDFダウンロード＆AI要約中...")
    try:
        # 弾かれないようUser-Agentを追加してダウンロード
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(item['pdf_url'], headers=headers, timeout=20)
        
        if res.status_code != 200:
            print(f"  ❌ [{item['code']}] PDF取得失敗 (HTTP {res.status_code})")
            return None

        doc_part = {"mime_type": "application/pdf", "data": res.content}
        
        base_prompt = os.environ.get("SECRET_PROMPT", "要約を作成してください。")
        request_contents = [base_prompt, doc_part]

        for attempt in range(3):
            try:
                ai_response = model.generate_content(
                    request_contents,
                    request_options={"timeout": 180}
                )
                break
            except Exception as e:
                err_msg = str(e)
                if ("429" in err_msg or "504" in err_msg or "Deadline" in err_msg) and attempt < 2:
                    print(f"    ⚠️ 制限/タイムアウト検知。30秒待機して再試行... ({attempt+1}/3)")
                    time.sleep(30)
                else:
                    raise e

        ai_text = ai_response.text.strip() if ai_response else ""
        lines = [l for l in ai_text.split('\n') if l.strip()]
        
        if len(lines) >= 2:
            catchy_title = lines[0].replace('1行目:', '').replace('*', '').strip()
            summary_text = '\n'.join(lines[1:]).replace('2行目以降:', '').strip()
        else:
            catchy_title = item.get("title", "決算短信")
            summary_text = ai_text

        return {
            "date": formatted_date,
            "code": item["code"],
            "title": catchy_title,
            "summary": summary_text,
            "url": item["pdf_url"]
        }
    except Exception as e:
        print(f"  ❌ [{item['code']}] 要約エラー: {e}")
        return None

# ==========================================
# 3. メイン一括実行
# ==========================================
if __name__ == "__main__":
    all_target_pdfs = []
    
    for schedule in TARGET_SCHEDULE:
        date_str = schedule["date"]
        target_codes = schedule["codes"]
        
        # 1. まずTDnetを探す
        tdnet_pdfs = fetch_target_pdfs(date_str, target_codes)
        all_target_pdfs.extend(tdnet_pdfs)
        
        # 2. TDnetで見つからなかった銘柄を特定（1ヶ月経過等）
        found_codes = {p['code'] for p in tdnet_pdfs}
        missing_codes = [c for c in target_codes if c not in found_codes]
        
        # 3. Yahooファイナンスから救済取得
        for m_code in missing_codes:
            print(f"  ⚠️ TDnetで見つからないためYahooファイナンスを検索します... ({m_code})")
            yahoo_pdf = fetch_pdf_from_yahoo(m_code, date_str)
            if yahoo_pdf:
                all_target_pdfs.append(yahoo_pdf)
                print(f"    ＞ YahooでPDFリンクを発見: {yahoo_pdf['title']}")
            else:
                print(f"    ＞ ❌ Yahooファイナンスでも見つかりませんでした。")
                
        time.sleep(1)

    print(f"\n==========================================")
    print(f"抽出された対象PDF: 合計 {len(all_target_pdfs)} 件")
    print(f"==========================================")

    existing_news = []
    if os.path.exists("ai_news.json"):
        with open("ai_news.json", "r", encoding="utf-8") as f:
            try:
                raw = json.load(f)
                if isinstance(raw, list): existing_news = raw
            except Exception: pass

    processed_urls = {n.get('url') or n.get('pdf_url', '') for n in existing_news if isinstance(n, dict)}
    
    new_generated_news = []
    for idx, item in enumerate(all_target_pdfs, 1):
        if item['pdf_url'] in processed_urls:
            print(f"[{idx}/{len(all_target_pdfs)}] [{item['code']}] すでに保存済みのためスキップ")
            continue
            
        print(f"\n[{idx}/{len(all_target_pdfs)}] 処理開始...")
        news_obj = summarize_single_pdf(item)
        if news_obj:
            new_generated_news.append(news_obj)
            processed_urls.add(news_obj['url'])
            print(f"＞ ✨ [{item['code']}] 要約成功！")
        time.sleep(3)

    JST = datetime.timezone(datetime.timedelta(hours=9), 'JST')
    today = datetime.datetime.now(JST)
    limit_date = (today - datetime.timedelta(days=99)).strftime('%Y-%m-%d')
    
    combined = new_generated_news + existing_news
    final_news = []
    seen = set()
    
    for n in combined:
        if isinstance(n, dict):
            u = n.get('url') or n.get('pdf_url', '')
            d = n.get('date', '')
            if u and u not in seen and d >= limit_date:
                final_news.append(n)
                seen.add(u)

    final_news.sort(key=lambda x: x.get('date', ''), reverse=True)

    with open("ai_news.json", "w", encoding="utf-8") as f:
        json.dump(final_news, f, ensure_ascii=False, indent=2)

    print(f"\n✨ 一括処理が完了しました！現在の総蓄積件数: {len(final_news)} 件")
