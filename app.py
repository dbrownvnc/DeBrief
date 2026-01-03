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

# 캐시 초기화
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

# --- 뉴스 검색 엔진 (번역 포함) ---
def get_integrated_news(ticker, strict_mode=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    sec_query = f"{ticker} SEC Filing OR 8-K OR 10-Q"
    search_urls = [
        f"https://news.google.com/rss/search?q={sec_query} when:1d&hl=en-US&gl=US&ceid=US:en",
        f"https://news.google.com/rss/search?q={ticker}+주가+when:1d&hl=ko&gl=KR&ceid=KR:ko",
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
                title = item.find('title').text.split(' - ')[0]
                link = item.find('link').text
                if link in seen_links: continue
                seen_links.add(link)
                
                is_foreign = ("en-US" in url or "SEC" in url)
                if is_foreign:
                    try: title = f"{translator.translate(title[:100])} (원문: {title})"
                    except: pass
                
                prefix = "🏛️[SEC]" if "SEC" in url else "🇺🇸" if is_foreign else "🇰🇷"
                collected_items.append({'title': f"{prefix} {title}", 'link': link})
        except: pass

    for url in search_urls: fetch(url)
    return collected_items

# --- 백그라운드 봇 ---
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
            
            # [복구] 실적 발표일 조회 명령어
            @bot.message_handler(commands=['earning', '실적'])
            def earning_cmd(m):
                try:
                    parts = m.text.split()
                    if len(parts) < 2: return bot.reply_to(m, "⚠️ 사용법: `/earning 티커` (예: /earning TSLA)")
                    t = parts[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    
                    stock = yf.Ticker(t)
                    calendar = stock.calendar
                    
                    if calendar is None or calendar.empty:
                        return bot.reply_to(m, f"❌ {t}: 예정된 실적 발표 데이터가 없습니다.")
                    
                    # 데이터 추출
                    e_date = calendar.iloc[0, 0].strftime('%Y-%m-%d')
                    eps_est = calendar.iloc[0, 1] if len(calendar) > 1 else "N/A"
                    rev_est = calendar.iloc[0, 2] if len(calendar) > 2 else "N/A"
                    
                    # 장 전/후 정보 (yf 라이브러리 특성상 info에서 보충 가능)
                    time_info = "발표 시간 미정"
                    try:
                        info = stock.info
                        if 'earningsCallTimestamp' in info:
                            # 상세 시간 정보가 있을 경우 처리
                            pass 
                    except: pass

                    msg = (f"📅 *{t} 실적 발표 예정*\n\n"
                           f"🗓️ 발표일: `{e_date}`\n"
                           f"💰 예상 EPS: `{eps_est}`\n"
                           f"📈 예상 매출: `{rev_est:,.0f}`\n\n"
                           f"_※ 날짜는 현지 시간 기준이며 변동될 수 있습니다._")
                    bot.reply_to(m, msg, parse_mode='Markdown')
                except Exception as e:
                    bot.reply_to(m, "❌ 실적 정보를 가져오는 중 오류가 발생했습니다.")

            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): bot.reply_to(m, "🤖 *DeBrief V27*\n/earning, /sec, /news, /p, /market")

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

            # [업데이트] 메뉴 설명 추가
            try:
                bot.set_my_commands([
                    BotCommand("earning", "📅 실적 발표일 (예상 EPS/매출)"),
                    BotCommand("sec", "🏛️ 공시 조회 (8-K/10-Q)"),
                    BotCommand("news", "📰 뉴스/소셜 통합 검색"),
                    BotCommand("p", "💰 현재가 조회"),
                    BotCommand("market", "🌍 시장 지수 현황"),
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
                    stock = yf.Ticker(ticker)
                    hist = stock.history(period="5d")
                    if hist.empty: return
                    close = hist['Close']; curr = close.iloc[-1]; prev = close.iloc[-2]
                    
                    # 가격 등락 필터
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

# --- UI (기존 컴팩트 디자인) ---
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
    if st.toggle("System Power", value=config.get('system_active', True)): st.success("🟢 Active")
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
