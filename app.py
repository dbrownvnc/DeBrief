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

price_alert_cache = st.session_state['price_alert_cache']
rsi_alert_status = st.session_state['rsi_alert_status']
eco_alert_cache = st.session_state['eco_alert_cache']

# 제외할 키워드
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
        "news_history": {} 
    }
    
    url = get_jsonbin_url(); headers = get_jsonbin_headers()
    loaded_data = None
    
    # 1. Cloud Load
    if url and headers:
        try:
            resp = requests.get(f"{url}/latest", headers=headers, timeout=5)
            if resp.status_code == 200: loaded_data = resp.json()['record']
        except: pass
    
    # 2. Local Load (if cloud failed or empty)
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
# [2] 데이터 엔진
# ---------------------------------------------------------
def get_integrated_news(ticker, is_sec_search=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    search_urls = []

    if is_sec_search:
        search_urls.append(f"https://news.google.com/rss/search?q={ticker}+SEC+Filing+OR+8-K+OR+10-Q+OR+10-K+when:2d&hl=en-US&gl=US&ceid=US:en")
    else:
        search_urls.append(f"https://news.google.com/rss/search?q={ticker}+stock+news+when:1d&hl=en-US&gl=US&ceid=US:en")
        search_urls.append(f"https://news.google.com/rss/search?q={ticker}+주가+OR+주식+when:1d&hl=ko&gl=KR&ceid=KR:ko")

    collected_items = []
    seen_titles = set()
    translator = GoogleTranslator(source='auto', target='ko')

    def fetch(url):
        try:
            response = requests.get(url, headers=headers, timeout=3)
            root = ET.fromstring(response.content)
            for item in root.findall('.//item')[:5]: 
                try:
                    raw_title = item.find('title').text.split(' - ')[0]
                    link = item.find('link').text
                    pubDate = item.find('pubDate').text
                    
                    if any(bad in raw_title.lower() for bad in EXCLUDED_KEYWORDS): continue

                    dt_obj = None
                    try: dt_obj = datetime.strptime(pubDate.replace(' GMT', ''), '%a, %d %b %Y %H:%M:%S')
                    except: pass
                    
                    if dt_obj and (datetime.utcnow() - dt_obj) > timedelta(hours=24): continue
                    date_str = dt_obj.strftime('%m/%d %H:%M') if dt_obj else "Recent"
                    
                    if raw_title in seen_titles: continue
                    seen_titles.add(raw_title)

                    title_ko = raw_title
                    if not any("\u3131" <= char <= "\u3163" or "\uac00" <= char <= "\ud7a3" for char in raw_title):
                        try: title_ko = translator.translate(raw_title[:150]) 
                        except: pass
                    
                    prefix = "🏛️" if is_sec_search else "📰"
                    unique_str = f"{raw_title}_{date_str}"
                    unique_hash = hashlib.md5(unique_str.encode()).hexdigest()

                    collected_items.append({
                        'title': f"{prefix} {title_ko}", 
                        'raw_title': raw_title, 
                        'link': link, 
                        'date': date_str,
                        'dt_obj': dt_obj if dt_obj else datetime.now(),
                        'hash': unique_hash
                    })
                except Exception as e: continue
        except: pass

    for url in search_urls: fetch(url)
    collected_items.sort(key=lambda x: x['dt_obj'], reverse=True)
    return collected_items

def get_finviz_data(ticker):
    try:
        url = f"https://finviz.com/quote.ashx?t={ticker}"
        try:
            scraper = cloudscraper.create_scraper()
            resp = scraper.get(url, timeout=5)
            text = resp.text
        except:
            headers = {'User-Agent': 'Mozilla/5.0'}
            resp = requests.get(url, headers=headers, timeout=5)
            text = resp.text
        dfs = pd.read_html(text)
        data = {}
        for df in dfs:
            if 'P/E' in df.to_string() or 'Market Cap' in df.to_string():
                if len(df.columns) > 1:
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
                'previous': event.find('previous').text or "",
                'actual': "", 
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
        write_log("🤖 봇 시스템 시작...")
        cfg = load_config()
        token = cfg['telegram']['bot_token']
        chat_id = cfg['telegram']['chat_id']
        if not token: return
        
        try:
            bot = telebot.TeleBot(token)
            last_weekly_sent = None
            last_daily_sent = None
            try: bot.send_message(chat_id, "🤖 DeBrief V56 (System OK) 가동")
            except: pass

            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): 
                msg = ("🤖 *DeBrief V56*\n/on : 켜기\n/off : 끄기\n/list : 목록\n/p [티커] : 가격")
                bot.reply_to(m, msg, parse_mode='Markdown')

            @bot.message_handler(commands=['on'])
            def on_cmd(m):
                c = load_config(); c['system_active'] = True; save_config(c)
                bot.reply_to(m, "🟢 시스템 가동")

            @bot.message_handler(commands=['off'])
            def off_cmd(m):
                c = load_config(); c['system_active'] = False; save_config(c)
                bot.reply_to(m, "⛔ 시스템 정지")

            @bot.message_handler(commands=['earning', '실적'])
            def earning_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    data = get_finviz_data(t)
                    msg = ""
                    if 'Earnings' in data and data['Earnings'] != '-':
                        e_date = data['Earnings'].replace(' BMO','').replace(' AMC','')
                        msg = f"📅 *{t} 실적 발표*\n🗓️ 일시: `{e_date}`\nℹ️ 출처: Finviz"
                    if msg: bot.reply_to(m, msg, parse_mode='Markdown')
                    else: bot.reply_to(m, f"❌ {t}: 정보 없음.")
                except: bot.reply_to(m, "오류 발생")

            @bot.message_handler(commands=['eco'])
            def eco_cmd(m):
                try:
                    bot.send_chat_action(m.chat.id, 'typing')
                    events = get_economic_events()
                    if not events: return bot.reply_to(m, "❌ 일정 없음")
                    msg = "📅 *주요 경제 일정 (USD)*\n────────────────"
                    c=0
                    for e in events:
                        icon = "🔥" if e['impact'] == 'High' else "🔸"
                        msg += f"\n{icon} `{e['date']} {e['time']}`\n*{e['event']}*\n"
                        c+=1; 
                        if c>=10: break
                    bot.reply_to(m, msg, parse_mode='Markdown')
                except: pass

            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    items = get_integrated_news(t, False)
                    if not items: return bot.reply_to(m, "뉴스 없음")
                    msg = [f"📰 *{t} News (최신)*"]
                    for i in items[:5]:
                        msg.append(f"▪️ `[{i['date']}]` [{i['title'].replace('[','').replace(']','')}]({i['link']})")
                    bot.reply_to(m, "\n\n".join(msg), parse_mode='Markdown', disable_web_page_preview=True)
                except: pass

            @bot.message_handler(commands=['p'])
            def p_cmd(m):
                try: bot.reply_to(m, f"💰 *{m.text.split()[1].upper()}*: `${yf.Ticker(m.text.split()[1].upper()).fast_info.last_price:.2f}`", parse_mode='Markdown')
                except: pass

            try:
                bot.set_my_commands([
                    BotCommand("eco", "📅 경제지표"), BotCommand("earning", "💰 실적"),
                    BotCommand("news", "📰 뉴스"), BotCommand("p", "💰 가격"),
                    BotCommand("on", "🟢 가동"), BotCommand("off", "⛔ 정지")
                ])
            except: pass

            def monitor_loop():
                nonlocal last_weekly_sent, last_daily_sent
                while True:
                    try:
                        cfg = load_config()
                        # 경제지표 알림
                        if cfg.get('eco_mode', True):
                            now = datetime.now()
                            if now.weekday() == 0 and now.hour == 8 and last_weekly_sent != now.strftime('%Y-%m-%d'):
                                events = get_economic_events()
                                if events:
                                    msg = "📅 *이번 주 주요 경제 일정*\n────────────────"
                                    c=0
                                    for e in events:
                                        if e['impact'] == 'High': msg += f"\n🗓️ `{e['date']} {e['time']}`\n🔥 {e['event']}"; c+=1
                                    if c>0: bot.send_message(chat_id, msg, parse_mode='Markdown'); last_weekly_sent = now.strftime('%Y-%m-%d')
                            
                            if now.hour == 8 and last_daily_sent != now.strftime('%Y-%m-%d'):
                                events = get_economic_events()
                                today = datetime.now().strftime('%Y-%m-%d')
                                todays = [e for e in events if e['date'] == today]
                                if todays:
                                    msg = f"☀️ *오늘({today}) 주요 일정*\n────────────────"
                                    for e in todays: msg += f"\n⏰ {e['time']} : {e['event']} (예상:{e['forecast']})"
                                    bot.send_message(chat_id, msg, parse_mode='Markdown'); last_daily_sent = now.strftime('%Y-%m-%d')

                        # 티커 감시
                        if cfg.get('system_active', True) and cfg['tickers']:
                            cur_token = cfg['telegram']['bot_token']; cur_chat = cfg['telegram']['chat_id']
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items(): exe.submit(analyze_ticker, t, s, cur_token, cur_chat)
                    except Exception as e: write_log(f"Loop Err: {e}")
                    time.sleep(60)

            def analyze_ticker(ticker, settings, token, chat_id):
                if not settings.get('🟢 감시', True): return
                try:
                    # 1. 뉴스 및 공시 (Race Condition Fix 적용)
                    if settings.get('📰 뉴스') or settings.get('🏛️ SEC'):
                        # 여기서 설정을 로드하지 말고, 아래에서 저장할 때 최신 설정을 다시 불러와야 함.
                        # 뉴스 히스토리 확인을 위해 임시로 로드
                        temp_config = load_config()
                        history = temp_config.get('news_history', {})
                        if ticker not in history: history[ticker] = []
                        
                        items = get_integrated_news(ticker, False)
                        updated = False
                        sent_count = 0 
                        
                        for item in items:
                            if item['hash'] in history[ticker] or item['link'] in history[ticker]: continue
                            
                            is_sec = "SEC" in item['title'] or "8-K" in item['title']
                            should_send = (is_sec and settings.get('🏛️ SEC')) or (not is_sec and settings.get('📰 뉴스'))
                            
                            if should_send:
                                prefix = "🏛️" if is_sec else "📰"
                                try:
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"🔔 {prefix} *[{ticker}]*\n`[{item['date']}]` [{item['title']}]({item['link']})", "parse_mode": "Markdown"})
                                except: pass
                                
                                history[ticker].append(item['hash'])
                                if len(history[ticker]) > 50: history[ticker].pop(0)
                                updated = True
                                sent_count += 1
                            
                            if sent_count >= 1: break 

                        if updated:
                            # [핵심 수정] 저장 직전에 '최신 설정'을 다시 로드하여
                            # 사용자 UI 변경사항(settings)을 덮어쓰지 않도록 함.
                            fresh_config = load_config()
                            fresh_config['news_history'] = history # 히스토리만 업데이트
                            save_config(fresh_config)

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
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"🔔 *[{ticker}] {'급등 🚀' if pct>0 else '급락 📉'}*\n변동: {pct:.2f}%\n현재: ${curr:.2f}", "parse_mode": "Markdown"})
                                    price_alert_cache[ticker] = pct
                    # 3. RSI
                    if settings.get('📉 RSI'):
                        h = stock.history(period="1mo")
                        if not h.empty:
                            delta = h['Close'].diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean(); loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                            rs = gain / loss; rsi = 100 - (100 / (1 + rs)).iloc[-1]
                            status = rsi_alert_status.get(ticker, "NORMAL")
                            if rsi >= 70 and status != "OB": requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"🔥 [{ticker}] RSI 과매수 ({rsi:.1f})"}); rsi_alert_status[ticker] = "OB"
                            elif rsi <= 30 and status != "OS": requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"💧 [{ticker}] RSI 과매도 ({rsi:.1f})"}); rsi_alert_status[ticker] = "OS"
                            elif 35 < rsi < 65: rsi_alert_status[ticker] = "NORMAL"
                except: pass

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
# [4] UI
# ---------------------------------------------------------
st.set_page_config(page_title="DeBrief", layout="wide", page_icon="📡")
st.markdown("""<style>
    .stApp { background-color: #FFFFFF; color: #202124; }
    .stock-card { background-color: #FFFFFF; border: 1px solid #DADCE0; border-radius: 8px; padding: 8px 5px; margin-bottom: 6px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
    .stock-symbol { font-size: 1.0em; font-weight: 800; color: #1A73E8; }
    .stock-price-box { display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 0.8em; font-weight: 700; }
    .up-theme { background-color: #E6F4EA; color: #137333; } .down-theme { background-color: #FCE8E6; color: #C5221F; }
    .small-btn { padding: 0px 5px; font-size: 12px; }
</style>""", unsafe_allow_html=True)

config = load_config()

with st.sidebar:
    st.header("🎛️ Control Panel")
    if "jsonbin" in st.secrets: st.success("☁️ Cloud Connected")
    
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

st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud (V56)</h3>", unsafe_allow_html=True)
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

    # [NEW] 컬럼별 일괄 제어 버튼
    if config['tickers']:
        first_t = next(iter(config['tickers']))
        opt_keys = list(config['tickers'][first_t].keys())
        
        st.markdown("⬇️ **항목별 일괄 켜기/끄기** (버튼 클릭 시 전체 적용)")
        # 버튼들을 가로로 배치
        toggle_cols = st.columns(len(opt_keys))
        for idx, key in enumerate(opt_keys):
            # 버튼 이름: "뉴스" 등 (이모지 포함된 키 그대로 사용)
            # 클릭 시: 모든 티커의 해당 옵션값이 하나라도 켜져있으면 -> 끄기, 다 꺼져있으면 -> 켜기
            # 로직: All False -> Turn On, Else -> Turn Off
            if toggle_cols[idx].button(f"{key}", key=f"tgl_{idx}", use_container_width=True):
                current_vals = [config['tickers'][t].get(key, False) for t in config['tickers']]
                # 전부 True이면 False로, 하나라도 False면 True로 (혹은 전부 False일때만 True로?)
                # UX상: 전부 켜져있을때만 끄고, 아니면 켠다. (Toggle All)
                # 여기선 단순하게: "전부 꺼져있으면 켠다. 하나라도 켜져있으면 끈다"가 안전함 (실수로 켜는 것 방지)
                # 아니면: "현재 상태의 반대"가 아니라 통일시키는 것이 목적이므로.
                # 로직: 현재 True인 개수가 과반수 이상이면 -> All False. 과반수 미만이면 -> All True.
                true_count = sum(current_vals)
                target_state = True if true_count < len(config['tickers']) / 2 else False
                
                for t in config['tickers']:
                    config['tickers'][t][key] = target_state
                save_config(config)
                st.rerun()

    input_t = st.text_input("Add Tickers")
    if st.button("➕ Add"):
        for t in [x.strip().upper() for x in input_t.split(',') if x.strip()]:
            if t not in config['tickers']:
                config['tickers'][t] = DEFAULT_OPTS.copy()
        save_config(config); st.rerun()
    
    if config['tickers']:
        df = pd.DataFrame(config['tickers']).T
        edited = st.data_editor(df, use_container_width=True, height=400)
        
        # 데이터 에디터 변경 감지 및 저장
        # DataFrame 비교를 통해 변경되었을 때만 저장
        current_df = pd.DataFrame(config['tickers']).T
        if not current_df.equals(edited):
            config['tickers'] = edited.to_dict(orient='index')
            save_config(config)
            st.toast("설정 저장됨!")
            
    st.divider()
    del_cols = st.columns([4, 1])
    del_target = del_cols[0].selectbox("삭제할 종목 선택", options=list(config['tickers'].keys()))
    if del_cols[1].button("삭제"):
        if del_target in config['tickers']: del config['tickers'][del_target]; save_config(config); st.rerun()

with t3:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()[-50:]): st.text(line.strip())
