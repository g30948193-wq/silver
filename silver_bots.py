import os, json, requests, feedparser
import pandas as pd
import yfinance as yf

TOKEN = os.environ["TG_TOKEN"]
CHAT = os.environ["TG_CHAT"]
FORCE = os.getenv("FORCE") == "true"
TICKER = "SI=F"
STATE_FILE = "state.json"


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(s):
    s["seen"] = s.get("seen", [])[-200:]
    json.dump(s, open(STATE_FILE, "w"))


def send(text):
    print(text, "\n" + "-" * 40)
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": CHAT, "text": text}, timeout=15)
    except Exception as e:
        print("Telegram error:", e)


def load(interval, period):
    df = yf.download(TICKER, interval=interval, period=period,
                     progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n).mean()
    return 100 - 100 / (1 + up / dn)


def atr(df, n=14):
    tr = pd.concat([df.High - df.Low,
                    (df.High - df.Close.shift()).abs(),
                    (df.Low - df.Close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n).mean()


def clamp(x):
    return max(-1.0, min(1.0, float(x)))


# БОТ 1: график
def bot_chart():
    df = load("1h", "60d")
    c = df.Close
    last = c.iloc[-1]
    s20, s50 = c.rolling(20).mean().iloc[-1], c.rolling(50).mean().iloc[-1]
    r = rsi(c).iloc[-1]
    hi, lo = df.High.tail(120).max(), df.Low.tail(120).min()
    score = 0
    if last > s20 > s50:
        score += 0.5
    elif last < s20 < s50:
        score -= 0.5
    score += (50 - r) / 100 if (r > 70 or r < 30) else (r - 50) / 100
    note = (f"Цена {last:.2f} | SMA20 {s20:.2f} | SMA50 {s50:.2f} | RSI {r:.0f}\n"
            f"Поддержка ~{lo:.2f}, сопротивление ~{hi:.2f}")
    return {"name": "График", "score": clamp(score), "note": note}


# БОТ 2: новости
BULL = ["surge", "rally", "rise", "gain", "jump", "soar", "record", "safe-haven",
        "rate cut", "weak dollar", "demand", "deficit", "higher"]
BEAR = ["fall", "drop", "slump", "plunge", "decline", "loss", "strong dollar",
        "rate hike", "hawkish", "lower", "sell-off", "selloff", "outflow"]


def bot_news(state):
    url = "https://news.google.com/rss/search?q=silver+price+XAG&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(url)
    seen = state.setdefault("seen", [])
    bull = bear = 0
    fresh = []
    for e in feed.entries[:30]:
        t = e.title.lower()
        bull += sum(k in t for k in BULL)
        bear += sum(k in t for k in BEAR)
        if e.link not in seen:
            seen.append(e.link)
            fresh.append(e.title)
    score = (bull - bear) / max(1, bull + bear)
    note = f"Бычьих слов: {bull}, медвежьих: {bear}"
    if fresh:
        note += "\nСвежее:\n- " + "\n- ".join(fresh[:3])
    return {"name": "Новости", "score": clamp(score), "note": note}


# БОТ 3: волны Эллиотта
def pivots(df, k=4):
    hi, lo = df.High.values, df.Low.values
    raw = []
    for i in range(k, len(df) - k):
        if hi[i] == hi[i - k:i + k + 1].max():
            raw.append((i, hi[i], "H"))
        elif lo[i] == lo[i - k:i + k + 1].min():
            raw.append((i, lo[i], "L"))
    res = []
    for p in raw:
        if res and res[-1][2] == p[2]:
            if (p[2] == "H" and p[1] > res[-1][1]) or (p[2] == "L" and p[1] < res[-1][1]):
                res[-1] = p
        else:
            res.append(p)
    return res


def check_impulse(v):
    if len(v) < 5:
        return None
    p0, p1, p2, p3, p4 = v[:5]
    if p2 > p0 and (p3 - p2) > (p1 - p0) and p4 > p1 and p4 < p3:
        done = len(v) >= 6 and v[5] > p3
        return {"done": done, "target5": p4 + (p1 - p0),
                "inval": p4 if not done else None}
    return None


def bot_elliott():
    df = load("1h", "180d")
    df = df.resample("4h").agg({"Open": "first", "High": "max", "Low": "min",
                                "Close": "last"}).dropna()
    pv = pivots(df, k=4)
    if len(pv) < 5:
        return {"name": "Эллиотт", "score": 0, "note": "Нет чёткой структуры"}
    for n in (6, 5):
        seg = pv[-n:]
        if len(seg) < 5:
            continue
        prices = [p[1] for p in seg]
        if seg[0][2] == "L":
            r = check_impulse(prices)
            side = 1
        else:
            r = check_impulse([-x for x in prices])
            side = -1
        if r:
            if r["done"]:
                return {"name": "Эллиотт", "score": -0.5 * side,
                        "note": "Похоже, 5 волн завершены. Ожидается коррекция."}
            t5 = r["target5"] * side
            return {"name": "Эллиотт", "score": 0.7 * side,
                    "note": f"Идёт волна 5, цель ~{t5:.2f}, отмена при {r['inval'] * side:.2f}"}
    return {"name": "Эллиотт", "score": 0,
            "note": "Импульс не найден (возможна коррекция)"}


# БОТ 4: свечи внутри дня
def eval_candle(df, k, a, ema):
    o, h, l, c = [df[x].iloc[k] for x in ("Open", "High", "Low", "Close")]
    po, pc = df.Open.iloc[k - 1], df.Close.iloc[k - 1]
    body = abs(c - o)
    up_sh, lo_sh = h - max(o, c), min(o, c) - l
    sig, score = [], 0
    if pc < po and c > o and c >= po and o <= pc:
        sig.append("Бычье поглощение"); score += 1
    if pc > po and c < o and c <= po and o >= pc:
        sig.append("Медвежье поглощение"); score -= 1
    if body > 0 and lo_sh > 2 * body and up_sh < body:
        sig.append("Молот"); score += 0.7
    if body > 0 and up_sh > 2 * body and lo_sh < body:
        sig.append("Падающая звезда"); score -= 0.7
    trend = 1 if c > ema.iloc[k] else -1
    if score != 0 and trend == (1 if score > 0 else -1):
        score *= 1.3
    return sig, score, c, a.iloc[k], trend, ema.iloc[k]


def bot_intraday(state):
    df = load("5m", "5d")
    if len(df) < 40:
        return {"name": "Свечи", "score": 0, "note": "Мало данных"}
    a = atr(df)
    ema = df.Close.ewm(span=20).mean()
    sig, score, c, av, trend, e = eval_candle(df, -2, a, ema)
    note = f"Тренд 5м: {'вверх' if trend == 1 else 'вниз'} (EMA20 {e:.2f})"
    if sig:
        note += "\nПаттерн: " + ", ".join(sig)
    # ищем сигнал на последних 3 закрытых свечах
    for k in (-2, -3, -4):
        sg, sc, c2, a2, tr2, _ = eval_candle(df, k, a, ema)
        ts = str(df.index[k])
        if abs(sc) >= 0.7 and state.get("last_ts") != ts:
            side = 1 if sc > 0 else -1
            stop, tp = c2 - side * 1.5 * a2, c2 + side * 3 * a2
            send(f"⚡ ВХОД ВНУТРИ ДНЯ (серебро)\n{', '.join(sg)}\n"
                 f"{'LONG' if side == 1 else 'SHORT'} от {c2:.2f}, "
                 f"стоп {stop:.2f}, цель {tp:.2f}\nСвеча: {ts}")
            state["last_ts"] = ts
            break
    return {"name": "Свечи", "score": clamp(score), "note": note}


# БОТ 5: вердикт
WEIGHTS = {"График": 0.25, "Новости": 0.15, "Эллиотт": 0.25, "Свечи": 0.35}


def bot_verdict(results):
    total = sum(WEIGHTS[r["name"]] * r["score"] for r in results)
    if total > 0.35:
        v = "ВВЕРХ (LONG) 📈"
    elif total < -0.35:
        v = "ВНИЗ (SHORT) 📉"
    else:
        v = "БОКОВИК / ждать ➖"
    lines = [f"{r['name']}: {r['score']:+.2f}\n  {r['note']}" for r in results]
    return v, (f"🥈 СЕРЕБРО — вердикт: {v}\nИтоговый балл: {total:+.2f}\n\n"
               + "\n\n".join(lines))


def main():
    state = load_state()
    bots = [("График", bot_chart), ("Новости", lambda: bot_news(state)),
            ("Эллиотт", bot_elliott), ("Свечи", lambda: bot_intraday(state))]
    results = []
    for name, fn in bots:
        try:
            results.append(fn())
        except Exception as e:
            print(name, "ошибка:", e)
            results.append({"name": name, "score": 0, "note": f"ошибка: {e}"})
    verdict, text = bot_verdict(results)
    if FORCE or verdict != state.get("verdict"):
        send(text)
    else:
        print(text)
    state["verdict"] = verdict
    save_state(state)


main()
