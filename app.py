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
news_cache = {} # 뉴스 중복 발송 방지 캐시

# ---------------------------------------------------------
# [1] 설정 로드/저장 (JSONBin + 로컬 백업)
# ---------------------------------------------------------
def get_jsonbin_headers():
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
    # 1. 기본값
    config = {
        "system_active": True, 
        "telegram": {"bot_token": "", "chat_id": ""}, 
        "tickers": {
            "TSLA": {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False},
            "NVDA": {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
        } 
    }

    # 2. JSONBin(클라우드) 로드
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

    # 3. 텔레그램 키는 Secrets 우선
    try:
        if "telegram" in st.secrets:
            config['telegram']['bot_token'] = st.secrets["telegram"]["bot_token"]
            config['telegram']['chat_id'] = st.secrets["telegram"]["chat_id"]
    except: pass
    
    return config

def save_config(config):
    # 1. JSONBin 저장
    url = get_jsonbin_url()
    headers = get_jsonbin_headers()
    if url and headers:
        try: requests.put(url, headers=headers, json=config, timeout=5)
        except: pass

    # 2. 로컬 파일 백업
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except: pass

# ---------------------------------------------------------
# [2] 뉴스/공시 검색 엔진
# ---------------------------------------------------------
def get_integrated_news(ticker, strict_mode=False):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    sec_query = f"{ticker} SEC Filing OR 8-K OR 10-Q"
    
    search_urls = [
        f"https://news.google.com/rss/search?q={sec_query} when:1d&hl=en-US&gl=US&ceid=US:en", # SEC
        f"https://news.google.com/rss/search?q={ticker}+주가+when:1d&hl=ko&gl=KR&ceid=KR:ko", # KR
        f"https://news.google.com/rss/search?q={ticker}+stock+news+when:1d&hl=en-US&gl=US&ceid=US:en", # US
        f"https://news.google.com/rss/search?q={ticker}+stock+(twitter+OR+reddit)+when:1d&hl=en-US&gl=US&ceid=US:en" # Social
    ]

    if not strict_mode:
        search_urls.append(f"https://news.google.com/rss/search?q={ticker}+stock&hl=ko&gl=KR&ceid=KR:ko")

    collected_items = []
    seen_links = set()

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
                    if "SEC" in url or "8-K" in title or "10-Q" in title: prefix = "🏛️[SEC]"
                    elif "twitter" in url or "reddit" in url: prefix = "🐦[Social]"
                    elif "en-US" in url: prefix = "🇺🇸[Global]"
                    
                    collected_items.append({'title': f"{prefix} {title}", 'link': link})
                except: continue
        except: pass

    for url in search_urls:
        fetch(url)
        if len(collected_items) >= 8: break
    return collected_items

# ---------------------------------------------------------
# [3] 백그라운드 봇 시스템
# ---------------------------------------------------------
@st.cache_resource
def start_background_worker():
    def run_bot_system():
        time.sleep(2)
        cfg = load_config()
        if not cfg['telegram']['bot_token']: return
        
        try:
            BOT_TOKEN = cfg['telegram']['bot_token']
            bot = telebot.TeleBot(BOT_TOKEN)
            
            def send_msg(token, chat_id, msg):
                try: requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": msg})
                except: pass

            # [A] 봇 메뉴
            try:
                bot.set_my_commands([
                    BotCommand("sec", "🏛️ 공시 조회"), BotCommand("news", "📰 뉴스"), 
                    BotCommand("info", "🏢 정보"), BotCommand("p", "💰 현재가"), 
                    BotCommand("market", "🌍 시장"), BotCommand("list", "📋 목록"),
                    BotCommand("help", "❓ 도움말")
                ])
            except: pass

            # [B] 명령어 핸들러
            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): 
                bot.reply_to(m, "🤖 *DeBrief Active*\n명령어: /sec, /news, /info, /p, /market", parse_mode='Markdown')

            @bot.message_handler(commands=['sec', '공시'])
            def sec_cmd(message):
                try:
                    parts = message.text.split()
                    if len(parts) < 2: return bot.reply_to(message, "사용법: `/sec 티커`")
                    t = parts[1].upper()
                    bot.reply_to(message, f"🏛️ *{t}* 공시 검색 중...", parse_mode='Markdown')
                    url = f"https://news.google.com/rss/search?q={t}+SEC+Filing+OR+8-K+OR+10-Q&hl=en-US&gl=US&ceid=US:en"
                    response = requests.get(url, timeout=5)
                    root = ET.fromstring(response.content)
                    items = []
                    for item in root.findall('.//item')[:5]:
                        title = item.find('title').text.split(' - ')[0]
                        link = item.find('link').text
                        pubDate = item.find('pubDate').text[:16]
                        items.append(f"📅 {pubDate}\n📄 [{title}]({link})")
                    if not items: return bot.reply_to(message, f"❌ {t} 공시 없음")
                    bot.reply_to(message, f"🏛️ *{t} Filings*\n\n" + "\n\n".join(items), parse_mode='Markdown', disable_web_page_preview=True)
                except: pass

            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    bot.reply_to(m, f"🔍 {t} 뉴스 검색...")
                    data = get_integrated_news(t, strict_mode=False)
                    if not data: return bot.reply_to(m, "❌ 소식 없음")
                    txt = f"📰 *{t} Radar*\n"
                    for i, n in enumerate(data): txt += f"\n{i+1}. {n['title']}\n🔗 {n['link']}\n"
                    bot.reply_to(m, txt, parse_mode='Markdown', disable_web_page_preview=True)
                except: pass

            @bot.message_handler(commands=['info'])
            def info_cmd(message):
                try:
                    parts = message.text.split()
                    if len(parts) < 2: return bot.reply_to(message, "사용법: `/info 티커`")
                    t = parts[1].upper()
                    msg = bot.reply_to(message, f"🏢 *{t}* 분석 중...", parse_mode='Markdown')
                    stock = yf.Ticker(t)
                    try: i = stock.info
                    except: return bot.edit_message_text("⚠️ 정보 접근 불가", message.chat.id, msg.message_id)
                    if not i: return bot.edit_message_text("❌ 정보 없음", message.chat.id, msg.message_id)
                    
                    def val(k, u="", m=1): 
                        v = i.get(k)
                        return f"{v*m:.2f}{u}" if v else "N/A"
                        
                    res = (f"🏢 *{i.get('shortName', t)}*\n"
                           f"📊 PER: `{val('trailingPE')}` | PBR: `{val('priceToBook')}`\n"
                           f"💰 배당: `{val('dividendYield', '%', 100)}` | 목표: `${val('targetMeanPrice')}`\n"
                           f"📢 의견: *{i.get('recommendationKey', 'none').upper()}*")
                    bot.edit_message_text(res, message.chat.id, msg.message_id, parse_mode='Markdown')
                except: pass

            @bot.message_handler(commands=['p'])
            def price_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    p = yf.Ticker(t).fast_info.last_price
                    bot.reply_to(m, f"💰 {t}: ${p:.2f}")
                except: pass

            @bot.message_handler(commands=['market'])
            def market_cmd(m):
                try:
                    idx = {"S&P500":"^GSPC", "Nasdaq":"^IXIC", "VIX":"^VIX", "USD/KRW":"KRW=X"}
                    txt = "🌍 *Market Status*\n"
                    for n, t in idx.items():
                        i = yf.Ticker(t).fast_info
                        curr = i.last_price
                        pct = ((curr-i.previous_close)/i.previous_close)*100
                        em = "🔺" if pct>=0 else "🔹"
                        txt += f"{em} {n}: `{curr:.2f}` ({pct:.2f}%)\n"
                    bot.reply_to(m, txt, parse_mode='Markdown')
                except: pass

            @bot.message_handler(commands=['list'])
            def list_cmd(m):
                c = load_config()
                bot.reply_to(m, f"📋 감시 목록: {', '.join(c['tickers'].keys())}")

            @bot.message_handler(commands=['add'])
            def add_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t not in c['tickers']:
                        c['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
                        save_config(c)
                        bot.reply_to(m, f"✅ {t} 추가됨")
                    else: bot.reply_to(m, "이미 있음")
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

            @bot.message_handler(commands=['on', 'off'])
            def toggle_cmd(m):
                is_on = '/on' in m.text
                c = load_config()
                c['system_active'] = is_on
                save_config(c)
                status = "🟢 가동 시작" if is_on else "⛔ 시스템 정지"
                bot.reply_to(m, status)

            # [C] 자동 감시 루프
            def monitor_loop():
                print("👀 백그라운드 감시 시작...")
                while True:
                    try:
                        cfg = load_config()
                        if cfg.get('system_active', True) and cfg['tickers']:
                            token = cfg['telegram']['bot_token']
                            chat_id = cfg['telegram']['chat_id']
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items():
                                    exe.submit(analyze_ticker, t, s, token, chat_id)
                    except Exception as e: print(f"Monitor Error: {e}")
                    time.sleep(60)

            def analyze_ticker(ticker, settings, token, chat_id):
                if not settings.get('감시_ON', True): return
                try:
                    stock = yf.Ticker(ticker)
                    
                    if settings.get('뉴스') or settings.get('SEC'):
                        if ticker not in news_cache: news_cache[ticker] = set()
                        items = get_integrated_news(ticker, strict_mode=True)
                        for item in items:
                            if item['link'] in news_cache[ticker]: continue
                            is_sec = "🏛️" in item['title']
                            should_send = (is_sec and settings.get('SEC')) or (not is_sec and settings.get('뉴스'))
                            if should_send:
                                if len(news_cache[ticker]) > 0:
                                    send_msg(token, chat_id, f"🚨 [속보] {ticker}\n{item['title']}\n{item['link']}")
                                news_cache[ticker].add(item['link'])

                    info = stock.fast_info
                    curr = info.last_price
                    prev = info.previous_close
                    if settings.get('가격_3%'):
                        pct = ((curr - prev) / prev) * 100
                        if abs(pct) >= 3.0:
                            emoji = "🚀" if pct > 0 else "📉"
                            send_msg(token, chat_id, f"[{ticker}] {emoji} {pct:.2f}%\n${curr:.2f}")

                    if any(settings.get(k) for k in ['MA_크로스', '볼린저', 'MACD', 'RSI']):
                        hist = stock.history(period="1y")
                        if not hist.empty:
                            close = hist['Close']
                            if settings.get('RSI'):
                                delta = close.diff()
                                gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                                loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                                rsi = 100 - (100 / (1 + gain/loss)).iloc[-1]
                                if rsi >= 70: send_msg(token, chat_id, f"[{ticker}] 🔥 RSI 과매수 ({rsi:.1f})")
                                elif rsi <= 30: send_msg(token, chat_id, f"[{ticker}] 💧 RSI 과매도 ({rsi:.1f})")
                            
                            if settings.get('MA_크로스'):
                                ma50 = close.rolling(50).mean()
                                ma200 = close.rolling(200).mean()
                                if ma50.iloc[-2] < ma200.iloc[-2] and ma50.iloc[-1] > ma200.iloc[-1]:
                                    send_msg(token, chat_id, f"[{ticker}] ✨ 골든크로스")
                                elif ma50.iloc[-2] > ma200.iloc[-2] and ma50.iloc[-1] < ma200.iloc[-1]:
                                    send_msg(token, chat_id, f"[{ticker}] ☠️ 데드크로스")
                except: pass

            t_mon = threading.Thread(target=monitor_loop, daemon=True)
            t_mon.start()
            try: bot.infinity_polling()
            except: pass
            
        except Exception as e: print(f"Bot Error: {e}")

    t_bot = threading.Thread(target=run_bot_system, daemon=True)
    t_bot.start()

start_background_worker()

# ---------------------------------------------------------
# [4] Streamlit UI (수정됨: 컴팩트 디자인)
# ---------------------------------------------------------
st.markdown("""
<style>
    .stApp { background-color: #FFFFFF; color: #202124; }
    
    /* 컴팩트 카드 디자인 */
    .stock-card {
        background-color: #FFFFFF; border: 1px solid #DADCE0; border-radius: 8px;
        padding: 8px 5px; margin-bottom: 6px; text-align: center; /* 패딩 축소 */
        box-shadow: 0 2px 4px rgba(0,0,0,0.05); transition: transform 0.2s;
    }
    .stock-card:hover { transform: translateY(-2px); box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
    
    /* 폰트 크기 축소 */
    .stock-symbol { font-family: 'Inter', sans-serif; font-size: 1.0em; font-weight: 800; color: #1A73E8; margin-bottom: 2px; }
    .stock-name { font-size: 0.65em; color: #5F6368; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; }
    .stock-price-box { display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 0.8em; font-weight: 700; }
    
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
    
    is_cloud_connected = False
    try:
        if "jsonbin" in st.secrets: is_cloud_connected = True
    except: pass

    if is_cloud_connected: st.success("☁️ 클라우드 저장소 연결됨")
    else: st.warning("📂 로컬 저장 모드")
        
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

st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud</h3>", unsafe_allow_html=True)
tab1, tab2, tab3 = st.tabs(["📊 Dashboard", "⚙️ Management", "📜 Logs"])

with tab1:
    col_top1, col_top2 = st.columns([8, 1])
    with col_top2:
        if st.button("Refresh", use_container_width=True): st.rerun()

    if config['tickers'] and config['system_active']:
        ticker_list = list(config['tickers'].keys())
        stock_data = get_stock_data(ticker_list)
        # [수정] 한 줄에 8개씩 배치하여 사이즈 줄임
        cols = st.columns(8)
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
            with cols[i % 8]: st.markdown(html_code, unsafe_allow_html=True)
    elif not config['system_active']: st.warning("Paused")
    else: st.info("No tickers found.")

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

with tab3:
    col_l1, col_l2 = st.columns([8, 1])
    with col_l1: st.markdown("##### System Logs")
    with col_l2: 
        if st.button("Reload Logs"): st.rerun()
        
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()[-50:]): 
                st.markdown(f"<div style='font-family: monospace; color: #444; font-size: 0.85em; border-bottom:1px solid #eee;'>{line.strip()}</div>", unsafe_allow_html=True)
