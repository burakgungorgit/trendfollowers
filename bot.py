#!/usr/bin/env python3
import os
import time
import json
from datetime import datetime
import pandas as pd
import yfinance as yf
import requests
from dotenv import load_dotenv

# ================== ENV ==================
load_dotenv("/home/ubuntu/trendfollowers/.env")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ================== AYARLAR ==================
EMA_FAST = 50
EMA_SLOW = 100
EMA_TRAIL = 200

STATE_FILE = "state.json"
LOG_FILE = "log.txt"
SIGNAL_LOG = "signals.csv"
CHECK_INTERVAL = 60 * 60  # saatlik kontrol (günlük veri)

ASSETS = [
    "BTC-USD","ETH-USD","SOL-USD","AVAX-USD",
    "TUPRS.IS","DOAS.IS","THYAO.IS","TTRAK.IS","MAVI.IS","ASELS.IS","ENJSA.IS",
    "KONTR.IS","ARDYZ.IS","MIATK.IS","MPARK.IS","EKGYO.IS","ASTOR.IS",
    "LOGO.IS","SMRTG.IS","GWIND.IS","YEOTK.IS","OYAKC.IS","CWENE.IS",
    "EREGL.IS","DESA.IS","BIMAS.IS","TUKAS.IS","AKSA.IS",
    "GOOGL","NVDA","META","INTC","AAPL","MSFT"
]

# ================== LOG ==================
def log(msg, symbol=None):
    t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{t}]"
    if symbol:
        line += f" [{symbol}]"
    line += f" {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ================== SIGNAL CSV ==================
def log_signal(symbol, signal, price):
    header = not os.path.exists(SIGNAL_LOG)
    df = pd.DataFrame([{
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "signal": signal,
        "price": round(price, 4)
    }])
    df.to_csv(SIGNAL_LOG, mode="a", index=False, header=header)

# ================== STATE ==================
def load_state():
    base = {}
    for s in ASSETS:
        base[s] = {
            "in_position": False,
            "entry_price": None,
            "tp50_sent": False,
            "tp70_sent": False,
            "below_ema200_count": 0,
            "last_checked_date": None
        }

    if not os.path.exists(STATE_FILE):
        return base

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        log("⚠️ state.json bozuk veya boş. Sıfırdan oluşturuluyor.")
        return base

    for s in base:
        if s not in data:
            data[s] = base[s]
        else:
            for k in base[s]:
                if k not in data[s]:
                    data[s][k] = base[s][k]

    return data

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)
    os.replace(tmp, STATE_FILE)

# ================== TELEGRAM ==================
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except Exception as e:
        log(f"Telegram hata: {e}")

# ================== DATA ==================
def get_data(symbol):
    df = yf.download(
        symbol,
        interval="1d",
        period="800d",  # istersen "max" da olabilir
        auto_adjust=True,
        progress=False
    )
    if df.empty or len(df) < 210:
        return None

    # MultiIndex kontrolü
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df["EMA50"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["EMA100"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=EMA_TRAIL, adjust=False).mean()
    return df

# ================== STRATEJİ ==================
def check():
    state = load_state()

    for s in ASSETS:
        try:
            df = get_data(s)
            if df is None:
                continue

            prev = df.iloc[-3]
            cross = df.iloc[-2]
            last = df.iloc[-1]

            # 🔒 HER ŞEY FLOAT
            prev_ema50 = float(prev["EMA50"])
            prev_ema100 = float(prev["EMA100"])
            cross_ema50 = float(cross["EMA50"])
            cross_ema100 = float(cross["EMA100"])
            last_ema50 = float(last["EMA50"])
            last_ema100 = float(last["EMA100"])

            price = float(last["Close"])
            ema200 = float(last["EMA200"])
            candle_date = str(last.name.date())

            # ========== ALIM ==========
            if not state[s]["in_position"]:
                if (
                    prev_ema50 < prev_ema100 and
                    cross_ema50 > cross_ema100 and
                    last_ema50 > last_ema100
                ):
                    state[s].update({
                        "in_position": True,
                        "entry_price": price,
                        "tp50_sent": False,
                        "tp70_sent": False,
                        "below_ema200_count": 0,
                        "last_checked_date": candle_date
                    })
                    msg = f"📈 {s} ALIM SİNYALİ\nFiyat: {price:.2f}"
                    send_telegram(msg)
                    log("ALIM SİNYALİ", s)
                    log_signal(s, "BUY", price)

            # ========== POZİSYON ==========
            else:
                entry = state[s]["entry_price"]

                if price >= entry * 1.5 and not state[s]["tp50_sent"]:
                    send_telegram(f"🔔 {s} +%50 KAR UYARISI\nFiyat: {price:.2f}")
                    state[s]["tp50_sent"] = True

                if price >= entry * 1.7 and not state[s]["tp70_sent"]:
                    send_telegram(f"🔔 {s} +%70 KAR UYARISI\nFiyat: {price:.2f}")
                    state[s]["tp70_sent"] = True

                if price >= entry * 2.0:
                    send_telegram(f"✅ {s} +%100 HEDEF\nPOZİSYON KAPATILDI")
                    log("POZİSYON +%100 KAPATILDI", s)
                    log_signal(s, "TP100_EXIT", price)
                    state[s]["in_position"] = False
                    state[s]["entry_price"] = None
                    continue

                # --- EMA200 GÜNLÜK STOP ---
                if state[s]["last_checked_date"] != candle_date:
                    if price < ema200:
                        state[s]["below_ema200_count"] += 1
                    else:
                        state[s]["below_ema200_count"] = 0
                    state[s]["last_checked_date"] = candle_date

                if state[s]["below_ema200_count"] >= 2:
                    send_telegram(
                        f"⛔ {s} STOPLOSS\nEMA200 altında 2 günlük kapanış\nFiyat: {price:.2f}"
                    )
                    log("EMA200 STOPLOSS", s)
                    log_signal(s, "EMA200_STOP", price)
                    state[s]["in_position"] = False
                    state[s]["entry_price"] = None

        except Exception as e:
            log(str(e), s)

    save_state(state)

# ================== MAIN ==================
if __name__ == "__main__":
    log("🚀 Bot başlatıldı")
    send_telegram("🚀 Bot başlatıldı")

    while True:
        check()
        time.sleep(CHECK_INTERVAL)
