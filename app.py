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
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from telebot.types import BotCommand
from deep_translator import GoogleTranslator

# --- 프로젝트 설정 ---
CONFIG_FILE = 'debrief_settings.json'
LOG_FILE = 'debrief.log'

# [알림 로직 개선] 캐시가 비어있을 때 발생하는 초기화 오류 방지
if 'news_cache' not in st.session_state: st.session_state['news_cache'] = {}
if 'price_alert_cache' not in st.session_state: st.session_state['price_alert_cache'] = {}
if 'rsi_alert_status' not in st.session_state: st.session_state['rsi_alert_status'] = {}

news_cache = st.session_state['news_cache']
price_alert_cache = st.session_state['price_alert_cache']
rsi_alert_status = st.session_state['rsi_alert_status']

def write_log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {msg}\n")
    except: pass

# --- 설정 로드/저장 ---
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
    
    # Secrets 우선 적용
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

# --- 뉴스 검색 엔진 (번역 포함) ---
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
                    title = item.find('title').text
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

# --- 봇 백그라운드 작업 ---
@st.cache_resource
def start_background_worker():
    def run_bot_system():
        time.sleep(1)
        cfg = load_config()
        token = cfg['telegram']['bot_token']
        chat_id = cfg['telegram']['chat_id']
        
        if not token: return
        
        try:
            bot = telebot.TeleBot(token)
            try: bot.send_message(chat_id, "🤖 DeBrief V29 가동\n실적 발표 기능이 복구되었습니다.")
            except: pass

            # 1. [수정] 실적 발표 명령어 (데이터 소스 변경)
            @bot.message_handler(commands=['earning', '실적'])
            def earning_cmd(m):
                try:
                    parts = m.text.split()
                    if len(parts) < 2: return bot.reply_to(m, "⚠️ 사용법: `/earning 티커` (예: /earning TSLA)")
                    t = parts[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    
                    stock = yf.Ticker(t)
                    
                    # earnings_dates 사용 (calendar 대신)
                    try:
                        dates = stock.earnings_dates
                        if dates is None or dates.empty:
                            raise Exception("데이터 없음")
                        
                        # 타임존 처리 (에러 방지 핵심)
                        now = pd.Timestamp.now().normalize()
                        if dates.index.tz is not None:
                            dates.index = dates.index.tz_localize(None)
                        
                        # 미래 날짜 찾기
                        future = dates[dates.index >= now].sort_index()
                        
                        if not future.empty:
                            target = future.index[0]
                            record = future.loc[target]
                            
                            d_str = target.strftime('%Y-%m-%d')
                            eps = record.get('EPS Estimate', 'N/A')
                            if pd.isna(eps): eps = "N/A"
                            
                            msg = (f"📅 *{t} 차기 실적 발표*\n\n"
                                   f"🗓️ 예정일: `{d_str}`\n"
                                   f"💰 예상 EPS: `{eps}`\n"
                                   f"_※ 날짜는 현지 시간 기준입니다._")
                        else:
                            # 미래 일정이 없으면 가장 최근 과거 기록 보여줌
                            last = dates.index[0]
                            d_str = last.strftime('%Y-%m-%d')
                            msg = f"⚠️ *{t}*의 예정된 발표일이 없습니다.\n(최근 발표일: `{d_str}`)"
                            
                        bot.reply_to(m, msg, parse_mode='Markdown')
                        
                    except Exception as e:
                        bot.reply_to(m, f"❌ {t}: 실적 데이터를 가져올 수 없습니다.\n({e})")

                except Exception as e:
                    bot.reply_to(m, "오류가 발생했습니다.")

            # 2. [복구] 관리 명령어
            @bot.message_handler(commands=['add'])
            def add_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t not in c['tickers']:
                        c['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
                        save_config(c)
                        bot.reply_to(m, f"✅ {t} 추가됨")
                    else: bot.reply_to(m, "이미 존재합니다.")
                except: pass

            @bot.message_handler(commands=['del'])
            def del_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t in c['tickers']:
                        del c['tickers'][t]
                        save_config(c)
                        bot.reply_to(m, f"🗑️ {t} 삭제됨")
                except: pass

            # 3. [복구] 조회 명령어
            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    items = get_integrated_news(t)
                    if not items: return bot.reply_to(m, "뉴스 없음")
                    msg = f"📰 *{t} News*\n" + "\n".join([f"- [{i['title']}]({i['link']})" for i in items[:3]])
                    bot.reply_to(m, msg, parse_mode='Markdown', disable_web_page_preview=True)
                except: pass

            @bot.message_handler(commands=['p'])
            def p_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    p = yf.Ticker(t).fast_info.last_price
                    bot.reply_to(m, f"💰 {t}: ${p:.2f}")
                except: pass

            @bot.message_handler(commands=['market'])
            def market_cmd(m):
                try:
                    idx = {"S&P500":"^GSPC", "Nasdaq":"^IXIC", "USD/KRW":"KRW=X"}
                    txt = "🌍 *Market*\n"
                    for n, t in idx.items():
                        i = yf.Ticker(t).fast_info
                        txt += f"{n}: `{i.last_price:.2f}`\n"
                    bot.reply_to(m, txt, parse_mode='Markdown')
                except: pass
                
            @bot.message_handler(commands=['help', 'start'])
            def help_cmd(m):
                bot.reply_to(m, "🤖 *명령어 목록*\n/earning [티커] : 실적발표일\n/news [티커] : 뉴스 검색\n/add [티커] : 종목 추가\n/del [티커] : 종목 삭제\n/p [티커] : 현재가\n/market : 시장 지수", parse_mode='Markdown')

            # 4. [수정] 메뉴 버튼 설정
            try:
                bot.set_my_commands([
                    BotCommand("earning", "📅 실적 발표일"),
                    BotCommand("news", "📰 뉴스 검색"),
                    BotCommand("add", "➕ 종목 추가"),
                    BotCommand("del", "🗑️ 종목 삭제"),
                    BotCommand("p", "💰 현재가"),
                    BotCommand("market", "🌍 시장 지수"),
                    BotCommand("help", "❓ 도움말")
                ])
            except: pass

            # 5. [수정] 알림 루프 (안정성 강화)
            def monitor_loop():
                while True:
                    try:
                        cfg = load_config()
                        if cfg.get('system_active', True) and cfg['tickers']:
                            t_token = cfg['telegram']['bot_token']
                            t_chat = cfg['telegram']['chat_id']
                            
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items():
                                    exe.submit(analyze_ticker, t, s, t_token, t_chat)
                    except Exception as e: write_log(f"Loop Err: {e}")
                    time.sleep(60)

            def analyze_ticker(ticker, settings, token, chat_id):
                if not settings.get('감시_ON', True): return
                try:
                    # 뉴스 알림
                    if settings.get('뉴스') or settings.get('SEC'):
                        if ticker not in news_cache: news_cache[ticker] = set()
                        items = get_integrated_news(ticker)
                        for item in items:
                            if item['link'] in news_cache[ticker]: continue
                            prefix = "🏛️" if "SEC" in item['title'] else "📰"
                            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                        data={"chat_id": chat_id, "text": f"🔔 {prefix} *[{ticker}]*\n{item['title']}\n{item['link']}", "parse_mode": "Markdown"})
                            news_cache[ticker].add(item['link'])

                    # 가격 알림
                    if settings.get('가격_3%'):
                        stock = yf.Ticker(ticker)
                        h = stock.history(period="2d")
                        if not h.empty:
                            curr = h['Close'].iloc[-1]
                            prev = h['Close'].iloc[-2]
                            pct = ((curr - prev) / prev) * 100
                            
                            if abs(pct) >= 3.0:
                                last = price_alert_cache.get(ticker, 0)
                                if abs(pct - last) >= 1.0:
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                                data={"chat_id": chat_id, "text": f"🔔 *[{ticker}] {'급등 🚀' if pct>0 else '급락 📉'}*\n변동: {pct:.2f}%\n현재: ${curr:.2f}", "parse_mode": "Markdown"})
                                    price_alert_cache[ticker] = pct
                except: pass

            threading.Thread(target=monitor_loop, daemon=True).start()
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
            
        except Exception as e: write_log(f"Bot Error: {e}")

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

st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud</h3>", unsafe_allow_html=True)
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
