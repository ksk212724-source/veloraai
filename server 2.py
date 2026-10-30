"""Velora — бэкенд + Telegram-бот.

Что делает:
  * отдаёт Mini App (static/index.html)
  * принимает задания на фото/видео, ходит в Pollinations (ключ остаётся на сервере)
  * проверяет, что запрос пришёл из Telegram (подпись initData)
  * запускает бота: /start показывает кнопку «Открыть студию»

Переменные окружения:
  BOT_TOKEN          токен от @BotFather
  WEBAPP_URL         публичный HTTPS-адрес этого сервера (для кнопки в боте)
  POLLINATIONS_KEY   необязательно: ключ с enter.pollinations.ai (нужен, если видео без ключа не отдаётся)
  LLM_API_URL        адрес вашего API чата, совместимого с OpenAI, например https://host/v1/chat/completions
  LLM_API_KEY        ключ вашего API
  LLM_MODEL          имя модели, которое ждёт ваш API
  (методичка нейросети лежит в persona.md и подставляется как системное сообщение)
  YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY   данные магазина ЮKassa (подписка 150 ₽ в месяц, 10 запросов каждые 3 часа)
  FREE_LLM_URL / FREE_LLM_MODEL   бесплатный запасной ИИ (по умолчанию Pollinations, модель openai); работает и без LLM_API_URL
  FREE_LIMIT         сколько бесплатных запросов за окно (по умолчанию 10)
  UNLIMITED          по умолчанию 1: бета-тест без лимитов; 0 включает лимиты и оплату
  USE_POLLING=1      бот через getUpdates вместо вебхука (локальный запуск)
  PORT               порт (по умолчанию 8080)
  MOCK=1             тестовый режим без интернета (рисует заглушки)
  ALLOW_BROWSER=1    разрешить открывать приложение вне Telegram (для проверки)
"""
import base64
import hashlib
import json
import re
import sqlite3
import hmac
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, request, send_from_directory

BASE = Path(__file__).parent
MEDIA = BASE / "media"
MEDIA.mkdir(exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
POLL_KEY = os.getenv("POLLINATIONS_KEY", "")
MOCK = os.getenv("MOCK") == "1"
ALLOW_BROWSER = os.getenv("ALLOW_BROWSER") == "1" or MOCK

# OpenRouter: один ключ на чат, распознавание фото, создание/правку фото и видео
OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
OR_BASE = "https://openrouter.ai/api/v1"
OR_CHAT_MODEL = os.getenv("OR_CHAT_MODEL", "google/gemini-2.5-flash")  # понимает фото
OR_IMAGE_MODEL = os.getenv("OR_IMAGE_MODEL", "bytedance-seed/seedream-4.5")
OR_EDIT_MODEL = os.getenv("OR_EDIT_MODEL", "") or OR_IMAGE_MODEL
OR_VIDEO_MODEL = os.getenv("OR_VIDEO_MODEL", "google/veo-3.1-fast")

LLM_API_URL = os.getenv("LLM_API_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
FREE_LLM_URL = os.getenv("FREE_LLM_URL", "https://gen.pollinations.ai/v1/chat/completions")
FREE_LLM_MODEL = os.getenv("FREE_LLM_MODEL", "openai")
PERSONA = (BASE / "persona.md").read_text(encoding="utf-8") if (BASE / "persona.md").exists() else ""

BETA_PASSWORD = os.getenv("BETA_PASSWORD", "5050")  # пароль закрытого бета-теста
BETA_GATE = os.getenv("BETA_GATE", "1") == "1"  # BETA_GATE=0 выключает пароль

FREE = int(os.getenv("FREE_LIMIT", "10"))
UNLIMITED = os.getenv("UNLIMITED", "1") == "1"  # бета-тест: лимитов нет; UNLIMITED=0 включает лимиты и оплату
PRICE = "150.00"
PERIOD = 30 * 86400
WINDOW = 3 * 3600  # 10 бесплатных запросов обновляются каждые 3 часа
YK_SHOP = os.getenv("YOOKASSA_SHOP_ID", "")
YK_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
YK_RETURN = os.getenv("YOOKASSA_RETURN_URL") or WEBAPP_URL
YK_EMAIL = os.getenv("YOOKASSA_RECEIPT_EMAIL", "")  # нужен, если магазин работает с чеками 54-ФЗ

GEN_URL = "https://gen.pollinations.ai/image/"
OLD_IMG_URL = "https://image.pollinations.ai/prompt/"

STYLES = {
    "none": "",
    "cinematic": ", cinematic lighting, film still, dramatic, high detail",
    "photo": ", ultra realistic photo, 35mm, natural light, sharp focus",
    "anime": ", anime style, clean lineart, vibrant, studio quality",
    "3d": ", 3D render, soft studio light, octane, glossy materials",
    "neon": ", neon noir, dark background, glowing accents, moody",
    "minimal": ", minimalist, clean composition, soft gradients, elegant",
}

IMAGE_SIZES = {"1:1": (1024, 1024), "3:4": (768, 1024), "16:9": (1280, 720), "9:16": (720, 1280)}

app = Flask(__name__, static_folder=None)
db = sqlite3.connect(BASE / "velora.db", check_same_thread=False)
db.executescript("create table if not exists users(uid text primary key, used int default 0, until real default 0);"
                 "create table if not exists pays(id text primary key, uid text, status text, t real);"
                 "create table if not exists beta(uid text primary key);")
tickets: dict[str, tuple] = {}
try:
    db.execute("alter table users add column lk real default 0")
except sqlite3.OperationalError:
    pass
jobs: dict[str, dict] = {}
hits: dict[str, list] = {}
lock = threading.Lock()


# ---------- защита ----------
def valid_init_data(raw: str) -> bool:
    try:
        pairs = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
        got = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        fresh = time.time() - int(pairs.get("auth_date", "0")) < 86400
        return hmac.compare_digest(calc, got) and fresh
    except Exception:
        return False


def is_beta(u: str) -> bool:
    if not BETA_GATE:
        return True
    with lock:
        return db.execute("select 1 from beta where uid=?", (u,)).fetchone() is not None


def add_beta(u: str):
    with lock:
        db.execute("insert or ignore into beta(uid) values(?)", (u,))
        db.commit()


def check_access():
    if ALLOW_BROWSER:
        return
    if not BOT_TOKEN or not valid_init_data(request.headers.get("X-Init-Data", "")):
        abort(403)


def rate_limit(key: str, limit=20, per=3600):
    now = time.time()
    with lock:
        arr = [t for t in hits.get(key, []) if now - t < per]
        if len(arr) >= limit:
            hits[key] = arr
            abort(429)
        arr.append(now)
        hits[key] = arr


# ---------- лимиты и оплата ----------
def uid() -> str:
    raw = request.headers.get("X-Init-Data", "")
    if ALLOW_BROWSER and not raw:
        return "web:" + (request.headers.get("X-Anon-Id") or request.remote_addr or "x")[:60]
    try:
        return "tg:" + str(json.loads(dict(urllib.parse.parse_qsl(raw)).get("user", "{}"))["id"])
    except Exception:
        abort(403)


def row(u: str):
    with lock:
        db.execute("insert or ignore into users(uid) values(?)", (u,))
        db.commit()
        used, until, lk = db.execute("select used, until, lk from users where uid=?", (u,)).fetchone()
        now = time.time()
        if used >= FREE and not lk:  # лимит кончился, пауза в 3 часа стартует с этого момента
            lk = now + WINDOW
            db.execute("update users set lk=? where uid=?", (lk, u))
            db.commit()
        if lk and now >= lk:  # пауза прошла: новые 10 запросов
            db.execute("update users set used=0, lk=0 where uid=?", (u,))
            db.commit()
            used, lk = 0, 0
        return used, until, lk


def spend(u: str) -> bool:
    if UNLIMITED:
        return True
    used, until, ws = row(u)
    if until > time.time():
        return True
    if used >= FREE:
        return False
    with lock:
        db.execute("update users set used=used+1, lk=case when used+1>=? then ? else lk end where uid=?", (FREE, time.time() + WINDOW, u))
        db.commit()
    return True


def paywall():
    return jsonify(error="Бесплатные запросы закончились", paywall=True), 402


def yk(method: str, path: str, body=None):
    r = requests.request(method, "https://api.yookassa.ru/v3" + path, json=body, auth=(YK_SHOP, YK_KEY),
                         headers={"Idempotence-Key": uuid.uuid4().hex}, timeout=30)
    r.raise_for_status()
    return r.json()


def grant(pid: str, u: str):
    with lock:
        st = db.execute("select status from pays where id=?", (pid,)).fetchone()
        if st and st[0] == "paid":
            return
        db.execute("update pays set status='paid' where id=?", (pid,))
        db.execute("update users set until=max(until, ?)+? where uid=?", (time.time(), PERIOD, u))
        db.commit()


def sync(pid: str):
    """Спрашиваем у ЮKassa настоящий статус платежа: на уведомление без проверки не полагаемся."""
    r = db.execute("select uid from pays where id=?", (pid,)).fetchone()
    if not r or MOCK:
        return
    p = yk("GET", "/payments/" + pid)
    if p.get("status") == "succeeded" and p.get("paid"):
        grant(pid, r[0])
    elif p.get("status") == "canceled":
        with lock:
            db.execute("update pays set status='canceled' where id=?", (pid,))
            db.commit()


# ---------- нейросеть чата ----------
def ask_llm(msgs, system=None, max_tokens=700) -> str:
    """Сначала ваш API (если задан), затем бесплатный Pollinations. Понимает картинки (image_url)."""
    providers = []
    if OR_KEY:
        providers.append((OR_BASE + "/chat/completions", OR_KEY, OR_CHAT_MODEL))
    if LLM_API_URL:
        providers.append((LLM_API_URL, LLM_API_KEY, LLM_MODEL))
    providers.append((FREE_LLM_URL, POLL_KEY, FREE_LLM_MODEL))
    last = None
    for url, key, model in providers:
        try:
            h = {"Content-Type": "application/json"}
            if key:
                h["Authorization"] = f"Bearer {key}"
            r = requests.post(url, headers=h, timeout=90, json={
                "model": model, "temperature": 0.8, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system or PERSONA}] + msgs})
            r.raise_for_status()
            txt = (r.json()["choices"][0]["message"]["content"] or "").strip()
            if txt:
                return txt
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(type(last).__name__ if last else "пустой ответ")


# ---------- генерация ----------
def auth_headers():
    return {"Authorization": f"Bearer {POLL_KEY}"} if POLL_KEY else {}


def mock_make(kind: str, path: Path, prompt: str):
    time.sleep(3)
    if kind == "image":
        from PIL import Image, ImageDraw

        h = int(hashlib.md5(prompt.encode()).hexdigest()[:6], 16)
        im = Image.new("RGB", (768, 768), (8, 8, 12))
        d = ImageDraw.Draw(im)
        for i in range(6):
            c = ((h >> (i * 2)) & 255, (h >> (i * 3)) & 255, 255 - ((h >> i) & 255))
            x, y = 120 + (h >> i) % 400, 120 + (h >> (i + 3)) % 400
            d.ellipse([x - 160, y - 160, x + 160, y + 160], fill=tuple(v // 3 for v in c))
        im.save(path, "PNG")
    else:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=480x854:rate=24:duration=3",
             "-pix_fmt", "yuv420p", "-preset", "veryfast", str(path)],
            check=True, capture_output=True,
        )


def or_headers():
    return {"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"}


def or_image(prompt: str, aspect: str, src: str = "") -> tuple[bytes, str]:
    body = {"model": OR_EDIT_MODEL if src else OR_IMAGE_MODEL, "prompt": prompt, "n": 1}
    if aspect in ("1:1", "3:4", "16:9", "9:16"):
        body["aspect_ratio"] = aspect
    if src:  # правка: исходное фото передаём ссылкой
        body["input_references"] = [{"type": "image_url", "image_url": {"url": src}}]
    r = requests.post(OR_BASE + "/images", headers=or_headers(), json=body, timeout=240)
    r.raise_for_status()
    item = r.json()["data"][0]
    return base64.b64decode(item["b64_json"]), item.get("media_type") or "image/png"


def or_video(prompt: str, aspect: str, dur: int) -> tuple[bytes, str]:
    body = {"model": OR_VIDEO_MODEL, "prompt": prompt, "duration": dur,
            "aspect_ratio": "9:16" if aspect == "9:16" else "16:9"}
    r = requests.post(OR_BASE + "/videos", headers=or_headers(), json=body, timeout=60)
    r.raise_for_status()
    job = r.json()
    poll = urllib.parse.urljoin("https://openrouter.ai", job["polling_url"])
    deadline = time.time() + 540
    while time.time() < deadline:
        time.sleep(6)
        j = requests.get(poll, headers=or_headers(), timeout=30).json()
        st = j.get("status")
        if st == "completed":
            for u in j.get("unsigned_urls") or []:
                v = requests.get(u, headers=or_headers(), timeout=300)
                if v.status_code == 200 and len(v.content) > 1000:
                    return v.content, "video/mp4"
            v = requests.get(f"{OR_BASE}/videos/{job['id']}/content", headers=or_headers(), timeout=300)
            v.raise_for_status()
            return v.content, "video/mp4"
        if st in ("failed", "cancelled", "expired"):
            raise RuntimeError("видео не получилось: " + str(j.get("error", st))[:120])
    raise RuntimeError("видео делается слишком долго")


def fetch(kind: str, prompt: str, opts: dict) -> tuple[bytes, str]:
    if OR_KEY and not MOCK:  # сначала OpenRouter, при ошибке падаем на бесплатный Pollinations
        try:
            style = STYLES.get(opts.get("style", "none"), "")
            full = (prompt.strip() + style)[:1500]
            aspect = opts.get("aspect", "1:1")
            if kind == "video":
                return or_video(full, aspect, int(opts.get("duration", 4)))
            return or_image(prompt.strip()[:1500] if kind == "edit" else full, aspect,
                            opts.get("src", "") if kind == "edit" else "")
        except Exception as e:  # noqa: BLE001
            print("OpenRouter:", type(e).__name__, str(e)[:200])
    return fetch_free(kind, prompt, opts)


def fetch_free(kind: str, prompt: str, opts: dict) -> tuple[bytes, str]:
    style = STYLES.get(opts.get("style", "none"), "")
    full = (prompt.strip() + style)[:1500]
    aspect = opts.get("aspect", "1:1")
    if kind in ("image", "edit"):
        w, h = IMAGE_SIZES.get(aspect, (1024, 1024))
        params = {"model": "flux", "width": w, "height": h, "nologo": "true",
                  "seed": opts.get("seed") or int(time.time()) % 1_000_000}
        if kind == "edit":  # картинка -> картинка: исходник отдаём по публичной ссылке
            params = {"model": "kontext", "image": opts.get("src", ""), "nologo": "true", "seed": params["seed"]}
        attempts = [(GEN_URL, auth_headers()), (OLD_IMG_URL, {})]
    else:
        dur = int(opts.get("duration", 4))
        params = {"model": "veo" if dur in (4, 6, 8) else "seedance",
                  "duration": dur, "aspectRatio": "9:16" if aspect == "9:16" else "16:9"}
        attempts = [(GEN_URL, auth_headers())]

    last = "нет ответа"
    for base, headers in attempts:
        try:
            r = requests.get(base + urllib.parse.quote(full, safe=""), params=params,
                             headers=headers, timeout=300)
            ct = r.headers.get("content-type", "").split(";")[0]
            if r.status_code == 200 and ct.startswith(("image/", "video/")):
                return r.content, ct
            last = f"сервис ответил {r.status_code}"
            if r.status_code in (401, 402, 403):
                last += " — для этого нужен ключ (см. README)"
        except requests.RequestException as e:
            last = f"сеть: {type(e).__name__}"
    raise RuntimeError(last)


def run_job(jid: str, kind: str, prompt: str, opts: dict):
    try:
        if MOCK:
            ext = "mp4" if kind == "video" else "png"
            path = MEDIA / f"{jid}.{ext}"
            mock_make("video" if kind == "video" else "image", path, prompt)
        else:
            data, ct = fetch(kind, prompt, opts)
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                   "video/mp4": "mp4", "video/webm": "webm"}.get(ct, "bin")
            path = MEDIA / f"{jid}.{ext}"
            path.write_bytes(data)
        jobs[jid].update(status="done", url=f"/media/{path.name}", kind="video" if kind == "video" else "image")
    except Exception as e:  # noqa: BLE001
        jobs[jid].update(status="error", error=str(e)[:200])


# ---------- маршруты ----------
@app.get("/")
def index():
    # index.html может лежать рядом с server.py или в папке static
    folder = BASE if (BASE / "index.html").exists() else BASE / "static"
    return send_from_directory(folder, "index.html")


@app.get("/api/config")
def config():
    return jsonify(styles=list(STYLES), mock=MOCK)


@app.post("/api/generate")
def generate():
    check_access()
    rate_limit(request.headers.get("X-Forwarded-For", request.remote_addr or "x"))
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    prompt = (body.get("prompt") or "").strip()
    if kind not in ("image", "video", "edit") or not (2 <= len(prompt) <= 600):
        return jsonify(error="Опишите, что создать (2–600 символов)"), 400
    raw_img = None
    if kind == "edit":
        src = body.get("image") or ""
        mm = re.match(r"data:image/(?:jpeg|png|webp);base64,(.+)$", src, re.S)
        if not mm or len(src) > 4_000_000:
            return jsonify(error="Добавьте фото, которое нужно изменить"), 400
        try:
            raw_img = base64.b64decode(mm.group(1))
        except Exception:  # noqa: BLE001
            return jsonify(error="Не удалось прочитать фото"), 400
    u = uid()
    tk = tickets.pop(body.get("ticket"), None)  # картинку, которую Velora рисует сама по ходу чата, второй раз не списываем
    if not (tk and tk[0] == u and tk[1] > time.time()) and not spend(u):
        return paywall()
    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {"status": "working", "t": time.time()}
    opts = {k: body.get(k) for k in ("style", "aspect", "duration", "seed")}
    if raw_img:
        sname = f"src_{jid}.jpg"
        (MEDIA / sname).write_bytes(raw_img)
        opts["src"] = (WEBAPP_URL or request.host_url.rstrip("/")) + "/media/" + sname
    threading.Thread(target=run_job, args=(jid, kind, prompt, opts), daemon=True).start()
    return jsonify(id=jid)


def mock_reply(last: str, photo=False) -> str:
    low = last.lower()
    if photo:
        return "На фото спокойная сцена: мягкий свет, чистая композиция, главный объект в центре. Могу описать подробнее или нарисовать похожее."
    if any(w in low for w in ("нарису", "фото", "картин")):
        return ("Хорошо, сейчас создам: мягкий свет, глубина, чистые цвета. "
                "[[image: a misty morning over a calm sea, soft painterly watercolor look, muted blues and violets, gentle haze]]")
    if "видео" in low:
        return ("Сделаю медленный кадр, пока поднимается рассвет. "
                "[[video: slow camera glide over calm water at dawn, thin mist rising, painterly muted colors]]")
    if "кто ты" in low or "умеешь" in low:
        return "Я Velora, ИИ-помощница этой студии. Разговариваю, придумываю идеи и рисую фото и видео по твоему описанию."
    return "Я здесь. Расскажи, что хочешь создать, или просто поговорим."


@app.post("/api/chat")
def chat():
    check_access()
    rate_limit("chat:" + request.headers.get("X-Forwarded-For", request.remote_addr or "x"), limit=120)
    body = request.get_json(silent=True) or {}
    msgs = [m for m in (body.get("messages") or [])
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)][-20:]
    if not msgs or msgs[-1]["role"] != "user":
        return jsonify(error="Пустое сообщение"), 400
    msgs = [{"role": m["role"], "content": m["content"][:2000]} for m in msgs]
    u = uid()
    if not spend(u):
        return paywall()
    text0 = msgs[-1]["content"]
    image = body.get("image")
    if isinstance(image, str) and image.startswith("data:image/") and len(image) < 4_000_000:
        msgs[-1]["content"] = [{"type": "text", "text": text0}, {"type": "image_url", "image_url": {"url": image}}]
    else:
        image = None

    def done(reply):
        out = {"reply": reply}
        if re.search(r"\[\[(image|video):", reply, re.I):
            t = uuid.uuid4().hex
            tickets[t] = (u, time.time() + 600)
            out["ticket"] = t
        return jsonify(out)

    if MOCK:
        time.sleep(1.2)
        return done(mock_reply(text0, bool(image)))
    try:
        prof = str(body.get("profile") or "")[:900].strip()
        system = PERSONA + ("\n\nПожелания пользователя (учитывай, если они не противоречат правилам выше): " + prof if prof else "")
        return done(ask_llm(msgs, system=system))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"Нейросеть не ответила ({e})"), 502


@app.get("/api/me")
def me():
    check_access()
    u = uid()
    for (pid,) in db.execute("select id from pays where uid=? and status='pending'", (u,)).fetchall():
        try:
            sync(pid)
        except Exception:  # noqa: BLE001
            pass
    used, until, ws = row(u)
    return jsonify(free_total=FREE, used=min(used, FREE), beta=UNLIMITED, active=UNLIMITED or until > time.time(), sub_until=(time.time() + PERIOD if UNLIMITED else until), price=int(float(PRICE)), reset=ws)


@app.post("/api/pay")
def pay():
    check_access()
    u = uid()
    if MOCK:
        pid = "mock" + uuid.uuid4().hex[:8]
        url = request.host_url + "api/mock-pay/" + pid
    else:
        if not (YK_SHOP and YK_KEY):
            return jsonify(error="Оплата ещё не подключена"), 503
        body = {"amount": {"value": PRICE, "currency": "RUB"}, "capture": True,
                "confirmation": {"type": "redirect", "return_url": YK_RETURN},
                "description": "Velora, подписка на 30 дней", "metadata": {"uid": u}}
        if YK_EMAIL:
            body["receipt"] = {"customer": {"email": YK_EMAIL}, "items": [{
                "description": "Подписка Velora, 30 дней", "quantity": "1", "amount": {"value": PRICE, "currency": "RUB"},
                "vat_code": int(os.getenv("YOOKASSA_VAT_CODE", "1")), "payment_subject": "service", "payment_mode": "full_payment"}]}
        try:
            p = yk("POST", "/payments", body)
        except Exception:  # noqa: BLE001
            return jsonify(error="ЮKassa не ответила, попробуйте ещё раз"), 502
        pid, url = p["id"], p["confirmation"]["confirmation_url"]
    with lock:
        db.execute("insert into pays values(?,?,?,?)", (pid, u, "pending", time.time()))
        db.commit()
    return jsonify(url=url, id=pid)


@app.get("/api/mock-pay/<pid>")
def mock_pay(pid):
    r = db.execute("select uid from pays where id=?", (pid,)).fetchone()
    if not MOCK or not r:
        abort(404)
    grant(pid, r[0])
    return ("<meta name=viewport content='width=device-width'><body style='background:#09090b;color:#fff;font:20px sans-serif;"
            "display:grid;place-items:center;height:100vh;text-align:center'>Тестовая оплата прошла.<br>Вернитесь в Velora.")


@app.post("/api/yookassa/webhook")  # в кабинете ЮKassa: Интеграция → HTTP-уведомления → payment.succeeded
def hook():
    pid = ((request.get_json(silent=True) or {}).get("object") or {}).get("id")
    if pid:
        try:
            sync(pid)
        except Exception:  # noqa: BLE001
            abort(502)
    return "", 200


@app.get("/api/job/<jid>")
def job(jid):
    j = jobs.get(jid)
    if not j:
        abort(404)
    return jsonify({k: v for k, v in j.items() if k != "t"})


@app.get("/media/<name>")
def media(name):
    return send_from_directory(MEDIA, name, max_age=3600)


def cleaner():
    while True:
        time.sleep(1800)
        cutoff = time.time() - 24 * 3600
        for f in MEDIA.iterdir():
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        for k in [k for k, v in jobs.items() if v.get("t", 0) < cutoff]:
            jobs.pop(k, None)


# ---------- отправка результата в чат с ботом ----------
@app.get("/health")
def health():
    return "ok"


@app.post("/api/send")
def send_to_chat():
    """Кнопка «Сохранить»: бот присылает фото/видео в личный чат (в Telegram это надёжнее, чем скачивание)."""
    check_access()
    u = uid()
    if not u.startswith("tg:") or not BOT_TOKEN:
        return jsonify(error="Доступно только внутри Telegram"), 400
    rate_limit("send:" + u, limit=30)
    name = Path((request.get_json(silent=True) or {}).get("name", "")).name
    path = MEDIA / name
    if not name or not path.is_file():
        abort(404)
    video = path.suffix.lower() in (".mp4", ".webm")
    data = path.read_bytes()
    r = tg_upload("sendVideo" if video else "sendPhoto", "video" if video else "photo", data, name, chat_id=u[3:], caption="Velora")
    if not r.get("ok") and not video:  # слишком большое/необычное фото: шлём файлом
        r = tg_upload("sendDocument", "document", data, name, chat_id=u[3:], caption="Velora")
    return (jsonify(ok=True), 200) if r.get("ok") else (jsonify(error="Не удалось отправить. Нажмите /start у бота"), 502)


# ---------- бот ----------
WH_SECRET = hashlib.sha256(("velora-wh:" + BOT_TOKEN).encode()).hexdigest()[:40]
USE_WEBHOOK = WEBAPP_URL.startswith("https://") and os.getenv("USE_POLLING") != "1"
bot_hist: dict[int, list] = {}
HELP = ("Я Velora: говорю, придумываю, рисую фото и видео и разбираю снимки.\n\n"
        "Что можно прямо в чате:\n"
        "• просто напишите вопрос\n"
        "• пришлите фото: расскажу, что на нём (подпись к фото = ваш вопрос)\n"
        "• фото с подписью /edit и описанием: изменю снимок\n"
        "• /img описание: нарисую фото\n"
        "• /video описание: сниму короткое видео\n"
        "• /new: начать разговор заново\n\n"
        "Кнопка ниже открывает приложение: камера, стили, форматы.")


def tg(method: str, **payload):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=60)
    return r.json()


def tg_upload(method: str, field: str, data: bytes, filename: str, **form):
    r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=form,
                      files={field: (filename, data)}, timeout=180)
    try:
        return r.json()
    except ValueError:
        return {"ok": False}


def app_button():
    return {"inline_keyboard": [[{"text": "Открыть Velora", "web_app": {"url": WEBAPP_URL}}]]}


def strip_tags(s: str) -> str:
    return re.sub(r"\[\[(image|video):[\s\S]*?\]\]", "", s, flags=re.I).strip()


def send_media(chat_id, kind: str, prompt: str, opts=None):
    data, _ = fetch(kind, prompt, opts or {})
    if kind == "video":
        tg_upload("sendVideo", "video", data, "velora.mp4", chat_id=chat_id)
        return
    r = tg_upload("sendPhoto", "photo", data, "velora.jpg", chat_id=chat_id, caption=prompt[:200])
    if not r.get("ok"):
        tg_upload("sendDocument", "document", data, "velora.jpg", chat_id=chat_id, caption=prompt[:200])


def handle_update(u: dict):
    msg = u.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id, frm = chat.get("id"), (msg.get("from") or {}).get("id")
    if not chat_id or not frm or chat.get("type") != "private":
        return
    who = f"tg:{frm}"
    text = (msg.get("text") or msg.get("caption") or "").strip()
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower() if cmd.startswith("/") else ""
    arg = arg.strip() if cmd else text

    def say(t, **kw):
        tg("sendMessage", chat_id=chat_id, text=(t or "…")[:4000], **kw)

    def need_pay():
        say("Бесплатные запросы закончились. Они обновятся через 3 часа, а подписка в Velora снимает лимит.",
            reply_markup=app_button())

    try:
        if not is_beta(who):  # закрытый бета-тест: ссылка выдаётся только после пароля
            if text == BETA_PASSWORD:
                add_beta(who)
                tg("deleteMessage", chat_id=chat_id, message_id=msg.get("message_id"))
                say("Пароль принят, добро пожаловать в бета-тест Velora!\nНажмите кнопку ниже, чтобы открыть мини-приложение.",
                    reply_markup=app_button())
            elif cmd == "/start":
                say("Velora сейчас в закрытом бета-тесте.\nВведите пароль, чтобы получить доступ.")
            else:
                say("Неверный пароль. Попробуйте ещё раз.")
            return
        if cmd in ("/start", "/help"):
            say(HELP, reply_markup=app_button())
            return
        if cmd == "/new":
            bot_hist.pop(chat_id, None)
            say("Начали заново.")
            return
        photos = msg.get("photo")
        if photos:
            if cmd == "/edit" and not arg:
                say("Добавьте описание: что изменить на фото.")
                return
            if not spend(who):
                need_pay()
                return
            pick = next((p for p in reversed(photos) if p.get("file_size", 0) <= 3_000_000), photos[0])
            tg("sendChatAction", chat_id=chat_id, action="upload_photo" if cmd == "/edit" else "typing")
            path = tg("getFile", file_id=pick["file_id"])["result"]["file_path"]
            raw = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=60).content
            if cmd == "/edit":
                name = f"src_{uuid.uuid4().hex[:12]}.jpg"
                (MEDIA / name).write_bytes(raw)
                send_media(chat_id, "edit", arg[:600], {"src": f"{WEBAPP_URL}/media/{name}"})
                return
            q = (arg or "Что на фото? Опиши подробно и по делу.")[:2000]
            durl = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
            reply = ask_llm([{"role": "user", "content": [{"type": "text", "text": q},
                                                          {"type": "image_url", "image_url": {"url": durl}}]}])
            say(strip_tags(reply))
            return
        if cmd in ("/img", "/image", "/video"):
            if len(arg) < 2:
                say("Добавьте описание, например: /img рассвет над горами")
                return
            if not spend(who):
                need_pay()
                return
            kind = "video" if cmd == "/video" else "image"
            tg("sendChatAction", chat_id=chat_id, action="upload_video" if kind == "video" else "upload_photo")
            send_media(chat_id, kind, arg[:600])
            return
        if cmd == "/edit":
            say("Пришлите фото и в подписи напишите /edit и что изменить.")
            return
        if cmd or not text:
            if cmd:
                say(HELP, reply_markup=app_button())
            return
        if not spend(who):
            need_pay()
            return
        tg("sendChatAction", chat_id=chat_id, action="typing")
        h = bot_hist.setdefault(chat_id, [])
        h.append({"role": "user", "content": text[:2000]})
        del h[:-10]
        while h and h[0]["role"] != "user":
            h.pop(0)
        try:
            reply = ask_llm(h)
        except Exception:
            h.pop()
            raise
        clean = strip_tags(reply)
        h.append({"role": "assistant", "content": clean[:2000] or "…"})
        if clean:
            say(clean)
        m = re.search(r"\[\[(image|video):\s*([\s\S]+?)\]\]", reply, re.I)
        if m:
            send_media(chat_id, m.group(1).lower(), m.group(2).strip()[:600])
    except Exception as e:  # noqa: BLE001
        say(f"Не вышло: {str(e)[:160]}")


@app.post("/tg/<secret>")
def tg_hook(secret):
    if not BOT_TOKEN or not hmac.compare_digest(secret, WH_SECRET):
        abort(404)
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WH_SECRET:
        abort(403)
    upd = request.get_json(silent=True) or {}
    threading.Thread(target=handle_update, args=(upd,), daemon=True).start()
    return "", 200


def setup_bot():
    if BETA_GATE:  # без пароля ссылки быть не должно, поэтому кнопка меню обычная
        tg("setChatMenuButton", menu_button={"type": "default"})
    else:
        tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Velora", "web_app": {"url": WEBAPP_URL}})
    tg("setMyCommands", commands=[
        {"command": "img", "description": "Нарисовать фото: /img описание"},
        {"command": "video", "description": "Снять видео: /video описание"},
        {"command": "edit", "description": "Изменить фото (подпись к фото)"},
        {"command": "new", "description": "Начать разговор заново"},
        {"command": "help", "description": "Что я умею"}])
    if USE_WEBHOOK:
        res = tg("setWebhook", url=f"{WEBAPP_URL}/tg/{WH_SECRET}", secret_token=WH_SECRET, allowed_updates=["message"])
        print("Вебхук:", "установлен" if res.get("ok") else res)
        return
    tg("deleteWebhook")
    offset = 0
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                threading.Thread(target=handle_update, args=(upd,), daemon=True).start()
        except Exception:  # noqa: BLE001
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=cleaner, daemon=True).start()
    if BOT_TOKEN and WEBAPP_URL and not MOCK:
        threading.Thread(target=setup_bot, daemon=True).start()
        print("Бот запущен (" + ("вебхук" if USE_WEBHOOK else "polling") + ")")
    elif not MOCK:
        print("BOT_TOKEN или WEBAPP_URL не заданы — бот не запущен, работает только сайт")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), threaded=True)
