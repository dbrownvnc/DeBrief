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
# [1] 설정 로드 함수 (Secrets 우선 확인 + 파일 백업)
# ---------------------------------------------------------
def load_config():
    # 1. 기본 설정 템플릿
    config = {
        "system_active": True, 
        "telegram": {"bot_token": "", "chat_id": ""}, 
        "tickers": {} 
    }

    # 2. Streamlit Secrets(금고) 확인
    if "telegram" in st.secrets:
        config['telegram']['bot_token'] = st.secrets["telegram"]["bot_token"]
        config['telegram']['chat_id'] = st.secrets["telegram"]["chat_id"]

    # 3. 로컬 파일 확인 (있으면 덮어쓰기)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                saved_config = json.load(f)
                if "tickers" in saved_config: 
                    config['tickers'] = saved_config['tickers']
                # 파일에 저장된 키가 있다면 (로컬 테스트용)
                if saved_config['telegram']['bot_token']:
                    config['telegram'] = saved_config['telegram']
        except: pass
    
    return config

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

# ---------------------------------------------------------
# [2] 백그라운드 봇
# ---------------------------------------------------------
@st.cache_resource
def start_background_worker():
    def run_bot_system():
        time.sleep(2)
        cfg = load_config()
        
        if not cfg['telegram']['bot_token']: 
            print("⚠️ 텔레그램 토큰 미설정")
            return
        
        try:
            BOT_TOKEN = cfg['telegram']['bot_token']
            bot = telebot.TeleBot(BOT_TOKEN)
            news_cache = {}

            def get_google_news_rss(ticker):
                headers = {"User-Agent": "Mozilla/5.0"}
                url = f"https://news.google.com/rss/search?q={ticker}+stock+when:1d&hl=ko&gl=KR&ceid=KR:ko"
                try:
                    response = requests.get(url, headers=headers, timeout=5)
                    root = ET.fromstring(response.content)
                    items = []
                    for item in root.findall('.//item')[:3]: 
                        try: items.append({'title': item.find('title').text.split(' - ')[0], 'link': item.find('link').text})
                        except: continue
                    return items
                except: return []

            def send_msg(token, chat_id, msg):
                try: requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": msg})
                except: pass

            def monitor_loop():
                while True:
                    try:
                        cfg = load_config()
                        if cfg.get('system_active', True) and cfg['tickers']:
                            token = cfg['telegram']['bot_token']
                            chat_id = cfg['telegram']['chat_id']
                            
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items():
                                    if not s.get('감시_ON', True): continue
                                    
                                    # 뉴스
                                    if s.get('뉴스'):
                                        if t not in news_cache: news_cache[t] = set()
                                        news = get_google_news_rss(t)
                                        for item in news:
                                            if item['link'] not in news_cache[t]:
                                                if len(news_cache[t]) > 0:
                                                    send_msg(token, chat_id, f"🚨 [속보] {t}\n📰 {item['title']}\n{item['link']}")
                                                news_cache[t].add(item['link'])
                                    
                                    # 가격 및 기술적 분석
                                    try:
                                        stock = yf.Ticker(t)
                                        info = stock.fast_info
                                        curr = info.last_price
                                        
                                        if s.get('가격_3%'):
                                            pct = ((curr - info.previous_close)/info.previous_close)*100
                                            if abs(pct) >= 3.0:
                                                emoji = "🚀" if pct>0 else "📉"
                                                send_msg(token, chat_id, f"[{t}] {emoji} {pct:.2f}%\n${curr:.2f}")

                                        if any(s.get(k) for k in ['RSI', 'MA_크로스', '볼린저', 'MACD']):
                                            hist = stock.history(period="1y")
                                            if not hist.empty:
                                                close = hist['Close']
                                                # RSI 예시
                                                if s.get('RSI'):
                                                    delta = close.diff()
                                                    gain = (delta.where(delta>0, 0)).rolling(14).mean()
                                                    loss = (-delta.where(delta<0, 0)).rolling(14).mean()
                                                    rs = gain/loss
                                                    rsi = 100 - (100/(1+rs)).iloc[-1]
                                                    if rsi >= 70: send_msg(token, chat_id, f"[{t}] 🔥 RSI 과매수 ({rsi:.1f})")
                                                    elif rsi <= 30: send_msg(token, chat_id, f"[{t}] 💧 RSI 과매도 ({rsi:.1f})")
                                    except: pass
                    except: pass
                    time.sleep(60)

            @bot.message_handler(commands=['start'])
            def s(m): bot.reply_to(m, "🤖 DeBrief Cloud Active")
            
            t_mon = threading.Thread(target=monitor_loop, daemon=True)
            t_mon.start()
            try: bot.infinity_polling()
            except: pass
            
        except Exception as e:
            print(f"Bot Error: {e}")

    t_bot = threading.Thread(target=run_bot_system, daemon=True)
    t_bot.start()

start_background_worker()

# ---------------------------------------------------------
# [3] Streamlit UI
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
    [data-testid="stDataEditor"] { border: 1px solid #DADCE0 !important; background-color: #FFFFFF !important; }
    .stTabs [data-baseweb="tab-list"] button[aria-selected="true"] { color: #1A73E8 !important; border-bottom-color: #1A73E8 !important; }
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

# 설정 로드
config = load_config()

# [사이드바]
with st.sidebar:
    st.header("🎛️ Control Panel")
    if "telegram" in st.secrets:
        st.success("🔒 Secrets Key 사용 중")
    else:
        st.warning("⚠️ Secrets 미설정 (파일 모드)")
        
    system_on = st.toggle("System Power", value=config.get('system_active', True))
    if system_on != config.get('system_active', True):
        config['system_active'] = system_on
        save_config(config)
        st.rerun()

    if not system_on: st.error("⛔ Paused")
    else: st.success("🟢 Active")
    
    st.divider()
    with st.expander("🔑 Key 설정 (수동)"):
        bot_token = st.text_input("Bot Token", value=config['telegram'].get('bot_token', ''), type="password")
        chat_id = st.text_input("Chat ID", value=config['telegram'].get('chat_id', ''))
        if st.button("Save Keys", type="primary"):
            config['telegram']['bot_token'] = bot_token
            config['telegram']['chat_id'] = chat_id
            save_config(config)
            st.success("저장됨")

# [메인]
st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud</h3>", unsafe_allow_html=True)
# [복구됨] 탭 3개 (Dashboard, Management, Logs)
tab1, tab2, tab3 = st.tabs(["📊 Dashboard", "⚙️ Management", "📜 Logs"])

# [Tab 1] 시세
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
                <div class="stock-name" title="{info['name']}">{info['name']}</div>
                <div class="stock-price-box {theme_class}">
                    ${info['price']:.2f} <span style="font-size:0.8em; margin-left:4px;">{sign}{info['change']:.2f}%</span>
                </div>
            </div>"""
            with cols[i % 6]: st.markdown(html_code, unsafe_allow_html=True)
    elif not config['system_active']: st.warning("Paused")
    else: st.info("No tickers found.")

# [Tab 2] 관리 (버튼 복구됨)
with tab2:
    st.markdown("##### ➕ Add Tickers")
    c1, c2 = st.columns([4, 1])
    with c1: input_tickers = st.text_input("Add Tickers", placeholder="e.g. TSLA, NVDA", label_visibility="collapsed")
    with c2:
        if st.button("➕ Add", use_container_width=True, type="primary"):
            if input_tickers:
                for t in [x.strip().upper() for x in input_tickers.split(',') if x.strip()]:
                    if t not in config['tickers']:
                        config['tickers'][t] = {"감시_ON": True, "뉴스": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
                save_config(config)
                st.rerun()
    
    st.markdown("---")
    # [복구됨] 전체 제어 버튼
    st.markdown("##### ⚡ Global Controls")
    c_all_1, c_all_2, c_blank = st.columns([1, 1, 3])
    ALL_KEYS = ["감시_ON", "뉴스", "가격_3%", "거래량_2배", "52주_신고가", "RSI", "MA_크로스", "볼린저", "MACD"]
    
    with c_all_1:
        if st.button("✅ ALL ON", use_container_width=True):
            for t in config['tickers']:
                for key in ALL_KEYS: config['tickers'][t][key] = True
            save_config(config)
            st.rerun()
    with c_all_2:
        if st.button("⛔ ALL OFF", use_container_width=True):
            for t in config['tickers']:
                for key in ALL_KEYS: config['tickers'][t][key] = False
            save_config(config)
            st.rerun()

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
        
        # [복구됨] 컬럼 헤더 텍스트 표시
        column_config = {
            "Name": st.column_config.TextColumn("Company", disabled=True, width="small"),
            "감시_ON": st.column_config.CheckboxColumn("✅ 감시"), "뉴스": st.column_config.CheckboxColumn("📰 뉴스"),
            "가격_3%": st.column_config.CheckboxColumn("📈 급등"), "거래량_2배": st.column_config.CheckboxColumn("📢 거래량"),
            "52주_신고가": st.column_config.CheckboxColumn("🏆 신고가"), "RSI": st.column_config.CheckboxColumn("📊 RSI"),
            "MA_크로스": st.column_config.CheckboxColumn("❌ MA"), "볼린저": st.column_config.CheckboxColumn("🍩 볼린저"),
            "MACD": st.column_config.CheckboxColumn("🌊 MACD")
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

# [복구됨] Tab 3 로그창
with tab3:
    col_l1, col_l2 = st.columns([8, 1])
    with col_l1: st.markdown("##### System Logs")
    with col_l2: 
        if st.button("Reload Logs"): st.rerun()
        
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()[-50:]): 
                st.markdown(f"<div style='font-family: monospace; color: #444; font-size: 0.85em; border-bottom:1px solid #eee;'>{line.strip()}</div>", unsafe_allow_html=True)
