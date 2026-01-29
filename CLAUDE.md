# CLAUDE.md - DeBrief Codebase Guide

## Project Overview

**DeBrief** is a Korean-language financial monitoring and alerting system that tracks stock market movements and economic indicators. It combines a Streamlit web dashboard with a Telegram bot backend for real-time notifications.

### Core Capabilities
- Stock price movement alerts (3% changes)
- Technical indicator monitoring (RSI, MA Crossover, Bollinger Bands, MACD)
- News and SEC filing alerts via Google News RSS
- Economic calendar events from Faireconomy.media
- Volume anomaly detection
- Multi-channel notifications (Web UI + Telegram)

### Target Audience
South Korean investors (UI is in Korean with auto-translation of English news)

---

## Tech Stack

| Technology | Purpose |
|------------|---------|
| **Python 3.11** | Runtime |
| **Streamlit** | Web dashboard framework |
| **pytelegrambotapi** | Telegram bot integration |
| **yfinance** | Yahoo Finance stock data |
| **pandas** | Data manipulation and display |
| **cloudscraper** | Web scraping (bypasses anti-bot) |
| **deep-translator** | Google Translate integration |
| **requests** | HTTP client for APIs |
| **lxml/html5lib** | HTML/XML parsing |

---

## Architecture

### Dual-Thread Model
```
Main Thread (Streamlit)          Background Thread
├── Web UI (port 8501)           ├── Telegram Bot (polling)
├── Config management            └── Monitor Loop (60s interval)
└── Dashboard rendering              ├── Ticker analysis
                                     ├── News detection
                                     └── Alert dispatch
```

### Data Flow
1. **Config** loads from JSONBin (cloud) → local file → Streamlit secrets (priority order)
2. **Monitor loop** runs every 60 seconds when system is active
3. **Alerts** sent via direct Telegram HTTP API (non-blocking)
4. **News deduplication** tracks sent links in `news_history` (max 30 per ticker)

---

## File Structure

```
DeBrief/
├── app.py                    # Monolithic application (all logic)
├── requirements.txt          # Python dependencies
├── CLAUDE.md                 # This file
├── .devcontainer/
│   └── devcontainer.json     # Dev container config
├── debrief_settings.json     # Local config (auto-generated)
└── debrief.log               # Application logs (auto-generated)
```

---

## Code Organization (app.py sections)

The application is organized into numbered sections:

| Section | Purpose | Key Functions |
|---------|---------|---------------|
| `[0]` | Logging | `write_log()` |
| `[1]` | Config I/O | `load_config()`, `save_config()`, `migrate_options()` |
| `[2]` | Data Engine | `get_integrated_news()`, `get_finviz_data()`, `get_economic_events()` |
| `[3]` | Bot Backend | `start_background_worker()`, `analyze_ticker()`, `monitor_loop()` |
| `[4]` | Streamlit UI | Tab-based interface (Dashboard, Management, Logs) |

---

## Configuration

### Secrets (Streamlit `st.secrets`)
```toml
[telegram]
bot_token = "your-bot-token"
chat_id = "your-chat-id"

[jsonbin]  # Optional cloud backup
master_key = "your-jsonbin-key"
bin_id = "your-bin-id"
```

### Settings Structure (`debrief_settings.json`)
```json
{
  "system_active": true,
  "eco_mode": true,
  "telegram": { "bot_token": "", "chat_id": "" },
  "tickers": {
    "TSLA": {
      "🟢 감시": true,
      "📰 뉴스": true,
      "🏛️ SEC": true,
      "📈 급등락(3%)": true,
      "📊 거래량(2배)": false,
      "🚀 신고가": true,
      "📉 RSI": false,
      "〰️ MA크로스": false,
      "🛁 볼린저": false,
      "🌊 MACD": false
    }
  },
  "news_history": {}
}
```

### Option Key Mapping (Korean Labels)
| Key | English Meaning |
|-----|-----------------|
| `🟢 감시` | Monitoring enabled |
| `📰 뉴스` | News alerts |
| `🏛️ SEC` | SEC filing alerts |
| `📈 급등락(3%)` | 3% price change |
| `📊 거래량(2배)` | 2x volume |
| `🚀 신고가` | 52-week high |
| `📉 RSI` | RSI overbought/oversold |
| `〰️ MA크로스` | MA crossover |
| `🛁 볼린저` | Bollinger Bands |
| `🌊 MACD` | MACD indicator |

---

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| `/start`, `/help` | Show command list |
| `/on` | Enable monitoring system |
| `/off` | Disable monitoring system |
| `/earning [ticker]` | Earnings date lookup |
| `/summary [ticker]` | Financial summary |
| `/eco` | Economic calendar |
| `/news [ticker]` | Recent news |
| `/sec [ticker]` | SEC filings |
| `/p [ticker]` | Current price |
| `/list` | Watched tickers |
| `/add [ticker]` | Add ticker |
| `/del [ticker]` | Remove ticker |
| `/ping` | Health check |

---

## Development Guidelines

### Running Locally
```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
streamlit run app.py --server.enableCORS false --server.enableXsrfProtection false
```

### Dev Container (Codespaces)
The project includes devcontainer configuration for GitHub Codespaces:
- Python 3.11 on Debian Bookworm
- Auto-installs requirements
- Auto-starts Streamlit on port 8501

### Key Conventions

1. **Single-file architecture**: All code lives in `app.py`
2. **Emoji-prefixed options**: All ticker settings use emoji icons for UI consistency
3. **Korean UI text**: User-facing strings are in Korean
4. **Auto-translation**: News titles translated via `GoogleTranslator(source='auto', target='ko')`
5. **Silent error handling**: Most exceptions are caught silently with fallback behavior
6. **Backward compatibility**: `migrate_options()` converts old config keys to new emoji-prefixed format

### Session State Keys
```python
st.session_state['price_alert_cache']  # {ticker: last_pct_change}
st.session_state['rsi_alert_status']   # {ticker: "OB"|"OS"|"NORMAL"}
st.session_state['eco_alert_cache']    # set() of sent event IDs
```

### Alert Thresholds
- **Price alerts**: >=3% change from previous close
- **Price re-alert**: >=1% change from last alerted percentage
- **RSI overbought**: >=70
- **RSI oversold**: <=30
- **RSI reset zone**: 35-65

### Scheduled Tasks
- **Weekly economic summary**: Monday 8 AM (High impact events only)
- **Daily economic alerts**: Every day 8 AM (today's events)
- **Ticker monitoring**: Every 60 seconds (when system active)

---

## Common Modification Tasks

### Adding a New Ticker Option
1. Add to `DEFAULT_OPTS` dict (line ~60)
2. Add migration mapping in `migrate_options()` if needed
3. Implement logic in `analyze_ticker()` function

### Adding a New Telegram Command
1. Add handler with `@bot.message_handler(commands=['cmd'])`
2. Register in `bot.set_my_commands([...])` list

### Adding a New Data Source
1. Create fetcher function in `[2] Data Engine` section
2. Call from `analyze_ticker()` or add new bot command

### Modifying Alert Logic
- Price alerts: `analyze_ticker()` lines ~493-503
- RSI alerts: `analyze_ticker()` lines ~505-513
- News alerts: `analyze_ticker()` lines ~467-490

---

## External APIs Used

| API | Purpose | Rate Limiting |
|-----|---------|---------------|
| Yahoo Finance (yfinance) | Stock data | Implicit |
| Google News RSS | News feeds | None |
| Finviz | Company metrics | Anti-bot (use cloudscraper) |
| Faireconomy.media | Economic calendar | None |
| Telegram Bot API | Notifications | 30 msg/sec |
| JSONBin | Cloud config backup | 10,000 req/month (free) |

---

## Troubleshooting

### Bot Not Responding
- Check `telegram.bot_token` and `chat_id` in secrets/config
- View `debrief.log` for errors
- Verify thread is running: `DeBrief_Worker` in `threading.enumerate()`

### News Not Updating
- Check `news_history` in config (may be full)
- Verify Google News RSS is accessible
- Check translation service availability

### Config Not Saving
- Verify JSONBin credentials if using cloud backup
- Check file write permissions for local `debrief_settings.json`

---

## Version History

Current version: **V55** (referenced in code comments)
- Icon recovery and full feature restoration
- Backward-compatible config migration
- Multi-source news integration

---

## Notes for AI Assistants

1. **Language**: Code comments and UI text are in Korean. Maintain this convention.
2. **Emojis**: All option keys must include emoji prefixes for UI consistency.
3. **Error handling**: Follow the silent try/except pattern with logging.
4. **Thread safety**: Config is re-loaded in monitor loop; use `load_config()` for fresh data.
5. **API calls**: Use `requests.post()` directly for Telegram (not bot object) in async contexts.
6. **Testing**: No test suite exists; manual testing via Streamlit UI and Telegram bot.
