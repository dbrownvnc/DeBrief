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
# 번역 라이브러리
from deep_translator import GoogleTranslator

# --- 프로젝트 설정 ---
CONFIG_FILE = 'debrief_settings.json'
LOG_FILE = 'debrief.log'

# 알림 캐시 통합 관리 (중복 방지용)
if 'news_cache' not in st.session_state: st.session_state['news_cache'] = {}
if 'price_alert_cache' not in st.session_state: st.session_state['price_alert_cache'] = {}
if 'rsi_alert_status' not in st.session_state: st.session_state['rsi_alert_status'] = {}

news_cache = st.session_state['news_cache']
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
# [1] 설정 로드/저장 (JSONBin + 로컬 백업)
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
    url = get_jsonbin_url()
    headers = get_jsonbin_headers()
    if url and headers:
        try:
            resp = requests.get(f"{url}/latest", headers=headers, timeout=5)
            if resp.status_code == 200:
                cloud_data = resp.json()['record']
                if "tickers" in cloud_data and cloud_data['tickers']:
                    config = cloud_data
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
# [2] 뉴스 검색 엔진 (번역 기능 포함)
# ---------------------------------------------------------
def get_integrated_news(ticker, strict_mode=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    sec_query = f"{ticker} SEC Filing OR 8-K OR 10-Q"
    search_urls = [
        f"https://news.google.com/rss/search?q={sec_query} when:1d&hl=en-US&gl=US&ceid=US:en",
        f"https://news.google.com/rss/search?q={ticker}+주가+when:1d&hl=ko&gl=KR&ceid=KR:ko",
        f"https://news.google.com/rss/search?q={ticker}+stock+news+when:1d&hl=en-US&gl=US&ceid=US:en",
        f"https://news.google.com/rss/search?q={ticker}+stock+(twitter+OR+reddit)+when:1d&hl=en-US&gl=US&ceid=US:en"
    ]
    if not strict_mode:
        search_urls.append(f"https://news.google.com/rss/search?q={ticker}+stock&hl=ko&gl=KR&ceid=KR:ko")

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
                    
                    prefix = "🇰🇷"
                    is_foreign = False
                    
                    if "SEC" in url or "8-K" in title or "10-Q" in title: 
                        prefix = "🏛️[SEC]"; is_foreign = True
                    elif "twitter" in url or "reddit" in url: 
                        prefix = "🐦[Social]"; is_foreign = True
                    elif "en-US" in url: 
                        prefix = "🇺🇸[Global]"; is_foreign = True
                    
                    if is_foreign:
                        try:
                            translated_title = translator.translate(title[:100])
                            title = f"{translated_title} (원문: {title})"
                        except: pass

                    collected_items.append({'title': f"{prefix} {title}", 'link': link})
                except: continue
        except: pass

    for url in search_urls:
        fetch(url)
        if len(collected_items) >= 8: break
    return collected_items

# ---------------------------------------------------------
# [3] 백그라운드 봇 (감시 및 명령어 통합)
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
            try: bot.send_message(chat_id, "🤖 DeBrief 시스템 가동 (V26)\n번역 및 알림 최적화 적용됨")
            except: pass

            # 명령어 핸들러
            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): bot.reply_to(m, "🤖 *DeBrief V26*\n/sec, /news, /add, /del, /market, /p")

            @bot.message_handler(commands=['add'])
            def add_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t not in c['tickers']:
                        c['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
                        save_config(c)
                        bot.reply_to(m, f"✅ {t} 추가됨")
                    else: bot.reply_to(m, "이미 존재함")
                except: bot.reply_to(m, "사용법: /add TSLA")

            @bot.message_handler(commands=['del'])
            def del_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t in c['tickers']:
                        del c['tickers'][t]
                        save_config(c)
                        bot.reply_to(m, f"🗑️ {t} 삭제됨")
                except: bot.reply_to(m, "사용법: /del TSLA")

            @bot.message_handler(commands=['sec'])
            def sec_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    url = f"https://news.google.com/rss/search?q={t}+SEC+Filing+OR+8-K+OR+10-Q&hl=en-US&gl=US&ceid=US:en"
                    res = requests.get(url, timeout=5)
                    root = ET.fromstring(res.content)
                    items = [f"📄 [{item.find('title').text}]({item.find('link').text})" for item in root.findall('.//item')[:5]]
                    bot.reply_to(m, f"🏛️ *{t} 공시*\n\n" + "\n\n".join(items), parse_mode='Markdown', disable_web_page_preview=True)
                except: bot.reply_to(m, "조회 실패")

            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    data = get_integrated_news(t)
                    txt = f"📰 *{t} 뉴스*\n"
                    for i, n in enumerate(data): txt += f"\n{i+1}. {n['title']}\n🔗 {n['link']}\n"
                    bot.reply_to(m, txt, parse_mode='Markdown', disable_web_page_preview=True)
                except: bot.reply_to(m, "조회 실패")

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
                    txt = "🌍 *시장 지수*\n"
                    for n, t in idx.items():
                        i = yf.Ticker(t).fast_info
                        txt += f"{n}: `{i.last_price:.2f}`\n"
                    bot.reply_to(m, txt, parse_mode='Markdown')
                except: pass

            try:
                bot.set_my_commands([
                    BotCommand("add", "➕ 추가"), BotCommand("del", "🗑️ 삭제"),
                    BotCommand("sec", "🏛️ 공시"), BotCommand("news", "📰 뉴스"),
                    BotCommand("p", "💰 현재가"), BotCommand("market", "🌍 시장"),
                    BotCommand("help", "❓ 도움말")
                ])
            except: pass

            # 감시 루프
            def send_alert(token, chat_id, title, msg):
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"🔔 *[{title}]*\n{msg}", "parse_mode": "Markdown"})

            def monitor_loop():
                while True:
                    try:
                        cfg = load_config()
                        if cfg.get('system_active', True) and cfg['tickers']:
                            t_token, t_chat = cfg['telegram']['bot_token'], cfg['telegram']['chat_id']
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for ticker, settings in cfg['tickers'].items():
                                    exe.submit(analyze_ticker, ticker, settings, t_token, t_chat)
                    except: pass
                    time.sleep(60)

            def analyze_ticker(ticker, settings, token, chat_id):
                if not settings.get('감시_ON', True): return
                try:
                    # 뉴스/공시
                    if settings.get('뉴스') or settings.get('SEC'):
                        if ticker not in news_cache: news_cache[ticker] = set()
                        items = get_integrated_news(ticker, strict_mode=True)
                        for item in items:
                            if item['link'] in news_cache[ticker]: continue
                            is_sec = "🏛️" in item['title']
                            if (is_sec and settings.get('SEC')) or (not is_sec and settings.get('뉴스')):
                                if len(news_cache[ticker]) > 0:
                                    send_alert(token, chat_id, f"{ticker} 소식", f"{item['title']}\n🔗 [Link]({item['link']})")
                                news_cache[ticker].add(item['link'])
                    
                    # 가격 및 RSI
                    stock = yf.Ticker(ticker); hist = stock.history(period="1y")
                    if hist.empty: return
                    close = hist['Close']; curr = close.iloc[-1]; prev = close.iloc[-2]
                    
                    # 등락 필터
                    if settings.get('가격_3%'):
                        pct = ((curr - prev) / prev) * 100
                        if abs(pct) >= 3.0:
                            last_p = price_alert_cache.get(ticker, 0.0)
                            if abs(pct - last_p) >= 1.0:
                                send_alert(token, chat_id, f"{ticker} {'급등 🚀' if pct>0 else '급락 📉'}", f"변동: {pct:.2f}%\n현재: ${curr:.2f}")
                                price_alert_cache[ticker] = pct

                    # RSI 필터
                    if settings.get('RSI'):
                        delta = close.diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean(); loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                        rsi = 100 - (100 / (1 + gain/loss)).iloc[-1]
                        status = rsi_alert_status.get(ticker, "NORMAL")
                        if rsi >= 70 and status != "OB":
                            send_alert(token, chat_id, f"{ticker} 과매수 🔥", f"RSI: {rsi:.1f}"); rsi_alert_status[ticker] = "OB"
                        elif rsi <= 30 and status != "OS":
                            send_alert(token, chat_id, f"{ticker} 과매도 💧", f"RSI: {rsi:.1f}"); rsi_alert_status[ticker] = "OS"
                        elif 35 < rsi < 65: rsi_alert_status[ticker] = "NORMAL"

                except: pass

            threading.Thread(target=monitor_loop, daemon=True).start()
            bot.infinity_polling(timeout=10)
        except Exception as e: write_log(f"Bot Error: {e}")

    threading.Thread(target=run_bot_system, daemon=True).start()

start_background_worker()

# ---------------------------------------------------------
# [4] UI (컴팩트 디자인)
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
    input_t = st.text_input("Add Tickers (TSLA, NVDA...)")
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
