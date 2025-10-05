#!/usr/bin/env python3
"""
Güncel bot.py
- 4H EMA_SHORT/EMA_LONG kesişimleriyle alım sinyali
- Günlük EMA100/EMA200 trend filtresi
- Atomic state kaydı (state.json)
- Log rotasyonu (log.txt -> log.txt.1 ...)
- Telegram bildirimleri (tablo formatında)
- Stop-loss bildirimi (tablo) ve pozisyon kapatma
- Basit telegram spam koruması (aynı mesajı kısa süre tekrar göndermez)

Kullanım:
1) .env dosyası oluşturup TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID doldurun
2) python bot.py ile çalıştırın

Not: Bu sürüm yfinance kullanıyor; ağ/indirme hatalarında retry yapar.
"""

import os
import time
import json
import math
import threading
from datetime import datetime, timezone
import pandas as pd
import yfinance as yf
import requests
from dotenv import load_dotenv

# --- Ortam Değişkenleri Yükle ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- İzlenecek varlıklar ---
ASSETS = [
    # Kripto paralar
    "BTC-USD",     # Bitcoin
    "ETH-USD",     # Ethereum
    "SOL-USD",     # Solana
    "AVAX-USD",    # Avalanche

    # Borsa İstanbul (Yahoo Finance'da .IS uzantılı)
    "TUPRS.IS",    # Tüpraş
    "DOAS.IS",     # Doğuş Otomotiv
    "THYAO.IS",    # Türk Hava Yolları
    "MAVI.IS",     # Mavi Giyim
    "ASELS.IS",    # Aselsan
    "KONTR.IS",    # Kontrolmatik
    "ARDYZ.IS",    # Ardyz Yazılım
    "MIATK.IS",    # Mia Teknoloji
    "MPARK.IS",    # MLP Sağlık
    "EKGYO.IS",    # Emlak Konut
    "LOGO.IS",     # Logo Yazılım
    "SMRTG.IS",    # Smart Güneş
    "GWIND.IS",    # Galata Wind
    "YEOTK.IS",    # Yeo Teknoloji
    "OYAKC.IS",    # OYAK Çimento
    "EREGL.IS",    # Ereğli Demir Çelik
    "DESA.IS",     # Desa Deri
    "BIMAS.IS",    # Bim
    "TUKAS.IS",    # Tukaş Gıda

    # ABD Hisseleri
    "GOOGL",       # Alphabet (Google)
    "NVDA",        # NVIDIA
    "META",        # Meta Platforms
    "INTC",        # Intel
    "AAPL",        # Apple
    "MSFT"         # Microsoft
]
EMA_SHORT = 100
EMA_LONG = 200
STOP_LOSS = 10      # %10 zarar
TAKE_PROFIT = 40    # %50 kar alım
UPGRADED_TP = 100   # Günlük EMA100 > EMA200 kesişim sonrası hedef

STATE_FILE = "state.json"
LOG_FILE = "log.txt"

# --- Log Rotasyon Ayarları ---
MAX_LOG_SIZE = 100 * 1024 * 1024  # 100 MB
BACKUP_COUNT = 50  # fazla eski log saklama sayısı
log_lock = threading.Lock()

# --- Telegram spam kontrol ---
# Aynı mesajı tekrar yollamamak için state içinde symbol->last_msg ve global->last_msg
MIN_TELEGRAM_INTERVAL = 60  # aynı mesajı en az 60s aralıkla gönder

# ----------------- Yardımcı Fonksiyonlar -----------------

def safe_download(symbol, interval, period, retries=3, pause=2, auto_adjust=True):
    """yfinance indirme işlemini retries ile sarar. Boş df veya exception durumunda tekrar dener."""
    for attempt in range(1, retries + 1):
        try:
            df = yf.download(symbol, interval=interval, period=period, progress=False, auto_adjust=auto_adjust)
            if not df.empty:
                return df
        except Exception as e:
            write_log(f"{symbol} download hatası (attempt {attempt}): {e}")
        time.sleep(pause)
    return pd.DataFrame()


def rotate_logs():
    """Log dosyası MAX_LOG_SIZE'ı aşınca döndürme işlemi yapar."""
    try:
        if not os.path.exists(LOG_FILE):
            return

        if os.path.getsize(LOG_FILE) >= MAX_LOG_SIZE:
            # eski logları kaydır
            for i in range(BACKUP_COUNT - 1, 0, -1):
                src = f"{LOG_FILE}.{i}"
                dst = f"{LOG_FILE}.{i+1}"
                if os.path.exists(src):
                    os.replace(src, dst)
            # Mevcut log.txt -> log.txt.1
            os.replace(LOG_FILE, f"{LOG_FILE}.1")
    except Exception as e:
        print(f"Log rotasyon hatası: {e}")


def write_log(msg: str, symbol: str = None, level: str = "INFO", notify: bool = False, state=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] [{level}] [{symbol}] {msg}" if symbol else f"[{now}] [{level}] {msg}"

    # Konsola yaz
    print(line)

    # Log dosyasına yaz
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

    # Telegram'a gönder (notify=True ise)
    if notify:
        try:
            send_telegram(line, state=state, symbol=symbol)
        except Exception as e:
            print(f"[Log->Telegram Hata] {e}")


def load_state():
    # Varsayılan state şeması
    default = {symbol: {"in_position": False, "entry_price": None, "take_profit": TAKE_PROFIT, "last_msg": None} for symbol in ASSETS}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            # eksik alanları tamamla
            for k, v in default.items():
                if k not in s:
                    s[k] = v
                else:
                    for field in v:
                        if field not in s[k]:
                            s[k][field] = v[field]
            if "global_last_msg" not in s:
                s["global_last_msg"] = None
            return s
        except Exception as e:
            write_log(f"State yüklenirken hata, varsayılan state oluşturuluyor: {e}", level="ERROR")
            return default
    return default


def save_state(state):
    tmp_file = STATE_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
        os.replace(tmp_file, STATE_FILE)
    except Exception as e:
        write_log(f"State kaydedilemedi: {e}", level="ERROR")


def get_ema(df, period):
    return df["Close"].ewm(span=period, adjust=False).mean()


# ----------------- Mesaj Formatlama -----------------

def format_signal_log(symbol, price, daily_ema100, daily_ema200, entry_price=None, tp=None):
    take_profit_pct = tp if tp is not None else TAKE_PROFIT
    stop_loss_price = (entry_price if entry_price else price) * (1 - STOP_LOSS / 100)
    potential_profit_price = (entry_price if entry_price else price) * (1 + take_profit_pct / 100)

    rel_ema100 = "ÜSTÜNDE ✅" if price > daily_ema100 else "ALTINDA ❌"
    rel_ema200 = "ÜSTÜNDE ✅" if price > daily_ema200 else "ALTINDA ❌"
    ema_relation = "EMA100 > EMA200 (YUKARIDA ✅)" if daily_ema100 > daily_ema200 else "EMA100 < EMA200 (AŞAĞIDA ❌)"

    upgraded_info = f"Günlük EMA100, EMA200'ü ÜSTÜNDE ✅ | Yeni TP %{UPGRADED_TP}" if daily_ema100 > daily_ema200 else "Günlük EMA100 henüz EMA200'ü yukarı kesmedi ❌"

    # Kar potansiyeli yüzdesi (entry bazlı)
    base = entry_price if entry_price else price
    profit_pct = take_profit_pct

    table = (
        f"\n📊 {symbol} ALIM SİNYALİ\n"
        f"4H EMA{EMA_SHORT} & EMA{EMA_LONG} Kesişimi Yukarı!\n\n"
        f"+-------------------+----------------+----------------------+\n"
        f"|   Gösterge        |   Değer        |   Durum              |\n"
        f"+-------------------+----------------+----------------------+\n"
        f"| Günlük EMA100     | {daily_ema100:,.2f} | Fiyat {rel_ema100:<12} |\n"
        f"| Günlük EMA200     | {daily_ema200:,.2f} | Fiyat {rel_ema200:<12} |\n"
        f"| Alış Fiyatı       | {price:,.2f}   |                    |\n"
        f"| Stop-Loss Seviyesi| {stop_loss_price:,.2f} | -%{STOP_LOSS:<15} |\n"
        f"| Take-Profit Hedef | {potential_profit_price:,.2f} | +%{profit_pct:<14} |\n"
        f"+-------------------+----------------+----------------------+\n"
        f"Trend Durumu: {ema_relation}\n"
        f"{upgraded_info}\n"
    )
    return table


def format_stoploss_log(symbol, price, entry, daily_ema100, daily_ema200):
    rel_ema100 = "ÜSTÜNDE ✅" if price > daily_ema100 else "ALTINDA ❌"
    rel_ema200 = "ÜSTÜNDE ✅" if price > daily_ema200 else "ALTINDA ❌"
    ema_relation = "EMA100 > EMA200 (YUKARIDA ✅)" if daily_ema100 > daily_ema200 else "EMA100 < EMA200 (AŞAĞIDA ❌)"

    stop_loss_price = entry * (1 - STOP_LOSS / 100)
    loss_pct = ((price - entry) / entry) * 100

    table = (
        f"\n⚠️ {symbol} STOP-LOSS TETİKLENDİ!\n\n"
        f"+-------------------+----------------+----------------------+\n"
        f"|   Gösterge        |   Değer        |   Durum              |\n"
        f"+-------------------+----------------+----------------------+\n"
        f"| Giriş Fiyatı      | {entry:,.2f}   |                      |\n"
        f"| Güncel Fiyat      | {price:,.2f}   |                      |\n"
        f"| Stop-Loss Seviyesi| {stop_loss_price:,.2f} | -%{STOP_LOSS:<15} |\n"
        f"| Günlük EMA100     | {daily_ema100:,.2f} | Fiyat {rel_ema100:<12} |\n"
        f"| Günlük EMA200     | {daily_ema200:,.2f} | Fiyat {rel_ema200:<12} |\n"
        f"+-------------------+----------------+----------------------+\n"
        f"Trend Durumu: {ema_relation}\n"
        f"Gerçekleşen Kayıp: %{loss_pct:.2f}\n"
    )
    return table


# ----------------- Telegram -----------------

def should_send(state, symbol, text):
    """Basit spam kontrolü: aynı mesajı kısa sürede yeniden gönderme.
    state içinde symbol->last_msg (metin) ve global_last_msg timestamp tutulur.
    """
    now_ts = int(time.time())
    symbol_last = state.get(symbol, {}).get("last_msg")
    global_last = state.get("global_last_msg")

    # Eğer tam olarak aynı mesaj son gönderilenle aynıysa  MIN_TELEGRAM_INTERVAL içinde engelle
    if symbol_last and isinstance(symbol_last, dict):
        if symbol_last.get("text") == text and now_ts - symbol_last.get("ts", 0) < MIN_TELEGRAM_INTERVAL:
            return False

    if global_last and isinstance(global_last, dict):
        if global_last.get("text") == text and now_ts - global_last.get("ts", 0) < 10:
            # global için daha kısa bekletme (aynı mesajın başka symbol'den gelmesi durumunda)
            return False

    # Gönderilebilir
    return True


def mark_sent(state, symbol, text):
    now_ts = int(time.time())
    if symbol not in state:
        state[symbol] = {}
    state[symbol]["last_msg"] = {"text": text, "ts": now_ts}
    state["global_last_msg"] = {"text": text, "ts": now_ts}
    save_state(state)


def send_telegram(msg: str, state=None, symbol=None):
    """Telegram’a bildirim gönderir (spam kontrolü entegre)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        write_log("⚠️ Telegram ayarları eksik, mesaj gönderilemedi.")
        return

    # Spam kontrolü aktif
    if state is not None and symbol is not None:
        if not should_send(state, symbol, msg):
            return
        mark_sent(state, symbol, msg)

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
        if r.status_code != 200:
            write_log(f"Telegram gönderim hatası: {r.text}")
    except Exception as e:
        write_log(f"Telegram bağlantı hatası: {e}")




# ----------------- Sinyal Kontrolü -----------------

def check_signals():
    state = load_state()

    for symbol in ASSETS:
        try:
            # 4H veriler
            df_4h = safe_download(symbol, interval="4h", period="720d", retries=3)
            if df_4h.empty or len(df_4h) < max(EMA_LONG, EMA_SHORT) + 2:
                write_log(f"{symbol} için yeterli 4h veri yok veya indirme başarısız.", symbol=symbol)
                continue

            df_4h["EMA_SHORT"] = get_ema(df_4h, EMA_SHORT)
            df_4h["EMA_LONG"] = get_ema(df_4h, EMA_LONG)

            last = df_4h.iloc[[-1]]
            prev = df_4h.iloc[[-2]]

            price = last["Close"].iloc[0].item()

            # Günlük veriler
            df_1d = safe_download(symbol, interval="1d", period="600d", retries=3)
            if df_1d.empty or len(df_1d) < 201:
                write_log(f"{symbol} için yeterli 1d veri yok veya indirme başarısız.", symbol=symbol)
                continue

            df_1d["EMA100"] = get_ema(df_1d, 100)
            df_1d["EMA200"] = get_ema(df_1d, 200)

            d_last = df_1d.iloc[[-1]]
            daily_ema100 = d_last["EMA100"].iloc[0]
            daily_ema200 = d_last["EMA200"].iloc[0]

            # --- Alım Sinyali ---
            if not state[symbol]["in_position"]:
                # 4H EMA kesişimi yukarı
                if prev["EMA_SHORT"].iloc[0] < prev["EMA_LONG"].iloc[0] and last["EMA_SHORT"].iloc[0] > last["EMA_LONG"].iloc[0]:
                    state[symbol]["in_position"] = True
                    state[symbol]["entry_price"] = price
                    state[symbol]["take_profit"] = TAKE_PROFIT

                    table_msg = format_signal_log(symbol, price, daily_ema100, daily_ema200, entry_price=price, tp=TAKE_PROFIT)
                    send_telegram(table_msg, state=state, symbol=symbol)
                    write_log(f"ALIM sinyali: {symbol} | Price: {price:.2f}", symbol=symbol)

            # --- Pozisyon Açıkken ---
            else:
                entry = state[symbol]["entry_price"]
                tp = state[symbol].get("take_profit", TAKE_PROFIT)

                # Take Profit
                if price >= entry * (1 + tp / 100):
                    msg = f"✅ {symbol} kar al hedefi (%{tp}) gerçekleşti! Fiyat: {price:.2f} | Giriş: {entry:.2f}"
                    send_telegram(msg, state=state, symbol=symbol)
                    write_log(msg, symbol=symbol)
                    state[symbol]["in_position"] = False
                    state[symbol]["entry_price"] = None
                    state[symbol]["take_profit"] = TAKE_PROFIT

                # Stop Loss
                elif price <= entry * (1 - STOP_LOSS / 100):
                    table_msg = format_stoploss_log(symbol, price, entry, daily_ema100, daily_ema200)
                    send_telegram(table_msg, state=state, symbol=symbol)
                    write_log(f"STOP LOSS: {symbol} | Price: {price:.2f} | Entry: {entry:.2f}", symbol=symbol)
                    state[symbol]["in_position"] = False
                    state[symbol]["entry_price"] = None
                    state[symbol]["take_profit"] = TAKE_PROFIT

                else:
                    # Günlük EMA kesişimiyle TP'yi yükselt
                    prev_ema100 = df_1d["EMA100"].iloc[-2]
                    prev_ema200 = df_1d["EMA200"].iloc[-2]
                    curr_ema100 = daily_ema100
                    curr_ema200 = daily_ema200

                    if prev_ema100 < prev_ema200 and curr_ema100 > curr_ema200:
                        if state[symbol].get("take_profit") != UPGRADED_TP:
                            state[symbol]["take_profit"] = UPGRADED_TP
                            msg = (f"🔄 {symbol} için GÜNCELLEME!\nGünlük EMA100, EMA200'ü yukarı kesti.\nYeni Take-Profit hedefi: %{UPGRADED_TP}")
                            send_telegram(msg, state=state, symbol=symbol)
                            write_log(msg, symbol=symbol)

        except Exception as e:
            write_log(f"{symbol} için hata: {e}", symbol=symbol, level="ERROR")

    save_state(state)


# ----------------- Ana Döngü -----------------
if __name__ == "__main__":
    write_log("🚀 Bot başlatıldı")
    send_telegram("🚀 Bot başlatıldı")
    # ilk state kaydetme (varsayılanları oluşturmak için)
    s = load_state()
    save_state(s)

    while True:
        check_signals()
        time.sleep(60)  # her dakika kontrol
