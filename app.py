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
import hashlib
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from telebot.types import BotCommand
from deep_translator import GoogleTranslator

# --- 프로젝트 설정 ---
CONFIG_FILE = 'debrief_settings.json'
LOG_FILE = 'debrief.log'

# [State] 캐시 및 전역 변수 초기화
if 'price_alert_cache' not in st.session_state: st.session_state['price_alert_cache'] = {}
if 'rsi_alert_status' not in st.session_state: st.session_state['rsi_alert_status'] = {}
# [핵심] 설정을 메모리에 유지하기 위한 세션 스테이트
if 'app_config' not in st.session_state: st.session_state['app_config'] = None
if 'unsaved_changes' not in st.session_state: st.session_state['unsaved_changes'] = False

price_alert_cache = st.session_state['price_alert_cache']
rsi_alert_status = st.session_state['rsi_alert_status']

# 제외할 키워드
EXCLUDED_KEYWORDS = ['casino', 'sport', 'baseball', 'football', 'soccer', 'lotto', 'horoscope', 
                     '카지노', '스포츠', '야구', '축구', '로또', '운세', '연예']

# ---------------------------------------------------------
# [0] 로그 및 유틸
# ---------------------------------------------------------
def write_log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {msg}\n")
    except: pass

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

DEFAULT_OPTS = {
    "🟢 감시": True, "📰 뉴스": True, "🏛️ SEC": True, "📈 급등락": True,
    "📊 거래량": False, "🚀 신고가": True, "📉 RSI": False,
    "〰️ MA": False, "🛁 볼린저": False, "🌊 MACD": False
}

# ---------------------------------------------------------
# [1] 설정 로드/저장 (봇/UI 분리)
# ---------------------------------------------------------
def load_config_from_disk():
    """디스크(또는 클라우드)에서 설정을 읽어옵니다."""
    config = {
        "system_active": True, "eco_mode": True,
        "telegram": {"bot_token": "", "chat_id": ""}, 
        "tickers": { "TSLA": DEFAULT_OPTS.copy(), "NVDA": DEFAULT_OPTS.copy() },
        "news_history": {} 
    }
    
    # 1. Cloud Load
    url = get_jsonbin_url(); headers = get_jsonbin_headers()
    loaded_data = None
    if url and headers:
        try:
            resp = requests.get(f"{url}/latest", headers=headers, timeout=2)
            if resp.status_code == 200: loaded_data = resp.json()['record']
        except: pass
    
    # 2. Local Load
    if not loaded_data and os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f: loaded_data = json.load(f)
        except: pass

    if loaded_data:
        # 병합 로직
        for key in ['system_active', 'eco_mode', 'telegram', 'news_history', 'tickers']:
            if key in loaded_data: config[key] = loaded_data[key]
            
    # 티커 옵션 마이그레이션 (구버전 호환)
    for t, opts in config['tickers'].items():
        # 누락된 키 채워넣기
        for def_k, def_v in DEFAULT_OPTS.items():
            if def_k not in opts: config['tickers'][t][def_k] = def_v

    # Secrets 덮어쓰기
    try:
        if "telegram" in st.secrets:
            config['telegram']['bot_token'] = st.secrets["telegram"]["bot_token"]
            config['telegram']['chat_id'] = st.secrets["telegram"]["chat_id"]
    except: pass
    
    return config

def save_config_to_disk(config):
    """설정을 디스크(또는 클라우드)에 저장합니다."""
    url = get_jsonbin_url(); headers = get_jsonbin_headers()
    if url and headers:
        try: requests.put(url, headers=headers, json=config, timeout=3)
        except: pass
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(config, f, indent=4, ensure_ascii=False)
    except: pass

# ---------------------------------------------------------
# [2] 데이터 엔진
# ---------------------------------------------------------
def get_integrated_news(ticker, is_sec_search=False):
    headers = {"User-Agent": "Mozilla/5.0"}
    search_urls = []
    if is_sec_search:
        search_urls.append(f"https://news.google.com/rss/search?q={ticker}+SEC+Filing+OR+8-K+OR+10-Q+OR+10-K+when:2d&hl=en-US&gl=US&ceid=US:en")
    else:
        search_urls.append(f"https://news.google.com/rss/search?q={ticker}+stock+news+when:1d&hl=en-US&gl=US&ceid=US:en")
        search_urls.append(f"https://news.google.com/rss/search?q={ticker}+주가+OR+주식+when:1d&hl=ko&gl=KR&ceid=KR:ko")

    collected_items = []
    seen_titles = set()
    translator = GoogleTranslator(source='auto', target='ko')

    def fetch(url):
        try:
            response = requests.get(url, headers=headers, timeout=2)
            root = ET.fromstring(response.content)
            for item in root.findall('.//item')[:3]: # 속도 최적화를 위해 상위 3개만
                try:
                    raw_title = item.find('title').text.split(' - ')[0]
                    link = item.find('link').text
                    pubDate = item.find('pubDate').text
                    
                    if any(bad in raw_title.lower() for bad in EXCLUDED_KEYWORDS): continue
                    dt_obj = None
                    try: dt_obj = datetime.strptime(pubDate.replace(' GMT', ''), '%a, %d %b %Y %H:%M:%S')
                    except: pass
                    
                    if dt_obj and (datetime.utcnow() - dt_obj) > timedelta(hours=24): continue
                    date_str = dt_obj.strftime('%m/%d %H:%M') if dt_obj else "Recent"
                    
                    if raw_title in seen_titles: continue
                    seen_titles.add(raw_title)

                    title_ko = raw_title
                    # 한글 번역 (필요시에만)
                    if not any("\u3131" <= char <= "\u3163" or "\uac00" <= char <= "\ud7a3" for char in raw_title):
                        try: title_ko = translator.translate(raw_title[:120]) 
                        except: pass
                    
                    prefix = "🏛️" if is_sec_search else "📰"
                    unique_hash = hashlib.md5(f"{raw_title}_{date_str}".encode()).hexdigest()

                    collected_items.append({
                        'title': f"{prefix} {title_ko}", 'link': link, 'date': date_str, 
                        'hash': unique_hash, 'is_sec': is_sec_search
                    })
                except: continue
        except: pass

    for url in search_urls: fetch(url)
    return collected_items

def get_economic_events():
    try:
        scraper = cloudscraper.create_scraper()
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
        resp = scraper.get(url, timeout=3)
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
                'date': event.find('date').text, 'time': event.find('time').text,
                'event': title, 'impact': event.find('impact').text,
                'forecast': event.find('forecast').text or ""
            })
        events.sort(key=lambda x: (x['date'], x['time']))
        return events
    except: return []

# ---------------------------------------------------------
# [3] 백그라운드 봇 (UI 간섭 최소화)
# ---------------------------------------------------------
@st.cache_resource
def start_background_worker():
    for t in threading.enumerate():
        if t.name == "DeBrief_Worker": return

    def run_bot_system():
        time.sleep(3)
        write_log("🤖 봇 시스템 시작")
        
        while True:
            try:
                # 설정 로드 (봇은 항상 최신 파일 기준)
                cfg = load_config_from_disk()
                token = cfg['telegram']['bot_token']
                chat_id = cfg['telegram']['chat_id']
                
                if not token: 
                    time.sleep(10); continue
                
                bot = telebot.TeleBot(token)
                
                # 봇 명령어 핸들러 (생략없이 필수 기능만 포함)
                @bot.message_handler(commands=['ping'])
                def ping(m): bot.reply_to(m, "🏓 Pong! (System OK)")

                @bot.message_handler(commands=['eco'])
                def eco(m):
                    evts = get_economic_events()
                    if not evts: return bot.reply_to(m, "일정 없음")
                    msg = "📅 *주요 경제 일정*\n"
                    for e in evts[:8]: msg += f"▪️ {e['date']} {e['time']} | {e['event']}\n"
                    bot.reply_to(m, msg, parse_mode='Markdown')

                # 모니터링 루프
                def monitor():
                    last_daily_sent = None
                    while True:
                        try:
                            # 1. 봇 전용 설정 읽기 (읽기 전용)
                            curr_cfg = load_config_from_disk()
                            
                            if not curr_cfg.get('system_active', True):
                                time.sleep(60); continue

                            # 경제지표 (매일 아침 8시)
                            now = datetime.now()
                            if curr_cfg.get('eco_mode', True) and now.hour == 8 and last_daily_sent != now.strftime('%Y-%m-%d'):
                                evts = get_economic_events()
                                today = now.strftime('%Y-%m-%d')
                                todays = [e for e in evts if e['date'] == today]
                                if todays:
                                    msg = f"☀️ *오늘({today}) 경제 일정*\n" + "\n".join([f"⏰ {e['time']} {e['event']}" for e in todays])
                                    bot.send_message(chat_id, msg, parse_mode='Markdown')
                                    last_daily_sent = today

                            # 티커 감시
                            if curr_cfg['tickers']:
                                with ThreadPoolExecutor(max_workers=2) as exe:
                                    for t, s in curr_cfg['tickers'].items():
                                        exe.submit(check_ticker, t, s, token, chat_id)
                                        
                        except Exception as e: write_log(f"Loop Err: {e}")
                        time.sleep(60)

                def check_ticker(ticker, settings, token, chat_id):
                    if not settings.get('🟢 감시', True): return

                    # [뉴스 감시]
                    if settings.get('📰 뉴스') or settings.get('🏛️ SEC'):
                        try:
                            # 파일에서 직접 히스토리만 읽음 (UI 설정 덮어쓰기 방지)
                            fresh_cfg = load_config_from_disk()
                            history = fresh_cfg.get('news_history', {})
                            if ticker not in history: history[ticker] = []
                            
                            items = get_integrated_news(ticker, False)
                            updated = False
                            
                            for item in items:
                                if item['hash'] in history[ticker]: continue
                                is_sec = item['is_sec']
                                if (is_sec and settings.get('🏛️ SEC')) or (not is_sec and settings.get('📰 뉴스')):
                                    try:
                                        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                                    data={"chat_id": chat_id, "text": f"🔔 *[{ticker}]*\n{item['title']}\n[Link]({item['link']})", "parse_mode": "Markdown"})
                                    except: pass
                                    history[ticker].append(item['hash'])
                                    if len(history[ticker]) > 50: history[ticker].pop(0)
                                    updated = True
                                    break # 1회 1뉴스 제한

                            if updated:
                                # 저장 시점: 파일을 다시 읽어서 'news_history' 부분만 교체하고 저장
                                # 이렇게 하면 사용자가 UI에서 바꾸고 있는 설정을 건드리지 않음
                                final_cfg = load_config_from_disk()
                                final_cfg['news_history'] = history
                                save_config_to_disk(final_cfg)
                        except: pass

                    # [가격 감시]
                    if settings.get('📈 급등락'):
                        try:
                            info = yf.Ticker(ticker).fast_info
                            curr = info.last_price; prev = info.previous_close
                            pct = ((curr - prev) / prev) * 100
                            if abs(pct) >= 3.0:
                                last = price_alert_cache.get(ticker, 0)
                                if abs(pct - last) >= 1.0:
                                    emoji = '🚀' if pct > 0 else '📉'
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                                data={"chat_id": chat_id, "text": f"🔔 *[{ticker}] {emoji} 급등락*\n변동: {pct:.2f}%\n현재: ${curr:.2f}", "parse_mode": "Markdown"})
                                    price_alert_cache[ticker] = pct
                        except: pass

                t_mon = threading.Thread(target=monitor, daemon=True)
                t_mon.start()
                
                bot.infinity_polling()
            except Exception as e:
                write_log(f"Bot Crash: {e}")
                time.sleep(10)

    t_bot = threading.Thread(target=run_bot_system, daemon=True, name="DeBrief_Worker")
    t_bot.start()

start_background_worker()

# ---------------------------------------------------------
# [4] UI (성능 최적화 및 깜빡임 방지)
# ---------------------------------------------------------
st.set_page_config(page_title="DeBrief Cloud", layout="wide", page_icon="📡")
st.markdown("""<style>
    .stApp { background-color: #FFFFFF; color: #202124; }
    .stock-card { background-color: #F8F9FA; border: 1px solid #DADCE0; border-radius: 8px; padding: 10px; text-align: center; margin-bottom: 5px;}
    .stock-symbol { font-size: 1.1em; font-weight: 800; color: #1A73E8; }
    .up-txt { color: #137333; font-weight: bold; } .down-txt { color: #C5221F; font-weight: bold; }
    div[data-testid="stCheckbox"] { min-height: 0px; margin-bottom: -15px; }
    .stButton button { width: 100%; }
</style>""", unsafe_allow_html=True)

# 1. 초기 로드 (앱 켤 때 한 번만 디스크에서 읽음)
if st.session_state['app_config'] is None:
    st.session_state['app_config'] = load_config_from_disk()

# 편의상 변수 바인딩 (참조)
config = st.session_state['app_config']

with st.sidebar:
    st.header("🎛️ 제어판")
    
    # 시스템 전원 (즉시 저장)
    sys_active = st.toggle("시스템 전원", value=config.get('system_active', True))
    if sys_active != config.get('system_active', True):
        config['system_active'] = sys_active
        save_config_to_disk(config) # 중요 설정은 즉시 저장
        st.toast("시스템 상태 변경됨")

    with st.expander("🔑 봇 설정"):
        token = st.text_input("Bot Token", value=config['telegram'].get('bot_token', ''), type="password")
        chatid = st.text_input("Chat ID", value=config['telegram'].get('chat_id', ''))
        if st.button("설정 저장"):
            config['telegram']['bot_token'] = token
            config['telegram']['chat_id'] = chatid
            save_config_to_disk(config)
            st.rerun()

st.title("📡 DeBrief Cloud (V57 Stable)")
t1, t2, t3 = st.tabs(["📊 대시보드", "⚙️ 감시 관리", "📜 로그"])

with t1:
    if config['tickers']:
        # 시세 조회는 렉 유발 가능성이 있으므로 예외처리 강화
        cols = st.columns(6)
        for i, (t, _) in enumerate(config['tickers'].items()):
            try:
                # 퀵하게 정보만 가져옴
                # yfinance 느릴 경우를 대비해 예외처리
                info = yf.Ticker(t).fast_info
                p = info.last_price
                if p:
                    prev = info.previous_close
                    chg = ((p - prev)/prev)*100
                    color_class = "up-txt" if chg >= 0 else "down-txt"
                    with cols[i % 6]:
                        st.markdown(f"""<div class="stock-card"><div class="stock-symbol">{t}</div>
                        <div class="{color_class}">${p:.2f} ({chg:+.2f}%)</div></div>""", unsafe_allow_html=True)
            except: pass
    else:
        st.info("등록된 종목이 없습니다.")

with t2:
    st.info("💡 설정을 변경한 후 반드시 하단의 **[💾 설정 저장하기]** 버튼을 눌러야 반영됩니다. (깜빡임 방지)")

    # 1. 일괄 제어 (Session State 조작)
    if config['tickers']:
        first_keys = list(next(iter(config['tickers'].values())).keys())
        
        # 버튼으로 일괄 제어 (체크박스보다 반응 빠름)
        st.markdown("**항목별 전체 켜기/끄기**")
        cols = st.columns(len(first_keys))
        for idx, key in enumerate(first_keys):
            if cols[idx].button(f"{key}", use_container_width=True, key=f"btn_{key}"):
                # 현재 상태 확인 (하나라도 켜져있으면 -> 끈다)
                any_true = any(config['tickers'][t].get(key, False) for t in config['tickers'])
                target = not any_true
                for t in config['tickers']:
                    config['tickers'][t][key] = target
                st.session_state['unsaved_changes'] = True
                st.rerun()

        # 2. 데이터 에디터
        df = pd.DataFrame(config['tickers']).T
        df = df[first_keys] # 컬럼 순서 고정

        # 편집 가능한 데이터프레임 표시
        edited_df = st.data_editor(df, use_container_width=True, height=len(df)*35 + 38, key="editor")

        # 변경 감지
        if not df.equals(edited_df):
            # 변경사항을 Session State에 반영 (디스크 저장 X)
            new_tickers = edited_df.to_dict(orient='index')
            # 기존 설정 유지하면서 값 업데이트
            for t in new_tickers:
                if t in config['tickers']:
                    config['tickers'][t].update(new_tickers[t])
            st.session_state['unsaved_changes'] = True

    # 3. 저장 버튼 (여기서만 파일 저장)
    st.divider()
    save_col, _ = st.columns([1, 4])
    if save_col.button("💾 설정 저장하기 (Save)", type="primary", use_container_width=True):
        save_config_to_disk(config)
        st.session_state['unsaved_changes'] = False
        st.success("✅ 모든 설정이 저장되었습니다.")
        time.sleep(1)
        st.rerun()

    if st.session_state['unsaved_changes']:
        st.warning("⚠️ 저장되지 않은 변경사항이 있습니다. [저장하기]를 눌러주세요.")

    # 추가/삭제 (즉시 반영)
    st.markdown("---")
    c1, c2, c3, c4 = st.columns([2, 1, 2, 1])
    
    new_t = c1.text_input("종목 추가", placeholder="예: TSLA", label_visibility='collapsed')
    if c2.button("➕ 추가", use_container_width=True):
        if new_t:
            targets = [x.strip().upper() for x in new_t.split(',') if x.strip()]
            for t in targets:
                if t not in config['tickers']: config['tickers'][t] = DEFAULT_OPTS.copy()
            save_config_to_disk(config) # 추가는 구조 변경이므로 즉시 저장
            st.rerun()

    del_t = c3.selectbox("삭제할 종목", options=list(config['tickers'].keys()) if config['tickers'] else [], label_visibility='collapsed')
    if c4.button("🗑️ 삭제", use_container_width=True):
        if del_t in config['tickers']:
            del config['tickers'][del_t]
            save_config_to_disk(config) # 삭제는 구조 변경이므로 즉시 저장
            st.rerun()

with t3:
    if st.button("로그 새로고침"): st.rerun()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in reversed(lines[-20:]):
                st.text(line.strip())
