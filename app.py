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
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from telebot.types import BotCommand
from deep_translator import GoogleTranslator

# --- 프로젝트 설정 ---
CONFIG_FILE = 'debrief_settings.json'
LOG_FILE = 'debrief.log'

# [State] 캐시 초기화
if 'news_cache' not in st.session_state: st.session_state['news_cache'] = {}
if 'price_alert_cache' not in st.session_state: st.session_state['price_alert_cache'] = {}
if 'rsi_alert_status' not in st.session_state: st.session_state['rsi_alert_status'] = {}
if 'eco_alert_cache' not in st.session_state: st.session_state['eco_alert_cache'] = set()

news_cache = st.session_state['news_cache']
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

def load_config():
    config = {
        "system_active": True,
        "eco_mode": True,
        "telegram": {"bot_token": "", "chat_id": ""}, 
        "tickers": {
            "TSLA": {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False},
            "NVDA": {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
        } 
    }
    url = get_jsonbin_url()
    headers = get_jsonbin_headers()
    
    if url and headers:
        try:
            resp = requests.get(f"{url}/latest", headers=headers, timeout=5)
            if resp.status_code == 200:
                cloud_data = resp.json()['record']
                if "tickers" in cloud_data: config.update(cloud_data)
        except: pass
    
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                config.update(saved)
    except: pass

    try:
        if "telegram" in st.secrets:
            config['telegram']['bot_token'] = st.secrets["telegram"]["bot_token"]
            config['telegram']['chat_id'] = st.secrets["telegram"]["chat_id"]
    except: pass
    return config

def save_config(config):
    url = get_jsonbin_url()
    headers = get_jsonbin_headers()
    if url and headers:
        try: requests.put(url, headers=headers, json=config, timeout=5)
        except: pass
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except: pass

# ---------------------------------------------------------
# [2] 데이터 엔진
# ---------------------------------------------------------
def parse_rss_date(date_str):
    try:
        dt = datetime.strptime(date_str.replace(' GMT', ''), '%a, %d %b %Y %H:%M:%S')
        return dt.strftime('%m/%d %H:%M')
    except:
        return date_str[:16]

def get_integrated_news(ticker, is_sec_search=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    if is_sec_search:
        search_urls = [f"https://news.google.com/rss/search?q={ticker}+SEC+Filing+OR+8-K+OR+10-Q+OR+10-K+when:2d&hl=en-US&gl=US&ceid=US:en"]
    else:
        search_urls = [f"https://news.google.com/rss/search?q={ticker}+stock+news+when:1d&hl=en-US&gl=US&ceid=US:en"]

    collected_items = []
    seen_links = set()
    translator = GoogleTranslator(source='auto', target='ko')

    def fetch(url):
        try:
            response = requests.get(url, headers=headers, timeout=3)
            root = ET.fromstring(response.content)
            for item in root.findall('.//item')[:3]: 
                try:
                    title = item.find('title').text.split(' - ')[0]
                    link = item.find('link').text
                    pubDate = item.find('pubDate').text
                    if link in seen_links: continue
                    seen_links.add(link)
                    
                    date_str = parse_rss_date(pubDate)
                    if not is_sec_search:
                        try: title = translator.translate(title[:150]) 
                        except: pass
                    
                    prefix = "🏛️" if is_sec_search else "📰"
                    collected_items.append({'title': f"{prefix} {title}", 'link': link, 'date': date_str})
                except: continue
        except: pass

    for url in search_urls: fetch(url)
    return collected_items

# 경제지표 (Cloudscraper)
def get_economic_events():
    try:
        scraper = cloudscraper.create_scraper()
        url = "https://sslecal2.forexprostools.com/?columns=exc_flags,exc_currency,exc_importance,exc_actual,exc_forecast,exc_previous&features=datepicker,timezone&countries=5&calType=week&timeZone=88&lang=1"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.investing.com/'
        }
        resp = scraper.get(url, headers=headers)
        if resp.status_code != 200: return []
        
        dfs = pd.read_html(resp.text)
        if not dfs: return []
        
        df = dfs[0]
        df.columns = ['Time', 'Cur', 'Imp', 'Event', 'Actual', 'Forecast', 'Previous', 'Diamond']
        
        events = []
        current_date = ""
        translator = GoogleTranslator(source='auto', target='ko')
        
        for idx, row in df.iterrows():
            val0 = str(row['Time'])
            if "년" in val0: current_date = val0; continue
            
            imp = str(row['Imp'])
            if ('🐂' in imp and imp.count('🐂') >= 2) or ('High' in imp or 'Medium' in imp):
                if row['Cur'] == 'USD':
                    title = str(row['Event'])
                    try: title = translator.translate(title)
                    except: pass
                    
                    events.append({
                        'date_kor': current_date,
                        'time': str(row['Time']),
                        'event': title,
                        'actual': str(row['Actual']).strip(),
                        'forecast': str(row['Forecast']).strip(),
                        'id': f"{current_date}_{row['Time']}_{row['Event']}"
                    })
        return events
    except: return []

# ---------------------------------------------------------
# [3] 백그라운드 봇
# ---------------------------------------------------------
@st.cache_resource
def start_background_worker():
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

            try: bot.send_message(chat_id, "🤖 DeBrief V41 가동\n명령어(/earning, /summary)가 복구되었습니다.")
            except: pass

            # --- 명령어 ---
            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): 
                msg = ("🤖 *DeBrief V41 사용법*\n\n"
                       "📊 *조회*\n"
                       "`/p 티커` : 현재가\n"
                       "`/earning 티커` : 실적 발표일 (Fix)\n"
                       "`/summary 티커` : 재무 요약 (Fix)\n"
                       "`/eco` : 경제 일정\n"
                       "`/news 티커` : 뉴스\n\n"
                       "⚙️ *관리*\n"
                       "`/add 티커` : 추가\n"
                       "`/del 티커` : 삭제\n"
                       "`/list` : 목록")
                bot.reply_to(m, msg, parse_mode='Markdown')

            # [수정] 실적 발표일 (3중 안전장치 적용)
            @bot.message_handler(commands=['earning', '실적'])
            def earning_cmd(m):
                try:
                    parts = m.text.split()
                    if len(parts) < 2: return bot.reply_to(m, "⚠️ 사용법: `/earning 티커`")
                    t = parts[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    
                    stock = yf.Ticker(t)
                    msg = ""
                    
                    # 1. earnings_dates 시도
                    try:
                        dates = stock.earnings_dates
                        if dates is not None and not dates.empty:
                            if dates.index.tz is not None: dates.index = dates.index.tz_localize(None)
                            future = dates[dates.index >= pd.Timestamp.now()].sort_index()
                            if not future.empty:
                                target = future.index[0]
                                rec = future.loc[target]
                                eps = rec.get('EPS Estimate', 'N/A')
                                if pd.isna(eps): eps = 'N/A'
                                timing = "☀️ 장전" if target.hour < 12 else "🌙 장후"
                                if target.hour == 0: timing = "시간 미정"
                                msg = f"📅 *{t} 실적 발표*\n🗓️ `{target.strftime('%Y-%m-%d')}` ({timing})\n💰 예상 EPS: `{eps}`"
                    except: pass
                    
                    # 2. 실패 시 calendar 시도
                    if not msg:
                        try:
                            cal = stock.calendar
                            if cal is not None and not cal.empty:
                                d = cal.iloc[0, 0]
                                msg = f"📅 *{t} 실적 발표 (Calendar)*\n🗓️ 예정일: `{d.strftime('%Y-%m-%d')}`"
                        except: pass
                        
                    # 3. 실패 시 info 시도
                    if not msg:
                        try:
                            ts = stock.info.get('earningsTimestamp')
                            if ts:
                                d = datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
                                msg = f"📅 *{t} 실적 발표 (Info)*\n🗓️ 예정일: `{d}`"
                        except: pass

                    if msg: bot.reply_to(m, msg, parse_mode='Markdown')
                    else: bot.reply_to(m, f"❌ {t}: 예정된 실적 발표 정보를 찾을 수 없습니다.")
                        
                except Exception as e: bot.reply_to(m, f"오류 발생: {e}")

            # [수정] 재무 요약 (안전한 데이터 접근)
            @bot.message_handler(commands=['summary', '요약'])
            def summary_cmd(m):
                try:
                    parts = m.text.split()
                    if len(parts) < 2: return bot.reply_to(m, "⚠️ 사용법: `/summary 티커`")
                    t = parts[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    
                    stock = yf.Ticker(t)
                    info = stock.info
                    
                    # 데이터가 아예 없으면 fast_info 시도
                    curr = None
                    if info: curr = info.get('currentPrice')
                    
                    if not curr:
                        try: curr = stock.fast_info.last_price
                        except: pass
                    
                    if not curr:
                        return bot.reply_to(m, "❌ 데이터를 가져올 수 없습니다.")

                    def safe(k):
                        if not info: return "N/A"
                        val = info.get(k)
                        return f"{val:,.2f}" if isinstance(val, (int, float)) else "N/A"
                    
                    cap = info.get('marketCap', 0) if info else 0
                    cap_str = f"${cap/1e9:.2f}B" if cap else "N/A"

                    msg = (f"📊 *{t} 재무 요약*\n"
                           f"💰 현재가: `${curr:.2f}`\n"
                           f"🏢 시가총액: `{cap_str}`\n"
                           f"📈 PER: `{safe('trailingPE')}` (Fwd: `{safe('forwardPE')}`)\n"
                           f"📚 PBR: `{safe('priceToBook')}`\n"
                           f"🎯 목표주가: `${safe('targetMeanPrice')}`\n"
                           f"🌊 52주: `${safe('fiftyTwoWeekLow')} ~ ${safe('fiftyTwoWeekHigh')}`")
                    
                    bot.reply_to(m, msg, parse_mode='Markdown')
                except Exception as e: bot.reply_to(m, f"조회 실패: {e}")

            @bot.message_handler(commands=['eco'])
            def eco_cmd(m):
                try:
                    bot.send_chat_action(m.chat.id, 'typing')
                    events = get_economic_events()
                    if not events: return bot.reply_to(m, "데이터 수신 실패")
                    msg = "📅 *주요 경제지표 일정*\n────────────────"
                    c=0
                    for e in events:
                        stt = f"✅{e['actual']}" if e['actual'] and 'nan' not in e['actual'].lower() else f"예상:{e['forecast']}"
                        if any(x in e['event'] for x in ['CPI','PPI','GDP','Rate','Fed','Employment']):
                            msg += f"\n🗓️ {e['date_kor']} {e['time']}\n🔥 *{e['event']}*\n({stt})\n"
                            c+=1
                            if c>=15: break
                    bot.reply_to(m, msg, parse_mode='Markdown')
                except: pass

            @bot.message_handler(commands=['add'])
            def add_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t not in c['tickers']:
                        c['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
                        save_config(c); bot.reply_to(m, f"✅ {t} 추가됨")
                except: pass

            @bot.message_handler(commands=['del'])
            def del_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t in c['tickers']: del c['tickers'][t]; save_config(c); bot.reply_to(m, f"🗑️ {t} 삭제됨")
                except: pass

            @bot.message_handler(commands=['list'])
            def list_cmd(m):
                c = load_config(); bot.reply_to(m, f"📋 목록: {', '.join(c['tickers'].keys())}")

            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    items = get_integrated_news(t, False)
                    if not items: return bot.reply_to(m, "뉴스 없음")
                    lines = [f"📰 *{t} News*"]
                    for i in items: lines.append(f"▪️ `[{i['date']}]` [{i['title'].replace('[','').replace(']','')}]({i['link']})")
                    bot.reply_to(m, "\n\n".join(lines), parse_mode='Markdown', disable_web_page_preview=True)
                except: pass

            @bot.message_handler(commands=['sec'])
            def sec_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    items = get_integrated_news(t, True)
                    if not items: return bot.reply_to(m, "공시 없음")
                    lines = [f"🏛️ *{t} SEC*"]
                    for i in items: lines.append(f"▪️ `[{i['date']}]` [{i['title'].replace('🏛️ ','').replace('[','').replace(']','')}]({i['link']})")
                    bot.reply_to(m, "\n\n".join(lines), parse_mode='Markdown', disable_web_page_preview=True)
                except: pass

            @bot.message_handler(commands=['p'])
            def p_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    p = yf.Ticker(t).fast_info.last_price
                    bot.reply_to(m, f"💰 *{t}*: `${p:.2f}`", parse_mode='Markdown')
                except: pass

            @bot.message_handler(commands=['vix'])
            def vix_cmd(m):
                try:
                    v = yf.Ticker("^VIX").fast_info.last_price
                    bot.reply_to(m, f"😨 *VIX*: `{v:.2f}`", parse_mode='Markdown')
                except: pass

            @bot.message_handler(commands=['market'])
            def market_cmd(m):
                try:
                    txt = "🌍 *Market*\n"
                    for k,v in {"S&P500":"^GSPC", "Nasdaq":"^IXIC"}.items():
                        txt += f"{k}: `{yf.Ticker(v).fast_info.last_price:.2f}`\n"
                    bot.reply_to(m, txt, parse_mode='Markdown')
                except: pass

            @bot.message_handler(commands=['on', 'off'])
            def toggle_cmd(m):
                c = load_config(); c['system_active'] = ('/on' in m.text); save_config(c)
                bot.reply_to(m, f"시스템 {'가동' if c['system_active'] else '정지'}")

            try:
                bot.set_my_commands([
                    BotCommand("eco", "📅 경제지표"), BotCommand("earning", "💰 실적 발표"),
                    BotCommand("summary", "📊 재무 요약"), BotCommand("news", "📰 뉴스"),
                    BotCommand("p", "💰 현재가"), BotCommand("list", "📋 목록"),
                    BotCommand("add", "➕ 추가"), BotCommand("del", "🗑️ 삭제"),
                    BotCommand("on", "🟢 켜기"), BotCommand("off", "⛔ 끄기"),
                    BotCommand("help", "❓ 도움말")
                ])
            except: pass

            # --- 감시 루프 ---
            def monitor_loop():
                nonlocal last_weekly_sent, last_daily_sent
                while True:
                    try:
                        cfg = load_config()
                        if cfg.get('eco_mode', True):
                            now = datetime.now()
                            # 주간/일간 브리핑 (기존 로직 유지)
                            if now.weekday()==0 and now.hour==8 and last_weekly_sent!=now.strftime('%Y-%m-%d'):
                                evs = get_economic_events()
                                if evs: bot.send_message(chat_id, "📅 주간 경제 일정 업데이트", parse_mode='Markdown'); last_weekly_sent=now.strftime('%Y-%m-%d')
                            
                            if now.hour==8 and last_daily_sent!=now.strftime('%Y-%m-%d'):
                                evs = get_economic_events()
                                # 오늘 날짜 필터링 로직 필요
                                last_daily_sent=now.strftime('%Y-%m-%d')

                            # 실시간 알림
                            evs = get_economic_events()
                            for e in evs:
                                if e['actual'] and 'nan' not in e['actual'].lower() and e['id'] not in eco_alert_cache:
                                    if any(k in e['event'] for k in ['CPI','PPI','GDP','Rate','Fed']):
                                        msg = f"🚨 *경제지표 발표*\n🔥 *{e['event']}*\n✅ 실제: `{e['actual']}`\n📊 예상: `{e['forecast']}`"
                                        bot.send_message(chat_id, msg, parse_mode='Markdown')
                                        eco_alert_cache.add(e['id'])

                        if cfg.get('system_active', True) and cfg['tickers']:
                            cur_token, cur_chat = cfg['telegram']['bot_token'], cfg['telegram']['chat_id']
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items():
                                    exe.submit(analyze_ticker, t, s, cur_token, cur_chat)
                    except: pass
                    time.sleep(60)

            def analyze_ticker(ticker, settings, token, chat_id):
                if not settings.get('감시_ON', True): return
                try:
                    # 뉴스 알림
                    if settings.get('뉴스') or settings.get('SEC'):
                        if ticker not in news_cache: news_cache[ticker] = set()
                        items = get_integrated_news(ticker, False)
                        for i in items:
                            if i['link'] in news_cache[ticker]: continue
                            prefix = "🏛️" if "SEC" in i['title'] else "📰"
                            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                        data={"chat_id": chat_id, "text": f"🔔 {prefix} *[{ticker}]*\n`[{i['date']}]` [{i['title']}]({i['link']})", "parse_mode": "Markdown"})
                            news_cache[ticker].add(i['link'])
                    
                    # 가격 알림
                    if settings.get('가격_3%'):
                        stock = yf.Ticker(ticker)
                        h = stock.history(period="1d")
                        if not h.empty:
                            curr = h['Close'].iloc[-1]; prev = stock.fast_info.previous_close
                            pct = ((curr - prev) / prev) * 100
                            if abs(pct) >= 3.0:
                                last = price_alert_cache.get(ticker, 0)
                                if abs(pct - last) >= 1.0:
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                                data={"chat_id": chat_id, "text": f"🔔 *[{ticker}] {'급등 🚀' if pct>0 else '급락 📉'}*\n변동: {pct:.2f}%\n현재: ${curr:.2f}", "parse_mode": "Markdown"})
                                    price_alert_cache[ticker] = pct
                except: pass

            t_mon = threading.Thread(target=monitor_loop, daemon=True)
            t_mon.start()
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e: write_log(f"Bot Error: {e}")

    t_bot = threading.Thread(target=run_bot_system, daemon=True)
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
    .stock-name { font-size: 0.65em; color: #5F6368; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .stock-price-box { display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 0.8em; font-weight: 700; }
    .up-theme { background-color: #E6F4EA; color: #137333; } .down-theme { background-color: #FCE8E6; color: #C5221F; }
</style>""", unsafe_allow_html=True)

config = load_config()

with st.sidebar:
    st.header("🎛️ Control Panel")
    if "jsonbin" in st.secrets: st.success("☁️ Cloud Connected")
    else: st.warning("📂 Local Mode")
    
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

st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud (V41)</h3>", unsafe_allow_html=True)
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
        config['eco_mode'] = eco_mode; save_config(config); st.toast("Saved")
    st.divider()
    
    c_all_1, c_all_2, c_blank = st.columns([1, 1, 3])
    if c_all_1.button("✅ ALL ON", use_container_width=True):
        for t in config['tickers']:
            for k in ["감시_ON", "뉴스", "SEC", "가격_3%", "RSI"]: config['tickers'][t][k] = True
        save_config(config); st.rerun()
    if c_all_2.button("⛔ ALL OFF", use_container_width=True):
        for t in config['tickers']:
            for k in ["감시_ON", "뉴스", "SEC", "가격_3%", "RSI"]: config['tickers'][t][k] = False
        save_config(config); st.rerun()

    input_t = st.text_input("Add Tickers")
    if st.button("➕ Add"):
        for t in [x.strip().upper() for x in input_t.split(',') if x.strip()]:
            config['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
        save_config(config); st.rerun()
    
    if config['tickers']:
        df = pd.DataFrame(config['tickers']).T
        edited = st.data_editor(df, use_container_width=True)
        if not df.equals(edited):
            config['tickers'] = edited.to_dict(orient='index'); save_config(config); st.toast("Saved!")
    
    del_cols = st.columns([4, 1])
    del_target = del_cols[0].selectbox("Delete", options=list(config['tickers'].keys()))
    if del_cols[1].button("🗑️"):
        if del_target in config['tickers']: del config['tickers'][del_target]; save_config(config); st.rerun()

with t3:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()[-50:]): st.text(line.strip())
