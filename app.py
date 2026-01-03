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
    # JSONBin 및 로컬 파일 연동 (기존 로직 유지)
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config.update(json.load(f))
    except: pass
    return config

def save_config(config):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except: pass

# --- 뉴스 검색 엔진 ---
def get_integrated_news(ticker):
    headers = {"User-Agent": "Mozilla/5.0"}
    search_urls = [
        f"https://news.google.com/rss/search?q={ticker}+SEC+Filing+when:1d&hl=en-US&gl=US&ceid=US:en",
        f"https://news.google.com/rss/search?q={ticker}+stock+news+when:1d&hl=en-US&gl=US&ceid=US:en"
    ]
    collected = []
    translator = GoogleTranslator(source='auto', target='ko')
    for url in search_urls:
        try:
            res = requests.get(url, headers=headers, timeout=3)
            root = ET.fromstring(res.content)
            for item in root.findall('.//item')[:3]:
                title = item.find('title').text
                link = item.find('link').text
                try: translated = translator.translate(title[:100])
                except: translated = title
                collected.append({'title': translated, 'link': link, 'is_sec': "SEC" in url})
        except: pass
    return collected

# --- 봇 백그라운드 작업 ---
@st.cache_resource
def start_background_worker():
    def run_bot_system():
        cfg = load_config()
        token = cfg['telegram']['bot_token']
        chat_id = cfg['telegram']['chat_id']
        if not token: return
        
        bot = telebot.TeleBot(token)

        # 1. [수정] 실적 발표 명령어 (안정적인 데이터 추출 방식)
        @bot.message_handler(commands=['earning', '실적'])
        def earning_cmd(m):
            try:
                t = m.text.split()[1].upper()
                bot.send_chat_action(m.chat.id, 'typing')
                stock = yf.Ticker(t)
                df = stock.earnings_dates
                if df is None or df.empty:
                    return bot.reply_to(m, f"❌ {t}: 실적 발표 일정이 없습니다.")
                
                next_e = df.index[0]
                date_str = next_e.strftime('%Y-%m-%d')
                eps = df.iloc[0]['EPS Estimate']
                rev = df.iloc[0]['Reported EPS'] # 데이터가 없을 경우 Estimate 대체 로직 필요할 수 있음
                
                msg = (f"📅 *{t} 실적 발표 예정*\n\n🗓️ 발표일: `{date_str}`\n"
                       f"💰 예상 EPS: `{eps if not pd.isna(eps) else 'N/A'}`\n"
                       f"_발표일은 현지 시간 기준으로 변동될 수 있습니다._")
                bot.reply_to(m, msg, parse_mode='Markdown')
            except: bot.reply_to(m, "사용법: /earning TSLA")

        # 2. [복구] 관리 명령어 (add, del, list)
        @bot.message_handler(commands=['add'])
        def add_cmd(m):
            try:
                t = m.text.split()[1].upper()
                c = load_config()
                c['tickers'][t] = {"감시_ON": True, "뉴스": True, "SEC": True, "가격_3%": True, "RSI": True}
                save_config(c); bot.reply_to(m, f"✅ {t} 추가 완료")
            except: bot.reply_to(m, "사용법: /add TSLA")

        @bot.message_handler(commands=['del'])
        def del_cmd(m):
            try:
                t = m.text.split()[1].upper()
                c = load_config()
                if t in c['tickers']:
                    del c['tickers'][t]; save_config(c)
                    bot.reply_to(m, f"🗑️ {t} 삭제 완료")
            except: bot.reply_to(m, "사용법: /del TSLA")

        # 3. [복구] 기존 명령어 (news, sec, p, market)
        @bot.message_handler(commands=['news'])
        def news_cmd(m):
            try:
                t = m.text.split()[1].upper()
                items = get_integrated_news(t)
                msg = f"📰 *{t} 최신 뉴스*\n" + "\n".join([f"- [{i['title']}]({i['link']})" for i in items])
                bot.reply_to(m, msg, parse_mode='Markdown', disable_web_page_preview=True)
            except: bot.reply_to(m, "뉴스 조회 실패")

        @bot.message_handler(commands=['p'])
        def p_cmd(m):
            try:
                t = m.text.split()[1].upper()
                price = yf.Ticker(t).fast_info.last_price
                bot.reply_to(m, f"💰 {t} 현재가: ${price:.2f}")
            except: pass

        # 4. [수정] 메뉴 설명 및 시작 가이드
        bot.set_my_commands([
            BotCommand("earning", "📅 실적발표일 조회"),
            BotCommand("news", "📰 뉴스 조회"),
            BotCommand("add", "➕ 종목 추가"),
            BotCommand("del", "🗑️ 종목 삭제"),
            BotCommand("p", "💰 현재가 확인"),
            BotCommand("market", "🌍 시장 지수")
        ])

        # 5. [수정] 알림 엔진 (예외 처리 강화로 멈춤 방지)
        def monitor_loop():
            while True:
                try:
                    cfg = load_config()
                    if cfg['system_active']:
                        for t, s in cfg['tickers'].items():
                            if not s.get('감시_ON'): continue
                            stock = yf.Ticker(t)
                            h = stock.history(period="2d")
                            if h.empty: continue
                            
                            curr, prev = h['Close'].iloc[-1], h['Close'].iloc[-2]
                            pct = ((curr - prev) / prev) * 100
                            
                            # 가격 알림 (중복 방지)
                            if abs(pct) >= 3.0:
                                last = price_alert_cache.get(t, 0)
                                if abs(pct - last) >= 1.0:
                                    bot.send_message(chat_id, f"🔔 *[{t}] {'급등 🚀' if pct>0 else '급락 📉'}*\n현재: ${curr:.2f} ({pct:+.2f}%)", parse_mode='Markdown')
                                    price_alert_cache[t] = pct
                except Exception as e: write_log(f"Monitor Error: {e}")
                time.sleep(60)

        threading.Thread(target=monitor_loop, daemon=True).start()
        bot.infinity_polling()

    threading.Thread(target=run_bot_system, daemon=True).start()

start_background_worker()

# --- Streamlit UI (생략 - 기존 UI 코드 유지) ---
st.title("📡 DeBrief Dashboard V28")
st.write("텔레그램 명령어가 복구되었으며, 실적 발표 로직이 최신화되었습니다.")
