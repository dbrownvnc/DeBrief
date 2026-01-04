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

# [알림 로직 개선] 캐시 초기화
if 'news_cache' not in st.session_state: st.session_state['news_cache'] = {}
if 'price_alert_cache' not in st.session_state: st.session_state['price_alert_cache'] = {}
if 'rsi_alert_status' not in st.session_state: st.session_state['rsi_alert_status'] = {}
# [NEW] 경제지표 알림 캐시 (중복 발송 방지용)
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
        "telegram": {"bot_token": "", "chat_id": ""}, 
        "tickers": {
            "TSLA": {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False},
            "NVDA": {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
        } 
    }
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
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except: pass

# ---------------------------------------------------------
# [2] 뉴스 검색 엔진
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

# ---------------------------------------------------------
# [NEW] 경제지표 크롤러 (Investing.com Widget)
# ---------------------------------------------------------
def get_economic_calendar():
    """
    Investing.com 위젯 데이터를 긁어와서 주요 미국 지표만 필터링
    """
    try:
        # 미국(5), 중요도(2,3), 서울시간(88)
        url = "https://sslecal2.forexprostools.com/?columns=exc_flags,exc_currency,exc_importance,exc_actual,exc_forecast,exc_previous&features=datepicker,timezone&countries=5&calType=week&timeZone=88&lang=1"
        headers = {'User-Agent': 'Mozilla/5.0'}
        
        # pandas로 테이블 읽기
        dfs = pd.read_html(url, headers=headers)
        if not dfs: return pd.DataFrame()
        
        df = dfs[0]
        # 컬럼 정리
        df.columns = ['Time', 'Cur', 'Imp', 'Event', 'Actual', 'Forecast', 'Previous', 'Diamond']
        
        # 오늘 날짜 처리 등 전처리
        # 위젯 특성상 날짜가 행으로 구분되어 들어올 수 있음 (복잡한 파싱 필요 없이 단순화)
        # 여기서는 단순 텍스트 매칭으로 처리 (실시간성을 위해)
        return df
    except Exception as e:
        write_log(f"Calendar Error: {e}")
        return pd.DataFrame()

def parse_investing_data():
    """
    정제된 데이터프레임 반환 (날짜, 시간, 이벤트, 중요도, 실제, 예측)
    """
    try:
        df = get_economic_calendar()
        if df.empty: return []
        
        events = []
        current_date = None
        
        # 데이터프레임 순회하며 구조화
        for idx, row in df.iterrows():
            # 날짜 행인지 확인 (예: 2024년 05월 20일 월요일)
            if isinstance(row['Time'], str) and '년' in row['Time']:
                current_date = row['Time']
                continue
            
            # 유효한 이벤트 행인지 확인 (USD, 중요도 별 2개 이상)
            if row['Cur'] == 'USD' and ('🐂' in str(row['Imp']) or '🐃' in str(row['Imp']) or 'High' in str(row['Imp']) or 'Medium' in str(row['Imp'])):
                # 중요도 별 개수 파악 (HTML 파싱이 아니므로 텍스트로 추정하거나, API 특성상 Imp 컬럼 활용)
                # 여기서는 모든 USD 중요 지표 수집
                event_data = {
                    'date': current_date,
                    'time': row['Time'],
                    'event': row['Event'],
                    'actual': str(row['Actual']).replace('\xa0', '').strip(), # 공백 제거
                    'forecast': str(row['Forecast']).replace('\xa0', '').strip(),
                    'previous': str(row['Previous']).replace('\xa0', '').strip(),
                    'id': f"{current_date}_{row['Time']}_{row['Event']}" # 고유 ID 생성
                }
                events.append(event_data)
        return events
    except: return []

# ---------------------------------------------------------
# [3] 백그라운드 봇 (통합 버전)
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
            
            # 상태 변수 (주간/일간 알림 중복 방지용)
            last_weekly_sent = None
            last_daily_sent = None

            try: bot.send_message(chat_id, "🤖 DeBrief V32 가동\n경제지표 스케쥴러 및 실시간 알림이 추가되었습니다.")
            except: pass

            # === [A] 명령어 핸들러 ===
            
            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): 
                msg = ("🤖 *DeBrief V32*\n\n"
                       "📅 *경제/실적*\n"
                       "`/eco` : 이번 주 주요 경제 일정\n"
                       "`/earning 티커` : 실적 발표일\n"
                       "`/vix` : 공포 지수\n\n"
                       "📊 *조회*\n"
                       "`/p 티커` : 현재가\n"
                       "`/news 티커` : 뉴스 검색\n"
                       "`/sec 티커` : 공시 조회\n\n"
                       "⚙️ *관리*\n"
                       "`/list` : 감시 목록\n"
                       "`/add 티커` : 추가\n"
                       "`/del 티커` : 삭제")
                bot.reply_to(m, msg, parse_mode='Markdown')

            # [NEW] 경제 일정 수동 조회
            @bot.message_handler(commands=['eco', '일정'])
            def eco_cmd(m):
                bot.send_chat_action(m.chat.id, 'typing')
                events = parse_investing_data()
                if not events:
                    return bot.reply_to(m, "❌ 경제지표 데이터를 가져올 수 없습니다.")
                
                # 오늘 이후 일정만 필터링 (간단히)
                msg = "📅 *주요 경제 일정 (USD)*\n────────────────"
                count = 0
                for e in events:
                    # 중요 이벤트만 표시 (FOMC, CPI, Rate, GDP 등 키워드 필터링 추천)
                    if any(k in e['event'] for k in ['Rate', 'CPI', 'PPI', 'GDP', 'Employment', 'Fed', 'Meeting']):
                        actual_display = f"✅{e['actual']}" if e['actual'] and e['actual'] != 'nan' else "⏳대기"
                        msg += f"\n🗓️ {e['date']} {e['time']}\n🔥 *{e['event']}*\n(예상: {e['forecast']} / 실제: {actual_display})\n"
                        count += 1
                        if count >= 10: break # 너무 길면 자름
                
                bot.reply_to(m, msg, parse_mode='Markdown')

            @bot.message_handler(commands=['earning'])
            def earning_cmd(m):
                try:
                    t = m.text.split()[1].upper()
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
                        timing = "☀️ 장전" if target.hour < 12 else "🌙 장후"
                        bot.reply_to(m, f"📅 *{t} 실적 발표*\n🗓️ {target.strftime('%Y-%m-%d')} ({timing})\n💰 예상 EPS: {est}", parse_mode='Markdown')
                    else:
                        bot.reply_to(m, f"⚠️ {t}: 예정된 일정 없음")
                except: bot.reply_to(m, "오류 발생")

            @bot.message_handler(commands=['vix'])
            def vix_cmd(m):
                try:
                    v = yf.Ticker("^VIX").fast_info
                    pct = ((v.last_price - v.previous_close)/v.previous_close)*100
                    bot.reply_to(m, f"😨 *VIX*: `{v.last_price:.2f}` ({pct:+.2f}%)", parse_mode='Markdown')
                except: pass

            @bot.message_handler(commands=['add'])
            def add_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    c['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
                    save_config(c); bot.reply_to(m, f"✅ {t} 추가됨")
                except: pass

            @bot.message_handler(commands=['del'])
            def del_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t in c['tickers']: del c['tickers'][t]; save_config(c)
                    bot.reply_to(m, f"🗑️ {t} 삭제됨")
                except: pass

            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    items = get_integrated_news(t)
                    msg = f"📰 *{t} News*\n" + "\n".join([f"- [{i['title']}]({i['link']})" for i in items])
                    bot.reply_to(m, msg, parse_mode='Markdown', disable_web_page_preview=True)
                except: pass

            @bot.message_handler(commands=['p'])
            def p_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    p = yf.Ticker(t).fast_info.last_price
                    bot.reply_to(m, f"💰 {t}: ${p:.2f}")
                except: pass

            @bot.message_handler(commands=['sec'])
            def sec_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    items = get_integrated_news(t) # SEC 통합 검색 사용
                    sec_items = [i for i in items if "SEC" in i['title']]
                    if sec_items:
                        msg = f"🏛️ *{t} SEC*\n" + "\n".join([f"- [{i['title']}]({i['link']})" for i in sec_items])
                        bot.reply_to(m, msg, parse_mode='Markdown')
                    else: bot.reply_to(m, "최근 공시 없음")
                except: pass

            @bot.message_handler(commands=['market'])
            def market_cmd(m):
                try:
                    idx = {"S&P500":"^GSPC", "Nasdaq":"^IXIC", "KRW":"KRW=X"}
                    txt = "🌍 *Market*\n"
                    for n, t in idx.items():
                        i = yf.Ticker(t).fast_info
                        txt += f"{n}: `{i.last_price:.2f}`\n"
                    bot.reply_to(m, txt, parse_mode='Markdown')
                except: pass

            try:
                bot.set_my_commands([
                    BotCommand("eco", "📅 경제 일정 (주요 지표)"),
                    BotCommand("earning", "💰 실적 발표일"),
                    BotCommand("news", "📰 뉴스 검색"),
                    BotCommand("vix", "😨 공포 지수"),
                    BotCommand("p", "💰 현재가"),
                    BotCommand("add", "➕ 추가"), BotCommand("del", "🗑️ 삭제"),
                    BotCommand("help", "❓ 도움말")
                ])
            except: pass

            # ==========================================
            # [B] 통합 감시 루프 (주식 + 경제지표)
            # ==========================================
            def monitor_loop():
                nonlocal last_weekly_sent, last_daily_sent
                
                while True:
                    try:
                        now = datetime.now()
                        current_hour = now.hour
                        current_weekday = now.weekday() # 0:월요일
                        
                        # --- 1. 경제지표 스케쥴링 알림 ---
                        
                        # (1) 월요일 아침 8시: 주간 브리핑
                        if current_weekday == 0 and current_hour == 8:
                            str_date = now.strftime('%Y-%m-%d')
                            if last_weekly_sent != str_date:
                                events = parse_investing_data()
                                # 이번주 중요 지표만 요약
                                key_events = [e for e in events if any(k in e['event'] for k in ['Rate', 'CPI', 'PPI', 'GDP', 'Fed'])]
                                if key_events:
                                    msg = "📅 *이번 주 주요 경제 일정*\n────────────────"
                                    for e in key_events:
                                        msg += f"\n🗓️ {e['date']} {e['time']}\n🔥 {e['event']} (예상: {e['forecast']})"
                                    bot.send_message(chat_id, msg, parse_mode='Markdown')
                                    last_weekly_sent = str_date

                        # (2) 매일 아침 8시: 데일리 브리핑
                        if current_hour == 8:
                            str_date = now.strftime('%Y-%m-%d')
                            if last_daily_sent != str_date:
                                events = parse_investing_data()
                                # 오늘 날짜(한글) 매칭 필요 (간단히 오늘 날짜 포함된 이벤트 필터링)
                                today_kor = f"{now.month}월 {now.day}일"
                                today_events = [e for e in events if today_kor in e['date']]
                                if today_events:
                                    msg = f"☀️ *오늘({today_kor}) 주요 일정*\n────────────────"
                                    for e in today_events:
                                        msg += f"\n⏰ {e['time']} : {e['event']} (예상: {e['forecast']})"
                                    bot.send_message(chat_id, msg, parse_mode='Markdown')
                                    last_daily_sent = str_date

                        # (3) 실시간 결과 알림 (Actual 값 떴을 때)
                        # 1분마다 체크
                        events = parse_investing_data()
                        for e in events:
                            # 실제값이 있고(nan이 아니고), 캐시에 없는 경우
                            if e['actual'] and e['actual'] != 'nan' and e['id'] not in eco_alert_cache:
                                # 중요 키워드 포함 시에만 알림 (알림 폭탄 방지)
                                if any(k in e['event'] for k in ['CPI', 'PPI', 'GDP', 'Rate', 'Employment', 'Inventory', 'Fed']):
                                    msg = (f"🚨 *경제지표 발표*\n"
                                           f"🔥 *{e['event']}*\n"
                                           f"────────────────\n"
                                           f"✅ 실제: `{e['actual']}`\n"
                                           f"📊 예상: `{e['forecast']}`\n"
                                           f"🔙 이전: `{e['previous']}`")
                                    bot.send_message(chat_id, msg, parse_mode='Markdown')
                                    eco_alert_cache.add(e['id']) # 알림 보냄 처리

                        # --- 2. 주식 가격/뉴스 알림 (기존 로직) ---
                        cfg = load_config()
                        if cfg['system_active'] and cfg['tickers']:
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items():
                                    exe.submit(analyze_ticker, t, s, token, chat_id)
                                    
                    except Exception as e: write_log(f"Loop Err: {e}")
                    time.sleep(60) # 1분 대기

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
                                        data={"chat_id": chat_id, "text": f"🔔 {prefix} *[{ticker}]*\n{item['title']}\n{item['link']}", "parse_mode": "Markdown"})
                            news_cache[ticker].add(item['link'])
                    
                    # 가격 급등락
                    if settings.get('가격_3%'):
                        stock = yf.Ticker(ticker)
                        h = stock.history(period="2d")
                        if not h.empty:
                            curr = h['Close'].iloc[-1]; prev = h['Close'].iloc[-2]
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
            
        except Exception as e:
            write_log(f"🔥 Bot Error: {e}")

    t_bot = threading.Thread(target=run_bot_system, daemon=True)
    t_bot.start()

start_background_worker()

# --- UI (기존 디자인 유지) ---
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
    else: st.error("⛔ Paused")
    with st.expander("🔑 Keys"):
        bot_t = st.text_input("Bot Token", value=config['telegram'].get('bot_token', ''), type="password")
        chat_i = st.text_input("Chat ID", value=config['telegram'].get('chat_id', ''))
        if st.button("Save Keys"):
            config['telegram'].update({"bot_token": bot_t, "chat_id": chat_i})
            save_config(config); st.rerun()

st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud (Eco V32)</h3>", unsafe_allow_html=True)
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
    input_t = st.text_input("Add Tickers")
    if st.button("➕ Add"):
        for t in [x.strip().upper() for x in input_t.split(',') if x.strip()]:
            config['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
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
