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

# --- 프로젝트 설정 ---
CONFIG_FILE = 'debrief_settings.json'
LOG_FILE = 'debrief.log'

# ---------------------------------------------------------
# [1] 시스템 함수 및 설정 (기존 worker.py + app.py 공통)
# ---------------------------------------------------------
def load_config():
    # 파일이 없으면 기본값 생성
    default_config = {"system_active": True, "telegram": {"bot_token": "", "chat_id": ""}, "tickers": {}}
    if not os.path.exists(CONFIG_FILE): return default_config
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def write_log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Streamlit Cloud에서는 로그를 터미널에도 찍어주는 게 좋음
    print(f"[{timestamp}] {msg}")
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {msg}\n")
    except: pass

# ---------------------------------------------------------
# [2] 백그라운드 봇 & 감시 로직 (st.cache_resource로 1회만 실행)
# ---------------------------------------------------------
# 이 함수는 앱이 처음 켜질 때 딱 한 번만 실행되어 백그라운드에서 계속 돕니다.
@st.cache_resource
def start_background_worker():
    # 별도 스레드에서 봇과 감시 루프 실행
    def run_bot_system():
        # 설정 로드 대기 (파일이 생길 때까지 잠시 대기하거나 기본값 로드)
        time.sleep(3) 
        
        # 봇 토큰 확인
        cfg = load_config()
        if not cfg['telegram']['bot_token']:
            print("⚠️ 봇 토큰이 없어 대기 중...")
            return

        BOT_TOKEN = cfg['telegram']['bot_token']
        bot = telebot.TeleBot(BOT_TOKEN)
        news_cache = {}

        # --- 구글 뉴스 RSS ---
        def get_google_news_rss(ticker):
            headers = {"User-Agent": "Mozilla/5.0"}
            url = f"https://news.google.com/rss/search?q={ticker}+stock+when:1d&hl=ko&gl=KR&ceid=KR:ko"
            try:
                response = requests.get(url, headers=headers, timeout=5)
                root = ET.fromstring(response.content)
                news_items = []
                for item in root.findall('.//item')[:3]: 
                    try:
                        title = item.find('title').text.split(' - ')[0]
                        link = item.find('link').text
                        news_items.append({'title': title, 'link': link})
                    except: continue
                return news_items
            except: return []

        # --- 메시지 전송 ---
        def send_msg(token, chat_id, msg):
            try: requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": msg})
            except: pass

        # --- 감시 로직 (1분 주기) ---
        def monitor_loop():
            while True:
                try:
                    cfg = load_config()
                    if cfg and cfg.get('system_active', True) and cfg['tickers']:
                        token, chat_id = cfg['telegram']['bot_token'], cfg['telegram']['chat_id']
                        if not token or not chat_id: 
                            time.sleep(60)
                            continue

                        # 뉴스/가격 감시 병렬 처리
                        with ThreadPoolExecutor(max_workers=5) as exe:
                            for t, s in cfg['tickers'].items():
                                if not s.get('감시_ON', True): continue
                                
                                # (A) 뉴스 감시
                                if s.get('뉴스'):
                                    if t not in news_cache: news_cache[t] = set()
                                    news = get_google_news_rss(t)
                                    for item in news:
                                        if item['link'] not in news_cache[t]:
                                            if len(news_cache[t]) > 0: # 최초 실행시는 알림 스킵
                                                send_msg(token, chat_id, f"🚨 [속보] {t}\n📰 {item['title']}\n{item['link']}")
                                            news_cache[t].add(item['link'])
                                
                                # (B) 가격/지표 감시 (기존 로직 축약)
                                try:
                                    stock = yf.Ticker(t)
                                    info = stock.fast_info
                                    curr = info.last_price
                                    
                                    # 가격 3%
                                    if s.get('가격_3%'):
                                        pct = ((curr - info.previous_close)/info.previous_close)*100
                                        if abs(pct) >= 3.0:
                                            emoji = "🚀" if pct>0 else "📉"
                                            send_msg(token, chat_id, f"[{t}] {emoji} {pct:.2f}%\n${curr:.2f}")
                                    
                                    # 기술적 분석 (데이터 필요시)
                                    if any(s.get(k) for k in ['RSI', 'MA_크로스', '볼린저', 'MACD']):
                                        hist = stock.history(period="1y")
                                        if not hist.empty:
                                            close = hist['Close']
                                            # RSI
                                            if s.get('RSI'):
                                                delta = close.diff()
                                                gain = (delta.where(delta>0, 0)).rolling(14).mean()
                                                loss = (-delta.where(delta<0, 0)).rolling(14).mean()
                                                rs = gain/loss
                                                rsi = 100 - (100/(1+rs)).iloc[-1]
                                                if rsi >= 70: send_msg(token, chat_id, f"[{t}] 🔥 RSI 과매수 ({rsi:.1f})")
                                                elif rsi <= 30: send_msg(token, chat_id, f"[{t}] 💧 RSI 과매도 ({rsi:.1f})")
                                except: pass
                except Exception as e: 
                    print(f"Monitor Error: {e}")
                
                time.sleep(60)

        # --- 봇 명령어 핸들러 ---
        @bot.message_handler(commands=['start', 'help'])
        def h(m): bot.reply_to(m, "🤖 DeBrief Cloud Bot Running!")

        @bot.message_handler(commands=['p'])
        def p(m):
            try:
                t = m.text.split()[1].upper()
                p = yf.Ticker(t).fast_info.last_price
                bot.reply_to(m, f"💰 {t}: ${p:.2f}")
            except: bot.reply_to(m, "Error")

        @bot.message_handler(commands=['news'])
        def n(m):
            try:
                t = m.text.split()[1].upper()
                d = get_google_news_rss(t)
                if not d: bot.reply_to(m, "No News")
                else:
                    txt = f"📰 {t} News\n"
                    for i, x in enumerate(d): txt += f"\n{i+1}. [{x['title']}]({x['link']})"
                    bot.reply_to(m, txt, parse_mode='Markdown', disable_web_page_preview=True)
            except: pass

        # 봇 메뉴 등록
        try:
            bot.set_my_commands([
                BotCommand("p", "현재가"), BotCommand("news", "뉴스"), 
                BotCommand("list", "목록"), BotCommand("help", "도움말")
            ])
        except: pass

        # 스레드 실행
        t_mon = threading.Thread(target=monitor_loop, daemon=True)
        t_mon.start()
        
        print("🚀 Background Worker Started")
        try: bot.infinity_polling()
        except: pass

    # 메인 봇 스레드 시작
    t_bot = threading.Thread(target=run_bot_system, daemon=True)
    t_bot.start()

# [핵심] 봇 실행 (캐시되어서 1번만 실행됨)
start_background_worker()


# ---------------------------------------------------------
# [3] Streamlit UI (기존 app.py UI 코드)
# ---------------------------------------------------------
st.markdown("""
<style>
    .stApp { background-color: #FFFFFF; color: #202124; }
    .stock-card {
        background-color: #FFFFFF; border: 1px solid #DADCE0; border-radius: 12px;
        padding: 15px 10px; margin-bottom: 12px; text-align: center;
        box-shadow: 0 4px 6px rgba(0,0,0,0.05); transition: transform 0.2s;
    }
    .stock-card:hover { transform: translateY(-3px); box-shadow: 0 6px 12px rgba(0,0,0,0.1); }
    .stock-symbol { font-family: 'Inter', sans-serif; font-size: 1.25em; font-weight: 800; color: #1A73E8; margin-bottom: 4px; }
    .stock-name { font-size: 0.8em; color: #5F6368; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 8px; }
    .stock-price-box { display: inline-block; padding: 5px 12px; border-radius: 16px; font-size: 0.95em; font-weight: 700; }
    .up-theme { background-color: #E6F4EA; color: #137333; border: 1px solid #CEEAD6; }
    .down-theme { background-color: #FCE8E6; color: #C5221F; border: 1px solid #FAD2CF; }
    .stTextInput input, .stSelectbox div[data-baseweb="select"], .stMultiSelect {
        background-color: #FFFFFF !important; color: #202124 !important; border: 1px solid #DADCE0 !important; border-radius: 8px !important;
    }
    [data-testid="stDataEditor"] { border: 1px solid #DADCE0 !important; border-radius: 8px; background-color: #FFFFFF !important; }
    [data-testid="stDataEditor"] * { color: #202124 !important; background-color: #FFFFFF !important; }
</style>
""", unsafe_allow_html=True)

def get_stock_data(tickers):
    if not tickers: return {}
    if 'company_names' not in st.session_state: st.session_state['company_names'] = {}
    info_dict = {}
    try:
        tickers_str = " ".join(tickers)
        data = yf.Tickers(tickers_str)
        for ticker in tickers:
            try:
                if ticker not in st.session_state['company_names']:
                    try: st.session_state['company_names'][ticker] = data.tickers[ticker].info.get('shortName', ticker)
                    except: st.session_state['company_names'][ticker] = ticker
                info = data.tickers[ticker].fast_info
                curr = info.last_price
                prev = info.previous_close
                change = ((curr - prev) / prev) * 100
                info_dict[ticker] = {"name": st.session_state['company_names'][ticker], "price": curr, "change": change}
            except: info_dict[ticker] = {"name": ticker, "price": 0, "change": 0}
        return info_dict
    except: return {}

st.set_page_config(page_title="DeBrief", layout="wide", page_icon="📡")
st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud</h3>", unsafe_allow_html=True)

config = load_config()

with st.sidebar:
    st.header("🎛️ Control Panel")
    system_on = st.toggle("System Power", value=config.get('system_active', True))
    if system_on != config.get('system_active', True):
        config['system_active'] = system_on
        save_config(config)
        st.rerun()
    if not system_on: st.error("⛔ Paused")
    else: st.success("🟢 Active")
    st.divider()
    with st.expander("🔑 Telegram Keys"):
        bot_token = st.text_input("Bot Token", value=config['telegram'].get('bot_token', ''), type="password")
        chat_id = st.text_input("Chat ID", value=config['telegram'].get('chat_id', ''))
        if st.button("Save Keys", type="primary"):
            config['telegram']['bot_token'] = bot_token
            config['telegram']['chat_id'] = chat_id
            save_config(config)

tab1, tab2, tab3 = st.tabs(["📊 Dashboard", "⚙️ Management", "📜 Logs"])

with tab1:
    col_top1, col_top2 = st.columns([8, 1])
    with col_top2:
        if st.button("Refresh", use_container_width=True): st.rerun()

    if config['tickers'] and config['system_active']:
        ticker_list = list(config['tickers'].keys())
        stock_data = get_stock_data(ticker_list)
        cols = st.columns(6)
        for i, ticker in enumerate(ticker_list):
            info = stock_data.get(ticker, {"name": ticker, "price":0, "change":0})
            theme_class = "up-theme" if info['change'] >= 0 else "down-theme"
            sign = "+" if info['change'] >= 0 else ""
            html_code = f"""
            <div class="stock-card">
                <div class="stock-symbol">{ticker}</div>
                <div class="stock-name">{info['name']}</div>
                <div class="stock-price-box {theme_class}">
                    ${info['price']:.2f} <span style="font-size:0.8em; margin-left:4px;">{sign}{info['change']:.2f}%</span>
                </div>
            </div>"""
            with cols[i % 6]: st.markdown(html_code, unsafe_allow_html=True)
    elif not config['system_active']: st.warning("Paused")
    else: st.info("No tickers.")

with tab2:
    st.markdown("##### Add New Tickers")
    c1, c2, c3 = st.columns([4, 1, 1])
    with c1: input_tickers = st.text_input("Add Tickers", placeholder="e.g. TSLA", label_visibility="collapsed")
    with c2:
        if st.button("➕ Add", use_container_width=True, type="primary"):
            if input_tickers:
                for t in [x.strip().upper() for x in input_tickers.split(',') if x.strip()]:
                    if t not in config['tickers']:
                        config['tickers'][t] = {"감시_ON": True, "뉴스": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
                save_config(config)
                st.rerun()
    with c3:
        if st.button("🔤 Sort", use_container_width=True):
            config['tickers'] = dict(sorted(config['tickers'].items()))
            save_config(config)
            st.rerun()
    st.markdown("---")
    st.markdown("##### Settings")
    if config['tickers']:
        data_list = []
        for t, settings in config['tickers'].items():
            row = settings.copy()
            row['Name'] = st.session_state.get('company_names', {}).get(t, t)
            data_list.append(row)
        df = pd.DataFrame(data_list, index=config['tickers'].keys())
        cols_order = ["Name", "감시_ON", "뉴스", "가격_3%", "거래량_2배", "52주_신고가", "RSI", "MA_크로스", "볼린저", "MACD"]
        df = df.reindex(columns=cols_order, fill_value=False)
        column_config = {
            "Name": st.column_config.TextColumn("Company", disabled=True, width="small"),
            "감시_ON": st.column_config.CheckboxColumn("✅"), "뉴스": st.column_config.CheckboxColumn("📰"),
            "가격_3%": st.column_config.CheckboxColumn("📈"), "거래량_2배": st.column_config.CheckboxColumn("📢"),
            "52주_신고가": st.column_config.CheckboxColumn("🏆"), "RSI": st.column_config.CheckboxColumn("📊"),
            "MA_크로스": st.column_config.CheckboxColumn("❌"), "볼린저": st.column_config.CheckboxColumn("🍩"),
            "MACD": st.column_config.CheckboxColumn("🌊")
        }
        edited_df = st.data_editor(df, column_config=column_config, use_container_width=True, key="ticker_editor")
        if not df.equals(edited_df):
            temp_dict = edited_df.to_dict(orient='index')
            for t in temp_dict:
                if 'Name' in temp_dict[t]: del temp_dict[t]['Name']
            config['tickers'] = temp_dict
            save_config(config)
            st.toast("Saved!", icon="💾")
        st.markdown("---")
        col_del1, col_del2 = st.columns([4, 1])
        with col_del1: del_targets = st.multiselect("Select tickers", options=list(config['tickers'].keys()), label_visibility="collapsed")
        with col_del2:
            if st.button("Delete", use_container_width=True, type="primary"):
                if del_targets:
                    for t in del_targets:
                        if t in config['tickers']: del config['tickers'][t]
                    save_config(config)
                    st.rerun()

with tab3:
    if st.button("Reload"): st.rerun()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()[-50:]): st.text(line.strip())