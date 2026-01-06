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
if 'eco_alert_cache' not in st.session_state: st.session_state['eco_alert_cache'] = set()

price_alert_cache = st.session_state['price_alert_cache']
rsi_alert_status = st.session_state['rsi_alert_status']
eco_alert_cache = st.session_state['eco_alert_cache']

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
# [1] 설정 로드/저장 (클라우드 동기화 강화)
# ---------------------------------------------------------
def get_jsonbin_headers():
    try:
        if "jsonbin" in st.secrets:
            return {'Content-Type': 'application/json', 'X-Master-Key': st.secrets["jsonbin"]["master_key"]}
    except: pass
    return None

def get_jsonbin_url():
    try:
        if "jsonbin" in st.secrets:
            bin_id = st.secrets["jsonbin"]["bin_id"]
            return f"https://api.jsonbin.io/v3/b/{bin_id}"
    except: pass
    return None

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

def load_config():
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
    
    url = get_jsonbin_url()
    headers = get_jsonbin_headers()
    loaded_data = None
    
    # 1. Cloud Load (우선순위)
    if url and headers:
        try:
            resp = requests.get(f"{url}/latest", headers=headers, timeout=5)
            if resp.status_code == 200:
                loaded_data = resp.json()['record']
        except: pass
    
    # 2. Local Backup Load (Cloud 실패 시)
    if not loaded_data and os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
        except: pass

    if loaded_data:
        # 데이터 병합 (기존 키 보존)
        if "telegram" in loaded_data: config['telegram'] = loaded_data['telegram']
        if "system_active" in loaded_data: config['system_active'] = loaded_data['system_active']
        if "eco_mode" in loaded_data: config['eco_mode'] = loaded_data['eco_mode']
        if "news_history" in loaded_data: config['news_history'] = loaded_data['news_history']
        if "tickers" in loaded_data:
            # 티커 목록은 덮어쓰기 하되 옵션은 마이그레이션
            config['tickers'] = {}
            for t, opts in loaded_data['tickers'].items():
                config['tickers'][t] = migrate_options(opts)

    # 3. Secrets Overwrite (보안 키)
    try:
        if "telegram" in st.secrets:
            config['telegram']['bot_token'] = st.secrets["telegram"]["bot_token"]
            config['telegram']['chat_id'] = st.secrets["telegram"]["chat_id"]
    except: pass
    
    return config

def save_config(config):
    # Cloud Save
    url = get_jsonbin_url()
    headers = get_jsonbin_headers()
    if url and headers:
        try: requests.put(url, headers=headers, json=config, timeout=5)
        except: pass
    # Local Save
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except: pass

# ---------------------------------------------------------
# [2] 데이터 엔진 (한국 뉴스 + 필터링 강화)
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
        if kw in title_lower:
            return False
    return True

def get_integrated_news(ticker, is_sec_search=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # [수정] 한국 뉴스 소스 추가
    search_urls = []
    if is_sec_search:
        search_urls.append((f"https://news.google.com/rss/search?q={ticker}+SEC+Filing+OR+8-K+OR+10-Q+when:2d&hl=en-US&gl=US&ceid=US:en", "US"))
    else:
        # 미국 뉴스
        search_urls.append((f"https://news.google.com/rss/search?q={ticker}+stock+finance+news+when:1d&hl=en-US&gl=US&ceid=US:en", "US"))
        # 한국 뉴스
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

                    # 제목 기반 중복 제거
                    clean_t = clean_title_for_check(title)
                    if clean_t in seen_titles: continue
                    seen_titles.add(clean_t)
                    
                    dt_obj = None
                    is_breaking = False
                    try: 
                        dt_obj = datetime.strptime(pubDate.replace(' GMT', ''), '%a, %d %b %Y %H:%M:%S')
                        # 1시간 이내 = 속보
                        if (datetime.utcnow() - dt_obj) < timedelta(hours=1):
                            is_breaking = True
                    except: pass
                    
                    if dt_obj and (datetime.utcnow() - dt_obj) > timedelta(hours=24): continue
                    date_str = dt_obj.strftime('%m/%d %H:%M') if dt_obj else "Recent"
                    
                    # 한국 뉴스는 번역 생략, 미국 뉴스는 번역
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
    
    # 최신순 정렬
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
            last_weekly_sent = None
            last_daily_sent = None
            try: bot.send_message(chat_id, "🤖 DeBrief V57 Updated\n- 🇰🇷 한국 뉴스 통합\n- 🛡️ 알림 폭주 방지\n- 💾 설정 저장 강화")
            except: pass

            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): 
                msg = "🤖 *DeBrief V57*\n/on /off : 시스템 제어\n/news [티커] : 한/미 뉴스\n/list : 감시 목록\n/add [티커] : 추가"
                bot.reply_to(m, msg, parse_mode='Markdown')

            @bot.message_handler(commands=['on'])
            def on_cmd(m):
                c = load_config(); c['system_active'] = True; save_config(c)
                bot.reply_to(m, "🟢 시스템 가동")

            @bot.message_handler(commands=['off'])
            def off_cmd(m):
                c = load_config(); c['system_active'] = False; save_config(c)
                bot.reply_to(m, "⛔ 시스템 정지")

            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    items = get_integrated_news(t, False)
                    if not items: return bot.reply_to(m, "뉴스 없음")
                    msg = [f"📰 *{t} News (한/미)*"]
                    for i in items[:5]: msg.append(f"▪️ `[{i['date']}]` [{i['title'].replace('[','').replace(']','')}]({i['link']})")
                    bot.reply_to(m, "\n\n".join(msg), parse_mode='Markdown', disable_web_page_preview=True)
                except: pass

            @bot.message_handler(commands=['list'])
            def list_cmd(m):
                try: c = load_config(); bot.reply_to(m, f"📋 목록: {', '.join(c['tickers'].keys())}")
                except: pass

            @bot.message_handler(commands=['add'])
            def add_cmd(m):
                try:
                    t = m.text.split()[1].upper(); c = load_config()
                    if t not in c['tickers']: c['tickers'][t] = DEFAULT_OPTS.copy(); save_config(c); bot.reply_to(m, f"✅ {t} 추가됨 (클라우드 저장)")
                except: pass

            @bot.message_handler(commands=['del'])
            def del_cmd(m):
                try:
                    t = m.text.split()[1].upper(); c = load_config()
                    if t in c['tickers']: del c['tickers'][t]; save_config(c); bot.reply_to(m, f"🗑️ {t} 삭제됨")
                except: pass

            def monitor_loop():
                nonlocal last_weekly_sent, last_daily_sent
                while True:
                    try:
                        cfg = load_config()
                        # [경제 일정 로직 생략 - 기존과 동일]
                        if cfg.get('eco_mode', True):
                            now = datetime.now()
                            if now.hour == 8 and last_daily_sent != now.strftime('%Y-%m-%d'):
                                events = get_economic_events(); today = now.strftime('%Y-%m-%d')
                                todays = [e for e in events if e['date'] == today]
                                if todays:
                                    msg = f"☀️ *오늘({today}) 주요 일정*\n────────────────"
                                    for e in todays: msg += f"\n⏰ {e['time']} : {e['event']} (예상:{e['forecast']})"
                                    bot.send_message(chat_id, msg, parse_mode='Markdown'); last_daily_sent = today

                        if cfg.get('system_active', True) and cfg['tickers']:
                            cur_token = cfg['telegram']['bot_token']; cur_chat = cfg['telegram']['chat_id']
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items(): exe.submit(analyze_ticker, t, s, cur_token, cur_chat)
                    except Exception as e: write_log(f"Loop Err: {e}")
                    time.sleep(60) # 1분 대기

            def analyze_ticker(ticker, settings, token, chat_id):
                if not settings.get('🟢 감시', True): return
                try:
                    # [뉴스 처리 - 몰림 방지 및 한/미 통합]
                    if settings.get('📰 뉴스') or settings.get('🏛️ SEC'):
                        current_config = load_config()
                        history = current_config.get('news_history', {})
                        if ticker not in history: history[ticker] = []
                        
                        items = get_integrated_news(ticker, False)
                        updated = False
                        sent_count = 0 # 이번 루프 전송 카운트
                        
                        for item in items:
                            clean_t = clean_title_for_check(item['title_full'])
                            # 이미 보낸 뉴스 건너뛰기
                            if any(clean_title_for_check(h_title) == clean_t for h_title in history[ticker]):
                                continue
                            
                            is_sec = "SEC" in item['title'] or "8-K" in item['title']
                            should_send = (is_sec and settings.get('🏛️ SEC')) or (not is_sec and settings.get('📰 뉴스'))
                            
                            if should_send:
                                # [몰림 방지 핵심]
                                # 속보(1시간 이내)는 무제한 전송, 일반 뉴스는 루프당(1분당) 1개만 전송
                                if item['is_breaking'] or sent_count < 1:
                                    prefix = "🏛️" if ("SEC" in item['title']) else ("🇰🇷" if "🇰🇷" in item['title'] else "📰")
                                    msg = f"🔔 {prefix} *[{ticker}]*\n`[{item['date']}]` [{item['title']}]({item['link']})"
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"})
                                    
                                    history[ticker].append(item['title_full'])
                                    sent_count += 1
                                    updated = True
                        
                        if updated:
                            if len(history[ticker]) > 50: history[ticker] = history[ticker][-50:]
                            current_config['news_history'] = history
                            save_config(current_config)

                    # [가격 급등락]
                    if settings.get('📈 급등락(3%)'):
                        stock = yf.Ticker(ticker); h = stock.history(period="1d")
                        if not h.empty:
                            curr = h['Close'].iloc[-1]; prev = stock.fast_info.previous_close
                            pct = ((curr - prev) / prev) * 100
                            if abs(pct) >= 3.0:
                                last = price_alert_cache.get(ticker, 0)
                                if abs(pct - last) >= 1.0:
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"🔔 *[{ticker}] {'급등 🚀' if pct>0 else '급락 📉'}*\n변동: {pct:.2f}%\n현재: ${curr:.2f}", "parse_mode": "Markdown"})
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
    
    # [리셋 방지 안내]
    if "jsonbin" in st.secrets: 
        st.success("☁️ Cloud Active (저장됨)")
    else:
        st.warning("⚠️ Local Mode (리셋 위험)")
        st.info("secrets에 jsonbin 설정을 추가해야 재부팅 시 데이터가 유지됩니다.")

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

st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud (V57)</h3>", unsafe_allow_html=True)
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
