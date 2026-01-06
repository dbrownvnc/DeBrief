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
# [0] 로그 및 유틸
# ---------------------------------------------------------
def write_log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {msg}\n")
    except: pass

# ---------------------------------------------------------
# [1] 설정 로드/저장 (클라우드 강제 동기화)
# ---------------------------------------------------------
def get_jsonbin_config():
    """Secrets에서 설정 가져오기 및 검증"""
    try:
        if "jsonbin" in st.secrets:
            return {
                'url': f"https://api.jsonbin.io/v3/b/{st.secrets['jsonbin']['bin_id']}",
                'headers': {
                    'Content-Type': 'application/json',
                    'X-Master-Key': st.secrets['jsonbin']['master_key']
                }
            }
    except Exception as e:
        write_log(f"Secrets Error: {e}")
    return None

DEFAULT_OPTS = {
    "🟢 감시": True, "📰 뉴스": True, "🏛️ SEC": True, 
    "📈 급등락(3%)": True, "📊 거래량(2배)": False, 
    "🚀 신고가": True, "📉 RSI": False
}

def migrate_options(old_opts):
    new_opts = DEFAULT_OPTS.copy()
    mapping = {
        "감시_ON": "🟢 감시", "뉴스": "📰 뉴스", "SEC": "🏛️ SEC",
        "가격_3%": "📈 급등락(3%)", "거래량_2배": "📊 거래량(2배)",
        "52주_신고가": "🚀 신고가", "RSI": "📉 RSI"
    }
    for old_k, val in old_opts.items():
        if old_k in mapping: new_opts[mapping[old_k]] = val
        elif old_k in new_opts: new_opts[old_k] = val
    return new_opts

def load_config():
    # 1. 기본 템플릿
    config = {
        "system_active": True,
        "eco_mode": True,
        "telegram": {"bot_token": "", "chat_id": ""}, 
        "tickers": { "TSLA": DEFAULT_OPTS.copy(), "NVDA": DEFAULT_OPTS.copy() },
        "news_history": {}
    }
    
    jb = get_jsonbin_config()
    loaded_data = None
    
    # 2. 클라우드 로드 시도 (우선순위 최상)
    if jb:
        try:
            resp = requests.get(f"{jb['url']}/latest", headers=jb['headers'], timeout=5)
            if resp.status_code == 200:
                loaded_data = resp.json().get('record')
                write_log("☁️ Cloud Config Loaded")
            else:
                write_log(f"☁️ Cloud Load Failed: {resp.status_code}")
        except Exception as e:
            write_log(f"☁️ Cloud Err: {e}")
    
    # 3. 로컬 로드 (클라우드 실패 시 백업)
    if not loaded_data and os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
                write_log("📂 Local Config Loaded")
        except: pass

    # 4. 데이터 병합
    if loaded_data:
        if "telegram" in loaded_data: config['telegram'] = loaded_data['telegram']
        if "system_active" in loaded_data: config['system_active'] = loaded_data['system_active']
        if "eco_mode" in loaded_data: config['eco_mode'] = loaded_data['eco_mode']
        if "news_history" in loaded_data: config['news_history'] = loaded_data['news_history']
        if "tickers" in loaded_data:
            config['tickers'] = {}
            for t, opts in loaded_data['tickers'].items():
                config['tickers'][t] = migrate_options(opts)

    # 5. Secrets 키 강제 적용
    try:
        if "telegram" in st.secrets:
            config['telegram']['bot_token'] = st.secrets["telegram"]["bot_token"]
            config['telegram']['chat_id'] = st.secrets["telegram"]["chat_id"]
    except: pass
    
    return config

def save_config(config):
    jb = get_jsonbin_config()
    success = False
    
    # 1. 클라우드 저장
    if jb:
        try:
            resp = requests.put(jb['url'], headers=jb['headers'], json=config, timeout=5)
            if resp.status_code == 200: success = True
        except: pass
        
    # 2. 로컬 백업 저장
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except: pass
    
    return success

# ---------------------------------------------------------
# [2] 데이터 엔진
# ---------------------------------------------------------
def clean_title_for_check(title):
    return re.sub(r'[^a-zA-Z0-9가-힣]', '', title).lower()

def is_relevant_news(title):
    exclude = ['sport', 'game', 'casino', 'coupon', 'deal', 'zodiac', 'football', 'soccer', 'baseball', '스포츠', '야구', '축구', '로또']
    t_low = title.lower()
    for kw in exclude:
        if kw in t_low: return False
    return True

def get_integrated_news(ticker, is_sec_search=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    search_urls = []
    if is_sec_search:
        search_urls.append((f"https://news.google.com/rss/search?q={ticker}+SEC+Filing+OR+8-K+OR+10-Q+when:2d&hl=en-US&gl=US&ceid=US:en", "US"))
    else:
        search_urls.append((f"https://news.google.com/rss/search?q={ticker}+stock+finance+news+when:1d&hl=en-US&gl=US&ceid=US:en", "US"))
        search_urls.append((f"https://news.google.com/rss/search?q={ticker}+주가+실적+공시+when:1d&hl=ko&gl=KR&ceid=KR:ko", "KR"))

    items = []
    seen = set()
    trans = GoogleTranslator(source='auto', target='ko')

    def fetch(url_tuple):
        url, region = url_tuple
        try:
            resp = requests.get(url, headers=headers, timeout=3)
            root = ET.fromstring(resp.content)
            for item in root.findall('.//item')[:5]:
                try:
                    title = item.find('title').text.split(' - ')[0]
                    if not is_relevant_news(title): continue
                    
                    clean = clean_title_for_check(title)
                    if clean in seen: continue
                    seen.add(clean)
                    
                    pubDate = item.find('pubDate').text
                    dt = datetime.strptime(pubDate.replace(' GMT',''), '%a, %d %b %Y %H:%M:%S')
                    if (datetime.utcnow() - dt) > timedelta(hours=24): continue
                    
                    is_breaking = (datetime.utcnow() - dt) < timedelta(hours=1)
                    
                    display_title = title
                    if region == "US":
                        try: display_title = trans.translate(title[:150])
                        except: pass
                    
                    prefix = "🏛️" if is_sec_search else ("🇰🇷" if region == "KR" else "📰")
                    items.append({
                        'title_full': title, 'title': f"{prefix} {display_title}",
                        'link': item.find('link').text, 'date': dt.strftime('%m/%d %H:%M'),
                        'is_breaking': is_breaking, 'timestamp': dt
                    })
                except: continue
        except: pass
    
    for u in search_urls: fetch(u)
    items.sort(key=lambda x: x['timestamp'], reverse=True)
    return items

def get_economic_events():
    try:
        scraper = cloudscraper.create_scraper()
        resp = scraper.get("https://nfs.faireconomy.media/ff_calendar_thisweek.xml")
        root = ET.fromstring(resp.content)
        events = []
        trans = GoogleTranslator(source='auto', target='ko')
        for e in root.findall('event'):
            if e.find('country').text == 'USD' and e.find('impact').text in ['High', 'Medium']:
                try: title = trans.translate(e.find('title').text)
                except: title = e.find('title').text
                events.append({
                    'date': e.find('date').text, 'time': e.find('time').text,
                    'event': title, 'forecast': e.find('forecast').text or ""
                })
        return sorted(events, key=lambda x: (x['date'], x['time']))
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
        token = cfg['telegram']['bot_token']; chat_id = cfg['telegram']['chat_id']
        if not token: return
        
        try:
            bot = telebot.TeleBot(token)
            last_sent = None
            
            try: bot.send_message(chat_id, "🤖 *DeBrief V58 재가동*\n클라우드 설정 확인 중...")
            except: pass

            @bot.message_handler(commands=['start', 'help'])
            def start(m): bot.reply_to(m, "🤖 DeBrief V58 Running")

            @bot.message_handler(commands=['news'])
            def news(m):
                try:
                    t = m.text.split()[1].upper()
                    items = get_integrated_news(t)
                    if not items: return bot.reply_to(m, "뉴스 없음")
                    msg = "\n\n".join([f"▪️ `[{i['date']}]` [{i['title']}]({i['link']})" for i in items[:5]])
                    bot.reply_to(m, f"📰 *{t} 뉴스*\n{msg}", parse_mode='Markdown', disable_web_page_preview=True)
                except: pass

            # 기본 커맨드 핸들러들 생략 (이전과 동일하다고 가정)

            def monitor_loop():
                nonlocal last_sent
                while True:
                    try:
                        cfg = load_config()
                        # 경제 알림 (매일 아침 8시)
                        if cfg.get('eco_mode', True):
                            now = datetime.now()
                            today = now.strftime('%Y-%m-%d')
                            if now.hour == 8 and last_sent != today:
                                evs = [e for e in get_economic_events() if e['date'] == today]
                                if evs:
                                    msg = f"☀️ *오늘({today}) 주요 일정*\n" + "\n".join([f"⏰ {e['time']} : {e['event']} ({e['forecast']})" for e in evs])
                                    bot.send_message(chat_id, msg, parse_mode='Markdown'); last_sent = today
                        
                        # 뉴스 모니터링
                        if cfg.get('system_active', True) and cfg['tickers']:
                            cur_t = cfg['telegram']['bot_token']; cur_c = cfg['telegram']['chat_id']
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items(): exe.submit(analyze, t, s, cur_t, cur_c)
                    except: pass
                    time.sleep(60)

            def analyze(ticker, settings, token, chat_id):
                if not settings.get('🟢 감시', True): return
                try:
                    # 뉴스 로직 (몰림 방지)
                    if settings.get('📰 뉴스') or settings.get('🏛️ SEC'):
                        curr_cfg = load_config() # 최신 로드
                        hist = curr_cfg.get('news_history', {})
                        if ticker not in hist: hist[ticker] = []
                        
                        items = get_integrated_news(ticker)
                        updated = False; sent_cnt = 0
                        
                        for item in items:
                            clean_t = clean_title_for_check(item['title_full'])
                            if any(clean_title_for_check(h) == clean_t for h in hist[ticker]): continue
                            
                            is_sec = "SEC" in item['title_full']
                            should = (is_sec and settings.get('🏛️ SEC')) or (not is_sec and settings.get('📰 뉴스'))
                            
                            if should and (item['is_breaking'] or sent_cnt < 1):
                                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                              data={"chat_id": chat_id, "text": f"🔔 {item['title']}\n`[{item['date']}]` [링크]({item['link']})", "parse_mode": "Markdown"})
                                hist[ticker].append(item['title_full']); updated = True; sent_cnt += 1
                        
                        if updated:
                            if len(hist[ticker]) > 50: hist[ticker] = hist[ticker][-50:]
                            curr_cfg['news_history'] = hist
                            save_config(curr_cfg) # 클라우드 저장
                            
                    # 가격 로직
                    if settings.get('📈 급등락(3%)'):
                        info = yf.Ticker(ticker).fast_info
                        pct = ((info.last_price - info.previous_close)/info.previous_close)*100
                        if abs(pct) >= 3.0:
                             requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                           data={"chat_id": chat_id, "text": f"🔔 *[{ticker}] {pct:.2f}% 변동*\n현재: ${info.last_price:.2f}", "parse_mode": "Markdown"})
                except: pass

            t_mon = threading.Thread(target=monitor_loop, daemon=True, name="DeBrief_Worker"); t_mon.start()
            while True: 
                try: bot.infinity_polling(timeout=10, skip_pending=True)
                except: time.sleep(5)
        except: pass

    t_bot = threading.Thread(target=run_bot_system, daemon=True, name="DeBrief_Worker"); t_bot.start()

start_background_worker()

# ---------------------------------------------------------
# [4] UI
# ---------------------------------------------------------
st.set_page_config(page_title="DeBrief Cloud", layout="wide", page_icon="📡")
config = load_config()

with st.sidebar:
    st.header("🎛️ Control Panel")
    
    # [상태 표시 핵심]
    jb_conf = get_jsonbin_config()
    if jb_conf:
        st.success(f"☁️ Cloud Connected\n(Bin ID: {st.secrets['jsonbin']['bin_id'][:6]}...)")
        if st.button("🔄 Force Save to Cloud"):
            if save_config(config): st.toast("Saved to Cloud!")
            else: st.error("Save Failed")
    else:
        st.error("⚠️ Local Mode (Data Risk)")
        st.info("Set 'jsonbin' in st.secrets to save data.")

    if st.toggle("System Power", value=config.get('system_active', True)):
        config['system_active'] = True
    else:
        config['system_active'] = False
    save_config(config)

st.title("📡 DeBrief Dashboard V58")
t1, t2 = st.tabs(["Main", "Config"])

with t1:
    if config['tickers']:
        cols = st.columns(8)
        for i, t in enumerate(config['tickers']):
            try:
                p = yf.Ticker(t).fast_info.last_price
                cols[i%8].metric(t, f"${p:.2f}")
            except: cols[i%8].metric(t, "N/A")

with t2:
    new_t = st.text_input("Add Ticker")
    if st.button("Add"):
        config['tickers'][new_t.upper()] = DEFAULT_OPTS.copy()
        save_config(config); st.rerun()
        
    st.json(config['tickers'])
