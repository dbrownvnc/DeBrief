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
# 로컬 파일은 이제 '백업용'으로만 쓰입니다.
CONFIG_FILE = 'debrief_settings.json'
LOG_FILE = 'debrief.log'

# ---------------------------------------------------------
# [1] 설정 로드/저장 (JSONBin 연동 - 핵심 수정됨)
# ---------------------------------------------------------
def get_jsonbin_headers():
    # Secrets에서 키를 가져옴 (로컬 실행 시 에러 방지)
    try:
        if "jsonbin" in st.secrets:
            return {
                'Content-Type': 'application/json',
                'X-Master-Key': st.secrets["jsonbin"]["master_key"]
            }
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
    # 1. 기본값 (최후의 보루)
    config = {
        "system_active": True, 
        "telegram": {"bot_token": "", "chat_id": ""}, 
        "tickers": {
            "TSLA": {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False},
            "NVDA": {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
        } 
    }

    # 2. [핵심] JSONBin(클라우드 저장소)에서 불러오기
    url = get_jsonbin_url()
    headers = get_jsonbin_headers()
    
    if url and headers:
        try:
            # 'latest'를 붙여서 최신 데이터 가져옴
            resp = requests.get(f"{url}/latest", headers=headers, timeout=5)
            if resp.status_code == 200:
                cloud_data = resp.json()['record']
                # 데이터가 비어있지 않다면 적용
                if "tickers" in cloud_data and cloud_data['tickers']:
                    config = cloud_data
                    # print("✅ 클라우드 설정 로드 성공")
        except Exception as e:
            print(f"⚠️ 클라우드 로드 실패: {e}")

    # 3. 텔레그램 키는 Secrets가 최우선
    try:
        if "telegram" in st.secrets:
            config['telegram']['bot_token'] = st.secrets["telegram"]["bot_token"]
            config['telegram']['chat_id'] = st.secrets["telegram"]["chat_id"]
    except: pass
    
    return config

def save_config(config):
    # 1. JSONBin(클라우드)에 저장
    url = get_jsonbin_url()
    headers = get_jsonbin_headers()
    
    if url and headers:
        try:
            # 비동기적으로 저장하면 좋지만, 간단하게 동기 처리 (데이터가 작으므로)
            requests.put(url, headers=headers, json=config, timeout=5)
            # print("✅ 클라우드 저장 성공")
        except Exception as e:
            print(f"⚠️ 클라우드 저장 실패: {e}")

    # 2. 로컬 파일에도 백업 (로컬 실행용)
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except: pass

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

            # (worker.py의 로직이 돌아가도록 헬스체크만 유지)
            @bot.message_handler(commands=['start'])
            def s(m): bot.reply_to(m, "🤖 DeBrief Cloud Active")
            
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

config = load_config()

# [사이드바]
with st.sidebar:
    st.header("🎛️ Control Panel")
    
    # 저장소 연결 상태 확인
    is_cloud_connected = False
    try:
        if "jsonbin" in st.secrets: is_cloud_connected = True
    except: pass

    if is_cloud_connected:
        st.success("☁️ 클라우드 저장소 연결됨")
    else:
        st.warning("📂 로컬 저장 모드 (재부팅 시 초기화)")
        
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

# [Tab 2] 관리
with tab2:
    st.markdown("##### ➕ Add Tickers")
    c1, c2 = st.columns([4, 1])
    with c1: input_tickers = st.text_input("Add Tickers", placeholder="e.g. TSLA, NVDA", label_visibility="collapsed")
    with c2:
        if st.button("➕ Add", use_container_width=True, type="primary"):
            if input_tickers:
                for t in [x.strip().upper() for x in input_tickers.split(',') if x.strip()]:
                    if t not in config['tickers']:
                        config['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
                save_config(config)
                st.rerun()
    
    st.markdown("---")
    st.markdown("##### ⚡ Global Controls")
    c_all_1, c_all_2, c_blank = st.columns([1, 1, 3])
    ALL_KEYS = ["감시_ON", "뉴스", "SEC", "가격_3%", "거래량_2배", "52주_신고가", "RSI", "MA_크로스", "볼린저", "MACD"]
    
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
            if 'SEC' not in row: row['SEC'] = True 
            row['Name'] = st.session_state.get('company_names', {}).get(t, t)
            data_list.append(row)
        df = pd.DataFrame(data_list, index=config['tickers'].keys())
        cols_order = ["Name", "감시_ON", "뉴스", "SEC", "가격_3%", "거래량_2배", "52주_신고가", "RSI", "MA_크로스", "볼린저", "MACD"]
        df = df.reindex(columns=cols_order, fill_value=False)
        
        column_config = {
            "Name": st.column_config.TextColumn("Company", disabled=True, width="small"),
            "감시_ON": st.column_config.CheckboxColumn("✅ 감시"), 
            "뉴스": st.column_config.CheckboxColumn("📰 뉴스", help="일반 뉴스/소셜 알림"),
            "SEC": st.column_config.CheckboxColumn("🏛️ SEC", help="8-K, 10-Q 등 공시 알림"),
            "가격_3%": st.column_config.CheckboxColumn("📈 급등"), 
            "거래량_2배": st.column_config.CheckboxColumn("📢 거래량"),
            "52주_신고가": st.column_config.CheckboxColumn("🏆 신고가"), 
            "RSI": st.column_config.CheckboxColumn("📊 RSI"),
            "MA_크로스": st.column_config.CheckboxColumn("⚡ 골든/데드", help="50일/200일 이평선 교차"),
            "볼린저": st.column_config.CheckboxColumn("🍩 볼린저"),
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

# [Tab 3] 로그창
with tab3:
    col_l1, col_l2 = st.columns([8, 1])
    with col_l1: st.markdown("##### System Logs")
    with col_l2: 
        if st.button("Reload Logs"): st.rerun()
        
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()[-50:]): 
                st.markdown(f"<div style='font-family: monospace; color: #444; font-size: 0.85em; border-bottom:1px solid #eee;'>{line.strip()}</div>", unsafe_allow_html=True)
