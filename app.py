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
def get_integrated_news(ticker, strict_mode=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    sec_query = f"{ticker} SEC Filing OR 8-K OR 10-Q"
    search_urls = [
        f"https://news.google.com/rss/search?q={sec_query} when:1d&hl=en-US&gl=US&ceid=US:en",
        f"https://news.google.com/rss/search?q={ticker}+stock+news+when:1d&hl=en-US&gl=US&ceid=US:en"
    ]
    collected_items = []
    seen_links = set()
    translator = GoogleTranslator(source='auto', target='ko')

    def fetch(url):
        try:
            response = requests.get(url, headers=headers, timeout=3)
            root = ET.fromstring(response.content)
            for item in root.findall('.//item')[:2]: 
                try:
                    title = item.find('title').text.split(' - ')[0]
                    link = item.find('link').text
                    if link in seen_links: continue
                    seen_links.add(link)
                    is_foreign = ("en-US" in url or "SEC" in url)
                    if is_foreign:
                        try: title = f"{translator.translate(title[:100])} (원문포함)"
                        except: pass
                    prefix = "🏛️" if "SEC" in url else "📰"
                    collected_items.append({'title': f"{prefix} {title}", 'link': link})
                except: continue
        except: pass

    for url in search_urls: fetch(url)
    return collected_items

# [NEW] 경제지표 크롤러 (403 우회 헤더 적용)
def get_economic_events():
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Referer': 'https://www.investing.com/',
            'Cache-Control': 'no-cache'
        }
        url = "https://sslecal2.forexprostools.com/?columns=exc_flags,exc_currency,exc_importance,exc_actual,exc_forecast,exc_previous&features=datepicker,timezone&countries=5&calType=week&timeZone=88&lang=1"
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code != 200:
            write_log(f"🔥 Eco URL 차단됨: {response.status_code}")
            return []

        dfs = pd.read_html(response.text)
        if not dfs: return []
        
        df = dfs[0]
        df.columns = ['Time', 'Cur', 'Imp', 'Event', 'Actual', 'Forecast', 'Previous', 'Diamond']
        
        events = []
        current_date_str = ""
        
        for idx, row in df.iterrows():
            val0 = str(row['Time'])
            if "년" in val0 and "월" in val0 and "일" in val0:
                current_date_str = val0
                continue
            
            imp_str = str(row['Imp'])
            is_important = ('🐂' in imp_str and imp_str.count('🐂') >= 2) or ('High' in imp_str or 'Medium' in imp_str)
            
            if row['Cur'] == 'USD' and is_important:
                events.append({
                    'date_kor': current_date_str,
                    'time': str(row['Time']),
                    'event': str(row['Event']),
                    'actual': str(row['Actual']).strip(),
                    'forecast': str(row['Forecast']).strip(),
                    'previous': str(row['Previous']).strip(),
                    'id': f"{current_date_str}_{row['Time']}_{row['Event']}"
                })
        return events
    except Exception as e:
        write_log(f"Eco Calendar Error: {e}")
        return []

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

            try: bot.send_message(chat_id, "🤖 DeBrief V37 가동\n시스템이 정상화되었습니다.")
            except: pass

            # --- 명령어 ---
            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): 
                msg = ("🤖 *DeBrief V37 사용법*\n\n"
                       "📅 *경제/실적*\n"
                       "`/eco` : 이번 주 경제 일정\n"
                       "`/earning 티커` : 실적 발표일\n"
                       "`/summary 티커` : 재무 요약\n"
                       "`/vix` : 공포 지수\n\n"
                       "📊 *조회*\n"
                       "`/p 티커` : 현재가\n"
                       "`/news 티커` : 뉴스 검색\n"
                       "`/sec 티커` : 공시 조회\n\n"
                       "⚙️ *관리*\n"
                       "`/list` : 감시 목록\n"
                       "`/add 티커` : 추가\n"
                       "`/del 티커` : 삭제\n"
                       "`/on`, `/off` : 전체 시스템")
                bot.reply_to(m, msg, parse_mode='Markdown')

            @bot.message_handler(commands=['eco'])
            def eco_cmd(m):
                try:
                    bot.send_chat_action(m.chat.id, 'typing')
                    events = get_economic_events()
                    if not events:
                        bot.reply_to(m, "❌ 경제지표 데이터를 가져올 수 없습니다.\n(잠시 후 다시 시도해주세요)")
                        return
                    
                    msg = "📅 *주요 경제지표 일정*\n────────────────"
                    count = 0
                    for e in events:
                        status = f"✅{e['actual']}" if e['actual'] and 'nan' not in e['actual'].lower() else f"예상:{e['forecast']}"
                        if any(x in e['event'] for x in ['CPI', 'PPI', 'Rate', 'GDP', 'Fed', 'Sales', 'Employment']):
                            msg += f"\n🗓️ {e['date_kor']} {e['time']}\n🔥 *{e['event']}*\n({status})\n"
                            count += 1
                            if count >= 15: break
                    
                    if count == 0: msg += "\n(이번 주 남은 주요 일정이 없습니다)"
                    bot.reply_to(m, msg, parse_mode='Markdown')
                except Exception as e:
                    bot.reply_to(m, f"오류: {e}")

            @bot.message_handler(commands=['earning', '실적'])
            def earning_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    stock = yf.Ticker(t)
                    dates = stock.earnings_dates
                    if dates is None or dates.empty:
                        bot.reply_to(m, f"❌ {t}: 데이터 없음")
                        return
                    
                    if dates.index.tz is not None: dates.index = dates.index.tz_localize(None)
                    future = dates[dates.index >= pd.Timestamp.now()].sort_index()
                    
                    if not future.empty:
                        target = future.index[0]
                        rec = future.loc[target]
                        est = rec.get('EPS Estimate', 'N/A')
                        if pd.isna(est): est = 'N/A'
                        timing = "☀️ 장전" if target.hour < 12 else "🌙 장후"
                        bot.reply_to(m, f"📅 *{t} 차기 실적 발표*\n🗓️ `{target.strftime('%Y-%m-%d')}` ({timing})\n💰 예상 EPS: `{est}`", parse_mode='Markdown')
                    else:
                        bot.reply_to(m, f"⚠️ {t}: 예정된 일정 없음")
                except Exception:
                    bot.reply_to(m, "오류 발생")

            @bot.message_handler(commands=['summary'])
            def summary_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    i = yf.Ticker(t).info
                    if not i:
                        bot.reply_to(m, "정보 없음")
                        return
                    
                    def s(k): 
                        val = i.get(k)
                        return f"{val:,.2f}" if isinstance(val, (int, float)) else "N/A"
                        
                    msg = (f"📊 *{t} 요약*\n💰 현재가: ${s('currentPrice')}\n🏢 시총: ${s('marketCap')}\n📈 PER: {s('trailingPE')}\n🎯 목표: ${s('targetMeanPrice')}")
                    bot.reply_to(m, msg, parse_mode='Markdown')
                except Exception:
                    bot.reply_to(m, "데이터 조회 실패")

            @bot.message_handler(commands=['vix'])
            def vix_cmd(m):
                try:
                    v = yf.Ticker("^VIX").fast_info
                    bot.reply_to(m, f"😨 *VIX*: `{v.last_price:.2f}`", parse_mode='Markdown')
                except Exception: pass

            @bot.message_handler(commands=['add'])
            def add_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t not in c['tickers']:
                        c['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
                        save_config(c); bot.reply_to(m, f"✅ {t} 추가됨")
                except Exception: pass

            @bot.message_handler(commands=['del'])
            def del_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t in c['tickers']: del c['tickers'][t]; save_config(c); bot.reply_to(m, f"🗑️ {t} 삭제됨")
                except Exception: pass

            @bot.message_handler(commands=['list'])
            def list_cmd(m):
                try:
                    c = load_config()
                    bot.reply_to(m, f"📋 목록: {', '.join(c['tickers'].keys())}")
                except Exception: pass

            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    items = get_integrated_news(t)
                    msg = f"📰 *{t} News*\n" + "\n".join([f"- [{i['title']}]({i['link']})" for i in items])
                    bot.reply_to(m, msg, parse_mode='Markdown', disable_web_page_preview=True)
                except Exception: pass

            @bot.message_handler(commands=['sec'])
            def sec_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    items = get_integrated_news(t)
                    secs = [i for i in items if "SEC" in i['title']]
                    if secs: bot.reply_to(m, f"🏛️ *{t} SEC*\n" + "\n".join([f"- [{i['title']}]({i['link']})" for i in secs]), parse_mode='Markdown')
                    else: bot.reply_to(m, "공시 없음")
                except Exception: pass

            @bot.message_handler(commands=['p'])
            def p_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    p = yf.Ticker(t).fast_info.last_price
                    bot.reply_to(m, f"💰 *{t}*: `${p:.2f}`", parse_mode='Markdown')
                except Exception: pass

            @bot.message_handler(commands=['market'])
            def market_cmd(m):
                try:
                    txt = "🌍 *Market*\n"
                    for k,v in {"S&P500":"^GSPC", "Nasdaq":"^IXIC"}.items():
                        txt += f"{k}: `{yf.Ticker(v).fast_info.last_price:.2f}`\n"
                    bot.reply_to(m, txt, parse_mode='Markdown')
                except Exception: pass

            @bot.message_handler(commands=['on', 'off'])
            def toggle_cmd(m):
                try:
                    c = load_config()
                    c['system_active'] = ('/on' in m.text)
                    save_config(c)
                    bot.reply_to(m, f"시스템 {'가동' if c['system_active'] else '정지'}")
                except Exception: pass

            try:
                bot.set_my_commands([
                    BotCommand("eco", "📅 경제지표"),
                    BotCommand("earning", "💰 실적 발표"),
                    BotCommand("news", "📰 뉴스"),
                    BotCommand("p", "💰 현재가"),
                    BotCommand("summary", "📊 요약"),
                    BotCommand("sec", "🏛️ 공시 조회"),
                    BotCommand("vix", "😨 공포 지수"),
                    BotCommand("list", "📋 감시 목록"),
                    BotCommand("add", "➕ 추가"), BotCommand("del", "🗑️ 삭제"),
                    BotCommand("on", "🟢 가동"), BotCommand("off", "⛔ 정지"),
                    BotCommand("help", "❓ 도움말")
                ])
            except: pass

            # --- 감시 루프 ---
            def monitor_loop():
                nonlocal last_weekly_sent, last_daily_sent
                
                while True:
                    try:
                        cfg = load_config()
                        
                        # 1. 경제지표 알림
                        if cfg.get('eco_mode', True):
                            now = datetime.now()
                            # 주간 브리핑
                            if now.weekday() == 0 and now.hour == 8 and last_weekly_sent != now.strftime('%Y-%m-%d'):
                                events = get_economic_events()
                                if events:
                                    msg = "📅 *이번 주 주요 경제 일정*\n────────────────"
                                    c = 0
                                    for e in events:
                                        if any(k in e['event'] for k in ['Fed', 'CPI', 'PPI', 'Rate']):
                                            msg += f"\n🗓️ {e['date_kor']} {e['time']} : {e['event']}"
                                            c += 1
                                    if c > 0:
                                        bot.send_message(chat_id, msg, parse_mode='Markdown')
                                        last_weekly_sent = now.strftime('%Y-%m-%d')

                            # 데일리 브리핑
                            if now.hour == 8 and last_daily_sent != now.strftime('%Y-%m-%d'):
                                events = get_economic_events()
                                today = f"{now.year}년 {now.month:02d}월 {now.day:02d}일"
                                todays = [e for e in events if e['date_kor'] == today]
                                if todays:
                                    msg = f"☀️ *오늘({today}) 주요 일정*\n────────────────"
                                    for e in todays:
                                        msg += f"\n⏰ {e['time']} : {e['event']} (예상:{e['forecast']})"
                                    bot.send_message(chat_id, msg, parse_mode='Markdown')
                                    last_daily_sent = now.strftime('%Y-%m-%d')

                            # 실시간 결과 알림
                            events = get_economic_events()
                            for e in events:
                                if e['actual'] and 'nan' not in e['actual'].lower() and e['id'] not in eco_alert_cache:
                                    if any(k in e['event'] for k in ['CPI', 'PPI', 'GDP', 'Rate', 'Fed', 'Employment']):
                                        msg = (f"🚨 *경제지표 발표*\n🔥 *{e['event']}*\n────────────────\n✅ 실제: `{e['actual']}`\n📊 예상: `{e['forecast']}`")
                                        bot.send_message(chat_id, msg, parse_mode='Markdown')
                                        eco_alert_cache.add(e['id'])

                        # 2. 주식 감시
                        if cfg.get('system_active', True) and cfg['tickers']:
                            cur_token = cfg['telegram']['bot_token']
                            cur_chat = cfg['telegram']['chat_id']
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items():
                                    exe.submit(analyze_ticker, t, s, cur_token, cur_chat)
                                    
                    except Exception as e: write_log(f"Loop Err: {e}")
                    time.sleep(60)

            def analyze_ticker(ticker, settings, token, chat_id):
                if not settings.get('감시_ON', True): return
                try:
                    # 뉴스
                    if settings.get('뉴스') or settings.get('SEC'):
                        if ticker not in news_cache: news_cache[ticker] = set()
                        items = get_integrated_news(ticker)
                        for item in items:
                            if item['link'] in news_cache[ticker]: continue
                            prefix = "🏛️" if "SEC" in item['title'] else "📰"
                            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                        data={"chat_id": chat_id, "text": f"🔔 {prefix} *[{ticker}]*\n{item['title']}\n🔗 [기사 원문 보기]({item['link']})", "parse_mode": "Markdown"})
                            news_cache[ticker].add(item['link'])
                    
                    # 가격
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
                                    
                    # RSI
                    if settings.get('RSI'):
                        h = stock.history(period="1mo")
                        if not h.empty:
                            delta = h['Close'].diff()
                            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                            rs = gain / loss
                            rsi = 100 - (100 / (1 + rs)).iloc[-1]
                            status = rsi_alert_status.get(ticker, "NORMAL")
                            if rsi >= 70 and status != "OB":
                                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"🔥 [{ticker}] RSI 과매수 ({rsi:.1f})"})
                                rsi_alert_status[ticker] = "OB"
                            elif rsi <= 30 and status != "OS":
                                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"💧 [{ticker}] RSI 과매도 ({rsi:.1f})"})
                                rsi_alert_status[ticker] = "OS"
                            elif 35 < rsi < 65: rsi_alert_status[ticker] = "NORMAL"

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
    if st.toggle("System Power", value=config.get('system_active', True)):
        st.success("🟢 Active")
        config['system_active'] = True
    else:
        st.error("⛔ Paused")
        config['system_active'] = False
    
    save_config(config)

    with st.expander("🔑 Keys"):
        bot_t = st.text_input("Bot Token", value=config['telegram'].get('bot_token', ''), type="password")
        chat_i = st.text_input("Chat ID", value=config['telegram'].get('chat_id', ''))
        if st.button("Save Keys"):
            config['telegram'].update({"bot_token": bot_t, "chat_id": chat_i})
            save_config(config); st.rerun()

st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud (V37)</h3>", unsafe_allow_html=True)
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
    eco_mode = st.checkbox("📢 경제지표/연준 알림 (CPI, FOMC 등)", value=config.get('eco_mode', True))
    if eco_mode != config.get('eco_mode', True):
        config['eco_mode'] = eco_mode
        save_config(config)
        st.toast("경제지표 알림 설정 저장됨")

    st.divider()
    
    input_t = st.text_input("Add Tickers")
    if st.button("➕ Add"):
        for t in [x.strip().upper() for x in input_t.split(',') if x.strip()]:
            config['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
        save_config(config); st.rerun()
    
    if config['tickers']:
        df = pd.DataFrame(config['tickers']).T
        edited = st.data_editor(df, use_container_width=True)
        if not df.equals(edited):
            config['tickers'] = edited.to_dict(orient='index')
            save_config(config); st.toast("Saved!")

with t3:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()[-50:]): st.text(line.strip())
