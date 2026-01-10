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

price_alert_cache = st.session_state['price_alert_cache']
rsi_alert_status = st.session_state['rsi_alert_status']

# 제외할 키워드
EXCLUDED_KEYWORDS = ['casino', 'sport', 'baseball', 'football', 'soccer', 'lotto', 'horoscope', 
                     '카지노', '스포츠', '야구', '축구', '로또', '운세', '연예']

# ---------------------------------------------------------
# [0] 로그 기록
# ---------------------------------------------------------
def write_log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # print(f"[{timestamp}] {msg}") # 로그가 너무 많으면 주석 처리
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"[{timestamp}] {msg}\n")
    except: pass

# ---------------------------------------------------------
# [1] 설정 로드/저장 (충돌 방지 로직 적용)
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

DEFAULT_OPTS = {
    "🟢 감시": True, "📰 뉴스": True, "🏛️ SEC": True, "📈 급등락": True,
    "📊 거래량": False, "🚀 신고가": True, "📉 RSI": False,
    "〰️ MA": False, "🛁 볼린저": False, "🌊 MACD": False
}

# 구버전 키 마이그레이션
def migrate_options(old_opts):
    new_opts = DEFAULT_OPTS.copy()
    mapping = {
        "감시_ON": "🟢 감시", "뉴스": "📰 뉴스", "SEC": "🏛️ SEC",
        "가격_3%": "📈 급등락", "급등락(3%)": "📈 급등락",
        "거래량_2배": "📊 거래량", "거래량(2배)": "📊 거래량",
        "52주_신고가": "🚀 신고가", "RSI": "📉 RSI", 
        "MA_크로스": "〰️ MA", "MA크로스": "〰️ MA",
        "볼린저": "🛁 볼린저", "MACD": "🌊 MACD"
    }
    for old_k, val in old_opts.items():
        if old_k in mapping: new_opts[mapping[old_k]] = val
        elif old_k in new_opts: new_opts[old_k] = val
    return new_opts

def load_config():
    config = {
        "system_active": True,
        "eco_mode": True,
        "telegram": {"bot_token": "", "chat_id": ""}, 
        "tickers": { "TSLA": DEFAULT_OPTS.copy(), "NVDA": DEFAULT_OPTS.copy() },
        "news_history": {} 
    }
    
    url = get_jsonbin_url(); headers = get_jsonbin_headers()
    loaded_data = None
    
    # 1. Cloud Load
    if url and headers:
        try:
            resp = requests.get(f"{url}/latest", headers=headers, timeout=3)
            if resp.status_code == 200: loaded_data = resp.json()['record']
        except: pass
    
    # 2. Local Load
    if not loaded_data and os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f: loaded_data = json.load(f)
        except: pass

    if loaded_data:
        if "telegram" in loaded_data: config['telegram'] = loaded_data['telegram']
        if "system_active" in loaded_data: config['system_active'] = loaded_data['system_active']
        if "eco_mode" in loaded_data: config['eco_mode'] = loaded_data['eco_mode']
        if "news_history" in loaded_data: config['news_history'] = loaded_data['news_history']
        if "tickers" in loaded_data:
            for t, opts in loaded_data['tickers'].items(): config['tickers'][t] = migrate_options(opts)

    # Secret Overwrite
    try:
        if "telegram" in st.secrets:
            config['telegram']['bot_token'] = st.secrets["telegram"]["bot_token"]
            config['telegram']['chat_id'] = st.secrets["telegram"]["chat_id"]
    except: pass
    
    return config

def save_config(config):
    # JSONBin
    url = get_jsonbin_url(); headers = get_jsonbin_headers()
    if url and headers:
        try: requests.put(url, headers=headers, json=config, timeout=3)
        except: pass
    # Local
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
            response = requests.get(url, headers=headers, timeout=3)
            root = ET.fromstring(response.content)
            for item in root.findall('.//item')[:5]: 
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
                    if not any("\u3131" <= char <= "\u3163" or "\uac00" <= char <= "\ud7a3" for char in raw_title):
                        try: title_ko = translator.translate(raw_title[:150]) 
                        except: pass
                    
                    prefix = "🏛️" if is_sec_search else "📰"
                    unique_str = f"{raw_title}_{date_str}"
                    unique_hash = hashlib.md5(unique_str.encode()).hexdigest()

                    collected_items.append({
                        'title': f"{prefix} {title_ko}", 
                        'link': link, 'date': date_str, 'dt_obj': dt_obj if dt_obj else datetime.now(),
                        'hash': unique_hash, 'is_sec': is_sec_search
                    })
                except: continue
        except: pass

    for url in search_urls: fetch(url)
    collected_items.sort(key=lambda x: x['dt_obj'], reverse=True)
    return collected_items

def get_economic_events():
    try:
        scraper = cloudscraper.create_scraper()
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
        resp = scraper.get(url)
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
# [3] 백그라운드 봇 (안정성 강화)
# ---------------------------------------------------------
@st.cache_resource
def start_background_worker():
    for t in threading.enumerate():
        if t.name == "DeBrief_Worker": return

    def run_bot_system():
        time.sleep(2)
        write_log("🤖 봇 시스템 시작")
        
        while True:
            try:
                cfg = load_config()
                token = cfg['telegram']['bot_token']
                chat_id = cfg['telegram']['chat_id']
                if not token: 
                    time.sleep(10); continue
                
                bot = telebot.TeleBot(token)
                write_log("🤖 텔레그램 연결 성공")
                
                # 봇 명령어 핸들러
                @bot.message_handler(commands=['ping'])
                def ping(m): bot.reply_to(m, "🏓 Pong! (System OK)")

                @bot.message_handler(commands=['eco'])
                def eco(m):
                    evts = get_economic_events()
                    if not evts: return bot.reply_to(m, "일정 없음")
                    msg = "📅 *주요 경제 일정*\n"
                    for e in evts[:8]:
                        msg += f"▪️ {e['date']} {e['time']} | {e['event']}\n"
                    bot.reply_to(m, msg, parse_mode='Markdown')

                try:
                    bot.set_my_commands([BotCommand("ping", "생존확인"), BotCommand("eco", "경제지표")])
                except: pass
                
                # 모니터링 루프 (Thread)
                def monitor():
                    last_daily_sent = None
                    while True:
                        try:
                            # 1. 설정 로드 (매 루프마다 최신 로드)
                            # 주의: 여기서 로드한 설정은 '참조'용입니다. 저장 시 다시 읽어야 합니다.
                            curr_cfg = load_config()
                            
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
                                with ThreadPoolExecutor(max_workers=3) as exe:
                                    for t, s in curr_cfg['tickers'].items():
                                        exe.submit(check_ticker, t, s, token, chat_id)
                                        
                        except Exception as e: write_log(f"Loop Err: {e}")
                        time.sleep(60) # 1분 주기

                def check_ticker(ticker, settings, token, chat_id):
                    if not settings.get('🟢 감시', True): return

                    # [뉴스 감시]
                    if settings.get('📰 뉴스') or settings.get('🏛️ SEC'):
                        # 중요: 히스토리는 파일에서 직접 최신 상태를 읽어서 판단해야 함
                        # 쓰기 직전에 다시 읽기 (Race Condition 방지)
                        try:
                            fresh_cfg = load_config()
                            history = fresh_cfg.get('news_history', {})
                            if ticker not in history: history[ticker] = []
                            
                            items = get_integrated_news(ticker, False)
                            updated = False
                            
                            for item in items:
                                if item['hash'] in history[ticker]: continue
                                
                                is_sec = item['is_sec'] or "SEC" in item['title']
                                if (is_sec and settings.get('🏛️ SEC')) or (not is_sec and settings.get('📰 뉴스')):
                                    prefix = "🏛️" if is_sec else "📰"
                                    try:
                                        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                                    data={"chat_id": chat_id, "text": f"🔔 {prefix} *[{ticker}]*\n`[{item['date']}]` [{item['title']}]({item['link']})", "parse_mode": "Markdown"})
                                    except: pass
                                    history[ticker].append(item['hash'])
                                    if len(history[ticker]) > 50: history[ticker].pop(0)
                                    updated = True
                                    break # 한 사이클에 1개만 발송 (폭탄 방지)

                            if updated:
                                # 저장 시점: 다시 한번 파일을 읽어서 'news_history'만 교체하고 저장
                                # 이렇게 해야 UI에서 변경한 'tickers' 설정이 날아가지 않음
                                final_cfg = load_config()
                                final_cfg['news_history'] = history
                                save_config(final_cfg)
                        except: pass

                    # [가격 감시]
                    if settings.get('📈 급등락'):
                        try:
                            info = yf.Ticker(ticker).fast_info
                            curr = info.last_price; prev = info.previous_close
                            pct = ((curr - prev) / prev) * 100
                            if abs(pct) >= 3.0:
                                last = price_alert_cache.get(ticker, 0)
                                if abs(pct - last) >= 1.0: # 1% 더 움직여야 알림
                                    emoji = '🚀' if pct > 0 else '📉'
                                    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                                data={"chat_id": chat_id, "text": f"🔔 *[{ticker}] {emoji} 급등락*\n변동: {pct:.2f}%\n현재: ${curr:.2f}", "parse_mode": "Markdown"})
                                    price_alert_cache[ticker] = pct
                        except: pass

                t_mon = threading.Thread(target=monitor, daemon=True)
                t_mon.start()
                
                bot.infinity_polling()
            except Exception as e:
                write_log(f"Bot Main Crash: {e}")
                time.sleep(10)

    t_bot = threading.Thread(target=run_bot_system, daemon=True, name="DeBrief_Worker")
    t_bot.start()

start_background_worker()

# ---------------------------------------------------------
# [4] UI (Streamlit)
# ---------------------------------------------------------
st.set_page_config(page_title="DeBrief Cloud", layout="wide", page_icon="📡")
st.markdown("""<style>
    .stApp { background-color: #FFFFFF; color: #202124; }
    .stock-card { background-color: #F8F9FA; border: 1px solid #DADCE0; border-radius: 8px; padding: 10px; text-align: center; margin-bottom: 5px;}
    .stock-symbol { font-size: 1.1em; font-weight: 800; color: #1A73E8; }
    .up-txt { color: #137333; font-weight: bold; } .down-txt { color: #C5221F; font-weight: bold; }
    /* 체크박스 레이아웃 조정 */
    div[data-testid="stCheckbox"] { min-height: 0px; margin-bottom: -15px; }
</style>""", unsafe_allow_html=True)

# 설정 로드
config = load_config()

with st.sidebar:
    st.header("🎛️ 제어판")
    if st.toggle("시스템 전원", value=config.get('system_active', True)):
        st.success("🟢 작동 중")
        if not config['system_active']:
            config['system_active'] = True; save_config(config)
    else:
        st.error("⛔ 정지됨")
        if config['system_active']:
            config['system_active'] = False; save_config(config)

    with st.expander("🔑 봇 설정"):
        token = st.text_input("Bot Token", value=config['telegram'].get('bot_token', ''), type="password")
        chatid = st.text_input("Chat ID", value=config['telegram'].get('chat_id', ''))
        if st.button("저장"):
            config['telegram']['bot_token'] = token
            config['telegram']['chat_id'] = chatid
            save_config(config); st.rerun()

st.title("📡 DeBrief Cloud (V56)")
t1, t2, t3 = st.tabs(["📊 대시보드", "⚙️ 감시 관리", "📜 로그"])

with t1:
    if config['tickers']:
        cols = st.columns(6)
        for i, (t, _) in enumerate(config['tickers'].items()):
            try:
                info = yf.Ticker(t).fast_info
                p = info.last_price; prev = info.previous_close
                chg = ((p - prev)/prev)*100
                color_class = "up-txt" if chg >= 0 else "down-txt"
                with cols[i % 6]:
                    st.markdown(f"""<div class="stock-card"><div class="stock-symbol">{t}</div>
                    <div class="{color_class}">${p:.2f} ({chg:+.2f}%)</div></div>""", unsafe_allow_html=True)
            except: pass
    else:
        st.info("등록된 종목이 없습니다.")

with t2:
    st.markdown("### ⚙️ 감시 항목 설정")
    st.caption("체크박스를 클릭하면 해당 열(Column) 전체가 켜지거나 꺼집니다. (일괄 적용)")

    # 1. 일괄 제어 체크박스 (Master Checkbox) 생성
    # 표의 헤더처럼 보이도록 컬럼 배치
    if config['tickers']:
        # 첫 번째 티커에서 옵션 키들을 가져옴
        first_keys = list(next(iter(config['tickers'].values())).keys())
        
        # 레이아웃: [종목명 공간] + [옵션들]
        # Streamlit DataEditor는 인덱스(종목명)가 왼쪽에 있으므로 비율을 맞춰줌
        # 대략 1.5 (종목명) : 1 (각 옵션) 비율로 생성
        cols = st.columns([1.5] + [1] * len(first_keys))
        
        # 첫 컬럼은 빈 공간 (종목명 위)
        cols[0].write("")
        
        master_toggles = {}
        has_changed = False
        
        # 각 옵션별 마스터 체크박스 렌더링
        for idx, key in enumerate(first_keys):
            # 현재 모든 티커가 이 옵션에 대해 True인지 확인
            all_true = all(config['tickers'][t].get(key, False) for t in config['tickers'])
            
            # 체크박스 표시 (헤더 역할)
            is_checked = cols[idx+1].checkbox(f"{key}", value=all_true, key=f"master_{key}")
            
            # 상태 변화 감지: 현재 상태(all_true)와 체크박스 값(is_checked)이 다르면 사용자가 누른 것
            if is_checked != all_true:
                for t in config['tickers']:
                    config['tickers'][t][key] = is_checked
                has_changed = True

        # 변경사항이 있으면 즉시 저장 및 리런 (UI 갱신)
        if has_changed:
            save_config(config)
            st.rerun()

        # 2. 데이터 에디터 (개별 설정)
        df = pd.DataFrame(config['tickers']).T
        # 컬럼 순서를 마스터 체크박스 순서와 동일하게 정렬
        df = df[first_keys]
        
        edited_df = st.data_editor(df, use_container_width=True, height=len(df)*35 + 38)

        # 개별 셀 변경 감지 및 저장
        # DataFrame을 딕셔너리로 변환하여 비교
        new_tickers = edited_df.to_dict(orient='index')
        if new_tickers != config['tickers']:
            # 중요: 봇이 건드리는 news_history는 건드리지 않고 tickers만 업데이트
            # 파일에서 최신본을 읽어와서 병합
            latest_conf = load_config() 
            latest_conf['tickers'] = new_tickers
            save_config(latest_conf)
            st.toast("✅ 설정 저장됨")
            time.sleep(0.5)
            st.rerun() # 동기화

    # 종목 추가/삭제
    st.divider()
    c1, c2 = st.columns([3, 1])
    new_t = c1.text_input("종목 추가 (예: AAPL, SOXL)", placeholder="티커 입력")
    if c2.button("➕ 추가", use_container_width=True):
        if new_t:
            targets = [x.strip().upper() for x in new_t.split(',') if x.strip()]
            for t in targets:
                if t not in config['tickers']: config['tickers'][t] = DEFAULT_OPTS.copy()
            save_config(config); st.rerun()

    c3, c4 = st.columns([3, 1])
    del_t = c3.selectbox("삭제할 종목", options=list(config['tickers'].keys()) if config['tickers'] else [])
    if c4.button("🗑️ 삭제", use_container_width=True):
        if del_t in config['tickers']:
            del config['tickers'][del_t]
            save_config(config); st.rerun()

with t3:
    st.markdown("### 시스템 로그")
    if st.button("로그 새로고침"): st.rerun()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in reversed(lines[-20:]):
                st.text(line.strip())
