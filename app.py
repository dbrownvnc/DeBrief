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
import hashlib
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
if 'config_loaded' not in st.session_state: st.session_state['config_loaded'] = False

price_alert_cache = st.session_state['price_alert_cache']
rsi_alert_status = st.session_state['rsi_alert_status']
eco_alert_cache = st.session_state['eco_alert_cache']

# 제외할 키워드 (경제와 무관한 뉴스 필터링)
EXCLUDED_KEYWORDS = ['casino', 'sport', 'baseball', 'football', 'soccer', 'lotto', 'horoscope', 
                     '카지노', '스포츠', '야구', '축구', '로또', '운세', '연예']

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
# [1] 설정 로드/저장
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
        "news_history": {} # 저장 포맷: {ticker: [hash_key1, hash_key2, ...]}
    }
    
    url = get_jsonbin_url(); headers = get_jsonbin_headers()
    loaded_data = None
    
    if url and headers:
        try:
            resp = requests.get(f"{url}/latest", headers=headers, timeout=5)
            if resp.status_code == 200: loaded_data = resp.json()['record']
        except: pass
    
    if not loaded_data and os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f: loaded_data = json.load(f)
        except: pass

    if loaded_data:
        if "telegram" in loaded_data: config['telegram'] = loaded_data['telegram']
        if "system_active" in loaded_data: config['system_active'] = loaded_data['system_active']
        if "eco_mode" in loaded_data: config['eco_mode'] = loaded_data['eco_mode']
        if "news_history" in loaded_data: config['news_history'] = loaded_data['news_history']
        if "tickers" in loaded_data:
            for t, opts in loaded_data['tickers'].items(): config['tickers'][t] = migrate_options(opts)

    try:
        if "telegram" in st.secrets:
            config['telegram']['bot_token'] = st.secrets["telegram"]["bot_token"]
            config['telegram']['chat_id'] = st.secrets["telegram"]["chat_id"]
    except: pass
    
    return config

def save_config(config):
    url = get_jsonbin_url(); headers = get_jsonbin_headers()
    if url and headers:
        try: requests.put(url, headers=headers, json=config, timeout=5)
        except: pass
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(config, f, indent=4, ensure_ascii=False)
    except: pass

# ---------------------------------------------------------
# [2] 데이터 엔진 (수정됨)
# ---------------------------------------------------------
def get_integrated_news(ticker, is_sec_search=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    search_urls = []

    # 1. 쿼리 설정 (한국어 뉴스 포함)
    if is_sec_search:
        # SEC 공시는 영어 원문이 정확하므로 영어 쿼리 유지
        search_urls.append(f"https://news.google.com/rss/search?q={ticker}+SEC+Filing+OR+8-K+OR+10-Q+OR+10-K+when:2d&hl=en-US&gl=US&ceid=US:en")
    else:
        # 일반 뉴스는 미국 + 한국 소스 병행
        search_urls.append(f"https://news.google.com/rss/search?q={ticker}+stock+news+when:1d&hl=en-US&gl=US&ceid=US:en") # 미국
        search_urls.append(f"https://news.google.com/rss/search?q={ticker}+주가+OR+주식+when:1d&hl=ko&gl=KR&ceid=KR:ko") # 한국

    collected_items = []
    seen_titles = set() # 이번 Fetch 내 중복 제거용
    translator = GoogleTranslator(source='auto', target='ko')

    def fetch(url):
        try:
            response = requests.get(url, headers=headers, timeout=3)
            root = ET.fromstring(response.content)
            # RSS 당 상위 5개만 파싱
            for item in root.findall('.//item')[:5]: 
                try:
                    raw_title = item.find('title').text.split(' - ')[0]
                    link = item.find('link').text
                    pubDate = item.find('pubDate').text
                    
                    # 2. 필터링: 제외 키워드 확인
                    if any(bad in raw_title.lower() for bad in EXCLUDED_KEYWORDS):
                        continue
                    
                    # 3. 중복 제거 (동일 제목)
                    if raw_title in seen_titles:
                        continue
                    seen_titles.add(raw_title)
                    
                    # 4. 날짜 파싱 (RFC 2822 -> datetime)
                    dt = datetime.strptime(pubDate, '%a, %d %b %Y %H:%M:%S %Z')
                    
                    # 5. 번역 (한글이 아닌 경우만)
                    try:
                        title = translator.translate(raw_title) if not any('\uac00' <= c <= '\ud7a3' for c in raw_title) else raw_title
                    except:
                        title = raw_title
                    
                    # 6. Hash Key 생성 (제목+날짜로 고유성 보장)
                    hash_key = hashlib.md5(f"{title}{dt.date()}".encode('utf-8')).hexdigest()[:12]
                    
                    collected_items.append({
                        'title': title,
                        'link': link,
                        'date': dt.strftime('%m/%d %H:%M'),
                        'timestamp': dt,
                        'hash': hash_key
                    })
                except: pass
        except: pass

    with ThreadPoolExecutor(max_workers=len(search_urls)) as exe:
        exe.map(fetch, search_urls)

    # 7. 최신순 정렬 (속보 우선)
    collected_items.sort(key=lambda x: x['timestamp'], reverse=True)
    return collected_items[:10] # 최종 10개만 리턴

def get_economic_calendar():
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get('https://finance.yahoo.com/calendar/economic', headers=headers, timeout=3)
        scraper = cloudscraper.create_scraper()
        soup = scraper.get('https://finance.yahoo.com/calendar/economic').text
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(soup, 'html.parser')
        events = []
        for row in soup.select('tbody tr')[:5]:
            cols = row.find_all('td')
            if len(cols) >= 3:
                date_str = cols[0].text.strip()
                event_name = cols[1].text.strip()
                impact = cols[2].text.strip() if len(cols) > 2 else ""
                events.append(f"{date_str} | {event_name} ({impact})")
        return events
    except: return []

# ---------------------------------------------------------
# [3] 백그라운드 워커 (알림 로직 개선)
# ---------------------------------------------------------
def start_background_worker():
    if not st.session_state.get('worker_started', False):
        st.session_state['worker_started'] = True
        
        def run_bot_system():
            try:
                config = load_config()
                if not config['telegram']['bot_token'] or not config['telegram']['chat_id']:
                    write_log("텔레그램 설정 누락"); return
                
                bot = telebot.TeleBot(config['telegram']['bot_token'])
                bot.set_my_commands([BotCommand("start", "시스템 상태"), BotCommand("help", "도움말")])
                
                @bot.message_handler(commands=['start'])
                def send_welcome(msg): bot.reply_to(msg, f"✅ DeBrief Cloud 활성화\n📊 감시 중: {len(config['tickers'])}개 종목")
                
                @bot.message_handler(commands=['help'])
                def send_help(msg): bot.reply_to(msg, "DeBrief Cloud V57\n- 실시간 시장 알림\n- 뉴스/공시 속보\n- 기술적 신호 감지")
                
                def monitor_loop():
                    while True:
                        try:
                            cfg = load_config()
                            if not cfg['system_active']: 
                                time.sleep(60); continue
                            
                            cur_token = cfg['telegram']['bot_token']
                            cur_chat = cfg['telegram']['chat_id']
                            
                            # 경제지표 알림 (하루 1회)
                            if cfg.get('eco_mode', True):
                                now = datetime.now()
                                cache_key = f"{now.year}-{now.month}-{now.day}"
                                if cache_key not in eco_alert_cache:
                                    events = get_economic_calendar()
                                    if events:
                                        msg = "📅 오늘의 경제지표\n" + "\n".join(events[:3])
                                        try: requests.post(f"https://api.telegram.org/bot{cur_token}/sendMessage", data={"chat_id": cur_chat, "text": msg})
                                        except: pass
                                        eco_alert_cache.add(cache_key)
                                        if len(eco_alert_cache) > 7: eco_alert_cache.pop()
                            
                            # 종목별 분석
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items(): exe.submit(analyze_ticker, t, s, cur_token, cur_chat)
                        except Exception as e: write_log(f"Loop Err: {e}")
                        time.sleep(60)

                def analyze_ticker(ticker, settings, token, chat_id):
                    if not settings.get('🟢 감시', True): return
                    try:
                        # 1. 뉴스 및 공시 (개별 체크로 수정)
                        news_enabled = settings.get('📰 뉴스', False)
                        sec_enabled = settings.get('🏛️ SEC', False)
                        
                        # 둘 다 꺼져있으면 뉴스 검색 자체를 하지 않음
                        if news_enabled or sec_enabled:
                            current_config = load_config()
                            history = current_config.get('news_history', {})
                            if ticker not in history: history[ticker] = []
                            
                            # SEC 뉴스와 일반 뉴스를 각각 가져옴
                            all_items = []
                            if news_enabled:
                                all_items.extend(get_integrated_news(ticker, False))
                            if sec_enabled:
                                all_items.extend(get_integrated_news(ticker, True))
                            
                            # 중복 제거 (hash 기준)
                            seen_hashes = set()
                            unique_items = []
                            for item in all_items:
                                if item['hash'] not in seen_hashes:
                                    seen_hashes.add(item['hash'])
                                    unique_items.append(item)
                            
                            # 최신순 재정렬
                            unique_items.sort(key=lambda x: x['timestamp'], reverse=True)
                            
                            updated = False
                            sent_count_this_cycle = 0
                            
                            for item in unique_items:
                                # 이미 보낸 뉴스인지 확인
                                if item['hash'] in history[ticker] or item['link'] in history[ticker]: 
                                    continue
                                
                                # SEC 여부 판단
                                is_sec = "SEC" in item['title'] or "8-K" in item['title'] or "10-K" in item['title'] or "10-Q" in item['title']
                                
                                # 해당 항목이 켜져있을 때만 발송
                                should_send = False
                                if is_sec and sec_enabled:
                                    should_send = True
                                elif not is_sec and news_enabled:
                                    should_send = True
                                
                                if should_send:
                                    prefix = "🏛️" if is_sec else "📰"
                                    try:
                                        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                                    data={"chat_id": chat_id, 
                                                          "text": f"🔔 {prefix} *[{ticker}]*\n`[{item['date']}]` [{item['title']}]({item['link']})", 
                                                          "parse_mode": "Markdown"})
                                    except: pass
                                    
                                    # 히스토리에 Hash 추가 (중복 방지)
                                    history[ticker].append(item['hash'])
                                    if len(history[ticker]) > 50: history[ticker].pop(0)
                                    updated = True
                                    sent_count_this_cycle += 1
                                
                                # [핵심] 한 사이클당 1개만 발송
                                if sent_count_this_cycle >= 1: 
                                    break

                            if updated:
                                current_config['news_history'] = history
                                save_config(current_config)

                        # 2. 가격 (3%)
                        if settings.get('📈 급등락(3%)'):
                            stock = yf.Ticker(ticker)
                            h = stock.history(period="1d")
                            if not h.empty:
                                curr = h['Close'].iloc[-1]; prev = stock.fast_info.previous_close
                                pct = ((curr - prev) / prev) * 100
                                if abs(pct) >= 3.0:
                                    last = price_alert_cache.get(ticker, 0)
                                    if abs(pct - last) >= 1.0:
                                        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                                    data={"chat_id": chat_id, 
                                                          "text": f"🔔 *[{ticker}] {'급등 🚀' if pct>0 else '급락 📉'}*\n변동: {pct:.2f}%\n현재: ${curr:.2f}", 
                                                          "parse_mode": "Markdown"})
                                        price_alert_cache[ticker] = pct
                        
                        # 3. RSI
                        if settings.get('📉 RSI'):
                            stock = yf.Ticker(ticker)
                            h = stock.history(period="1mo")
                            if not h.empty:
                                delta = h['Close'].diff()
                                gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                                loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                                rs = gain / loss
                                rsi = 100 - (100 / (1 + rs)).iloc[-1]
                                status = rsi_alert_status.get(ticker, "NORMAL")
                                if rsi >= 70 and status != "OB": 
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                                data={"chat_id": chat_id, "text": f"🔥 [{ticker}] RSI 과매수 ({rsi:.1f})"})
                                    rsi_alert_status[ticker] = "OB"
                                elif rsi <= 30 and status != "OS": 
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                                data={"chat_id": chat_id, "text": f"💧 [{ticker}] RSI 과매도 ({rsi:.1f})"})
                                    rsi_alert_status[ticker] = "OS"
                                elif 35 < rsi < 65: 
                                    rsi_alert_status[ticker] = "NORMAL"
                    except Exception as e: 
                        write_log(f"Ticker {ticker} Err: {e}")

                t_mon = threading.Thread(target=monitor_loop, daemon=True, name="DeBrief_Worker")
                t_mon.start()
                
                while True:
                    try: bot.infinity_polling(timeout=10, long_polling_timeout=5, skip_pending=True)
                    except: time.sleep(5)

            except Exception as e: write_log(f"Bot Error: {e}")

        t_bot = threading.Thread(target=run_bot_system, daemon=True, name="DeBrief_Worker")
        t_bot.start()

start_background_worker()

# ---------------------------------------------------------
# [4] UI (개선된 버전)
# ---------------------------------------------------------
st.set_page_config(page_title="DeBrief", layout="wide", page_icon="📡")
st.markdown("""<style>
    .stApp { background-color: #FFFFFF; color: #202124; }
    .stock-card { background-color: #FFFFFF; border: 1px solid #DADCE0; border-radius: 8px; padding: 8px 5px; margin-bottom: 6px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
    .stock-symbol { font-size: 1.0em; font-weight: 800; color: #1A73E8; }
    .stock-price-box { display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 0.8em; font-weight: 700; }
    .up-theme { background-color: #E6F4EA; color: #137333; } .down-theme { background-color: #FCE8E6; color: #C5221F; }
</style>""", unsafe_allow_html=True)

# 설정 로드 (한 번만)
if 'config' not in st.session_state or not st.session_state.config_loaded:
    st.session_state.config = load_config()
    st.session_state.config_loaded = True

config = st.session_state.config

with st.sidebar:
    st.header("🎛️ Control Panel")
    if "jsonbin" in st.secrets: st.success("☁️ Cloud Connected")
    
    system_active = st.toggle("System Power", value=config.get('system_active', True), key='system_toggle')
    if system_active != config.get('system_active'):
        config['system_active'] = system_active
        save_config(config)
        if system_active:
            st.success("🟢 Active")
        else:
            st.error("⛔ Paused")

    with st.expander("🔑 Keys"):
        bot_t = st.text_input("Bot Token", value=config['telegram'].get('bot_token', ''), type="password", key='bot_token_input')
        chat_i = st.text_input("Chat ID", value=config['telegram'].get('chat_id', ''), key='chat_id_input')
        if st.button("Save Keys", key='save_keys_btn'):
            config['telegram'].update({"bot_token": bot_t, "chat_id": chat_i})
            save_config(config)
            st.success("저장 완료!")
            time.sleep(1)
            st.rerun()

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
    eco_mode = st.checkbox("📢 경제지표/연준 알림", value=config.get('eco_mode', True), key='eco_mode_checkbox')
    if eco_mode != config.get('eco_mode', True):
        config['eco_mode'] = eco_mode
        save_config(config)
        st.success("저장됨")

    st.divider()
    
    # 종목 추가
    st.markdown("#### ➕ 종목 추가")
    input_t = st.text_input("Add Tickers (쉼표로 구분)", key='add_ticker_input')
    if st.button("➕ Add", key='add_ticker_btn'):
        added = []
        for t in [x.strip().upper() for x in input_t.split(',') if x.strip()]:
            if t not in config['tickers']:
                config['tickers'][t] = DEFAULT_OPTS.copy()
                added.append(t)
        if added:
            save_config(config)
            st.success(f"추가됨: {', '.join(added)}")
            time.sleep(1)
            st.rerun()
    
    st.divider()
    
    # 일괄 설정
    st.markdown("#### 🎛️ 일괄 설정")
    c_all_1, c_all_2 = st.columns(2)
    if c_all_1.button("✅ 전체 켜기", use_container_width=True, key='all_on_btn'):
        for t in config['tickers']:
            for k in config['tickers'][t]: 
                config['tickers'][t][k] = True
        save_config(config)
        st.success("모든 알림 활성화!")
        time.sleep(1)
        st.rerun()
        
    if c_all_2.button("⛔ 전체 끄기", use_container_width=True, key='all_off_btn'):
        for t in config['tickers']:
            for k in config['tickers'][t]: 
                config['tickers'][t][k] = False
        save_config(config)
        st.warning("모든 알림 비활성화!")
        time.sleep(1)
        st.rerun()
    
    st.divider()
    
    # 개별 종목 설정 (체크박스로 변경)
    st.markdown("#### 📋 종목별 알림 설정")
    
    if config['tickers']:
        for ticker in sorted(config['tickers'].keys()):
            with st.expander(f"**{ticker}**", expanded=False):
                settings = config['tickers'][ticker]
                
                # 각 옵션별 체크박스
                col1, col2 = st.columns(2)
                
                option_keys = list(DEFAULT_OPTS.keys())
                changed = False
                
                for i, opt in enumerate(option_keys):
                    current_value = settings.get(opt, DEFAULT_OPTS[opt])
                    
                    if i % 2 == 0:
                        new_value = col1.checkbox(opt, value=current_value, key=f"{ticker}_{opt}")
                    else:
                        new_value = col2.checkbox(opt, value=current_value, key=f"{ticker}_{opt}")
                    
                    if new_value != current_value:
                        settings[opt] = new_value
                        changed = True
                
                # 삭제 버튼
                if st.button(f"🗑️ {ticker} 삭제", key=f"delete_{ticker}", type="secondary"):
                    del config['tickers'][ticker]
                    save_config(config)
                    st.warning(f"{ticker} 삭제됨")
                    time.sleep(1)
                    st.rerun()
                
                # 변경사항이 있을 때만 저장
                if changed:
                    config['tickers'][ticker] = settings
                    save_config(config)
                    st.success(f"{ticker} 설정 저장됨")

with t3:
    st.markdown("#### 📜 최근 로그")
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in reversed(lines[-50:]):
                st.text(line.strip())
    else:
        st.info("로그 파일이 없습니다.")
