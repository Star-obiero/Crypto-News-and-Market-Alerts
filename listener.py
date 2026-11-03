"""
Market spike listener -> n8n webhook.

Watches Binance Futures WebSocket streams and POSTs an event to your n8n
Webhook node when something unusual happens:

  1. PRICE SPIKE      price moved more than X% within a rolling window
  2. LIQUIDATION      large liquidations piled up on one symbol in a window
  3. FUNDING EXTREME  funding rate is unusually high/negative

Everything is configured through environment variables (see README.md).
Detection logic lives in plain classes so it can be tested without a network.
"""

import asyncio
import json
import os
import time
from collections import defaultdict, deque

import httpx
import websockets

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")  # e.g. https://your-n8n/webhook/market-alert
N8N_WEBHOOK_SECRET = os.getenv("N8N_WEBHOOK_SECRET", "")  # sent as header X-Alert-Secret

WS_BASE = os.getenv("BINANCE_WS_BASE", "wss://fstream.binance.com")

# Comma separated, e.g. "BTCUSDT,ETHUSDT,SOLUSDT". Empty = every USDT perpetual (noisy!).
WATCHLIST = [s.strip().upper() for s in os.getenv("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT").split(",") if s.strip()]

# Price spike: alert if |change| >= SPIKE_PCT within SPIKE_WINDOW_SEC
SPIKE_PCT = float(os.getenv("SPIKE_PCT", "1.5"))
SPIKE_WINDOW_SEC = int(os.getenv("SPIKE_WINDOW_SEC", "300"))
SPIKE_COOLDOWN_SEC = int(os.getenv("SPIKE_COOLDOWN_SEC", "600"))

# Liquidations: alert if total notional (USD) on one symbol >= LIQ_USD within LIQ_WINDOW_SEC
LIQ_USD = float(os.getenv("LIQ_USD", "5000000"))
LIQ_WINDOW_SEC = int(os.getenv("LIQ_WINDOW_SEC", "60"))
LIQ_COOLDOWN_SEC = int(os.getenv("LIQ_COOLDOWN_SEC", "300"))

# Funding: alert if |rate| >= FUNDING_ABS (0.001 = 0.1% per funding interval)
FUNDING_ABS = float(os.getenv("FUNDING_ABS", "0.001"))
FUNDING_COOLDOWN_SEC = int(os.getenv("FUNDING_COOLDOWN_SEC", "3600"))


# Whale transfers via Whale Alert's WebSocket "Alerts API" (paid; leave the key empty to switch this off).
WHALE_API_KEY = os.getenv("WHALE_ALERT_API_KEY", "")
WHALE_WS_URL = os.getenv("WHALE_WS_URL", "wss://leviathan.whale-alert.io/ws")
WHALE_MIN_USD = max(float(os.getenv("WHALE_MIN_USD", "25000000")), 100000.0)  # Whale Alert's own floor is $100k
WHALE_SYMBOLS = [s.strip().lower() for s in os.getenv("WHALE_SYMBOLS", "btc,eth,usdt,usdc").split(",") if s.strip()]
WHALE_TX_TYPES = [s.strip().lower() for s in os.getenv("WHALE_TX_TYPES", "transfer,mint,burn").split(",") if s.strip()]


def _watched(symbol: str) -> bool:
    if WATCHLIST:
        return symbol in WATCHLIST
    return symbol.endswith("USDT")


# --------------------------------------------------------------------------
# Detection logic (pure, testable)
# --------------------------------------------------------------------------
class Cooldown:
    """Stops the same alert from firing repeatedly."""

    def __init__(self, seconds: int):
        self.seconds = seconds
        self._last = {}

    def ready(self, key, now: float) -> bool:
        last = self._last.get(key)
        if last is not None and now - last < self.seconds:
            return False
        self._last[key] = now
        return True


class SpikeDetector:
    def __init__(self, pct=SPIKE_PCT, window=SPIKE_WINDOW_SEC, cooldown=SPIKE_COOLDOWN_SEC):
        self.pct = pct
        self.window = window
        self.history = defaultdict(deque)  # symbol -> deque[(ts, price)]
        self.cooldown = Cooldown(cooldown)

    def feed(self, symbol: str, ts: float, price: float):
        h = self.history[symbol]
        h.append((ts, price))
        while h and ts - h[0][0] > self.window:
            h.popleft()
        if len(h) < 2:
            return None

        prices = [p for _, p in h]
        lo, hi = min(prices), max(prices)
        # Compare the current price to the window extreme that is furthest from it.
        up = (price - lo) / lo * 100
        down = (price - hi) / hi * 100
        change = up if abs(up) >= abs(down) else down

        if abs(change) >= self.pct and self.cooldown.ready((symbol, "up" if change > 0 else "down"), ts):
            return {
                "type": "price_spike",
                "symbol": symbol,
                "direction": "UP" if change > 0 else "DOWN",
                "change_pct": round(change, 2),
                "window_sec": self.window,
                "price": price,
                "severity": 4 if abs(change) >= self.pct * 2 else 3,
            }
        return None


class LiquidationWatcher:
    def __init__(self, usd=LIQ_USD, window=LIQ_WINDOW_SEC, cooldown=LIQ_COOLDOWN_SEC):
        self.usd = usd
        self.window = window
        self.events = defaultdict(deque)  # symbol -> deque[(ts, side, notional)]
        self.cooldown = Cooldown(cooldown)

    def feed(self, symbol: str, ts: float, side: str, notional: float):
        """side is the liquidation ORDER side: SELL = a long was liquidated, BUY = a short was."""
        q = self.events[symbol]
        q.append((ts, side, notional))
        while q and ts - q[0][0] > self.window:
            q.popleft()

        longs = sum(n for _, s, n in q if s == "SELL")
        shorts = sum(n for _, s, n in q if s == "BUY")
        total = longs + shorts
        if total >= self.usd and self.cooldown.ready(symbol, ts):
            return {
                "type": "liquidations",
                "symbol": symbol,
                "total_usd": round(total),
                "longs_liquidated_usd": round(longs),
                "shorts_liquidated_usd": round(shorts),
                "window_sec": self.window,
                "severity": 4 if total >= self.usd * 3 else 3,
            }
        return None


class FundingWatcher:
    def __init__(self, threshold=FUNDING_ABS, cooldown=FUNDING_COOLDOWN_SEC):
        self.threshold = threshold
        self.cooldown = Cooldown(cooldown)

    def feed(self, symbol: str, ts: float, rate: float):
        if abs(rate) >= self.threshold and self.cooldown.ready(symbol, ts):
            return {
                "type": "funding_extreme",
                "symbol": symbol,
                "funding_rate_pct": round(rate * 100, 4),
                "note": "Longs are paying shorts (crowded long)" if rate > 0 else "Shorts are paying longs (crowded short)",
                "severity": 2,
            }
        return None


# Owner names Whale Alert may report for exchange wallets. This list is mine and is not complete,
# so an exchange missing here will show up as an ordinary wallet.
EXCHANGE_NAMES = (
    "binance", "coinbase", "kraken", "okx", "okex", "bybit", "bitfinex", "huobi", "htx", "kucoin",
    "gemini", "bitstamp", "crypto.com", "bitget", "gate.io", "mexc", "upbit", "bithumb", "bitflyer",
    "deribit", "bitmex", "poloniex", "bitvavo", "coincheck", "robinhood",
)
STABLECOINS = {"usdt", "usdc", "dai", "fdusd", "usde", "pyusd", "tusd", "usdd"}


def _owner(x) -> str:
    if isinstance(x, dict):
        x = x.get("owner") or x.get("name") or ""
    x = (x or "").strip() if isinstance(x, str) else ""
    return x


def _exchange_of(name: str):
    n = name.lower()
    for ex in EXCHANGE_NAMES:
        if ex in n:
            return ex
    return None


class WhaleWatcher:
    """Turns Whale Alert 'alert' messages into events, sorted by what they usually mean."""

    def __init__(self, min_usd=WHALE_MIN_USD):
        self.min_usd = min_usd
        self._seen = deque(maxlen=1000)
        self._seen_set = set()

    def _first_time(self, key) -> bool:
        if key in self._seen_set:
            return False
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)
        return True

    def feed(self, msg):
        if not isinstance(msg, dict) or msg.get("type") != "alert":
            return None
        amounts = [a for a in (msg.get("amounts") or []) if isinstance(a, dict)]
        total = sum(float(a.get("value_usd") or 0) for a in amounts)
        if not amounts or total < self.min_usd:
            return None

        tx = msg.get("transaction")
        key = (tx.get("hash") if isinstance(tx, dict) else None) or f"{msg.get('timestamp')}|{msg.get('text')}"
        if not self._first_time(key):  # Whale Alert resends missed alerts after a reconnect
            return None

        main = max(amounts, key=lambda a: float(a.get("value_usd") or 0))
        symbol = (str(main.get("symbol") or "").upper()) or "?"
        amount = float(main.get("amount") or 0)
        tx_type = str(msg.get("transaction_type") or "transfer").lower()
        src, dst = _owner(msg.get("from")), _owner(msg.get("to"))
        src_ex, dst_ex = _exchange_of(src), _exchange_of(dst)
        stable = symbol.lower() in STABLECOINS

        if tx_type == "mint":
            flow = "mint"
            note = (f"New {symbol} supply created. Often read as fresh buying power entering the market."
                    if stable else f"New {symbol} created.")
        elif tx_type == "burn":
            flow, note = "burn", f"{symbol} supply destroyed."
        elif tx_type != "transfer":
            flow, note = tx_type, f"Transaction type: {tx_type}."
        elif src_ex and dst_ex:
            if src_ex == dst_ex:
                return None  # shuffling between one exchange's own wallets: noise
            flow, note = "exchange_to_exchange", "Moving between two exchanges."
        elif dst_ex:
            flow = "exchange_inflow"
            note = ("Stablecoins moving onto an exchange. Often read as buying power arriving."
                    if stable else
                    "Coins moving onto an exchange. Often read as possible selling pressure, but it can also be collateral or internal moves.")
        elif src_ex:
            flow = "exchange_outflow"
            note = ("Stablecoins leaving an exchange." if stable else
                    "Coins leaving an exchange. Often read as accumulation or moving to self-custody.")
        else:
            flow, note = "wallet_to_wallet", "Neither side is a labelled exchange, so direction can't be read from this."

        return {
            "type": "whale_transfer",
            "symbol": symbol,
            "amount": amount,
            "total_usd": round(total),
            "flow": flow,
            "from": src or "unknown",
            "to": dst or "unknown",
            "note": note,
            "severity": 4 if total >= self.min_usd * 4 else 3,
        }


# --------------------------------------------------------------------------
# Message handling
# --------------------------------------------------------------------------
spikes = SpikeDetector()
liqs = LiquidationWatcher()
funding = FundingWatcher()
whales = WhaleWatcher()


def handle_message(raw: str, now: float | None = None):
    """Take one raw WebSocket message, return a list of alert dicts (possibly empty)."""
    now = time.time() if now is None else now
    msg = json.loads(raw)
    data = msg.get("data", msg)  # combined streams wrap payload in {"stream":..., "data":...}
    out = []

    # Mark price stream: list of {"s": symbol, "p": markPrice, "r": fundingRate, ...}
    if isinstance(data, list):
        for item in data:
            sym = item.get("s")
            if not sym or not _watched(sym) or "p" not in item:
                continue
            a = spikes.feed(sym, now, float(item["p"]))
            if a:
                out.append(a)
            if item.get("r") not in (None, ""):
                a = funding.feed(sym, now, float(item["r"]))
                if a:
                    out.append(a)
        return out

    # Liquidation stream: {"e": "forceOrder", "o": {...}}
    if data.get("e") == "forceOrder":
        o = data["o"]
        sym = o["s"]
        if _watched(sym):
            price = float(o.get("ap") or o["p"])  # average fill price, fallback to order price
            qty = float(o["z"] if float(o.get("z", 0)) else o["q"])
            a = liqs.feed(sym, now, o["S"], price * qty)
            if a:
                out.append(a)
    return out


# --------------------------------------------------------------------------
# Delivery to n8n
# --------------------------------------------------------------------------
async def send_to_n8n(client: httpx.AsyncClient, alert: dict):
    alert["detected_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    alert["source"] = "binance-futures-ws"
    print("ALERT", json.dumps(alert))
    if not N8N_WEBHOOK_URL:
        return
    headers = {"X-Alert-Secret": N8N_WEBHOOK_SECRET} if N8N_WEBHOOK_SECRET else {}
    for attempt in range(3):
        try:
            r = await client.post(N8N_WEBHOOK_URL, json=alert, headers=headers, timeout=10)
            r.raise_for_status()
            return
        except Exception as e:  # noqa: BLE001
            print(f"webhook attempt {attempt + 1} failed: {e}")
            await asyncio.sleep(1 + attempt)


# --------------------------------------------------------------------------
# Main loop with auto-reconnect
# --------------------------------------------------------------------------
async def run():
    # markPrice@1s gives price + funding for all symbols; forceOrder gives liquidations.
    url = f"{WS_BASE}/stream?streams=!markPrice@arr@1s/!forceOrder@arr"
    async with httpx.AsyncClient() as client:
        backoff = 1
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    print("connected; watching:", WATCHLIST or "all USDT pairs")
                    backoff = 1
                    async for raw in ws:
                        for alert in handle_message(raw):
                            asyncio.create_task(send_to_n8n(client, alert))
            except Exception as e:  # noqa: BLE001
                print(f"connection lost: {e}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


async def run_whale():
    """Whale Alert WebSocket. Needs a paid Alerts API key (WHALE_ALERT_API_KEY)."""
    url = f"{WHALE_WS_URL}?api_key={WHALE_API_KEY}"  # never print this URL: it contains your key
    sub = {
        "type": "subscribe_alerts",
        "id": "crypto-news-alerts",  # same id after a short disconnect = Whale Alert resends what you missed
        "tx_types": WHALE_TX_TYPES,
        "min_value_usd": WHALE_MIN_USD,
    }
    if WHALE_SYMBOLS:
        sub["symbols"] = WHALE_SYMBOLS
    async with httpx.AsyncClient() as client:
        backoff = 1
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps(sub))
                    print(f"whale alerts connected; minimum ${WHALE_MIN_USD:,.0f}")
                    backoff = 1
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        kind = msg.get("type") if isinstance(msg, dict) else None
                        if kind == "subscribed_alerts":
                            print("whale subscription confirmed")
                            continue
                        if kind != "alert":
                            print("whale feed message:", str(msg)[:200])
                            continue
                        alert = whales.feed(msg)
                        if alert:
                            asyncio.create_task(send_to_n8n(client, alert))
            except Exception as e:  # noqa: BLE001
                print(f"whale connection lost: {type(e).__name__}; reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


async def main():
    jobs = [run()]
    if WHALE_API_KEY:
        jobs.append(run_whale())
    else:
        print("whale alerts off (set WHALE_ALERT_API_KEY to turn them on)")
    await asyncio.gather(*jobs)


if __name__ == "__main__":
    asyncio.run(main())
