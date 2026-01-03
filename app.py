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
# [NEW] 번역 라이브러리
from deep_translator import GoogleTranslator

# --- 프로젝트 설정 ---
CONFIG_FILE = 'debrief_settings.json'
LOG_FILE = 'debrief.log'
news_cache = {}
price_alert_cache = {}

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
# [2] 뉴스 검색 엔진 (번역 기능 추가됨)
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
    
    # [NEW] 번역기 초기화
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
                        prefix = "🏛️[SEC]"
                        is_foreign = True
                    elif "twitter" in url or "reddit" in url: 
                        prefix = "🐦[Social]"
                        is_foreign = True
                    elif "en-US" in url: 
                        prefix = "🇺🇸[Global]"
                        is_foreign = True
                    
                    # [NEW] 외국 뉴스면 제목 번역 시도
                    if is_foreign:
                        try:
                            # 100자 이내로 잘라서 번역 (속도 최적화)
                            translated_title = translator.translate(title[:100])
                            title = f"{translated_title} (원문: {title})"
                        except: pass # 번역 실패시 원문 유지

                    collected_items.append({'title': f"{prefix} {title}", 'link': link})
                except: continue
        except: pass

    for url in search_urls:
        fetch(url)
        if len(collected_items) >= 8: break
    return collected_items

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
            try: bot.send_message(chat_id, "🤖 시스템 업데이트 (V26)\n이제 영어 뉴스 제목을 한글로 번역해드립니다.")
            except: pass

            # --- 명령어 핸들러 ---
            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): 
                bot.reply_to(m, "🤖 *DeBrief V26* (번역기 탑재)\n뉴스 검색 시 제목을 한글로 보여줍니다.", parse_mode='Markdown')

            @bot.message_handler(commands=['add', '추가'])
            def add_cmd(m):
                try:
                    parts = m.text.split()
                    if len(parts) < 2: return bot.reply_to(m, "⚠️ 사용법: `/add 티커`")
                    t = parts[1].upper()
                    c = load_config()
                    if t not in c['tickers']:
                        c['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "거래량_2배": False, "52주_신고가": True, "RSI": False, "MA_크로스":False, "볼린저":False, "MACD":False}
                        save_config(c)
                        bot.reply_to(m, f"✅ *{t}* 추가 완료", parse_mode='Markdown')
                    else: bot.reply_to(m, "이미 존재합니다.")
                except: pass

            @bot.message_handler(commands=['del', '삭제'])
            def del_cmd(m):
                try:
                    parts = m.text.split()
                    if len(parts) < 2: return bot.reply_to(m, "⚠️ 사용법: `/del 티커`")
                    t = parts[1].upper()
                    c = load_config()
                    if t in c['tickers']:
                        del c['tickers'][t]
                        save_config(c)
                        bot.reply_to(m, f"🗑️ *{t}* 삭제 완료", parse_mode='Markdown')
                    else: bot.reply_to(m, "목록에 없습니다.")
                except: pass

            @bot.message_handler(commands=['sec', '공시'])
            def sec_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    bot.reply_to(m, f"🏛️ {t} 공시 검색...")
                    url = f"https://news.google.com/rss/search?q={t}+SEC+Filing+OR+8-K+OR+10-Q&hl=en-US&gl=US&ceid=US:en"
                    res = requests.get(url, timeout=5)
                    root = ET.fromstring(res.content)
                    items = []
                    for item in root.findall('.//item')[:5]:
                        title = item.find('title').text.split(' - ')[0]
                        link = item.find('link').text
                        pubDate = item.find('pubDate').text[:16]
                        items.append(f"📅 {pubDate}\n📄 [{title}]({link})")
                    if not items: bot.reply_to(m, f"❌ {t} 공시 없음")
                    else: bot.reply_to(m, "\n\n".join(items), disable_web_page_preview=True)
                except: bot.reply_to(m, "오류 발생")

            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    bot.reply_to(m, f"🔍 {t} 뉴스 검색 (번역 중)...")
                    data = get_integrated_news(t)
                    if not data: bot.reply_to(m, "❌ 뉴스 없음")
                    else:
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
                    
                    def val(k, u="", m=1): 
                        v = i.get(k)
                        return f"{v*m:.2f}{u}" if v else "N/A"
                    res = (f"🏢 *{i.get('shortName', t)}*\n"
                           f"📊 PER: `{val('trailingPE')}` | PBR: `{val('priceToBook')}`\n"
                           f"💰 배당: `{val('dividendYield', '%', 100)}` | 목표: `${val('targetMeanPrice')}`")
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
                    idx = {"S&P500":"^GSPC", "Nasdaq":"^IXIC", "USD/KRW":"KRW=X"}
                    txt = "🌍 *Market*\n"
                    for n, t in idx.items():
                        i = yf.Ticker(t).fast_info
                        txt += f"{n}: `{i.last_price:.2f}`\n"
                    bot.reply_to(m, txt, parse_mode='Markdown')
                except: pass

            @bot.message_handler(commands=['list'])
            def list_cmd(m):
                c = load_config()
                bot.reply_to(m, f"📋 목록: {', '.join(c['tickers'].keys())}")

            @bot.message_handler(commands=['on', 'off'])
            def toggle_cmd(m):
                is_on = '/on' in m.text
                c = load_config()
                c['system_active'] = is_on
                save_config(c)
                bot.reply_to(m, "🟢 가동" if is_on else "⛔ 정지")

            try:
                bot.set_my_commands([
                    BotCommand("add", "➕ 추가"), BotCommand("del", "🗑️ 삭제"),
                    BotCommand("sec", "🏛️ 공시"), BotCommand("news", "📰 뉴스"),
                    BotCommand("p", "💰 현재가"), BotCommand("market", "🌍 시장"),
                    BotCommand("list", "📋 목록"), BotCommand("on", "🟢 켜기"),
                    BotCommand("off", "⛔ 끄기"), BotCommand("help", "❓ 도움말")
                ])
            except: pass

            # --- 감시 루프 ---
            def send_alert(token, chat_id, title, msg):
                text = f"🔔 *[{title}]*\n{msg}"
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

            def monitor_loop():
                write_log("👀 감시 루프 시작")
                while True:
                    try:
                        cfg = load_config()
                        if cfg.get('system_active', True) and cfg['tickers']:
                            cur_token = cfg['telegram']['bot_token']
                            cur_chat = cfg['telegram']['chat_id']
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items():
                                    exe.submit(analyze_ticker, t, s, cur_token, cur_chat)
                    except: pass
                    time.sleep(60)

            def analyze_ticker(ticker, settings, token, chat_id):
                if not settings.get('감시_ON', True): return
                try:
                    # [1] 뉴스/공시 (번역 적용)
                    if settings.get('뉴스') or settings.get('SEC'):
                        if ticker not in news_cache: news_cache[ticker] = set()
                        items = get_integrated_news(ticker, strict_mode=True)
                        for item in items:
                            if item['link'] in news_cache[ticker]: continue
                            is_sec = "🏛️" in item['title']
                            should_send = (is_sec and settings.get('SEC')) or (not is_sec and settings.get('뉴스'))
                            if should_send:
                                if len(news_cache[ticker]) > 0:
                                    send_alert(token, chat_id, f"{ticker} 소식", f"{item['title']}\n🔗 [Link]({item['link']})")
                                news_cache[ticker].add(item['link'])
                    
                    # [2] 가격 (스마트 필터)
                    stock = yf.Ticker(ticker)
                    hist = stock.history(period="1y")
                    if hist.empty: return
                    close = hist['Close']
                    curr = close.iloc[-1]
                    prev = close.iloc[-2]
                    
                    if settings.get('가격_3%'):
                        pct = ((curr - prev) / prev) * 100
                        if abs(pct) >= 3.0:
                            last_pct = price_alert_cache.get(ticker, 0.0)
                            if abs(pct - last_pct) >= 1.0: 
                                direction = "급등 🚀" if pct > 0 else "급락 📉"
                                send_alert(token, chat_id, f"{ticker} {direction}", f"변동폭: {pct:.2f}%\n현재가: ${curr:.2f}")
                                price_alert_cache[ticker] = pct

                    # 보조지표 (RSI 등)
                    if settings.get('RSI'):
                        delta = close.diff()
                        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
                        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                        rs = gain / loss
                        rsi = 100 - (100 / (1 + rs)).iloc[-1]
                        if rsi >= 70: send_alert(token, chat_id, f"{ticker} RSI 과매수 🔥", f"RSI: {rsi:.1f}")
                        elif rsi <= 30: send_alert(token, chat_id, f"{ticker} RSI 과매도 💧", f"RSI: {rsi:.1f}")

                    if settings.get('MA_크로스'):
                        ma50 = close.rolling(50).mean()
                        ma200 = close.rolling(200).mean()
                        if ma50.iloc[-2] < ma200.iloc[-2] and ma50.iloc[-1] > ma200.iloc[-1]:
                            send_alert(token, chat_id, f"{ticker} 골든크로스 ✨", "50일선 돌파")
                        elif ma50.iloc[-2] > ma200.iloc[-2] and ma50.iloc[-1] < ma200.iloc[-1]:
                            send_alert(token, chat_id, f"{ticker} 데드크로스 ☠️", "50일선 이탈")

                except: pass

            t_mon = threading.Thread(target=monitor_loop, daemon=True)
            t_mon.start()
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
            
        except Exception as e:
            write_log(f"🔥 Bot Error: {e}")

    t_bot = threading.Thread(target=run_bot_system, daemon=True)
    t_bot.start()

start_background_worker()

# ---------------------------------------------------------
# [4] UI
# ---------------------------------------------------------
st.markdown("""
<style>
    .stApp { background-color: #FFFFFF; color: #202124; }
    .stock-card {
        background-color: #FFFFFF; border: 1px solid #DADCE0; border-radius: 8px;
        padding: 8px 5px; margin-bottom: 6px; text-align: center;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05); transition: transform 0.2s;
    }
    .stock-card:hover { transform: translateY(-2px); box-shadow: 0 4px 8px rgba(0,0,0,0.1); }
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
    with st.expander("🔑 Key 설정"):
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
        try:
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
                "뉴스": st.column_config.CheckboxColumn("📰 뉴스", help="일반/소셜"),
                "SEC": st.column_config.CheckboxColumn("🏛️ SEC", help="공시"),
                "가격_3%": st.column_config.CheckboxColumn("📉 등락", help="3% 이상 급등/급락 시 알림"),
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
        except Exception as e:
            st.error(f"테이블 렌더링 오류: {e}")
        
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
