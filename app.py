import streamlit as st
import json
import os
import pandas as pd
import requests
import yfinance as yf
import time
import threading
import telebot
import xml.etree.ElementTree as ET
import cloudscraper
import re
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from telebot.types import BotCommand
from deep_translator import GoogleTranslator

# --- 프로젝트 설정 ---
CONFIG_FILE = 'debrief_settings.json'
LOG_FILE = 'debrief.log'

# [State] 캐시 및 전역 변수
if 'price_alert_cache' not in st.session_state: st.session_state['price_alert_cache'] = {}
if 'rsi_alert_status' not in st.session_state: st.session_state['rsi_alert_status'] = {}

price_alert_cache = st.session_state['price_alert_cache']
rsi_alert_status = st.session_state['rsi_alert_status']

# ---------------------------------------------------------
# [0] 로그 기록
# ---------------------------------------------------------
def write_log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {msg}\n")
    except: pass

# ---------------------------------------------------------
# [1] 설정 로드/저장 (JSONBin 디버깅 강화)
# ---------------------------------------------------------
DEFAULT_OPTS = {
    "🟢 감시": True, 
    "📰 뉴스": True, 
    "🏛️ SEC": True, 
    "📈 급등락(3%)": True,
    "📊 거래량(2배)": False, 
    "🚀 신고가": True, 
    "📉 RSI": False,
    "〰️ MA크로스": False, 
    "🛁 볼린저": False, 
    "🌊 MACD": False
}

def migrate_options(old_opts):
    new_opts = DEFAULT_OPTS.copy()
    mapping = {
        "감시_ON": "🟢 감시", "뉴스": "📰 뉴스", "SEC": "🏛️ SEC",
        "가격_3%": "📈 급등락(3%)", "거래량_2배": "📊 거래량(2배)",
        "52주_신고가": "🚀 신고가", "RSI": "📉 RSI", "MA_크로스": "〰️ MA크로스",
        "볼린저": "🛁 볼린저", "MACD": "🌊 MACD"
    }
    for old_k, val in old_opts.items():
        if old_k in mapping: new_opts[mapping[old_k]] = val
        elif old_k in new_opts: new_opts[old_k] = val
    return new_opts

def get_jsonbin_config():
    """Secrets에서 JSONBin 설정 가져오기 (오류 처리 강화)"""
    try:
        if "jsonbin" in st.secrets:
            m_key = st.secrets["jsonbin"]["master_key"]
            b_id = st.secrets["jsonbin"]["bin_id"]
            return {
                "url": f"https://api.jsonbin.io/v3/b/{b_id}",
                "headers": {
                    'Content-Type': 'application/json',
                    'X-Master-Key': m_key
                }
            }
    except Exception as e:
        write_log(f"Secrets Read Error: {e}")
    return None

def load_config():
    # 1. 기본값 생성
    config = {
        "system_active": True,
        "eco_mode": True,
        "telegram": {"bot_token": "", "chat_id": ""}, 
        "tickers": {
            "TSLA": DEFAULT_OPTS.copy(),
            "NVDA": DEFAULT_OPTS.copy()
        },
        "news_history": {}
    }
    
    loaded_data = None
    jb_conf = get_jsonbin_config()
    
    # 2. Cloud Load (JSONBin)
    if jb_conf:
        try:
            # /latest 엔드포인트 사용
            resp = requests.get(f"{jb_conf['url']}/latest", headers=jb_conf['headers'], timeout=5)
            if resp.status_code == 200:
                loaded_data = resp.json().get('record')
                write_log("✅ Cloud Config Loaded Successfully")
            else:
                write_log(f"⚠️ Cloud Load Failed: {resp.status_code} - {resp.text}")
        except Exception as e:
            write_log(f"⚠️ Cloud Connection Error: {e}")
    
    # 3. Local Backup Load (Cloud 실패 시)
    if not loaded_data and os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
                write_log("⚠️ Loaded from Local Backup")
        except: pass

    # 4. 데이터 병합 (Merge)
    if loaded_data:
        if "telegram" in loaded_data: config['telegram'] = loaded_data['telegram']
        if "system_active" in loaded_data: config['system_active'] = loaded_data['system_active']
        if "eco_mode" in loaded_data: config['eco_mode'] = loaded_data['eco_mode']
        if "news_history" in loaded_data: config['news_history'] = loaded_data['news_history']
        
        # 티커 복구 (기존 키 덮어쓰기)
        if "tickers" in loaded_data and loaded_data['tickers']:
            config['tickers'] = {} 
            for t, opts in loaded_data['tickers'].items():
                config['tickers'][t] = migrate_options(opts)

    # 5. Secrets 강제 적용 (가장 높은 우선순위)
    try:
        if "telegram" in st.secrets:
            config['telegram']['bot_token'] = st.secrets["telegram"]["bot_token"]
            config['telegram']['chat_id'] = st.secrets["telegram"]["chat_id"]
    except: pass
    
    return config

def save_config(config):
    # 1. Cloud Save (동기 방식)
    jb_conf = get_jsonbin_config()
    if jb_conf:
        try:
            # PUT 요청은 bin_id URL 그대로 사용
            resp = requests.put(jb_conf['url'], headers=jb_conf['headers'], json=config, timeout=5)
            if resp.status_code != 200:
                write_log(f"❌ Cloud Save Error: {resp.status_code} - {resp.text}")
        except Exception as e:
            write_log(f"❌ Cloud Save Connection Error: {e}")

    # 2. Local Save
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except: pass

# ---------------------------------------------------------
# [2] 데이터 엔진 (뉴스 로직)
# ---------------------------------------------------------
def clean_title_for_check(title):
    return re.sub(r'[^a-zA-Z0-9가-힣]', '', title).lower()

def is_relevant_news(title):
    exclude_keywords = [
        'sport', 'baseball', 'football', 'soccer', 'game', 'casino', 
        'giveaway', 'lottery', 'horoscope', 'zodiac', 'celebrity', 
        'movie review', 'best deal', 'coupon', 'discount code',
        '스포츠', '야구', '축구', '연예', '방송', '드라마', '영화'
    ]
    title_lower = title.lower()
    for kw in exclude_keywords:
        if kw in title_lower: return False
    return True

def get_integrated_news(ticker, is_sec_search=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    search_urls = []
    if is_sec_search:
        search_urls.append((f"https://news.google.com/rss/search?q={ticker}+SEC+Filing+OR+8-K+OR+10-Q+when:2d&hl=en-US&gl=US&ceid=US:en", "US"))
    else:
        search_urls.append((f"https://news.google.com/rss/search?q={ticker}+stock+finance+news+when:1d&hl=en-US&gl=US&ceid=US:en", "US"))
        search_urls.append((f"https://news.google.com/rss/search?q={ticker}+주가+실적+공시+when:1d&hl=ko&gl=KR&ceid=KR:ko", "KR"))

    collected_items = []
    seen_titles = set()
    translator = GoogleTranslator(source='auto', target='ko')

    def fetch(url_tuple):
        url, region = url_tuple
        try:
            response = requests.get(url, headers=headers, timeout=3)
            root = ET.fromstring(response.content)
            for item in root.findall('.//item')[:5]: 
                try:
                    title_raw = item.find('title').text
                    title = title_raw.split(' - ')[0]
                    link = item.find('link').text
                    pubDate = item.find('pubDate').text
                    
                    if not is_relevant_news(title): continue

                    clean_t = clean_title_for_check(title)
                    if clean_t in seen_titles: continue
                    seen_titles.add(clean_t)
                    
                    dt_obj = None
                    is_breaking = False
                    try: 
                        dt_obj = datetime.strptime(pubDate.replace(' GMT', ''), '%a, %d %b %Y %H:%M:%S')
                        if (datetime.utcnow() - dt_obj) < timedelta(hours=1): is_breaking = True
                    except: pass
                    
                    if dt_obj and (datetime.utcnow() - dt_obj) > timedelta(hours=24): continue
                    date_str = dt_obj.strftime('%m/%d %H:%M') if dt_obj else "Recent"
                    
                    final_title = title
                    if region == "US":
                        try: final_title = translator.translate(title[:150]) 
                        except: final_title = title
                    
                    prefix = "🏛️" if is_sec_search else ("🇰🇷" if region == "KR" else "📰")
                    
                    collected_items.append({
                        'title_full': title, 
                        'title': f"{prefix} {final_title}", 
                        'raw_title': title, 
                        'link': link, 
                        'date': date_str,
                        'is_breaking': is_breaking,
                        'timestamp': dt_obj if dt_obj else datetime.utcnow()
                    })
                except: continue
        except: pass
    
    for url_t in search_urls: fetch(url_t)
    collected_items.sort(key=lambda x: x['timestamp'], reverse=True)
    return collected_items

def get_finviz_data(ticker):
    try:
        url = f"https://finviz.com/quote.ashx?t={ticker}"
        try: scraper = cloudscraper.create_scraper(); resp = scraper.get(url, timeout=5); text = resp.text
        except: headers = {'User-Agent': 'Mozilla/5.0'}; resp = requests.get(url, headers=headers, timeout=5); text = resp.text
        dfs = pd.read_html(text)
        data = {}
        for df in dfs:
            if 'P/E' in df.to_string():
                for i in range(0, len(df.columns), 2):
                    try:
                        keys = df.iloc[:, i]; values = df.iloc[:, i+1]
                        for k, v in zip(keys, values): data[str(k)] = str(v)
                    except: pass
        return data
    except: return {}

def get_economic_events():
    try:
        scraper = cloudscraper.create_scraper()
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
        resp = scraper.get(url)
        if resp.status_code != 200: return []
        root = ET.fromstring(resp.content)
        events = []
        translator = GoogleTranslator(source='auto', target='ko')
        for event in root.findall('event'):
            if event.find('country').text != 'USD': continue
            if event.find('impact').text not in ['High', 'Medium']: continue
            title = event.find('title').text
            try: title = translator.translate(title)
            except: pass
            events.append({
                'date': event.find('date').text,
                'time': event.find('time').text,
                'event': title,
                'impact': event.find('impact').text,
                'forecast': event.find('forecast').text or "",
                'id': f"{event.find('date').text}_{event.find('time').text}_{title}"
            })
        events.sort(key=lambda x: (x['date'], x['time']))
        return events
    except: return []

# ---------------------------------------------------------
# [3] 백그라운드 봇
# ---------------------------------------------------------
@st.cache_resource
def start_background_worker():
    for t in threading.enumerate():
        if t.name == "DeBrief_Worker": return

    def run_bot_system():
        time.sleep(1)
        cfg = load_config()
        token = cfg['telegram']['bot_token']
        chat_id = cfg['telegram']['chat_id']
        if not token: return
        
        try:
            bot = telebot.TeleBot(token)
            last_daily_sent = None
            try: bot.send_message(chat_id, "🤖 DeBrief V59 Connected\n(설정 저장소 연결됨)")
            except: pass

            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): 
                bot.reply_to(m, "🤖 *DeBrief V59*\n/on /off : 제어\n/news [티커] : 뉴스\n/list : 목록", parse_mode='Markdown')

            @bot.message_handler(commands=['on'])
            def on_cmd(m):
                c = load_config(); c['system_active'] = True; save_config(c)
                bot.reply_to(m, "🟢 감시 시작")

            @bot.message_handler(commands=['off'])
            def off_cmd(m):
                c = load_config(); c['system_active'] = False; save_config(c)
                bot.reply_to(m, "⛔ 감시 중단")

            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    items = get_integrated_news(t, False)
                    if not items: return bot.reply_to(m, "뉴스 없음")
                    msg = [f"📰 *{t} News*"]
                    for i in items[:5]: msg.append(f"▪️ `[{i['date']}]` [{i['title'].replace('[','').replace(']','')}]({i['link']})")
                    bot.reply_to(m, "\n\n".join(msg), parse_mode='Markdown', disable_web_page_preview=True)
                except: pass

            @bot.message_handler(commands=['list'])
            def list_cmd(m):
                try: c = load_config(); bot.reply_to(m, f"📋 {', '.join(c['tickers'].keys())}")
                except: pass

            @bot.message_handler(commands=['add'])
            def add_cmd(m):
                try:
                    t = m.text.split()[1].upper(); c = load_config()
                    if t not in c['tickers']: c['tickers'][t] = DEFAULT_OPTS.copy(); save_config(c); bot.reply_to(m, f"✅ {t} 저장됨")
                except: pass

            @bot.message_handler(commands=['del'])
            def del_cmd(m):
                try:
                    t = m.text.split()[1].upper(); c = load_config()
                    if t in c['tickers']: del c['tickers'][t]; save_config(c); bot.reply_to(m, f"🗑️ {t} 삭제됨")
                except: pass

            def monitor_loop():
                nonlocal last_daily_sent
                while True:
                    try:
                        cfg = load_config()
                        # 경제 일정
                        if cfg.get('eco_mode', True):
                            now = datetime.now()
                            if now.hour == 8 and last_daily_sent != now.strftime('%Y-%m-%d'):
                                events = get_economic_events(); today = now.strftime('%Y-%m-%d')
                                todays = [e for e in events if e['date'] == today]
                                if todays:
                                    msg = f"☀️ *오늘({today}) 일정*\n"
                                    for e in todays: msg += f"\n⏰ {e['time']} : {e['event']}"
                                    bot.send_message(chat_id, msg, parse_mode='Markdown'); last_daily_sent = today

                        # 뉴스 & 가격
                        if cfg.get('system_active', True) and cfg['tickers']:
                            cur_token = cfg['telegram']['bot_token']; cur_chat = cfg['telegram']['chat_id']
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items(): exe.submit(analyze_ticker, t, s, cur_token, cur_chat)
                    except Exception as e: write_log(f"Loop Err: {e}")
                    time.sleep(60)

            def analyze_ticker(ticker, settings, token, chat_id):
                if not settings.get('🟢 감시', True): return
                try:
                    # 뉴스 (몰림 방지)
                    if settings.get('📰 뉴스') or settings.get('🏛️ SEC'):
                        current_config = load_config()
                        history = current_config.get('news_history', {})
                        if ticker not in history: history[ticker] = []
                        
                        items = get_integrated_news(ticker, False)
                        updated = False
                        sent_count = 0 
                        
                        for item in items:
                            clean_t = clean_title_for_check(item['title_full'])
                            if any(clean_title_for_check(h_title) == clean_t for h_title in history[ticker]): continue
                            
                            is_sec = "SEC" in item['title'] or "8-K" in item['title']
                            should_send = (is_sec and settings.get('🏛️ SEC')) or (not is_sec and settings.get('📰 뉴스'))
                            
                            if should_send:
                                if item['is_breaking'] or sent_count < 1:
                                    prefix = "🏛️" if ("SEC" in item['title']) else ("🇰🇷" if "🇰🇷" in item['title'] else "📰")
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"🔔 {prefix} *[{ticker}]*\n`[{item['date']}]` [{item['title']}]({item['link']})", "parse_mode": "Markdown"})
                                    history[ticker].append(item['title_full'])
                                    sent_count += 1
                                    updated = True
                        
                        if updated:
                            if len(history[ticker]) > 50: history[ticker] = history[ticker][-50:]
                            current_config['news_history'] = history
                            save_config(current_config)

                    # 급등락
                    if settings.get('📈 급등락(3%)'):
                        stock = yf.Ticker(ticker); h = stock.history(period="1d")
                        if not h.empty:
                            curr = h['Close'].iloc[-1]; prev = stock.fast_info.previous_close
                            pct = ((curr - prev) / prev) * 100
                            if abs(pct) >= 3.0:
                                last = price_alert_cache.get(ticker, 0)
                                if abs(pct - last) >= 1.0:
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"🔔 *[{ticker}] {'급등 🚀' if pct>0 else '급락 📉'}*\n{pct:.2f}% (${curr:.2f})", "parse_mode": "Markdown"})
                                    price_alert_cache[ticker] = pct
                except: pass

            t_mon = threading.Thread(target=monitor_loop, daemon=True, name="DeBrief_Worker")
            t_mon.start()
            while True:
                try: bot.infinity_polling(timeout=10, long_polling_timeout=5, skip_pending=True)
                except: time.sleep(5)
        except: pass

    t_bot = threading.Thread(target=run_bot_system, daemon=True, name="DeBrief_Worker")
    t_bot.start()

start_background_worker()

# ---------------------------------------------------------
# [4] UI
# ---------------------------------------------------------
st.set_page_config(page_title="DeBrief", layout="wide", page_icon="📡")
st.markdown("""<style>
    .stApp { background-color: #FFFFFF; color: #202124; }
    .stock-card { background-color: #FFFFFF; border: 1px solid #DADCE0; border-radius: 8px; padding: 8px 5px; margin-bottom: 6px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
    .stock-symbol { font-size: 1.0em; font-weight: 800; color: #1A73E8; }
    .stock-price-box { display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 0.8em; font-weight: 700; }
    .up-theme { background-color: #E6F4EA; color: #137333; } .down-theme { background-color: #FCE8E6; color: #C5221F; }
</style>""", unsafe_allow_html=True)

config = load_config()

with st.sidebar:
    st.header("🎛️ Control Panel")
    
    # [Connection Test with Detailed Error]
    jb = get_jsonbin_config()
    if jb:
        st.success("☁️ Secrets Found")
        if st.button("Test Connection"):
            try:
                # v3 API /latest
                r = requests.get(f"{jb['url']}/latest", headers=jb['headers'])
                if r.status_code == 200: 
                    st.toast("✅ 연결 성공!"); st.write("Data:", r.json().get('record', {}).get('tickers', {}))
                else: 
                    # 에러 상세 출력
                    st.error(f"❌ 연결 실패: {r.status_code}")
                    st.code(r.text, language='json')
                    st.warning("위 에러 메시지를 확인하세요. (Unauthorized = 키 오류 / Forbidden = 권한 오류)")
            except Exception as e: st.error(f"❌ 요청 에러: {e}")
    else:
        st.error("❌ Secrets Not Found")
        st.info("Set 'jsonbin' in st.secrets")

    if st.toggle("System Power", value=config.get('system_active', True)):
        st.success("🟢 Active"); config['system_active'] = True
    else:
        st.error("⛔ Paused"); config['system_active'] = False
    save_config(config)

    with st.expander("🔑 Keys"):
        bot_t = st.text_input("Bot Token", value=config['telegram'].get('bot_token', ''), type="password")
        chat_i = st.text_input("Chat ID", value=config['telegram'].get('chat_id', ''))
        if st.button("Save Keys"):
            config['telegram'].update({"bot_token": bot_t, "chat_id": chat_i})
            save_config(config); st.rerun()

st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud (V59)</h3>", unsafe_allow_html=True)
t1, t2, t3 = st.tabs(["📊 Dashboard", "⚙️ Management", "📜 Logs"])

with t1:
    if config['tickers'] and config['system_active']:
        ticker_list = list(config['tickers'].keys())
        cols = st.columns(8)
        for i, ticker in enumerate(ticker_list):
            try:
                info = yf.Ticker(ticker).fast_info
                curr = info.last_price; chg = ((curr - info.previous_close)/info.previous_close)*100
                theme = "up-theme" if chg >= 0 else "down-theme"
                with cols[i % 8]:
                    st.markdown(f"""<div class="stock-card"><div class="stock-symbol">{ticker}</div><div class="stock-price-box {theme}">${curr:.2f} ({chg:+.2f}%)</div></div>""", unsafe_allow_html=True)
            except: pass

with t2:
    st.markdown("#### 📢 알림 설정")
    eco_mode = st.checkbox("📢 경제지표/연준 알림", value=config.get('eco_mode', True))
    if eco_mode != config.get('eco_mode', True):
        config['eco_mode'] = eco_mode; save_config(config); st.toast("저장됨")

    st.divider()
    c_all_1, c_all_2, c_blank = st.columns([1, 1, 3])
    if c_all_1.button("✅ ALL ON", use_container_width=True):
        for t in config['tickers']:
            for k in config['tickers'][t]: config['tickers'][t][k] = True
        save_config(config); st.rerun()
        
    if c_all_2.button("⛔ ALL OFF", use_container_width=True):
        for t in config['tickers']:
            for k in config['tickers'][t]: config['tickers'][t][k] = False
        save_config(config); st.rerun()

    input_t = st.text_input("Add Tickers")
    if st.button("➕ Add"):
        for t in [x.strip().upper() for x in input_t.split(',') if x.strip()]:
            config['tickers'][t] = DEFAULT_OPTS.copy()
        save_config(config); st.rerun()
    
    if config['tickers']:
        df = pd.DataFrame(config['tickers']).T
        edited = st.data_editor(df, use_container_width=True)
        if not df.equals(edited):
            config['tickers'] = edited.to_dict(orient='index')
            save_config(config); st.toast("Saved!")
            
    st.divider()
    del_cols = st.columns([4, 1])
    del_target = del_cols[0].selectbox("삭제할 종목 선택", options=list(config['tickers'].keys()))
    if del_cols[1].button("삭제"):
        if del_target in config['tickers']: del config['tickers'][del_target]; save_config(config); st.rerun()

with t3:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()[-50:]): st.text(line.strip())
