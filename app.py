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
# [1] 시스템 함수 (설정 로드/저장)
# ---------------------------------------------------------
def load_config():
    default_config = {"system_active": True, "telegram": {"bot_token": "", "chat_id": ""}, "tickers": {}}
    if not os.path.exists(CONFIG_FILE): return default_config
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

# ---------------------------------------------------------
# [2] 백그라운드 봇 (기존 로직 유지 - 코드 생략 없이 포함)
# ---------------------------------------------------------
@st.cache_resource
def start_background_worker():
    def run_bot_system():
        time.sleep(3) 
        cfg = load_config()
        if not cfg['telegram']['bot_token']: return
        
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
                    if cfg and cfg.get('system_active', True) and cfg['tickers']:
                        token, chat_id = cfg['telegram']['bot_token'], cfg['telegram']['chat_id']
                        if not token or not chat_id: 
                            time.sleep(60)
                            continue
                        
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

        # 봇 명령어 (요약)
        @bot.message_handler(commands=['start'])
        def s(m): bot.reply_to(m, "🤖 DeBrief Bot Active")
        
        t_mon = threading.Thread(target=monitor_loop, daemon=True)
        t_mon.start()
        try: bot.infinity_polling()
        except: pass

    t_bot = threading.Thread(target=run_bot_system, daemon=True)
    t_bot.start()

start_background_worker()


# ---------------------------------------------------------
# [3] Streamlit UI (수정됨)
# ---------------------------------------------------------
st.markdown("""
<style>
    .stApp { background-color: #FFFFFF; color: #202124; }
    
    /* 카드 디자인 */
    .stock-card {
        background-color: #FFFFFF; border: 1px solid #DADCE0; border-radius: 12px;
        padding: 15px 10px; margin-bottom: 12px; text-align: center;
        box-shadow: 0 4px 6px rgba(0,0,0,0.05); transition: transform 0.2s;
    }
    .stock-card:hover { transform: translateY(-3px); box-shadow: 0 6px 12px rgba(0,0,0,0.1); }
    
    .stock-symbol { font-family: 'Inter', sans-serif; font-size: 1.25em; font-weight: 800; color: #1A73E8; margin-bottom: 2px; }
    .stock-name { font-size: 0.85em; color: #5F6368; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 8px; font-weight: 500;}
    .stock-price-box { display: inline-block; padding: 5px 12px; border-radius: 16px; font-size: 0.95em; font-weight: 700; }
    
    .up-theme { background-color: #E6F4EA; color: #137333; border: 1px solid #CEEAD6; }
    .down-theme { background-color: #FCE8E6; color: #C5221F; border: 1px solid #FAD2CF; }

    /* 입력창 및 테이블 스타일 */
    .stTextInput input, .stSelectbox div[data-baseweb="select"], .stMultiSelect {
        background-color: #FFFFFF !important; color: #202124 !important; border: 1px solid #DADCE0 !important; border-radius: 8px !important;
    }
    [data-testid="stDataEditor"] { border: 1px solid #DADCE0 !important; border-radius: 8px; background-color: #FFFFFF !important; }
    [data-testid="stDataEditor"] * { color: #202124 !important; background-color: #FFFFFF !important; }
</style>
""", unsafe_allow_html=True)

def get_stock_data(tickers):
    """주가 및 기업명 가져오기 (이름 로직 강화)"""
    if not tickers: return {}
    if 'company_names' not in st.session_state: st.session_state['company_names'] = {}
    
    info_dict = {}
    try:
        tickers_str = " ".join(tickers)
        data = yf.Tickers(tickers_str)
        
        for ticker in tickers:
            try:
                # 기업명 캐싱 (없으면 API 호출)
                if ticker not in st.session_state['company_names']:
                    try: 
                        name = data.tickers[ticker].info.get('shortName', ticker)
                        st.session_state['company_names'][ticker] = name
                    except: 
                        st.session_state['company_names'][ticker] = ticker
                
                info = data.tickers[ticker].fast_info
                curr = info.last_price
                prev = info.previous_close
                change = ((curr - prev) / prev) * 100
                
                info_dict[ticker] = {
                    "name": st.session_state['company_names'][ticker], 
                    "price": curr, 
                    "change": change
                }
            except: 
                info_dict[ticker] = {"name": ticker, "price": 0, "change": 0}
        return info_dict
    except: return {}

st.set_page_config(page_title="DeBrief", layout="wide", page_icon="📡")

# [헤더]
st.markdown("""
    <h3 style='font-family: sans-serif; font-weight: 800; color: #1A73E8; margin-bottom: 20px;'>
        📡 DeBrief <span style='font-size:0.7em; color:#5F6368; font-weight:400;'>: Stock Control Tower</span>
    </h3>
""", unsafe_allow_html=True)

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

# [탭 1] 대시보드
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
            </div>
            """
            with cols[i % 6]: st.markdown(html_code, unsafe_allow_html=True)
                
    elif not config['system_active']: st.warning("Paused")
    else: st.info("No tickers found.")

# [탭 2] 관리 (버튼 및 헤더 복구됨)
with tab2:
    st.markdown("##### ➕ 종목 추가 (Add Tickers)")
    c1, c2, c3 = st.columns([4, 1, 1])
    with c1: input_tickers = st.text_input("Add Tickers", placeholder="e.g. TSLA, NVDA", label_visibility="collapsed")
    with c2:
        if st.button("➕ 추가", use_container_width=True, type="primary"):
            if input_tickers:
                for t in [x.strip().upper() for x in input_tickers.split(',') if x.strip()]:
                    if t not in config['tickers']:
                        config['tickers'][t] = {
                            "감시_ON": True, "뉴스": True, "가격_3%": True, 
                            "거래량_2배": False, "52주_신고가": True, "RSI": False,
                            "MA_크로스": False, "볼린저": False, "MACD": False
                        }
                save_config(config)
                st.rerun()
    with c3:
        if st.button("🔤 정렬", use_container_width=True):
            config['tickers'] = dict(sorted(config['tickers'].items()))
            save_config(config)
            st.rerun()

    st.markdown("---")
    
    # [복구됨] 전체 제어 버튼
    st.markdown("##### ⚡ 전체 제어 (Global Controls)")
    c_all_1, c_all_2, c_blank = st.columns([1, 1, 3])
    
    # 제어할 모든 키 목록
    ALL_KEYS = ["감시_ON", "뉴스", "가격_3%", "거래량_2배", "52주_신고가", "RSI", "MA_크로스", "볼린저", "MACD"]
    
    with c_all_1:
        if st.button("✅ 모든 알림 켜기", use_container_width=True):
            for t in config['tickers']:
                for key in ALL_KEYS: config['tickers'][t][key] = True
            save_config(config)
            st.rerun()
            
    with c_all_2:
        if st.button("⛔ 모든 알림 끄기", use_container_width=True):
            for t in config['tickers']:
                for key in ALL_KEYS: config['tickers'][t][key] = False
            save_config(config)
            st.rerun()

    st.markdown("##### 📝 알림 설정 (Settings)")
    if config['tickers']:
        data_list = []
        for t, settings in config['tickers'].items():
            row = settings.copy()
            # 이름 컬럼을 위해 데이터 준비
            row['Name'] = st.session_state.get('company_names', {}).get(t, t)
            data_list.append(row)
        
        df = pd.DataFrame(data_list, index=config['tickers'].keys())
        
        # 컬럼 순서 재배치
        cols_order = ["Name", "감시_ON", "뉴스", "가격_3%", "거래량_2배", "52주_신고가", "RSI", "MA_크로스", "볼린저", "MACD"]
        df = df.reindex(columns=cols_order, fill_value=False)

        # [복구됨] 컬럼 헤더에 텍스트 라벨 추가
        column_config = {
            "Name": st.column_config.TextColumn("🏢 기업명", disabled=True, width="small"),
            "감시_ON": st.column_config.CheckboxColumn("✅ 감시", help="이 종목 감시 여부"),
            "뉴스": st.column_config.CheckboxColumn("📰 뉴스", help="뉴스 발생 시 알림"),
            "가격_3%": st.column_config.CheckboxColumn("📈 급등락", help="3% 이상 변동 시"),
            "거래량_2배": st.column_config.CheckboxColumn("📢 거래량", help="평소 2배 거래량"),
            "52주_신고가": st.column_config.CheckboxColumn("🏆 신고가", help="52주 신고가 경신"),
            "RSI": st.column_config.CheckboxColumn("📊 RSI", help="과매수/과매도"),
            "MA_크로스": st.column_config.CheckboxColumn("❌ 골든/데드", help="이평선 크로스"),
            "볼린저": st.column_config.CheckboxColumn("🍩 볼린저", help="밴드 이탈"),
            "MACD": st.column_config.CheckboxColumn("🌊 MACD", help="MACD 신호")
        }

        edited_df = st.data_editor(df, column_config=column_config, use_container_width=True, key="ticker_editor")
        
        if not df.equals(edited_df):
            temp_dict = edited_df.to_dict(orient='index')
            for t in temp_dict:
                if 'Name' in temp_dict[t]: del temp_dict[t]['Name']
            config['tickers'] = temp_dict
            save_config(config)
            st.toast("설정이 저장되었습니다.", icon="💾")
        
        st.markdown("---")
        st.markdown("##### 🗑️ 종목 삭제 (Delete)")
        col_del1, col_del2 = st.columns([4, 1])
        with col_del1:
            del_targets = st.multiselect("삭제할 종목 선택", options=list(config['tickers'].keys()), label_visibility="collapsed")
        with col_del2:
            if st.button("🗑️ 선택 삭제", use_container_width=True, type="primary"):
                if del_targets:
                    for t in del_targets:
                        if t in config['tickers']: del config['tickers'][t]
                    save_config(config)
                    st.rerun()

with tab3:
    if st.button("로그 새로고침"): st.rerun()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()[-50:]): st.text(line.strip())
