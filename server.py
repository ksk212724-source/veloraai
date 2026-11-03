#!/usr/bin/env python3
"""
Velora — полный сервер для мини-приложения (только стандартная библиотека Python 3.9+, ничего ставить не нужно).

Что умеет
  • отдаёт index.html (приложение) и принимает все запросы приложения: /api/me, /api/unlock, /api/chat,
    /api/generate + /api/job (картинки), /api/tts, /api/stt, /api/send, /media/*
  • оплата: ЮKassa (карта, СБП) и Telegram Stars; подписки хранятся в SQLite
  • ответы нейросети: любой OpenAI-совместимый сервис (OpenRouter, Groq, DeepSeek, OpenAI, Pollinations …)

Запуск
    1) положите рядом velora_server.py и index.html (ваш velora_17_….html переименуйте в index.html)
    2) задайте переменные окружения (ниже) и запустите:   python3 velora_server.py
    3) откройте https://ВАШ-ДОМЕН/ в Telegram как Web App бота (BotFather → /newapp или кнопка меню)

ОБЯЗАТЕЛЬНЫЕ переменные
    BOT_TOKEN              токен бота от @BotFather
    LLM_API_KEY            ключ нейросети (см. LLM_* ниже)
    PUBLIC_URL             https-адрес сервера, например https://velora.example.com

ЮKassa (shopId из вашего письма: 1513722)
    YOOKASSA_SHOP_ID       по умолчанию 1513722
    YOOKASSA_SECRET        секретный ключ: личный кабинет ЮKassa → Интеграция → Ключи API → «Выпустить ключ».
                           Ключ показывают один раз. Никому его не отправляйте, только в переменную окружения.
    В личном кабинете: Интеграция → HTTP-уведомления → адрес  PUBLIC_URL/yookassa/webhook  → событие payment.succeeded.
    (Даже без уведомлений подписка включится: приложение само опрашивает статус платежа.)
    YK_RECEIPT_EMAIL       нужен только если магазин на ИП/ООО с онлайн-кассой (чек 54-ФЗ): e-mail для чеков.
                           Самозанятым чек не нужен, оставьте пустым.

Нейросеть (любая совместимая с OpenAI):
    LLM_BASE_URL   по умолчанию https://text.pollinations.ai/openai
                   Groq:       https://api.groq.com/openai/v1        модель llama-3.3-70b-versatile
                   OpenRouter: https://openrouter.ai/api/v1          модель, например, deepseek/deepseek-chat-v3.1
                   DeepSeek:   https://api.deepseek.com/v1           модель deepseek-chat
                   OpenAI:     https://api.openai.com/v1             модель gpt-4o-mini
    LLM_MODEL      по умолчанию openai   (для Pollinations)
    LLM_MAX_TOKENS по умолчанию 8000     (игры и анимации длинные, не ставьте меньше 6000)
  Картинки:  IMG_PROVIDER=pollinations (по умолчанию) или openai;  IMG_BASE_URL, IMG_API_KEY, IMG_MODEL
  Голос:     TTS_BASE_URL / TTS_API_KEY / TTS_MODEL (озвучка), STT_BASE_URL / STT_API_KEY / STT_MODEL (распознавание).
             Не заданы — приложение само использует озвучку браузера, а голосовой ввод сообщит, что недоступен.

Остальное
    BETA_PASSWORD    пароль закрытой беты (пусто = без пароля)
    PRICE_PLUS_RUB / PRICE_PRO_RUB / PRICE_ULTRA_RUB / PRICE_TRIAL_RUB     цены в рублях (по умолчанию везде 1)
    PRICE_PLUS_STARS / PRICE_PRO_STARS / PRICE_ULTRA_STARS / PRICE_TRIAL_STARS   цены в звёздах (по умолчанию везде 1)
    BETA=1           лимиты сняты (по умолчанию 1); BETA=0 — бесплатно 10 сообщений за 3 часа
    PORT=8080, DB_PATH=velora.db, MEDIA_DIR=media, INDEX_PATH=index.html, ALLOW_ORIGIN=*

За HTTPS-прокси (nginx/Caddy) поставьте таймаут ответа не меньше 180 секунд: игры пишутся долго.
"""
import base64, hashlib, hmac, json, mimetypes, os, re, secrets, sqlite3, sys, threading, time, urllib.error, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, quote

E = os.environ.get
BOT_TOKEN = E("BOT_TOKEN", "")
PUBLIC_URL = (E("PUBLIC_URL", "") or E("RENDER_EXTERNAL_URL", "") or "").rstrip("/")
TG_API = E("TG_API", "https://api.telegram.org")
YK_API = E("YK_API", "https://api.yookassa.ru/v3")
YK_SHOP = E("YOOKASSA_SHOP_ID", "1513722")
YK_SECRET = E("YOOKASSA_SECRET", "")
YK_EMAIL = E("YK_RECEIPT_EMAIL", "")
LLM_BASE = (E("LLM_BASE_URL", "https://text.pollinations.ai/openai")).rstrip("/")
LLM_KEY = E("LLM_API_KEY", "")
LLM_MODEL = E("LLM_MODEL", "openai")
LLM_MAX = int(E("LLM_MAX_TOKENS", "8000"))
IMG_PROVIDER = E("IMG_PROVIDER", "pollinations")
IMG_BASE = (E("IMG_BASE_URL", "https://image.pollinations.ai")).rstrip("/")
IMG_KEY = E("IMG_API_KEY", "") or LLM_KEY
IMG_MODEL = E("IMG_MODEL", "flux" if IMG_PROVIDER == "pollinations" else "gpt-image-1")
TTS_BASE, TTS_KEY, TTS_MODEL = (E("TTS_BASE_URL", "")).rstrip("/"), E("TTS_API_KEY", ""), E("TTS_MODEL", "tts-1")
STT_BASE, STT_KEY, STT_MODEL = (E("STT_BASE_URL", "")).rstrip("/"), E("STT_API_KEY", ""), E("STT_MODEL", "whisper-1")
BETA = E("BETA", "1") == "1"
PASSWORD = E("BETA_PASSWORD", "")
ORIGIN = E("ALLOW_ORIGIN", "*")
DB_PATH = E("DB_PATH", "velora.db")
MEDIA = E("MEDIA_DIR", "media")
INDEX = E("INDEX_PATH", "index.html")
SECRET = E("WEBHOOK_SECRET") or hashlib.sha256(("wh" + BOT_TOKEN).encode()).hexdigest()[:32]
FREE_MSGS, FREE_WINDOW = 10, 3 * 3600
os.makedirs(MEDIA, exist_ok=True)
_lock = threading.Lock()

def _price(name, default=1):
    try: return max(1, int(E(name, str(default))))
    except ValueError: return default

PLANS = {
    "plus":      {"days": 30, "rub": _price("PRICE_PLUS_RUB"),  "stars": _price("PRICE_PLUS_STARS"),  "title": "Velora Plus",  "desc": "600K токенов в день, фото без лимита, голос и проекты"},
    "pro":       {"days": 30, "rub": _price("PRICE_PRO_RUB"),   "stars": _price("PRICE_PRO_STARS"),   "title": "Velora Pro",   "desc": "1,5M токенов в день, видео из кода, глубокое исследование"},
    "ultra":     {"days": 30, "rub": _price("PRICE_ULTRA_RUB"), "stars": _price("PRICE_ULTRA_STARS"), "title": "Velora Ultra", "desc": "3M токенов в день, всё без ограничений"},
    "pro_trial": {"days": 3,  "rub": _price("PRICE_TRIAL_RUB"), "stars": _price("PRICE_TRIAL_STARS"), "title": "Velora Pro · 3 дня", "desc": "Пробный Pro на 3 дня"},
}

# ───────────────────────── база ─────────────────────────
def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("create table if not exists subs(user_id integer primary key, plan text, until integer)")
    c.execute("create table if not exists pays(charge_id text primary key, user_id integer, plan text, amount integer, ts integer)")
    c.execute("create table if not exists pending(pid text primary key, user_id integer, plan text, ts integer, done integer default 0)")
    c.execute("create table if not exists usage(user_id text primary key, win integer, n integer)")
    return c

def get_sub(uid):
    with _lock, db() as c:
        r = c.execute("select plan, until from subs where user_id=?", (uid,)).fetchone()
    return {"active": True, "plan": r[0], "sub_until": int(r[1])} if r and r[1] > time.time() else {"active": False, "plan": "free", "sub_until": 0}

def grant(uid, plan, charge_id, amount):
    p = PLANS[plan]
    with _lock, db() as c:
        if c.execute("select 1 from pays where charge_id=?", (charge_id,)).fetchone():
            return False
        r = c.execute("select until from subs where user_id=?", (uid,)).fetchone()
        base = max(time.time(), r[0] if r else 0)
        c.execute("insert or replace into subs values(?,?,?)", (uid, "pro" if plan == "pro_trial" else plan, int(base + p["days"] * 86400)))
        c.execute("insert into pays values(?,?,?,?,?)", (charge_id, uid, plan, amount, int(time.time())))
    return True

def usage(key, bump=0):
    now = int(time.time())
    with _lock, db() as c:
        r = c.execute("select win, n from usage where user_id=?", (key,)).fetchone()
        win, n = (r if r and now - r[0] < FREE_WINDOW else (now, 0))
        if bump: n += bump
        c.execute("insert or replace into usage values(?,?,?)", (key, win, n))
    return n, win + FREE_WINDOW

# ───────────────────────── HTTP-клиент ─────────────────────────
def http(url, data=None, headers=None, method=None, timeout=60, raw=False):
    body = None if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
    h = {"User-Agent": "VeloraServer/1.0"}
    if isinstance(data, (dict, list)): h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, body, h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read()
            return r.status, (out if raw else (json.loads(out) if out else {}))
    except urllib.error.HTTPError as e:
        out = e.read()
        try: return e.code, (out if raw else json.loads(out))
        except Exception: return e.code, (out if raw else {"error": out[:300].decode("utf-8", "ignore")})

def multipart(fields, files):
    b = uuid.uuid4().hex; out = b""
    for k, v in fields.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (name, data, ctype) in files.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n"
    return out + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"

def tg(method, **params):
    st, j = http(f"{TG_API}/bot{BOT_TOKEN}/{method}", params, timeout=35)
    if not (isinstance(j, dict) and j.get("ok")): raise RuntimeError((j or {}).get("description", f"telegram HTTP {st}") if isinstance(j, dict) else "telegram error")
    return j["result"]

def check_init_data(init_data):
    try:
        d = dict(parse_qsl(init_data, keep_blank_values=True)); h = d.pop("hash", "")
        chk = "\n".join(f"{k}={d[k]}" for k in sorted(d))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h): return None
        if time.time() - int(d.get("auth_date", 0)) > 86400 * 3: return None
        return int(json.loads(d["user"])["id"])
    except Exception:
        return None

# ───────────────────────── нейросеть ─────────────────────────
def llm_chat(messages, profile="", image=None):
    msgs = []
    if profile: msgs.append({"role": "system", "content": profile[:6000]})
    msgs += [{"role": m.get("role", "user"), "content": str(m.get("content", ""))[:30000]} for m in messages[-24:] if m.get("content")]
    if image and msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] = [{"type": "text", "text": msgs[-1]["content"]}, {"type": "image_url", "image_url": {"url": image}}]
    hdr = {"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {}
    body = {"model": LLM_MODEL, "messages": msgs, "temperature": 0.7, "max_tokens": LLM_MAX}
    last = ""
    for attempt in range(2):
        st, j = http(f"{LLM_BASE}/chat/completions" if not LLM_BASE.endswith("/openai") else LLM_BASE, body, hdr, timeout=170)
        try:
            t = j["choices"][0]["message"]["content"]
            if t and t.strip(): return t
        except Exception:
            last = f"HTTP {st}: {json.dumps(j, ensure_ascii=False)[:200]}"
        time.sleep(1.2)
    raise RuntimeError("нейросеть не ответила (" + last + ")")

def make_image(prompt, kind_style="", w=768, h=1024, src_image=None):
    """Возвращает (bytes, расширение)."""
    p = (prompt + (", " + kind_style if kind_style else "")).strip()
    if IMG_PROVIDER == "openai":
        hdr = {"Authorization": f"Bearer {IMG_KEY}"}
        size = "1024x1024" if abs(w - h) < 100 else ("1536x1024" if w > h else "1024x1536")
        st, j = http(f"{IMG_BASE if IMG_BASE != 'https://image.pollinations.ai' else 'https://api.openai.com/v1'}/images/generations",
                     {"model": IMG_MODEL, "prompt": p, "size": size, "n": 1}, hdr, timeout=170)
        d = (j.get("data") or [{}])[0] if isinstance(j, dict) else {}
        if d.get("b64_json"): return base64.b64decode(d["b64_json"]), "png"
        if d.get("url"): return http(d["url"], raw=True, timeout=60)[1], "png"
        raise RuntimeError("картиночный сервис: " + json.dumps(j, ensure_ascii=False)[:200])
    url = f"{IMG_BASE}/prompt/{quote(p)}?width={w}&height={h}&nologo=true&model={IMG_MODEL}&seed={uuid.uuid4().int % 10**6}"
    hdr = {"Authorization": f"Bearer {IMG_KEY}"} if IMG_KEY else {}
    last = ""
    for _ in range(3):
        st, data = http(url, headers=hdr, raw=True, timeout=150)
        if st == 200 and data[:4] in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb") or (st == 200 and data[:8] == b"\x89PNG\r\n\x1a\n"):
            return data, ("png" if data[:4] == b"\x89PNG" else "jpg")
        last = f"HTTP {st}: {data[:120]!r}"; time.sleep(2)
    raise RuntimeError("не удалось создать картинку (" + last + ")")

SIZES = {"1:1": (1024, 1024), "3:4": (768, 1024), "4:3": (1024, 768), "16:9": (1280, 720), "9:16": (720, 1280)}
STYLES = {"cinematic": "cinematic lighting, film still", "photo": "photorealistic, high detail", "anime": "anime style", "3d": "3d render, octane",
          "neon": "neon cyberpunk", "minimal": "minimal clean illustration"}
JOBS = {}

def run_job(jid, b):
    try:
        kind = b.get("kind", "image")
        if kind == "video": raise RuntimeError("Видео нейросетью на сервере не включено, приложение соберёт его кодом")
        if kind == "edit": raise RuntimeError("Правка фото на этом сервере не подключена")
        w, h = SIZES.get(b.get("aspect") or "3:4", (768, 1024))
        data, ext = make_image(str(b.get("prompt", ""))[:1500], STYLES.get(b.get("style"), ""), w, h)
        name = f"{uuid.uuid4().hex[:16]}.{ext}"
        open(os.path.join(MEDIA, name), "wb").write(data)
        JOBS[jid].update(status="done", kind="image", url=f"/media/{name}")
    except Exception as e:
        JOBS[jid].update(status="error", error=str(e)[:300])

# ───────────────────────── ЮKassa ─────────────────────────
def yk_auth():
    return {"Authorization": "Basic " + base64.b64encode(f"{YK_SHOP}:{YK_SECRET}".encode()).decode()}

def yk_create(uid, plan):
    p = PLANS[plan]; val = f"{p['rub']}.00"
    body = {"amount": {"value": val, "currency": "RUB"}, "capture": True,
            "confirmation": {"type": "redirect", "return_url": E("RETURN_URL") or (f"https://t.me/{BOT_NAME[0]}" if BOT_NAME[0] else PUBLIC_URL or "https://t.me")},
            "description": f"{p['title']} · {p['days']} дн.", "metadata": {"user_id": str(uid), "plan": plan}}
    if YK_EMAIL:
        body["receipt"] = {"customer": {"email": YK_EMAIL}, "items": [{"description": p["title"][:128], "quantity": "1.00",
                           "amount": {"value": val, "currency": "RUB"}, "vat_code": 1, "payment_mode": "full_payment", "payment_subject": "service"}]}
    st, j = http(f"{YK_API}/payments", body, {**yk_auth(), "Idempotence-Key": uuid.uuid4().hex}, timeout=30)
    if st >= 300 or not isinstance(j, dict) or "confirmation" not in j:
        raise RuntimeError(f"ЮKassa: {j.get('description') or j.get('error') or st}" if isinstance(j, dict) else f"ЮKassa HTTP {st}")
    with _lock, db() as c:
        c.execute("insert or replace into pending(pid,user_id,plan,ts,done) values(?,?,?,?,0)", (j["id"], uid, plan, int(time.time())))
    return j["confirmation"]["confirmation_url"], j["id"]

def yk_check(pid):
    """Спрашиваем у ЮKassa статус платежа и, если он оплачен, выдаём подписку. Подделать вебхук так нельзя."""
    st, j = http(f"{YK_API}/payments/{pid}", headers=yk_auth(), timeout=25)
    if st != 200 or not isinstance(j, dict): return False
    with _lock, db() as c:
        r = c.execute("select user_id, plan from pending where pid=?", (pid,)).fetchone()
    if not r: return False
    if j.get("status") == "succeeded" and j.get("paid"):
        am = j.get("amount") or {}
        if am.get("currency") != "RUB" or float(am.get("value", 0)) + 0.001 < PLANS[r[1]]["rub"]:
            print("ЮKassa: сумма платежа не совпадает с тарифом, подписка не выдана:", pid, am, file=sys.stderr); return False
        grant(r[0], r[1], "yk:" + pid, int(float(am.get("value", 0))))
        with _lock, db() as c: c.execute("update pending set done=1 where pid=?", (pid,))
        return True
    if j.get("status") == "canceled":
        with _lock, db() as c: c.execute("update pending set done=1 where pid=?", (pid,))
    return False

def check_pending(uid):
    if not YK_SECRET: return
    with _lock, db() as c:
        rows = c.execute("select pid from pending where user_id=? and done=0 and ts>?", (uid, int(time.time()) - 7200)).fetchall()
    for (pid,) in rows:
        try: yk_check(pid)
        except Exception as e: print("yk_check:", e, file=sys.stderr)

# ───────────────────────── Telegram Stars ─────────────────────────
def stars_invoice(uid, plan):
    p = PLANS[plan]
    return tg("createInvoiceLink", title=p["title"], description=p["desc"], payload=json.dumps({"u": uid, "p": plan}),
              provider_token="", currency="XTR", prices=[{"label": p["title"], "amount": p["stars"]}])

def _stars_payload(raw):
    """Разбирает payload счёта. Возвращает (user_id, plan) или None, если это не наш счёт."""
    try:
        pl = json.loads(raw)
        if isinstance(pl, dict) and pl.get("p") in PLANS: return int(pl["u"]), pl["p"]
    except Exception: pass
    return None

def handle_update(u):
    if "pre_checkout_query" in u:
        q = u["pre_checkout_query"]; pl = _stars_payload(q.get("invoice_payload", ""))
        ok = bool(pl) and q.get("currency") == "XTR" and q.get("total_amount") == PLANS[pl[1]]["stars"]
        a = {"pre_checkout_query_id": q["id"], "ok": ok}
        if not ok: a["error_message"] = "Не удалось проверить счёт, попробуйте ещё раз"
        tg("answerPreCheckoutQuery", **a)
    m = u.get("message") or {}; sp = m.get("successful_payment")
    if sp and sp.get("currency") == "XTR":
        pl = _stars_payload(sp.get("invoice_payload", ""))
        if not pl:
            print("Stars: платёж с чужим/битым payload, пропущен:", str(sp)[:300], file=sys.stderr); return
        uid, plan = pl; paid = int(sp.get("total_amount", 0))
        if paid < PLANS[plan]["stars"]:
            print(f"Stars: ВНИМАНИЕ, оплачено {paid} ⭐ вместо {PLANS[plan]['stars']} за {plan}, user {uid}, подписка не выдана:", sp.get("telegram_payment_charge_id"), file=sys.stderr); return
        if grant(uid, plan, "tg:" + (sp.get("telegram_payment_charge_id") or uuid.uuid4().hex), paid):
            try: tg("sendMessage", chat_id=m["chat"]["id"], text="Оплата прошла ⭐ Подписка подключена. Вернитесь в приложение.")
            except Exception: pass

def poll_loop():
    try: tg("deleteWebhook")
    except Exception as e: print("deleteWebhook:", e)
    off = 0
    while True:
        try:
            for u in tg("getUpdates", offset=off, timeout=25, allowed_updates=["pre_checkout_query", "message"]):
                off = u["update_id"] + 1
                try: handle_update(u)
                except Exception as e: print("update error:", e, file=sys.stderr)
        except Exception as e:
            print("poll error:", e, file=sys.stderr); time.sleep(3)

BOT_NAME = [""]
SESS = {}      # sid -> время выдачи
TRIES = {}     # ip -> [время попыток]

# ───────────────────────── HTTP-сервер ─────────────────────────
class H(BaseHTTPRequestHandler):
    server_version = "Velora/1"
    protocol_version = "HTTP/1.1"
    def log_message(self, f, *a): print(self.address_string(), f % a)
    def _send(self, code, data=None, raw=None, ctype="application/json; charset=utf-8", extra=None):
        b = raw if raw is not None else json.dumps(data if data is not None else {}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", ORIGIN); self.send_header("Access-Control-Allow-Headers", "*"); self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(b)
    def _body(self):
        try: return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception: return {}
    def _uid(self):
        raw = self.headers.get("X-Init-Data") or self.headers.get("X-Telegram-Init-Data") or ""
        return check_init_data(raw) if raw and BOT_TOKEN else None
    def _key(self):
        u = self._uid()
        return (u, str(u)) if u else (None, "anon:" + (self.headers.get("X-Anon-Id") or self.client_address[0])[:40])
    def _authed(self):
        if not PASSWORD: return True
        s = SESS.get(self.headers.get("X-Sess") or "")
        return bool(s and time.time() - s < 12 * 3600)
    def do_OPTIONS(self): self._send(204, raw=b"")
    do_HEAD = lambda self: self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try: return self._send(200, raw=open(INDEX, "rb").read(), ctype="text/html; charset=utf-8", extra={"Cache-Control": "no-cache"})
            except OSError: return self._send(404, {"error": f"нет файла {INDEX}"})
        if path == "/health": return self._send(200, {"ok": True, "llm": bool(LLM_KEY or "pollinations" in LLM_BASE), "yookassa": bool(YK_SECRET), "stars": bool(BOT_TOKEN)})
        if path.startswith("/media/"):
            f = os.path.join(MEDIA, os.path.basename(unquote(path)))
            if os.path.isfile(f):
                return self._send(200, raw=open(f, "rb").read(), ctype=mimetypes.guess_type(f)[0] or "application/octet-stream", extra={"Cache-Control": "public,max-age=86400"})
            return self._send(404, {"error": "нет файла"})
        if path == "/api/me": return self._me()
        if path.startswith("/api/job/"):
            j = JOBS.get(path.rsplit("/", 1)[-1])
            return self._send(200, j) if j else self._send(404, {"status": "error", "error": "задача не найдена"})
        if path == "/api/stars/me":
            uid = self._uid(); return self._send(200, get_sub(uid)) if uid else self._send(401, {"error": "Откройте приложение через бота в Telegram"})
        self._send(404, {"error": "not found"})

    def _me(self):
        if not self._authed(): return self._send(401, {"error": "auth"})
        uid, key = self._key(); s = get_sub(uid) if uid else {"active": False, "sub_until": 0, "plan": "free"}
        if uid and not s["active"]:
            check_pending(uid); s = get_sub(uid)
        n, reset = usage(key)
        self._send(200, {"free_total": FREE_MSGS, "used": 0 if BETA else n, "reset": 0 if BETA else reset, "active": s["active"], "sub_until": s["sub_until"],
                         "plan": s["plan"], "price": PLANS["plus"]["rub"], "beta": BETA and not s["active"],
                         "prices": {k: {"rub": v["rub"], "stars": v["stars"]} for k, v in PLANS.items()}})

    def do_POST(self):
        path = self.path.split("?")[0]; b = self._body()
        try:
            if path == "/tg/webhook":
                if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET: return self._send(403, {"error": "forbidden"})
                handle_update(b); return self._send(200, {"ok": True})
            if path == "/yookassa/webhook":
                pid = ((b.get("object") or {}).get("id")) if b.get("event") == "payment.succeeded" else None
                if pid and YK_SECRET: yk_check(pid)
                return self._send(200, {"ok": True})
            if path == "/api/unlock":
                ip = self.client_address[0]; now = time.time(); t = [x for x in TRIES.get(ip, []) if now - x < 600]
                if len(t) >= 10: return self._send(429, {"error": "Слишком много попыток, подождите"})
                if PASSWORD and not hmac.compare_digest(str(b.get("password", "")), PASSWORD):
                    TRIES[ip] = t + [now]; return self._send(401, {"error": "Неверный пароль"})
                sid = secrets.token_urlsafe(24); SESS[sid] = now; return self._send(200, {"sid": sid})
            if not self._authed(): return self._send(401, {"error": "auth"})
            if path == "/api/chat": return self._chat(b)
            if path == "/api/generate":
                uid, key = self._key()
                jid = uuid.uuid4().hex[:12]; JOBS[jid] = {"id": jid, "status": "pending"}
                threading.Thread(target=run_job, args=(jid, b), daemon=True).start()
                return self._send(200, {"id": jid})
            if path in ("/api/pay", "/api/stars/invoice"): return self._pay(b, path)
            if path == "/api/tts": return self._tts(b)
            if path == "/api/stt": return self._stt(b)
            if path == "/api/send": return self._send_file(b)
            self._send(404, {"error": "not found"})
        except Exception as e:
            print("ошибка запроса", path, e, file=sys.stderr)
            self._send(500, {"error": str(e)[:300]})

    def _chat(self, b):
        uid, key = self._key(); s = get_sub(uid) if uid else {"active": False}
        if not BETA and not s["active"]:
            n, _ = usage(key)
            if n >= FREE_MSGS: return self._send(402, {"error": "Лимит исчерпан"})
        reply = llm_chat(b.get("messages") or [], b.get("profile") or "", b.get("image"))
        if not BETA: usage(key, 1)
        self._send(200, {"reply": reply, "ticket": ""})

    def _pay(self, b, path):
        uid = self._uid()
        if not uid: return self._send(401, {"error": "Откройте приложение через бота в Telegram"})
        plan = b.get("plan") if b.get("plan") in PLANS else "plus"
        method = "stars" if path == "/api/stars/invoice" else (b.get("method") or "rub")
        if method == "stars":
            if not BOT_TOKEN: return self._send(503, {"error": "На сервере не задан BOT_TOKEN"})
            try: return self._send(200, {"invoice": stars_invoice(uid, plan), "plan": plan})
            except Exception as e: return self._send(502, {"error": f"Telegram не создал счёт: {e}"})
        if not YK_SECRET: return self._send(503, {"error": "ЮKassa: на сервере не задан YOOKASSA_SECRET"})
        try:
            url, pid = yk_create(uid, plan); self._send(200, {"url": url, "id": pid, "plan": plan})
        except Exception as e: self._send(502, {"error": str(e)})

    def _tts(self, b):
        if not TTS_BASE: return self._send(501, {"error": "озвучка на сервере не настроена"})
        st, data = http(f"{TTS_BASE}/audio/speech", {"model": TTS_MODEL, "input": str(b.get("text", ""))[:3500], "voice": b.get("voice") or "nova"},
                        {"Authorization": f"Bearer {TTS_KEY}"}, raw=True, timeout=90)
        if st != 200: return self._send(502, {"error": f"озвучка HTTP {st}"})
        self._send(200, raw=data, ctype="audio/mpeg")

    def _stt(self, b):
        if not STT_BASE: return self._send(501, {"error": "распознавание речи на сервере не настроено"})
        mime = b.get("mime") or "audio/webm"; ext = "mp4" if "mp4" in mime else "webm"
        body, ct = multipart({"model": STT_MODEL, "language": "ru"}, {"file": (f"a.{ext}", base64.b64decode(b.get("audio", "")), mime)})
        st, j = http(f"{STT_BASE}/audio/transcriptions", body, {"Authorization": f"Bearer {STT_KEY}", "Content-Type": ct}, timeout=90)
        if st != 200 or "text" not in j: return self._send(502, {"error": "не удалось распознать речь"})
        self._send(200, {"text": j["text"]})

    def _send_file(self, b):
        uid = self._uid(); name = os.path.basename(str(b.get("name", "")))
        f = os.path.join(MEDIA, name)
        if not uid or not os.path.isfile(f): return self._send(404, {"error": "нет файла"})
        ctype = mimetypes.guess_type(f)[0] or "application/octet-stream"
        body, ct = multipart({"chat_id": str(uid)}, {"document": (name, open(f, "rb").read(), ctype)})
        st, j = http(f"{TG_API}/bot{BOT_TOKEN}/sendDocument", body, {"Content-Type": ct}, timeout=60)
        self._send(200 if st == 200 else 502, {"ok": st == 200})

def main():
    if BOT_TOKEN:
        try: BOT_NAME[0] = tg("getMe").get("username", "")
        except Exception as e: print("getMe:", e)
        if "--poll" in sys.argv or not PUBLIC_URL:
            threading.Thread(target=poll_loop, daemon=True).start(); print("платежи Stars: getUpdates")
        else:
            try: tg("setWebhook", url=PUBLIC_URL + "/tg/webhook", secret_token=SECRET, allowed_updates=["pre_checkout_query", "message"]); print("webhook установлен")
            except Exception as e: print("setWebhook:", e)
    else: print("ВНИМАНИЕ: BOT_TOKEN не задан — вход через Telegram и Stars не будут работать")
    print("нейросеть:", LLM_BASE, LLM_MODEL, "| ЮKassa:", "ключ задан" if YK_SECRET else "НЕТ YOOKASSA_SECRET")
    if E("RENDER") and not os.path.abspath(DB_PATH).startswith("/var/data"):
        print("!!! ВНИМАНИЕ: на Render файловая система стирается при каждом деплое и перезапуске. Подписки в", DB_PATH,
              "пропадут. Подключите Disk (Mount Path /var/data) и задайте DB_PATH=/var/data/velora.db и MEDIA_DIR=/var/data/media", file=sys.stderr)
    port = int(E("PORT", "8080")); print("Velora слушает порт", port)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()

if __name__ == "__main__":
    main()
