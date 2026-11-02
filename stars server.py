#!/usr/bin/env python3
"""
Velora — оплата Telegram Stars. Только стандартная библиотека Python 3.9+ (ничего ставить не нужно).

БЫСТРЫЙ ЗАПУСК (бот сам забирает платежи, домен и webhook не нужны):
    BOT_TOKEN=123456:ABC... python3 stars_server.py --poll

Если у бота уже есть webhook или свой сервер — запускайте без --poll и задайте PUBLIC_URL:
    BOT_TOKEN=... PUBLIC_URL=https://api.example.com python3 stars_server.py

ВАЖНО: адрес этого сервера должен быть тем же, откуда открыто приложение, либо задайте в index.html
перед скриптами:   <script>window.VELORA_STARS_API="https://api.example.com"</script>
Так «Оплата ещё не подключена» от старого сервера больше не попадается: звёзды идут на /api/stars/invoice.

Окружение: BOT_TOKEN (обяз.), PORT (8080), DB_PATH, ALLOW_ORIGIN (*), WEBHOOK_SECRET, TG_API (для тестов)
Маршруты:  POST /api/stars/invoice  {plan}  ->  {"invoice": "https://t.me/$..."}      (также POST /api/pay с method=stars)
           GET  /api/stars/me               ->  {"active":bool,"plan":..,"sub_until":unix}
           POST /tg/webhook                 <-  апдейты Telegram (webhook-режим)
Встроить в Flask/FastAPI: используйте create_invoice(uid, plan), handle_update(update), get_sub(uid), check_init_data(str).
"""
import hashlib, hmac, json, os, sqlite3, sys, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "velora_subs.db")
ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")
TG_API = os.environ.get("TG_API", "https://api.telegram.org")
SECRET = os.environ.get("WEBHOOK_SECRET") or hashlib.sha256(("wh" + BOT_TOKEN).encode()).hexdigest()[:32]
_lock = threading.Lock()

# 1 ₽ = 1 ⭐
PLANS = {
    "plus":      {"stars": 150, "days": 30, "title": "Velora Plus",  "desc": "600K токенов в день, фото без лимита, голос и проекты"},
    "pro":       {"stars": 300, "days": 30, "title": "Velora Pro",   "desc": "1,5M токенов в день, видео из кода, глубокое исследование"},
    "ultra":     {"stars": 500, "days": 30, "title": "Velora Ultra", "desc": "3M токенов в день, всё без ограничений"},
    "pro_trial": {"stars": 10,  "days": 3,  "title": "Velora Pro · 3 дня", "desc": "Пробный Pro на 3 дня"},
}

def _db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("create table if not exists subs(user_id integer primary key, plan text, until integer)")
    c.execute("create table if not exists pays(charge_id text primary key, user_id integer, plan text, stars integer, ts integer)")
    return c

def get_sub(user_id):
    with _lock, _db() as c:
        r = c.execute("select plan, until from subs where user_id=?", (user_id,)).fetchone()
    if r and r[1] > time.time():
        return {"active": True, "plan": r[0], "sub_until": int(r[1])}
    return {"active": False, "plan": "free", "sub_until": 0}

def grant(user_id, plan, charge_id="", stars=0):
    p = PLANS[plan]
    with _lock, _db() as c:
        if charge_id and c.execute("select 1 from pays where charge_id=?", (charge_id,)).fetchone():
            return False  # повторная доставка апдейта
        r = c.execute("select until from subs where user_id=?", (user_id,)).fetchone()
        base = max(time.time(), r[0] if r else 0)
        c.execute("insert or replace into subs values(?,?,?)", (user_id, "pro" if plan == "pro_trial" else plan, int(base + p["days"] * 86400)))
        if charge_id:
            c.execute("insert into pays values(?,?,?,?,?)", (charge_id, user_id, plan, stars, int(time.time())))
    return True

def tg(method, **params):
    req = urllib.request.Request(f"{TG_API}/bot{BOT_TOKEN}/{method}", json.dumps(params).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=35) as r:
            j = json.load(r)
    except urllib.error.HTTPError as e:
        try: j = json.load(e)
        except Exception: j = {"ok": False, "description": f"HTTP {e.code}"}
    if not j.get("ok"):
        raise RuntimeError(j.get("description", "telegram error"))
    return j["result"]

def check_init_data(init_data):
    """Проверка подписи Telegram WebApp initData -> id пользователя или None."""
    try:
        d = dict(parse_qsl(init_data, keep_blank_values=True))
        h = d.pop("hash", "")
        chk = "\n".join(f"{k}={d[k]}" for k in sorted(d))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(d.get("auth_date", 0)) > 86400 * 3:
            return None
        return int(json.loads(d["user"])["id"])
    except Exception:
        return None

def create_invoice(uid, plan):
    p = PLANS[plan if plan in PLANS else "plus"]
    plan = plan if plan in PLANS else "plus"
    return tg("createInvoiceLink", title=p["title"], description=p["desc"],
              payload=json.dumps({"u": uid, "p": plan}), provider_token="", currency="XTR",
              prices=[{"label": p["title"], "amount": p["stars"]}])

def handle_update(u):
    if "pre_checkout_query" in u:
        q = u["pre_checkout_query"]
        try:
            pl = json.loads(q["invoice_payload"])
            ok = pl["p"] in PLANS and q["currency"] == "XTR" and q["total_amount"] == PLANS[pl["p"]]["stars"]
        except Exception:
            ok = False
        args = {"pre_checkout_query_id": q["id"], "ok": ok}
        if not ok: args["error_message"] = "Не удалось проверить счёт, попробуйте ещё раз"
        tg("answerPreCheckoutQuery", **args)
    m = u.get("message") or {}
    sp = m.get("successful_payment")
    if sp and sp.get("currency") == "XTR":
        pl = json.loads(sp["invoice_payload"])
        if grant(int(pl["u"]), pl["p"], sp.get("telegram_payment_charge_id", ""), sp.get("total_amount", 0)):
            try: tg("sendMessage", chat_id=m["chat"]["id"], text="Оплата прошла ⭐ Подписка подключена. Вернитесь в приложение.")
            except Exception: pass

# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    server_version = "VeloraStars/1"
    def log_message(self, fmt, *a): print("%s %s" % (self.address_string(), fmt % a))
    def _send(self, code, data=None, raw=None):
        b = raw if raw is not None else json.dumps(data or {}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _uid(self):
        if os.environ.get("DEV_NO_AUTH") == "1" and self.headers.get("X-Dev-User"):
            return int(self.headers["X-Dev-User"])
        raw = self.headers.get("X-Init-Data") or self.headers.get("X-Telegram-Init-Data") or ""
        return check_init_data(raw) if raw else None
    def _body(self):
        try: return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception: return {}
    def do_OPTIONS(self): self._send(204, raw=b"")
    def do_GET(self):
        if self.path.split("?")[0] in ("/api/stars/me", "/api/stars/status"):
            uid = self._uid()
            return self._send(200, get_sub(uid)) if uid else self._send(401, {"error": "Откройте приложение через бота в Telegram"})
        if self.path == "/health": return self._send(200, {"ok": True, "stars": bool(BOT_TOKEN)})
        self._send(404, {"error": "not found"})
    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/tg/webhook":
            if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
                return self._send(403, {"error": "forbidden"})
            try: handle_update(self._body())
            except Exception as e: print("update error:", e, file=sys.stderr)
            return self._send(200, {"ok": True})
        if path in ("/api/stars/invoice", "/api/pay"):
            b = self._body()
            if path == "/api/pay" and b.get("method", "stars") != "stars":
                return self._send(501, {"error": "Оплата картой ещё не подключена, выберите Telegram Stars"})
            uid = self._uid()
            if not uid: return self._send(401, {"error": "Откройте приложение через бота в Telegram"})
            if not BOT_TOKEN: return self._send(503, {"error": "На сервере не задан BOT_TOKEN"})
            try:
                link = create_invoice(uid, b.get("plan"))
            except Exception as e:
                return self._send(502, {"error": f"Telegram не создал счёт: {e}"})
            return self._send(200, {"invoice": link, "plan": b.get("plan") if b.get("plan") in PLANS else "plus"})
        self._send(404, {"error": "not found"})

def poll_loop():
    try: tg("deleteWebhook")
    except Exception as e: print("deleteWebhook:", e)
    off = 0
    print("получаю платежи через getUpdates…")
    while True:
        try:
            for u in tg("getUpdates", offset=off, timeout=25, allowed_updates=["pre_checkout_query", "message"]):
                off = u["update_id"] + 1
                try: handle_update(u)
                except Exception as e: print("update error:", e, file=sys.stderr)
        except Exception as e:
            print("poll error:", e, file=sys.stderr); time.sleep(3)

def main():
    if not BOT_TOKEN: sys.exit("Задайте BOT_TOKEN, например: BOT_TOKEN=123:abc python3 stars_server.py --poll")
    if "--poll" in sys.argv:
        threading.Thread(target=poll_loop, daemon=True).start()
    elif os.environ.get("PUBLIC_URL"):
        tg("setWebhook", url=os.environ["PUBLIC_URL"].rstrip("/") + "/tg/webhook", secret_token=SECRET, allowed_updates=["pre_checkout_query", "message"])
        print("webhook установлен")
    port = int(os.environ.get("PORT", 8080))
    print(f"Velora Stars слушает порт {port}")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()

if __name__ == "__main__":
    main()
