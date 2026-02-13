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
import cloudscraper
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from telebot.types import BotCommand
from deep_translator import GoogleTranslator

# --- 프로젝트 설정 ---
CONFIG_FILE = 'debrief_settings.json'
LOG_FILE = 'debrief.log'

# [State] 캐시 및 전역 변수
if 'price_alert_cache' not in st.session_state: st.session_state['price_alert_cache'] = {}
if 'rsi_alert_status' not in st.session_state: st.session_state['rsi_alert_status'] = {}
if 'eco_alert_cache' not in st.session_state: st.session_state['eco_alert_cache'] = set()

price_alert_cache = st.session_state['price_alert_cache']
rsi_alert_status = st.session_state['rsi_alert_status']
eco_alert_cache = st.session_state['eco_alert_cache']

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
# [1] 설정 로드/저장 (자동 마이그레이션 포함)
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

# [개편] 뉴스/SEC 개별 옵션을 제거하고 심플하게 통합
DEFAULT_OPTS = {
    "🟢 감시": True, 
    "📈 급등락(3%)": True,
    "📊 거래량(2배)": False, 
    "🚀 신고가": True, 
    "📉 RSI": False,
    "〰️ MA크로스": False, 
    "🛁 볼린저": False, 
    "🌊 MACD": False
}

def migrate_options(old_opts):
    """구버전 키를 신버전으로 자동 변환 (삭제된 기능 제외)"""
    new_opts = DEFAULT_OPTS.copy()
    mapping = {
        "감시_ON": "🟢 감시",
        "가격_3%": "📈 급등락(3%)", "거래량_2배": "📊 거래량(2배)",
        "52주_신고가": "🚀 신고가", "RSI": "📉 RSI", "MA_크로스": "〰️ MA크로스",
        "볼린저": "🛁 볼린저", "MACD": "🌊 MACD"
    }
    
    for old_k, val in old_opts.items():
        if old_k in mapping:
            new_opts[mapping[old_k]] = val
        elif old_k in new_opts:
            new_opts[old_k] = val
            
    return new_opts

def load_config():
    config = {
        "system_active": True,
        "eco_mode": True,
        "telegram": {"bot_token": "", "chat_id": ""}, 
        "tickers": {
            "TSLA": DEFAULT_OPTS.copy(),
            "NVDA": DEFAULT_OPTS.copy()
        }
    }
    
    url = get_jsonbin_url()
    headers = get_jsonbin_headers()
    loaded_data = None
    
    if url and headers:
        try:
            resp = requests.get(f"{url}/latest", headers=headers, timeout=5)
            if resp.status_code == 200: loaded_data = resp.json()['record']
        except: pass
    
    if not loaded_data and os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f: loaded_data = json.load(f)
        except: pass

    if loaded_data:
        if "telegram" in loaded_data: config['telegram'] = loaded_data['telegram']
        if "system_active" in loaded_data: config['system_active'] = loaded_data['system_active']
        if "eco_mode" in loaded_data: config['eco_mode'] = loaded_data['eco_mode']
        
        if "tickers" in loaded_data:
            for t, opts in loaded_data['tickers'].items():
                config['tickers'][t] = migrate_options(opts)

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
# [2] 데이터 엔진
# ---------------------------------------------------------
def get_finviz_data(ticker):
    try:
        url = f"https://finviz.com/quote.ashx?t={ticker}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            scraper = cloudscraper.create_scraper()
            resp = scraper.get(url, timeout=10)
            text = resp.text
        except:
            resp = requests.get(url, headers=headers, timeout=10)
            text = resp.text
        dfs = pd.read_html(text)
        data = {}
        for df in dfs:
            if 'P/E' in df.to_string() or 'Market Cap' in df.to_string():
                if len(df.columns) > 1:
                    for i in range(0, len(df.columns), 2):
                        try:
                            keys = df.iloc[:, i]; values = df.iloc[:, i+1]
                            for k, v in zip(keys, values): data[str(k)] = str(v)
                        except: pass
        return data
    except Exception as e:
        return {}

def get_economic_events():
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            scraper = cloudscraper.create_scraper()
            resp = scraper.get(url, timeout=10)
        except:
            resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200: return []
        
        root = ET.fromstring(resp.content)
        events = []
        translator = GoogleTranslator(source='auto', target='ko')
        for event in root.findall('event'):
            if event.find('country').text != 'USD': continue
            if event.find('impact').text not in ['High', 'Medium']: continue
            title = event.find('title').text
            try: title = translator.translate(title)
            except: pass
            events.append({
                'date': event.find('date').text,
                'time': event.find('time').text,
                'event': title,
                'impact': event.find('impact').text,
                'forecast': event.find('forecast').text or "",
                'id': f"{event.find('date').text}_{event.find('time').text}_{title}"
            })
        events.sort(key=lambda x: (x['date'], x['time']))
        return events
    except Exception as e: return []

# ---------------------------------------------------------
# [3] 백그라운드 봇 & 분석 엔진
# ---------------------------------------------------------
@st.cache_resource
def start_background_worker():
    for t in threading.enumerate():
        if t.name == "DeBrief_Worker": return

    def run_bot_system():
        time.sleep(1)
        write_log("🤖 봇 시스템 시작...")
        cfg = load_config()
        token = cfg['telegram']['bot_token']
        chat_id = cfg['telegram']['chat_id']
        if not token: return
        
        try:
            bot = telebot.TeleBot(token)
            last_weekly_sent = None
            last_daily_sent = None
            try: bot.send_message(chat_id, "🤖 DeBrief V56 가동\n통합 재료 포착 엔진 업데이트 완료.")
            except: pass

            @bot.message_handler(commands=['start', 'help'])
            def start_cmd(m): 
                msg = ("🤖 *DeBrief V56*\n"
                       "/on : 시스템 켜기\n"
                       "/off : 시스템 끄기\n"
                       "/earning [티커] : 실적발표\n"
                       "/summary [티커] : 재무요약\n"
                       "/eco : 경제지표\n"
                       "/news [티커] : 미국 현지 뉴스\n"
                       "/p [티커] : 현재가\n"
                       "/list : 감시목록\n"
                       "/add [티커] : 추가\n"
                       "/del [티커] : 삭제\n"
                       "/vix : VIX 지수\n"
                       "/ping : 생존확인")
                bot.reply_to(m, msg, parse_mode='Markdown')

            @bot.message_handler(commands=['on'])
            def on_cmd(m):
                c = load_config()
                c['system_active'] = True
                save_config(c)
                bot.reply_to(m, "🟢 시스템 가동 (모니터링 시작)")

            @bot.message_handler(commands=['off'])
            def off_cmd(m):
                c = load_config()
                c['system_active'] = False
                save_config(c)
                bot.reply_to(m, "⛔ 시스템 정지 (모니터링 중단)")

            @bot.message_handler(commands=['earning', '실적'])
            def earning_cmd(m):
                try:
                    parts = m.text.split()
                    if len(parts) < 2: return bot.reply_to(m, "사용법: /earning [티커]")
                    t = parts[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    msg = ""
                    try:
                        stock = yf.Ticker(t)
                        dates = stock.earnings_dates
                        if dates is not None and not dates.empty:
                            if dates.index.tz is not None: dates.index = dates.index.tz_localize(None)
                            target = dates.index[0]
                            msg = f"📅 *{t} 실적 발표*\n🗓️ 일시: `{target.strftime('%Y-%m-%d')}`"
                    except: pass
                    if not msg:
                        data = get_finviz_data(t)
                        if 'Earnings' in data and data['Earnings'] != '-':
                            e_date = data['Earnings']
                            clean_date = e_date.replace(' BMO','').replace(' AMC','')
                            time_icon = "☀️ 장전" if "BMO" in e_date else "🌙 장후" if "AMC" in e_date else ""
                            msg = f"📅 *{t} 실적 발표*\n🗓️ 일시: `{clean_date}` {time_icon}"
                    if msg: bot.reply_to(m, msg, parse_mode='Markdown')
                    else: bot.reply_to(m, f"❌ {t}: 실적 발표 정보 없음")
                except Exception as e: bot.reply_to(m, f"❌ 조회 실패")

            @bot.message_handler(commands=['summary', '요약'])
            def summary_cmd(m):
                try:
                    parts = m.text.split()
                    if len(parts) < 2: return bot.reply_to(m, "사용법: /summary [티커]")
                    t = parts[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    curr_p = None; mkt_cap_y = None; prev_close = None
                    try:
                        fi = yf.Ticker(t).fast_info
                        curr_p = fi.last_price
                        mkt_cap_y = fi.market_cap
                        prev_close = fi.previous_close
                    except: pass
                    d = get_finviz_data(t)
                    price = f"{curr_p:.2f}" if curr_p else d.get('Price', 'N/A')
                    pe = d.get('P/E', 'N/A'); pbr = d.get('P/B', 'N/A')
                    cap = d.get('Market Cap', 'N/A'); target = d.get('Target Price', 'N/A')
                    if cap == 'N/A' and mkt_cap_y: cap = f"${mkt_cap_y/1e9:.2f}B"
                    chg_str = ""
                    if curr_p and prev_close:
                        chg = ((curr_p - prev_close) / prev_close) * 100
                        chg_str = f" ({chg:+.2f}%)"
                    msg = (f"📊 *{t} 재무 요약*\n"
                           f"💰 현재가: `${price}`{chg_str}\n"
                           f"🏢 시가총액: `{cap}`\n"
                           f"📈 PER: `{pe}`\n"
                           f"📚 PBR: `{pbr}`\n"
                           f"🎯 목표주가: `${target}`")
                    bot.reply_to(m, msg, parse_mode='Markdown')
                except Exception as e: bot.reply_to(m, f"❌ 조회 실패")

            @bot.message_handler(commands=['eco'])
            def eco_cmd(m):
                try:
                    bot.send_chat_action(m.chat.id, 'typing')
                    events = get_economic_events()
                    if not events: return bot.reply_to(m, "❌ 일정 없음")
                    msg = "📅 *주요 경제 일정 (USD)*\n────────────────"
                    c=0
                    for e in events:
                        icon = "🔥" if e['impact'] == 'High' else "🔸"
                        fcst = f"(예상:{e['forecast']})" if e['forecast'] else ""
                        msg += f"\n{icon} `{e['date']} {e['time']}`\n*{e['event']}* {fcst}\n"
                        c+=1
                        if c>=15: break
                    bot.reply_to(m, msg, parse_mode='Markdown')
                except Exception as e: bot.reply_to(m, f"❌ 경제일정 조회 실패")

            # [수정됨] 야후 파이낸스 뉴스 + 번역 로직으로 전면 교체
            @bot.message_handler(commands=['news'])
            def news_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    news_items = yf.Ticker(t).news
                    if not news_items: return bot.reply_to(m, "최근 뉴스가 없습니다.")
                    
                    msg = [f"📰 *{t} 최신 뉴스*"]
                    translator = GoogleTranslator(source='en', target='ko')
                    
                    for i in news_items[:5]: # 상위 5개만
                        raw_title = i.get('title', '')
                        link = i.get('link', '')
                        try:
                            title_ko = translator.translate(raw_title)
                        except:
                            title_ko = raw_title
                        msg.append(f"▪️ [{title_ko}]({link})")
                        
                    bot.reply_to(m, "\n\n".join(msg), parse_mode='Markdown', disable_web_page_preview=True)
                except IndexError: bot.reply_to(m, "사용법: /news [티커]")
                except Exception as e: bot.reply_to(m, f"❌ 뉴스 조회 실패: {e}")

            @bot.message_handler(commands=['p'])
            def p_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    bot.send_chat_action(m.chat.id, 'typing')
                    fi = yf.Ticker(t).fast_info
                    price = fi.last_price
                    prev = fi.previous_close
                    chg = ((price - prev) / prev) * 100
                    emoji = "🔴" if chg >= 0 else "🔵"
                    bot.reply_to(m, f"💰 *{t}*: `${price:.2f}` {emoji} ({chg:+.2f}%)", parse_mode='Markdown')
                except IndexError: bot.reply_to(m, "사용법: /p [티커]")
                except Exception as e: bot.reply_to(m, f"❌ 조회 실패")

            @bot.message_handler(commands=['list'])
            def list_cmd(m):
                try:
                    c = load_config()
                    tickers = list(c['tickers'].keys())
                    if tickers:
                        bot.reply_to(m, f"📋 감시목록 ({len(tickers)}개):\n`{', '.join(tickers)}`", parse_mode='Markdown')
                    else:
                        bot.reply_to(m, "📋 감시목록이 비어있습니다.")
                except Exception as e: bot.reply_to(m, f"❌ 목록 조회 실패")

            @bot.message_handler(commands=['add'])
            def add_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t in c['tickers']: bot.reply_to(m, f"⚠️ {t}은(는) 이미 목록에 있습니다.")
                    else:
                        c['tickers'][t] = DEFAULT_OPTS.copy()
                        save_config(c)
                        bot.reply_to(m, f"✅ {t} 추가됨")
                except IndexError: bot.reply_to(m, "사용법: /add [티커]")
                except Exception as e: bot.reply_to(m, f"❌ 추가 실패")

            @bot.message_handler(commands=['del'])
            def del_cmd(m):
                try:
                    t = m.text.split()[1].upper()
                    c = load_config()
                    if t in c['tickers']:
                        del c['tickers'][t]
                        save_config(c)
                        bot.reply_to(m, f"🗑️ {t} 삭제됨")
                    else: bot.reply_to(m, f"⚠️ {t}은(는) 목록에 없습니다.")
                except IndexError: bot.reply_to(m, "사용법: /del [티커]")
                except Exception as e: bot.reply_to(m, f"❌ 삭제 실패")

            @bot.message_handler(commands=['ping'])
            def ping_cmd(m): bot.reply_to(m, "🏓 Pong! 정상.")

            @bot.message_handler(commands=['vix'])
            def vix_cmd(m):
                try:
                    bot.send_chat_action(m.chat.id, 'typing')
                    vix = yf.Ticker("^VIX")
                    info = vix.fast_info
                    curr = info.last_price
                    prev = info.previous_close
                    chg = ((curr - prev) / prev) * 100
                    if curr < 15: level = "😌 낮음 (안정)"
                    elif curr < 20: level = "🙂 보통"
                    elif curr < 25: level = "😰 높음 (주의)"
                    elif curr < 30: level = "😱 매우 높음 (경계)"
                    else: level = "🚨 극단적 (공포)"
                    bot.reply_to(m, f"📊 *VIX 공포지수*\n\n현재: `{curr:.2f}` ({chg:+.2f}%)\n전일: `{prev:.2f}`\n상태: {level}", parse_mode='Markdown')
                except Exception as e: bot.reply_to(m, "❌ VIX 조회 실패")

            try:
                bot.set_my_commands([
                    BotCommand("start", "🤖 시작/도움말"),
                    BotCommand("p", "💰 현재가"),
                    BotCommand("vix", "📊 VIX 공포지수"),
                    BotCommand("summary", "📊 재무요약"),
                    BotCommand("earning", "💰 실적발표"),
                    BotCommand("news", "📰 현지 뉴스 번역"),
                    BotCommand("eco", "📅 경제지표"),
                    BotCommand("list", "📋 감시목록"),
                    BotCommand("add", "➕ 티커추가"),
                    BotCommand("del", "🗑️ 티커삭제"),
                    BotCommand("on", "🟢 시스템가동"),
                    BotCommand("off", "⛔ 시스템정지"),
                    BotCommand("ping", "🏓 생존확인")
                ])
            except Exception as e: pass

            # --- 핵심 분석 엔진 (가격 급등락 + 재료 포착 통합) ---
            def analyze_ticker(ticker, settings, token, chat_id):
                if not settings.get('🟢 감시', True): return
                
                try:
                    stock = yf.Ticker(ticker)
                    fi = stock.fast_info
                    curr_price = fi.last_price
                    prev_close = fi.previous_close
                    pct_change = ((curr_price - prev_close) / prev_close) * 100
                    
                    # [트리거 발동]: 3% 이상 급등락 발생 시
                    if settings.get('📈 급등락(3%)') and abs(pct_change) >= 3.0:
                        last_pct = price_alert_cache.get(ticker, 0)
                        
                        # 기존 알림 대비 추가로 1% 이상 더 변동했을 때만 재발송 (도배 방지)
                        if abs(pct_change - last_pct) >= 1.0:
                            price_alert_cache[ticker] = pct_change
                            
                            # 재료(뉴스) 즉시 검색 및 번역
                            news_text = "관련 뉴스를 찾을 수 없습니다. (수급/커뮤니티 이슈 가능성)"
                            try:
                                news_items = stock.news
                                if news_items:
                                    latest_news = news_items[0]
                                    raw_title = latest_news.get('title', '')
                                    link = latest_news.get('link', '')
                                    
                                    translator = GoogleTranslator(source='en', target='ko')
                                    try: translated_title = translator.translate(raw_title)
                                    except: translated_title = raw_title
                                        
                                    news_text = f"[{translated_title}]({link})"
                            except Exception as e: pass

                            # 메시지 조립 및 전송
                            direction = "🚀 급등" if pct_change > 0 else "📉 급락"
                            color = "🔴" if pct_change > 0 else "🔵"
                            
                            msg = f"{color} *[{ticker}] {direction} 감지!*\n"
                            msg += f"────────────────\n"
                            msg += f"▪️ 변동률: `{pct_change:+.2f}%`\n"
                            msg += f"▪️ 현재가: `${curr_price:.2f}`\n\n"
                            msg += f"📰 **[상승/하락 추정 재료]**\n{news_text}"
                            
                            requests.post(
                                f"https://api.telegram.org/bot{token}/sendMessage", 
                                data={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True}
                            )

                    # 보조지표 로직 (RSI)
                    if settings.get('📉 RSI'):
                        h = stock.history(period="1mo")
                        if not h.empty:
                            delta = h['Close'].diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean(); loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
                            rs = gain / loss; rsi = 100 - (100 / (1 + rs)).iloc[-1]
                            status = rsi_alert_status.get(ticker, "NORMAL")
                            if rsi >= 70 and status != "OB": 
                                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"🔥 [{ticker}] RSI 과매수 ({rsi:.1f})"})
                                rsi_alert_status[ticker] = "OB"
                            elif rsi <= 30 and status != "OS": 
                                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": f"💧 [{ticker}] RSI 과매도 ({rsi:.1f})"})
                                rsi_alert_status[ticker] = "OS"
                            elif 35 < rsi < 65: 
                                rsi_alert_status[ticker] = "NORMAL"
                except Exception as e: pass

            def monitor_loop():
                nonlocal last_weekly_sent, last_daily_sent
                while True:
                    try:
                        cfg = load_config()
                        if cfg.get('eco_mode', True):
                            now = datetime.now()
                            if now.weekday() == 0 and now.hour == 8 and last_weekly_sent != now.strftime('%Y-%m-%d'):
                                events = get_economic_events()
                                if events:
                                    msg = "📅 *이번 주 주요 경제 일정*\n────────────────"
                                    c=0
                                    for e in events:
                                        if e['impact'] == 'High': msg += f"\n🗓️ `{e['date']} {e['time']}`\n🔥 {e['event']}"; c+=1
                                    if c>0: bot.send_message(chat_id, msg, parse_mode='Markdown'); last_weekly_sent = now.strftime('%Y-%m-%d')
                            if now.hour == 8 and last_daily_sent != now.strftime('%Y-%m-%d'):
                                events = get_economic_events()
                                today = datetime.now().strftime('%Y-%m-%d')
                                todays = [e for e in events if e['date'] == today]
                                if todays:
                                    msg = f"☀️ *오늘({today}) 주요 일정*\n────────────────"
                                    for e in todays: msg += f"\n⏰ {e['time']} : {e['event']} (예상:{e['forecast']})"
                                    bot.send_message(chat_id, msg, parse_mode='Markdown'); last_daily_sent = now.strftime('%Y-%m-%d')

                        if cfg.get('system_active', True) and cfg['tickers']:
                            cur_token = cfg['telegram']['bot_token']; cur_chat = cfg['telegram']['chat_id']
                            with ThreadPoolExecutor(max_workers=5) as exe:
                                for t, s in cfg['tickers'].items(): exe.submit(analyze_ticker, t, s, cur_token, cur_chat)
                    except Exception as e: write_log(f"Loop Err: {e}")
                    time.sleep(60)

            t_mon = threading.Thread(target=monitor_loop, daemon=True, name="DeBrief_Monitor")
            t_mon.start()

            while True:
                try: bot.infinity_polling(timeout=10, long_polling_timeout=5)
                except: time.sleep(5)

        except Exception as e: write_log(f"Bot Error: {e}")

    t_bot = threading.Thread(target=run_bot_system, daemon=True, name="DeBrief_Worker")
    t_bot.start()

start_background_worker()

# ---------------------------------------------------------
# [4] UI
# ---------------------------------------------------------
st.set_page_config(page_title="DeBrief", layout="wide", page_icon="📡")
st.markdown("""<style>
    .stApp { background-color: #FFFFFF; color: #202124; }
    .stock-card { background-color: #FFFFFF; border: 1px solid #DADCE0; border-radius: 8px; padding: 8px 5px; margin-bottom: 6px; text-align: center; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
    .stock-symbol { font-size: 1.0em; font-weight: 800; color: #1A73E8; }
    .stock-price-box { display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 0.8em; font-weight: 700; }
    .up-theme { background-color: #FCE8E6; color: #C5221F; } .down-theme { background-color: #E6F4EA; color: #137333; }
</style>""", unsafe_allow_html=True)

config = load_config()

with st.sidebar:
    st.header("🎛️ Control Panel")
    if "jsonbin" in st.secrets: st.success("☁️ Cloud Connected")
    
    if st.toggle("System Power", value=config.get('system_active', True)):
        st.success("🟢 Active"); config['system_active'] = True
    else:
        st.error("⛔ Paused"); config['system_active'] = False
    save_config(config)

st.markdown("<h3 style='color: #1A73E8;'>📡 DeBrief Cloud (V56)</h3>", unsafe_allow_html=True)
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
    st.markdown("#### 📢 알림 설정")
    eco_mode = st.checkbox("📢 경제지표/연준 알림", value=config.get('eco_mode', True))
    if eco_mode != config.get('eco_mode', True):
        config['eco_mode'] = eco_mode; save_config(config); st.toast("저장됨")

    st.divider()
    c_all_1, c_all_2, c_blank = st.columns([1, 1, 3])
    if c_all_1.button("✅ ALL ON", use_container_width=True):
        for t in config['tickers']:
            for k in config['tickers'][t]: config['tickers'][t][k] = True
        save_config(config); st.rerun()
        
    if c_all_2.button("⛔ ALL OFF", use_container_width=True):
        for t in config['tickers']:
            for k in config['tickers'][t]: config['tickers'][t][k] = False
        save_config(config); st.rerun()

    input_t = st.text_input("Add Tickers (ex: TSLA, AAPL)")
    if st.button("➕ Add"):
        for t in [x.strip().upper() for x in input_t.split(',') if x.strip()]:
            config['tickers'][t] = DEFAULT_OPTS.copy()
        save_config(config); st.rerun()
    
    if config['tickers']:
        df = pd.DataFrame(config['tickers']).T
        edited = st.data_editor(df, use_container_width=True)
        if not df.equals(edited):
            config['tickers'] = edited.to_dict(orient='index')
            save_config(config); st.toast("Saved!")
            
    st.divider()
    del_cols = st.columns([4, 1])
    del_target = del_cols[0].selectbox("삭제할 종목 선택", options=list(config['tickers'].keys()))
    if del_cols[1].button("삭제"):
        if del_target in config['tickers']: del config['tickers'][del_target]; save_config(config); st.rerun()

with t3:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            for line in reversed(f.readlines()[-50:]): st.text(line.strip())
