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
# [2] 데이터 엔진 (날짜 파싱 + 번역 강화)
# ---------------------------------------------------------
def parse_rss_date(date_str):
    """ RSS 날짜 형식을 짧은 형식(MM/DD HH:mm)으로 변환 """
    try:
        # 예: Mon, 06 Jan 2025 08:21:56 GMT
        dt = datetime.strptime(date_str.replace(' GMT', ''), '%a, %d %b %Y %H:%M:%S')
        return dt.strftime('%m/%d %H:%M')
    except:
        return date_str[:16] # 실패 시 앞부분만 자름

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
                    pubDate = item.find('pubDate').text # 날짜 추출
                    
                    if link in seen_links: continue
                    seen_links.add(link)
                    
                    # [NEW] 날짜 포맷팅
                    date_str = parse_rss_date(pubDate)
                    
                    # [NEW] 제목 번역 (SEC도 포함)
                    try: 
                        title = translator.translate(title[:150]) 
                    except: pass
                    
                    prefix = "🏛️" if is_sec_search else "📰"
                    collected_items.append({'title': f"{prefix} {title}", 'link': link, 'date': date_str})
                except: continue
        except: pass

    for url in search_urls: fetch(url)
    return collected_items

# [NEW] 경제지표 (ForexFactory XML + 한글 번역)
def get_economic_events():
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200: return []
        
        root = ET.fromstring(resp.content)
        events = []
        translator = GoogleTranslator(source='auto', target='ko')
        
        for event in root.findall('event'):
            country = event.find('country').text
            if country != 'USD': continue 
            
            impact = event.find('impact').text
            if impact not in ['High', 'Medium']: continue 
            
            title = event.find('title').text
            # [NEW] 지표명 한글 번역
            try: title = translator.translate(title)
            except: pass
            
            date_str = event.find('date').text
            time_str = event.find('time').text
            forecast = event.find('forecast').text if event.find('forecast') is not None else ""
            previous = event.find('previous').text if event.find('previous') is not None else ""
            
            events.append({
                'date': date_str,
                'time': time_str,
                'event': title,
                'impact': impact,
                'forecast': forecast,
                'previous': previous,
                'actual': "", 
                'id': f"{date_str}_{time_str}_{title}"
            })
            
        events.sort(key=lambda x: (x['date'], x['time']))
        return events
    except Exception as e:
        write_log(f"Eco Error: {e}")
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

            try: bot.send_message(chat_id, "🤖 DeBrief V40 가동\n날짜 표시/번역 강화/UI 복구가 완료되었습니다.")
            except: pass

            # --- 명령어 핸들러 ---
            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): 
                msg = ("🤖 *DeBrief V40 사용법*\n\n"
                       "📅 *경제/실적*\n"
                       "`/eco` : 이번 주 경제 일정 (한글)\n"
                       "`/earning 티커` : 실적 발표일\n"
                       "`/summary 티커` : 재무 요약\n"
                       "`/vix` : 공포 지수\n\n"
                       "📊 *조회*\n"
                       "`/p 티커` : 현재가\n"
                       "`/news 티커` : 뉴스 검색 (번역+날짜)\n"
                       "`/sec 티커` : 공시 조회 (번역+날짜)\n\n"
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
                        bot.reply_to(m, "❌ 이번 주 남은 주요 일정이 없습니다.")
                        return
                    
                    msg = "📅 *주요 경제 일정 (USD)*\n────────────────"
                    count = 0
                    for e in events:
                        icon = "🔥" if e['impact'] == 'High' else "🔸"
                        fcst = f"(예상: {e['forecast']})" if e['forecast'] else ""
                        msg += f"\n{icon} `{e['date']} {e['time']}`\n*{e['event']}* {fcst}\n"
                        count += 1
                        if count >= 15: break
                    
                    bot.reply_to(m, msg, parse_mode='Markdown')
                except: bot.reply_to(m, "오류 발생")

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
                except: bot.reply_to(m, "오류 발생")

            @bot.message_handler(commands=['summary'])
            def summary_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    i = yf.Ticker(t).info
                    if not i: return bot.reply_to(m, "정보 없음")
                    def s(k): 
                        val = i.get(k)
                        return f"{val:,.2f}" if isinstance(val, (int, float)) else "N/A"
                    msg = (f"📊 *{t} 요약*\n💰 현재가: ${s('currentPrice')}\n🏢 시총: ${s('marketCap')}\n📈 PER: {s('trailingPE')}\n🎯 목표: ${s('targetMeanPrice')}")
                    bot.reply_to(m, msg, parse_mode='Markdown')
                except: pass

            @bot.message_handler(commands=['vix'])
            def vix_cmd(m):
                try:
                    v = yf.Ticker("^VIX").fast_info
                    bot.reply_to(m, f"😨 *VIX*: `{v.last_price:.2f}`", parse_mode='Markdown')
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
                try:
                    c = load_config()
                    bot.reply_to(m, f"📋 목록: {', '.join(c['tickers'].keys())}")
                except: pass

            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    # 뉴스 (번역 + 날짜 포함)
                    items = get_integrated_news(t, is_sec_search=False)
                    if not items: return bot.reply_to(m, "뉴스 없음")
                    
                    msg_lines = [f"📰 *{t} News*"]
                    for i in items:
                        # [날짜] 제목 (링크)
                        msg_lines.append(f"▪️ `[{i['date']}]` [{i['title'].replace('[','').replace(']','')}]({i['link']})")
                    
                    bot.reply_to(m, "\n\n".join(msg_lines), parse_mode='Markdown', disable_web_page_preview=True)
                except: pass

            @bot.message_handler(commands=['sec'])
            def sec_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    # SEC (번역 + 날짜 포함)
                    items = get_integrated_news(t, is_sec_search=True)
                    
                    if items:
                        msg_lines = [f"🏛️ *{t} SEC*"]
                        for i in items:
                            msg_lines.append(f"▪️ `[{i['date']}]` [{i['title'].replace('🏛️ ','').replace('[','').replace(']','')}]({i['link']})")
                        bot.reply_to(m, "\n\n".join(msg_lines), parse_mode='Markdown', disable_web_page_preview=True)
                    else: 
                        bot.reply_to(m, f"❌ {t} 공시 없음")
                except: pass

            @bot.message_handler(commands=['p'])
            def p_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    p = yf.Ticker(t).fast_info.last_price
                    bot.reply_to(m, f"💰 *{t}*: `${p:.2f}`", parse_mode='Markdown')
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
                try:
                    c = load_config()
                    c['system_active'] = ('/on' in m.text)
                    save_config(c)
                    bot.reply_to(m, f"시스템 {'가동' if c['system_active'] else '정지'}")
                except: pass

            try:
                bot.set_my_commands([
                    BotCommand("eco", "📅 경제지표"),
                    BotCommand("earning", "💰 실적 발표"),
                    BotCommand("news", "📰 뉴스"),
                    BotCommand("p", "💰 현재가"),
                    BotCommand("summary", "📊 요약"),
                    BotCommand("sec", "🏛️ 공시"),
                    BotCommand("vix", "😨 공포 지수"),
                    BotCommand("list", "📋 목록"),
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
                                        if e['impact'] == 'High':
                                            msg += f"\n🗓️ `{e['date']} {e['time']}`\n🔥 {e['event']}"
                                            c += 1
                                    if c > 0:
                                        bot.send_message(chat_id, msg, parse_mode='Markdown')
                                        last_weekly_sent = now.strftime('%Y-%m-%d')

                            # 매일 아침 브리핑
                            if now.hour == 8 and last_daily_sent != now.strftime('%Y-%m-%d'):
                                events = get_economic_events()
                                # 날짜 매칭 로직 간소화 (오늘 날짜 포함된 것)
                                today = datetime.now().strftime('%Y-%m-%d') # YYYY-MM-DD
                                # ForexFactory XML Date format check required. Assuming YYYY-MM-DD.
                                # Simple check:
                                todays = [e for e in events if e['date'] == today]
                                
                                if todays:
                                    msg = f"☀️ *오늘({today}) 주요 일정*\n────────────────"
                                    for e in todays:
                                        msg += f"\n⏰ {e['time']} : {e['event']} (예상:{e['forecast']})"
                                    bot.send_message(chat_id, msg, parse_mode='Markdown')
                                    last_daily_sent = now.strftime('%Y-%m-%d')

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
                        items = get_integrated_news(ticker, is_sec_search=False) # 공시도 통합 검색
                        for item in items:
                            if item['link'] in news_cache[ticker]: continue
                            
                            is_sec = "SEC" in item['title'] or "8-K" in item['title']
                            should_send = (is_sec and settings.get('SEC')) or (not is_sec and settings.get('뉴스'))
                            
                            if should_send:
                                prefix = "🏛️" if is_sec else "📰"
                                # [수정] 날짜 포함 + 링크 클릭형
                                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                            data={"chat_id": chat_id, "text": f"🔔 {prefix} *[{ticker}]*\n`[{item['date']}]` [{item['title']}]({item['link']})", "parse_mode": "Markdown"})
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
    
    # [복구] 클라우드 상태 표시
    if "jsonbin" in st.secrets:
        st.success("☁️ Cloud Connected")
    else:
        st.warning("📂 Local Mode")

    # 시스템 스위치
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

st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud (V40)</h3>", unsafe_allow_html=True)
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
    
    # [복구] 전체 켜기/끄기 버튼
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
            config['tickers'] = edited.to_dict(orient='index')
            save_config(config); st.toast("Saved!")
            
    st.divider()
    # 개별 삭제 기능 복구
    del_cols = st.columns([4, 1])
    del_target = del_cols[0].selectbox("삭제할 종목 선택", options=list(config['tickers'].keys()))
    if del_cols[1].button("삭제"):
        if del_target in config['tickers']:
            del config['tickers'][del_target]
            save_config(config); st.rerun()

with t3:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()[-50:]): st.text(line.strip())
