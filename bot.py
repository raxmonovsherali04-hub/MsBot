#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         MILLIY SERTIFIKAT TEST TEKSHIRUVCHI BOT              ║
║              RUSH Model Scoring System                        ║
║                                                              ║
║  O'zbekiston Milliy Sertifikat Imtihonlari tizimi            ║
╚══════════════════════════════════════════════════════════════╝

TALAB QILINADIGAN KUTUBXONALAR:
  pip install python-telegram-bot==20.7 flask requests

ISHGA TUSHIRISH:
  1. Railway Variables bo'limiga qo'shing:
       BOT_TOKEN=...
       OWNER_IDS=123456789
       WEBAPP_URL=https://your-app.railway.app
       DB_PATH=/data/sertifikat.db
  2. Railway → Add Volume → Mount: /data
  3. python bot.py

ARXITEKTURA:
  - WebApp'lar InlineKeyboardButton orqali ochiladi
  - Barcha ma'lumot almashinuvi fetch() → Flask API orqali
  - Telegram initData HMAC-SHA256 bilan tekshiriladi
  - Flask javobdan so'ng Telegram xabari background thread'da yuboriladi
"""

import logging
import json
import os
import sys
import sqlite3
import threading
import hmac
import hashlib
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests as http_client  # FIX #9: top-level import, funksiya ichida emas
from flask import Flask, request, jsonify
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo, ReplyKeyboardRemove, KeyboardButton, ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ================================================================
#                        SOZLAMALAR
# ================================================================
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
OWNER_IDS  = [int(x) for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
FLASK_PORT = int(os.environ.get("PORT", 5000))
TIMEZONE   = os.environ.get("TIMEZONE", "Asia/Tashkent")
# FIX #3: /data — Railway Volume uchun doimiy papka.
# Railway → Add Volume → Mount Point: /data  qilinishi SHART!
DB_PATH    = os.environ.get("DB_PATH", "/data/sertifikat.db")

# FIX #14: SystemExit o'rniga sys.exit(1) — Railway cheksiz restart qilmaydi
def _check_env():
    missing = []
    if not BOT_TOKEN:  missing.append("BOT_TOKEN")
    if not OWNER_IDS:  missing.append("OWNER_IDS")
    if not WEBAPP_URL: missing.append("WEBAPP_URL")
    if missing:
        print(
            f"\n❌ ENV o'zgaruvchilar topilmadi: {', '.join(missing)}\n"
            "Railway → Variables bo'limiga qo'shing:\n"
            "  BOT_TOKEN=...\n"
            "  OWNER_IDS=123456789\n"
            "  WEBAPP_URL=https://...\n"
            "  DB_PATH=/data/sertifikat.db  (Railway Volume kerak!)\n",
            file=sys.stderr
        )
        sys.exit(1)

_check_env()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
UZ_TZ = ZoneInfo(TIMEZONE)

# ================================================================
#                       MA'LUMOTLAR BAZASI
# ================================================================
def get_db() -> sqlite3.Connection:
    """Thread-safe DB ulanish qaytaradi"""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")  # FK cheklovlarini yoqish
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                phone       TEXT,
                full_name   TEXT,
                username    TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            -- FIX #12: owner_id → owner_tg_id (aniq semantika: Telegram user ID)
            -- FIX #8:  rush_calculated — RUSH faqat bir marta hisoblanadi
            CREATE TABLE IF NOT EXISTS tests (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                code             TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                subject          TEXT    NOT NULL,
                open_count       INTEGER NOT NULL DEFAULT 0,
                closed_count     INTEGER NOT NULL DEFAULT 0,
                owner_tg_id      INTEGER NOT NULL,
                start_time       TEXT    NOT NULL,
                end_time         TEXT    NOT NULL,
                is_saved         INTEGER NOT NULL DEFAULT 0,
                rush_calculated  INTEGER NOT NULL DEFAULT 0,
                created_at       TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS questions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id     INTEGER NOT NULL,
                q_num       INTEGER NOT NULL,
                q_type      TEXT    NOT NULL CHECK(q_type IN ('open','closed')),
                correct_ans TEXT    NOT NULL,
                FOREIGN KEY (test_id) REFERENCES tests(id) ON DELETE CASCADE,
                UNIQUE(test_id, q_num)
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                test_id       INTEGER NOT NULL,
                correct_count INTEGER NOT NULL DEFAULT 0,
                rush_score    REAL    NOT NULL DEFAULT 0,
                submitted_at  TEXT    DEFAULT (datetime('now')),
                UNIQUE(user_id, test_id),
                FOREIGN KEY (user_id)  REFERENCES users(id),
                FOREIGN KEY (test_id)  REFERENCES tests(id)
            );

            CREATE TABLE IF NOT EXISTS sub_answers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sub_id     INTEGER NOT NULL,
                q_num      INTEGER NOT NULL,
                user_ans   TEXT,
                is_correct INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (sub_id) REFERENCES submissions(id) ON DELETE CASCADE
            );
        """)

        # ── Mavjud DB uchun migratsiya ──────────────────────────────
        # owner_id → owner_tg_id (SQLite 3.25+)
        try:
            conn.execute("ALTER TABLE tests RENAME COLUMN owner_id TO owner_tg_id")
            logger.info("Migration: owner_id → owner_tg_id")
        except Exception:
            pass  # Allaqachon o'zgartirilgan yoki ustun yo'q

        # rush_calculated ustuni qo'shish
        try:
            conn.execute("ALTER TABLE tests ADD COLUMN rush_calculated INTEGER NOT NULL DEFAULT 0")
            logger.info("Migration: rush_calculated ustuni qo'shildi")
        except Exception:
            pass  # Allaqachon mavjud

    logger.info("✅ Ma'lumotlar bazasi tayyor: %s", DB_PATH)

# ================================================================
#                  VAQT YORDAMCHI FUNKSIYALAR
# ================================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def parse_dt(s: str) -> datetime:
    """ISO 8601 string → timezone-aware datetime.
       'Z' ham, '+00:00' ham qabul qilinadi."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def fmt_dt(s: str) -> str:
    """ISO string → O'zbekcha ko'rsatish formati"""
    try:
        dt = parse_dt(s).astimezone(UZ_TZ)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        logger.warning("fmt_dt: noto'g'ri format: %r", s)
        return s  # Xatolikda asl stringni qaytaradi

# FIX #2: String solishtirish o'rniga datetime ob'ektlari bilan solishtirish
def check_test_status(test) -> str:
    """Test statusini qaytaradi: pending | active | expired | invalid"""
    if not test["start_time"] or not test["end_time"]:
        return "invalid"
    try:
        start = parse_dt(test["start_time"])
        end   = parse_dt(test["end_time"])
    except Exception:
        logger.error("check_test_status: vaqt parse xatosi, test_id=%s", test["id"])
        return "invalid"
    now = now_utc()
    if now < start:
        return "pending"
    if now > end:
        return "expired"
    return "active"

# ================================================================
#            TELEGRAM INITDATA XAVFSIZLIK TEKSHIRUVI
# ================================================================
# FIX #5: tg.initDataUnsafe o'rniga server-side HMAC-SHA256 tekshiruvi
def verify_init_data(init_data: str) -> dict | None:
    """
    Telegram WebApp initData ni HMAC-SHA256 bilan tekshiradi.
    Muvaffaqiyatli bo'lsa user dict qaytaradi, aks holda None.
    Hujjat: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not BOT_TOKEN:
        return None
    try:
        params: dict[str, str] = {}
        for pair in init_data.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[urllib.parse.unquote_plus(k)] = urllib.parse.unquote_plus(v)

        received_hash = params.pop("hash", None)
        if not received_hash:
            return None

        # data-check-string: kalitlar alfavit tartibida, "\n" bilan ajratilgan
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))

        # HMAC kaliti: HMAC-SHA256("WebAppData", bot_token)
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed   = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed, received_hash):
            logger.warning("initData HMAC tekshiruvi rad etildi!")
            return None

        user_json = params.get("user", "{}")
        return json.loads(user_json)
    except Exception as e:
        logger.error("verify_init_data xato: %s", e)
        return None

def get_verified_user() -> dict | None:
    """Flask request'dagi X-Telegram-Init-Data headerini tekshiradi"""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return verify_init_data(init_data)

# ================================================================
#                   RUSH MODEL SCORING
# ================================================================
def calculate_rush_scores(test_id: int):
    """
    RUSH Model:
      weight_i  = N / correct_count_i   (noyob javob qimmatroq)
      rush_score = (user_raw / max_possible) * 100
    """
    with get_db() as conn:
        subs = conn.execute(
            "SELECT id FROM submissions WHERE test_id=?", (test_id,)
        ).fetchall()
        N = len(subs)
        if N == 0:
            return

        q_nums = [
            r["q_num"] for r in conn.execute(
                "SELECT q_num FROM questions WHERE test_id=? ORDER BY q_num", (test_id,)
            ).fetchall()
        ]

        # Har bir savol uchun to'g'ri javob soni
        correct_counts: dict[int, int] = {}
        for qn in q_nums:
            cnt = conn.execute("""
                SELECT COUNT(*) AS c FROM sub_answers sa
                JOIN submissions s ON sa.sub_id = s.id
                WHERE s.test_id=? AND sa.q_num=? AND sa.is_correct=1
            """, (test_id, qn)).fetchone()["c"]
            correct_counts[qn] = max(cnt, 1)  # 0 ga bo'lishdan saqlaydi

        max_raw = sum(N / correct_counts[qn] for qn in q_nums)
        if max_raw == 0:
            return

        for sub in subs:
            raw = 0.0
            for qn in q_nums:
                row = conn.execute(
                    "SELECT is_correct FROM sub_answers WHERE sub_id=? AND q_num=?",
                    (sub["id"], qn)
                ).fetchone()
                if row and row["is_correct"]:
                    raw += N / correct_counts[qn]
            rush_score = round((raw / max_raw) * 100, 4)
            conn.execute(
                "UPDATE submissions SET rush_score=? WHERE id=?",
                (rush_score, sub["id"])
            )

        # FIX #8: Bir marta hisoblangandan keyin flag'ni o'rnatish
        conn.execute("UPDATE tests SET rush_calculated=1 WHERE id=?", (test_id,))
        logger.info("RUSH scores hisoblandi: test_id=%s, N=%s", test_id, N)

def maybe_calculate_rush(test_id: int, test) -> None:
    """FIX #8: Faqat test tugagandan keyin va faqat bir marta hisoblaydi"""
    if test["rush_calculated"]:
        return
    if check_test_status(test) != "expired":
        return
    calculate_rush_scores(test_id)

# ================================================================
#              BACKGROUND TELEGRAM XABAR YUBORISH
# ================================================================
def _send_tg_message(chat_id: int, text: str) -> None:
    """Telegram Bot API orqali xabar yuborish (background thread'da)"""
    try:
        http_client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        logger.warning("Telegram xabar yuborishda xato (chat_id=%s): %s", chat_id, e)

def send_tg_async(chat_id: int, text: str) -> None:
    """FIX #9 davomi: Flask thread'ini bloklаmasdan xabar yuboradi"""
    threading.Thread(
        target=_send_tg_message, args=(chat_id, text), daemon=True
    ).start()

# ================================================================
#                     HTML SAHIFALAR
# ================================================================

# ---- Foydalanuvchi: Test Tekshirish ----
# FIX #1: tg.sendData() → fetch('/api/test/submit') + tg.initData header
#         InlineKeyboardButton bilan ochilgan WebApp'da sendData() ishlamaydi!
USER_TEST_HTML = r"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Test Tekshirish</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--blue:#1a73e8;--green:#27ae60;--red:#e74c3c;--purple:#8e44ad}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#f0f2f5;color:#333;min-height:100vh}
.container{max-width:600px;margin:0 auto;padding:14px}
h2{text-align:center;color:var(--blue);margin-bottom:18px;font-size:20px}
.card{background:#fff;border-radius:14px;padding:18px;margin-bottom:14px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
input[type=text]{width:100%;padding:13px 14px;border:2px solid #dde0e8;border-radius:10px;font-size:16px;transition:.2s}
input[type=text]:focus{border-color:var(--blue);outline:none;box-shadow:0 0 0 3px rgba(26,115,232,.1)}
.btn{width:100%;padding:14px;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;margin-top:10px;transition:.2s}
.btn:disabled{opacity:.6;cursor:not-allowed}
.btn-blue{background:var(--blue);color:#fff}.btn-blue:hover:not(:disabled){background:#1557b0}
.btn-green{background:var(--green);color:#fff}.btn-green:hover:not(:disabled){background:#229954}
.q-block{background:#fff;border-radius:12px;padding:14px;margin-bottom:10px;
  box-shadow:0 1px 6px rgba(0,0,0,.07);border-left:4px solid var(--blue)}
.q-block.closed{border-left-color:var(--purple)}
.q-label{font-weight:700;color:var(--blue);margin-bottom:10px;font-size:15px}
.q-label.closed-label{color:var(--purple)}
.opts{display:flex;gap:8px}
.opt-btn{flex:1;min-width:52px;padding:11px 4px;border:2px solid #dde0e8;border-radius:9px;
  background:#fff;cursor:pointer;font-size:16px;font-weight:700;text-align:center;transition:.15s}
.opt-btn:hover{border-color:var(--blue);background:#f0f5ff}
.opt-btn.sel{background:var(--blue);color:#fff;border-color:var(--blue)}
.closed-inp{width:100%;padding:11px 13px;border:2px solid #dde0e8;border-radius:9px;
  font-size:15px;margin-top:6px;transition:.2s}
.closed-inp:focus{border-color:var(--purple);outline:none}
.badge{display:inline-flex;align-items:center;gap:4px;padding:5px 12px;
  border-radius:20px;font-size:13px;font-weight:600}
.badge-blue{background:#e8f0fe;color:var(--blue)}
.error{color:var(--red);font-size:13px;margin-top:6px}
.hidden{display:none!important}
.loading{text-align:center;padding:40px;color:#999;font-size:15px}
.summary-bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.chip{background:#f5f7ff;border:1px solid #e0e6ff;border-radius:8px;padding:6px 12px;font-size:13px;color:#555}
.success-box{text-align:center;padding:30px 20px;background:#eafaf1;
  border-radius:14px;border:2px solid #27ae60}
.detail-line{text-align:left;padding:6px 0;font-size:15px;line-height:1.7;border-bottom:1px dashed #d5f5e3}
.detail-line:last-child{border-bottom:none}
</style>
</head>
<body>
<div class="container">
  <h2>📝 Test Tekshirish</h2>

  <div id="step1" class="card">
    <p style="color:#666;margin-bottom:10px;font-size:15px;">Test kodini kiriting:</p>
    <input id="testCode" type="text" placeholder="Masalan: ONA2024A" autocomplete="off"
           onkeydown="if(event.key==='Enter')loadTest()" />
    <div id="codeErr" class="error"></div>
    <button class="btn btn-blue" onclick="loadTest()">Davom etish →</button>
  </div>

  <div id="loadingDiv" class="loading hidden">⏳ Yuklanmoqda...</div>

  <div id="step2" class="hidden">
    <div class="card">
      <div class="summary-bar">
        <span class="badge badge-blue" id="subjectBadge">📚 Fan</span>
        <span class="chip" id="openInfo"></span>
        <span class="chip" id="closedInfo"></span>
      </div>
    </div>
    <div id="questionsContainer"></div>
    <button id="submitBtn" class="btn btn-green" onclick="submitAnswers()" style="margin-bottom:24px">
      ✅ Javoblarni Yuborish
    </button>
  </div>

  <div id="successDiv" class="hidden">
    <div class="success-box">
      <div style="font-size:56px;margin-bottom:14px">✅</div>
      <h3 style="color:#27ae60;margin-bottom:16px;font-size:20px">Javoblar qabul qilindi!</h3>
      <div id="successDetails"></div>
      <button onclick="tg.close()" style="
        margin-top:20px;background:#27ae60;color:#fff;border:none;
        border-radius:10px;padding:13px 32px;font-size:15px;font-weight:700;
        cursor:pointer;width:100%">Yopish ✕</button>
    </div>
  </div>
</div>

<script>
const tg = window.Telegram.WebApp;
tg.expand(); tg.ready();

let testData = null;
const answers = {};

async function loadTest() {
  const code = document.getElementById('testCode').value.trim().toUpperCase();
  const errDiv = document.getElementById('codeErr');
  errDiv.textContent = '';
  if (!code) { errDiv.textContent = '❗ Test kodini kiriting!'; return; }

  show('loadingDiv'); hide('step1');
  try {
    const r = await fetch('/api/test/' + encodeURIComponent(code));
    const d = await r.json();
    hide('loadingDiv');
    if (!d.success) { show('step1'); errDiv.textContent = '❌ ' + d.message; return; }
    testData = d.test;
    renderQuestions(testData);
    show('step2');
  } catch(e) {
    hide('loadingDiv'); show('step1');
    errDiv.textContent = "❌ Xatolik. Qaytadan urinib ko'ring.";
  }
}

function renderQuestions(t) {
  // XSS-safe: textContent ishlatiladi
  document.getElementById('subjectBadge').textContent = '📚 ' + t.subject;
  document.getElementById('openInfo').textContent   = 'Ochiq: ' + t.open_count + ' ta';
  document.getElementById('closedInfo').textContent = 'Yopiq: ' + t.closed_count + ' ta';

  const c = document.getElementById('questionsContainer');
  c.innerHTML = '';
  const total = t.open_count + t.closed_count;

  for (let i = 1; i <= total; i++) {
    const isOpen = i <= t.open_count;
    const blk = document.createElement('div');
    blk.className = 'q-block' + (isOpen ? '' : ' closed');

    const lbl = document.createElement('div');
    lbl.className = 'q-label' + (isOpen ? '' : ' closed-label');
    lbl.textContent = (isOpen ? '🔵 ' : '🟣 ') + i + '-savol' + (isOpen ? '' : ' (yopiq)');
    blk.appendChild(lbl);

    if (isOpen) {
      const opts = document.createElement('div');
      opts.className = 'opts';
      ['A','B','C','D'].forEach(function(o) {
        const btn = document.createElement('button');
        btn.className = 'opt-btn';
        btn.textContent = o;
        btn.addEventListener('click', (function(qn, opt, b){
          return function() { selOpt(qn, opt, b); };
        })(i, o, btn));
        opts.appendChild(btn);
      });
      blk.appendChild(opts);
    } else {
      const inp = document.createElement('input');
      inp.className = 'closed-inp';
      inp.type = 'text';
      inp.placeholder = "Javobingizni kiriting...";
      inp.addEventListener('input', (function(qn){ return function(){ answers[qn] = this.value.trim().toUpperCase(); }; })(i));
      blk.appendChild(inp);
    }
    c.appendChild(blk);
  }
}

function selOpt(qn, opt, btn) {
  btn.closest('.opts').querySelectorAll('.opt-btn').forEach(b => b.classList.remove('sel'));
  btn.classList.add('sel');
  answers[qn] = opt;
}

async function submitAnswers() {
  if (!testData) return;
  const total = testData.open_count + testData.closed_count;
  const miss = [];
  for (let i = 1; i <= total; i++) {
    if (!answers[i] || answers[i] === '') miss.push(i);
  }
  if (miss.length > 0) {
    const names = miss.slice(0, 5).join(', ') + (miss.length > 5 ? '...' : '');
    if (!confirm(miss.length + ' ta savol javobsiz qoldi (' + names + ').\nShunga qaramay yuborasizmi?')) return;
  }

  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Yuborilmoqda...';

  try {
    // FIX #1: tg.sendData() EMAS — fetch() + initData header ishlatiladi
    const resp = await fetch('/api/test/submit', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Init-Data': tg.initData   // Server tomonda HMAC tekshiriladi
      },
      body: JSON.stringify({ test_code: testData.code, answers: answers })
    });
    const result = await resp.json();

    if (result.success) {
      hide('step2');
      // FIX #19: XSS-safe — innerHTML o'rniga textContent
      const det = document.getElementById('successDetails');
      det.innerHTML = '';
      const lines = [
        '📚 Fan: ' + result.subject,
        '🔑 Kod: ' + result.code,
        "📊 To'g'ri: " + result.correct + '/' + result.total + ' (' + result.pct + '%)',
        "🏆 RUSH ball ega natijalarni e'lon qilganda ma'lum bo'ladi."
      ];
      lines.forEach(function(line) {
        const p = document.createElement('div');
        p.className = 'detail-line';
        p.textContent = line;
        det.appendChild(p);
      });
      show('successDiv');
    } else {
      btn.disabled = false;
      btn.textContent = '✅ Javoblarni Yuborish';
      alert('❌ ' + result.message);
    }
  } catch(err) {
    btn.disabled = false;
    btn.textContent = '✅ Javoblarni Yuborish';
    alert("❌ Tarmoq xatosi. Qaytadan urinib ko'ring.");
    console.error(err);
  }
}

function show(id) { document.getElementById(id).classList.remove('hidden'); }
function hide(id) { document.getElementById(id).classList.add('hidden'); }
</script>
</body>
</html>"""


# ---- Ega: Test Yaratish ----
# FIX #1/#5: owner_tg_id JSON body'dan olib tashlandi — initData headerdan olinadi
# FIX #19: success ekranda DOM manipulation, innerHTML emas
# FIX (JS syntax): alert string apostrofi to'g'rilandi
OWNER_CREATE_HTML = r"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Test Yaratish</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--orange:#e67e22;--green:#27ae60;--blue:#1a73e8;--red:#e74c3c;--purple:#8e44ad}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#f8f4ef;color:#333}
.container{max-width:640px;margin:0 auto;padding:14px}
h2{text-align:center;color:var(--orange);margin-bottom:18px;font-size:20px}
.card{background:#fff;border-radius:14px;padding:18px;margin-bottom:14px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
label{display:block;font-weight:600;margin-bottom:5px;color:#555;font-size:14px}
input[type=text],input[type=number],input[type=datetime-local]{
  width:100%;padding:12px 13px;border:2px solid #dde0e8;border-radius:10px;font-size:15px;transition:.2s}
input:focus{border-color:var(--orange);outline:none;box-shadow:0 0 0 3px rgba(230,126,34,.1)}
.row{display:flex;gap:12px}.row .fg{flex:1}
.fg{margin-bottom:14px}
.btn{width:100%;padding:13px;border:none;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;margin-top:8px;transition:.2s}
.btn:disabled{opacity:.6;cursor:not-allowed}
.btn-ora{background:var(--orange);color:#fff}.btn-ora:hover:not(:disabled){background:#d35400}
.btn-grn{background:var(--green);color:#fff}.btn-grn:hover:not(:disabled){background:#229954}
.btn-gry{background:#95a5a6;color:#fff}.btn-gry:hover{background:#7f8c8d}
.q-block{background:#fafafa;border:1px solid #eee;border-radius:10px;padding:13px;margin-bottom:9px}
.q-lbl{font-weight:700;margin-bottom:9px;font-size:14px}
.opts{display:flex;gap:7px}
.opt-btn{flex:1;padding:10px 2px;border:2px solid #dde0e8;border-radius:8px;background:#fff;
  cursor:pointer;font-size:15px;font-weight:700;text-align:center;transition:.15s}
.opt-btn:hover{border-color:var(--green);background:#eafaf1}
.opt-btn.sel{background:var(--green);color:#fff;border-color:var(--green)}
.cl-inp{width:100%;padding:10px 12px;border:2px solid #dde0e8;border-radius:8px;
  font-size:14px;margin-top:6px}
.cl-inp:focus{border-color:var(--purple);outline:none}
.sec-hdr{font-weight:700;padding:8px 0 10px;margin-bottom:8px;border-bottom:2px dashed #eee}
.sec-open{color:var(--green)}.sec-closed{color:var(--purple)}
.error{color:var(--red);font-size:12px;margin-top:3px}
.hidden{display:none!important}
.step-bar{display:flex;justify-content:center;gap:6px;margin-bottom:18px}
.step-dot{width:30px;height:5px;border-radius:3px;background:#ddd;transition:.3s}
.step-dot.active{background:var(--orange)}
.info-box{background:#fff8f0;border:1px solid #ffd8a8;border-radius:10px;padding:12px;
  font-size:13px;color:#8a5010;margin-bottom:12px}
</style>
</head>
<body>
<div class="container">
  <h2>➕ Test Yaratish</h2>
  <div class="step-bar">
    <div class="step-dot active" id="dot1"></div>
    <div class="step-dot" id="dot2"></div>
    <div class="step-dot" id="dot3"></div>
  </div>

  <!-- === QADAM 1: Meta ma'lumotlar === -->
  <div id="step1">
    <div class="card">
      <div class="sec-hdr sec-open">📋 Test Ma'lumotlari</div>
      <div class="fg">
        <label>Test Kodi *</label>
        <input id="testCode" type="text" placeholder="Masalan: ONA2024A" autocomplete="off"/>
        <div class="error" id="codeErr"></div>
      </div>
      <div class="row">
        <div class="fg">
          <label>Ochiq savollar soni</label>
          <input id="openCount" type="number" min="0" value="0"/>
        </div>
        <div class="fg">
          <label>Yopiq savollar soni</label>
          <input id="closedCount" type="number" min="0" value="0"/>
        </div>
      </div>
      <div class="fg">
        <label>Fan nomi *</label>
        <input id="subject" type="text" placeholder="Masalan: Ona tili"/>
      </div>
      <button class="btn btn-ora" onclick="step1Next()">Davom etish →</button>
    </div>
  </div>

  <!-- === QADAM 2: Javoblar === -->
  <div id="step2" class="hidden">
    <div class="info-box" id="metaInfo"></div>
    <div class="card">
      <div id="answersContainer"></div>
    </div>
    <button class="btn btn-gry" onclick="goStep(1)" style="margin-bottom:6px">← Orqaga</button>
    <button class="btn btn-ora" onclick="step2Next()">Davom etish →</button>
  </div>

  <!-- === QADAM 3: Vaqt sozlamalari === -->
  <div id="step3" class="hidden">
    <div class="card">
      <div class="sec-hdr" style="color:var(--blue)">🕐 Vaqt Sozlamalari</div>
      <div class="fg">
        <label>Test boshlanish vaqti *</label>
        <input id="startTime" type="datetime-local"/>
      </div>
      <div class="fg">
        <label>Test davomiyligi (daqiqa) *</label>
        <input id="duration" type="number" min="1" value="90" placeholder="Masalan: 90"/>
      </div>
      <div class="info-box" style="background:#eef5ff;border-color:#b3cfff;color:#1a3a6e">
        ℹ️ Belgilangan vaqt tugagandan so'ng test <strong>avtomatik yopiladi</strong>.
      </div>
    </div>
    <button class="btn btn-gry" onclick="goStep(2)" style="margin-bottom:6px">← Orqaga</button>
    <button id="saveBtn" class="btn btn-grn" onclick="saveTest()">💾 Testni Saqlash</button>
  </div>
</div>

<script>
const tg = window.Telegram.WebApp;
tg.expand(); tg.ready();

let meta = {}, answers = {};

function show(id) { document.getElementById(id).classList.remove('hidden'); }
function hide(id) { document.getElementById(id).classList.add('hidden'); }

function goStep(n) {
  [1,2,3].forEach(function(i) {
    document.getElementById('step' + i).classList[i === n ? 'remove' : 'add']('hidden');
    document.getElementById('dot'  + i).classList[i === n ? 'add'    : 'remove']('active');
  });
}

function step1Next() {
  const code   = document.getElementById('testCode').value.trim().toUpperCase();
  const open   = parseInt(document.getElementById('openCount').value)   || 0;
  const closed = parseInt(document.getElementById('closedCount').value) || 0;
  const subj   = document.getElementById('subject').value.trim();
  const errDiv = document.getElementById('codeErr');
  errDiv.textContent = '';
  if (!code) { errDiv.textContent = 'Test kodi kiritilishi shart!'; return; }
  if (!/^[A-Z0-9]{3,20}$/.test(code)) {
    errDiv.textContent = "Kod 3-20 ta harf/raqamdan iborat bo'lsin!"; return;
  }
  if (open + closed === 0) { alert("Kamida 1 ta savol bo'lishi kerak!"); return; }
  if (!subj) { alert('Fan nomini kiriting!'); return; }
  meta = { code: code, open_count: open, closed_count: closed, subject: subj };
  renderAnswers();
  // XSS-safe: textContent ishlatiladi
  document.getElementById('metaInfo').textContent =
    '\uD83D\uDCCB ' + code + ' | \uD83D\uDCDA ' + subj +
    ' | Ochiq: ' + open + ' | Yopiq: ' + closed + ' | Jami: ' + (open + closed) + ' ta';
  goStep(2);
}

function renderAnswers() {
  const c = document.getElementById('answersContainer');
  c.innerHTML = '';
  const op = meta.open_count, cl = meta.closed_count;
  if (op > 0) {
    const h = document.createElement('div');
    h.className = 'sec-hdr sec-open';
    h.textContent = "\uD83D\uDCD7 Ochiq savollar (1-" + op + ')';
    c.appendChild(h);
    for (let i = 1; i <= op; i++) {
      const blk = document.createElement('div');
      blk.className = 'q-block';
      const lbl = document.createElement('div');
      lbl.className = 'q-lbl';
      lbl.style.color = '#27ae60';
      lbl.textContent = i + '-savol';
      blk.appendChild(lbl);
      const opts = document.createElement('div');
      opts.className = 'opts';
      ['A','B','C','D'].forEach(function(o) {
        const btn = document.createElement('button');
        btn.className = 'opt-btn' + (answers[i] === o ? ' sel' : '');
        btn.textContent = o;
        btn.addEventListener('click', (function(qn, opt, b){ return function(){ selAns(qn, opt, b); }; })(i, o, btn));
        opts.appendChild(btn);
      });
      blk.appendChild(opts);
      c.appendChild(blk);
    }
  }
  if (cl > 0) {
    const h2 = document.createElement('div');
    h2.className = 'sec-hdr sec-closed';
    h2.style.marginTop = '14px';
    h2.textContent = "\uD83D\uDCD8 Yopiq savollar (" + (op + 1) + '-' + (op + cl) + ')';
    c.appendChild(h2);
    for (let i = op + 1; i <= op + cl; i++) {
      const blk = document.createElement('div');
      blk.className = 'q-block';
      const lbl = document.createElement('div');
      lbl.className = 'q-lbl';
      lbl.style.color = '#8e44ad';
      lbl.textContent = i + '-savol (yopiq)';
      blk.appendChild(lbl);
      const inp = document.createElement('input');
      inp.className = 'cl-inp';
      inp.type = 'text';
      inp.placeholder = "To'g'ri javobni kiriting...";
      inp.value = answers[i] || '';
      inp.addEventListener('input', (function(qn){ return function(){ answers[qn] = this.value.trim().toUpperCase(); }; })(i));
      blk.appendChild(inp);
      c.appendChild(blk);
    }
  }
}

function selAns(qn, opt, btn) {
  btn.closest('.opts').querySelectorAll('.opt-btn').forEach(function(b){ b.classList.remove('sel'); });
  btn.classList.add('sel');
  answers[qn] = opt;
}

// Lokal datetime-local → Date (browser lokal vaqtida)
function localStrToDate(val) {
  const parts = val.split('T');
  const date = parts[0].split('-').map(Number);
  const time = (parts[1] || '00:00').split(':').map(Number);
  return new Date(date[0], date[1]-1, date[2], time[0], time[1]);
}
function dateToLocalStr(dt) {
  const p = function(n){ return String(n).padStart(2,'0'); };
  return dt.getFullYear() + '-' + p(dt.getMonth()+1) + '-' + p(dt.getDate()) +
         'T' + p(dt.getHours()) + ':' + p(dt.getMinutes());
}

function step2Next() {
  const total = meta.open_count + meta.closed_count;
  const miss = [];
  for (let i = 1; i <= total; i++) {
    if (!answers[i] || !answers[i].trim()) miss.push(i);
  }
  if (miss.length > 0) { alert('❌ Javobsiz savollar: ' + miss.join(', ')); return; }
  const now = new Date();
  const def = new Date(now.getTime() + 10 * 60000);
  const dt = document.getElementById('startTime');
  if (!dt.value) dt.value = dateToLocalStr(def);
  dt.min = dateToLocalStr(now);
  goStep(3);
}

async function saveTest() {
  const st  = document.getElementById('startTime').value;
  const dur = parseInt(document.getElementById('duration').value);
  if (!st)       { alert('Boshlanish vaqtini kiriting!'); return; }
  if (!dur||dur<1){ alert('Davomiylikni kiriting!'); return; }

  const startDt = localStrToDate(st);
  const endDt   = new Date(startDt.getTime() + dur * 60000);

  const btn = document.getElementById('saveBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Saqlanmoqda...';

  // FIX #5: owner_tg_id body'dan olib tashlandi — server initData'dan oladi
  const payload = {
    test_code:    meta.code,
    open_count:   meta.open_count,
    closed_count: meta.closed_count,
    subject:      meta.subject,
    answers:      answers,
    start_time:   startDt.toISOString(),
    end_time:     endDt.toISOString()
  };

  try {
    const resp = await fetch('/api/test/create', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Telegram-Init-Data': tg.initData   // Server tomonda HMAC tekshiriladi
      },
      body: JSON.stringify(payload)
    });
    const result = await resp.json();

    if (result.success) {
      // FIX #19: DOM manipulation — innerHTML + untrusted data emas
      const container = document.querySelector('.container');
      container.innerHTML = '';

      const wrap = document.createElement('div');
      wrap.style.cssText = 'text-align:center;padding:40px 20px';

      const emojiEl = document.createElement('div');
      emojiEl.style.cssText = 'font-size:64px;margin-bottom:16px';
      emojiEl.textContent = '\uD83C\uDF89';
      wrap.appendChild(emojiEl);

      const titleEl = document.createElement('h2');
      titleEl.style.cssText = 'color:#27ae60;margin-bottom:20px';
      titleEl.textContent = 'Test Saqlandi!';
      wrap.appendChild(titleEl);

      const infoBox = document.createElement('div');
      infoBox.style.cssText = 'background:#eafaf1;border:2px solid #27ae60;border-radius:14px;padding:20px;text-align:left;margin-bottom:24px';
      [
        ['\uD83D\uDD11', 'Kod',       result.code],
        ['\uD83D\uDCDA', 'Fan',       result.subject],
        ['\uD83D\uDCCA', 'Jami',      result.total + ' ta savol'],
        ['\uD83D\uDD50', 'Boshlanish',result.start],
        ['\uD83D\uDD51', 'Tugash',    result.end]
      ].forEach(function(row) {
        const p = document.createElement('div');
        p.style.cssText = 'margin-bottom:10px;font-size:15px';
        p.textContent = row[0] + ' ' + row[1] + ': ' + row[2];
        infoBox.appendChild(p);
      });
      wrap.appendChild(infoBox);

      const note = document.createElement('p');
      note.style.cssText = 'color:#666;font-size:14px;margin-bottom:20px';
      note.textContent = 'Chatda ham tasdiqlash xabari yuborildi \u2705';
      wrap.appendChild(note);

      const closeBtn = document.createElement('button');
      closeBtn.style.cssText = 'background:#27ae60;color:#fff;border:none;border-radius:10px;padding:14px 40px;font-size:16px;font-weight:700;cursor:pointer;width:100%';
      closeBtn.textContent = '\u2705 Yopish';
      closeBtn.onclick = function(){ tg.close(); };
      wrap.appendChild(closeBtn);

      container.appendChild(wrap);
    } else {
      btn.disabled = false;
      btn.textContent = '\uD83D\uDCBE Testni Saqlash';
      alert('❌ Xatolik: ' + result.message);
    }
  } catch(err) {
    btn.disabled = false;
    btn.textContent = '\uD83D\uDCBE Testni Saqlash';
    // FIX (JS syntax): apostrofli alert string tuzatildi
    alert("❌ Tarmoq xatosi. Qaytadan urinib ko'ring.");
    console.error(err);
  }
}
</script>
</body>
</html>"""


# ---- Ega: Natijalar ----
# FIX #18/#5: initData header bilan owner tekshiruvi qo'shildi
OWNER_RESULTS_HTML = r"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Natijalar</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--purple:#8e44ad;--blue:#1a73e8;--green:#27ae60;--orange:#e67e22;--red:#e74c3c;--gold:#f1c40f}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#f5f0f8;color:#333}
.container{max-width:640px;margin:0 auto;padding:14px}
h2{text-align:center;color:var(--purple);margin-bottom:18px;font-size:20px}
.card{background:#fff;border-radius:14px;padding:18px;margin-bottom:14px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
input[type=text]{width:100%;padding:12px 14px;border:2px solid #dde0e8;border-radius:10px;font-size:15px;transition:.2s}
input:focus{border-color:var(--purple);outline:none}
.btn{padding:12px 20px;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;transition:.2s}
.btn-full{width:100%;margin-top:8px}
.btn-pur{background:var(--purple);color:#fff}.btn-pur:hover{background:#7d3c98}
.btn-gry{background:#95a5a6;color:#fff}
.tab-bar{display:flex;gap:8px;margin-bottom:14px}
.tab{flex:1;padding:10px;border:2px solid #dde0e8;border-radius:10px;background:#fff;
  cursor:pointer;text-align:center;font-size:13px;font-weight:600;transition:.2s}
.tab.active{background:var(--purple);color:#fff;border-color:var(--purple)}
.q-row{margin-bottom:13px}
.q-row-hdr{display:flex;justify-content:space-between;margin-bottom:4px;font-size:14px}
.q-num{font-weight:700}
.q-cnt{color:var(--green);font-weight:600;font-size:13px}
.bar-bg{height:8px;background:#eee;border-radius:4px}
.bar-fill{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--green),#52d68a);transition:width .4s}
.rank-row{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #f5f0f8}
.rank-row:last-child{border-bottom:none}
.medal{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:13px;flex-shrink:0}
.m1{background:var(--gold);color:#333}.m2{background:#bdc3c7;color:#333}
.m3{background:#cd7f32;color:#fff}.mx{background:#ecf0f1;color:#888}
.user-name{font-weight:700;font-size:14px}
.user-stats{font-size:12px;color:#888;margin-top:2px}
.rush-badge{background:#f3e8ff;color:var(--purple);border-radius:6px;padding:2px 8px;font-size:12px;font-weight:700}
.info-row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.chip{background:#f5f0f8;border:1px solid #ddd;border-radius:8px;padding:5px 12px;font-size:13px;color:#666}
.error{color:var(--red);font-size:13px;margin-top:6px}
.hidden{display:none!important}
.loading{text-align:center;padding:30px;color:#999}
.total-badge{background:#e8f4fd;color:var(--blue);border-radius:20px;padding:4px 14px;font-size:14px;font-weight:700}
</style>
</head>
<body>
<div class="container">
  <h2>📊 Natijalar</h2>

  <div id="inputSection" class="card">
    <p style="color:#666;margin-bottom:10px;font-size:15px">Test kodini kiriting:</p>
    <input id="codeInput" type="text" placeholder="Test kodi" autocomplete="off"
           onkeydown="if(event.key==='Enter')loadResults()"/>
    <div id="inputErr" class="error"></div>
    <button class="btn btn-pur btn-full" onclick="loadResults()">Ko'rish →</button>
  </div>

  <div id="loadDiv" class="loading hidden">⏳ Yuklanmoqda...</div>

  <div id="resultsSection" class="hidden">
    <div class="card">
      <div class="info-row">
        <span class="chip" id="subjectChip">📚</span>
        <span class="chip" id="codeChip">🔑</span>
        <span class="total-badge" id="totalBadge">👥 0 ta</span>
      </div>
    </div>
    <div class="tab-bar">
      <button class="tab active" onclick="switchTab('q')">📋 Savollar bo'yicha</button>
      <button class="tab" onclick="switchTab('u')">🏆 Foydalanuvchilar</button>
    </div>
    <div id="qTab" class="card"><div id="qStats"></div></div>
    <div id="uTab" class="card hidden"><div id="uRanks"></div></div>
    <button class="btn btn-gry btn-full" onclick="resetView()">← Orqaga</button>
  </div>
</div>

<script>
const tg = window.Telegram.WebApp;
tg.expand(); tg.ready();

async function loadResults() {
  const code = document.getElementById('codeInput').value.trim().toUpperCase();
  const err = document.getElementById('inputErr');
  err.textContent = '';
  if (!code) return;
  show('loadDiv'); hide('inputSection');
  try {
    // FIX #18/#5: initData header qo'shildi — server owner tekshiradi
    const r = await fetch('/api/results/' + encodeURIComponent(code), {
      headers: { 'X-Telegram-Init-Data': tg.initData }
    });
    const d = await r.json();
    hide('loadDiv');
    if (!d.success) { show('inputSection'); err.textContent = '❌ ' + d.message; return; }
    renderAll(d);
    show('resultsSection');
  } catch(e) {
    hide('loadDiv'); show('inputSection');
    err.textContent = '❌ Xatolik yuz berdi';
  }
}

function renderAll(d) {
  // XSS-safe: textContent
  document.getElementById('subjectChip').textContent = '\uD83D\uDCDA ' + d.test.subject;
  document.getElementById('codeChip').textContent    = '\uD83D\uDD11 ' + d.test.code;
  document.getElementById('totalBadge').textContent  = '\uD83D\uDC65 ' + d.total + ' ta qatnashuvchi';
  renderQStats(d);
  renderURanks(d);
}

function renderQStats(d) {
  const c = document.getElementById('qStats');
  c.innerHTML = '';
  const hdr = document.createElement('div');
  hdr.style.cssText = 'font-weight:700;margin-bottom:14px;color:#555';
  hdr.textContent = 'Jami ' + d.questions.length + ' ta savol';
  c.appendChild(hdr);
  const N = d.total || 1;
  d.questions.forEach(function(q) {
    const pct = N > 0 ? Math.round(q.correct / N * 100) : 0;
    const row = document.createElement('div');
    row.className = 'q-row';
    const hdrRow = document.createElement('div');
    hdrRow.className = 'q-row-hdr';
    const qNum = document.createElement('span');
    qNum.className = 'q-num';
    qNum.textContent = q.q_num + '-savol (' + (q.q_type === 'open' ? 'ochiq' : 'yopiq') + ')';
    const qCnt = document.createElement('span');
    qCnt.className = 'q-cnt';
    qCnt.textContent = q.correct + ' ta \u2713 (' + pct + '%)';
    hdrRow.appendChild(qNum);
    hdrRow.appendChild(qCnt);
    const barBg = document.createElement('div');
    barBg.className = 'bar-bg';
    const barFill = document.createElement('div');
    barFill.className = 'bar-fill';
    barFill.style.width = pct + '%';
    barBg.appendChild(barFill);
    row.appendChild(hdrRow);
    row.appendChild(barBg);
    c.appendChild(row);
  });
}

function renderURanks(d) {
  const c = document.getElementById('uRanks');
  c.innerHTML = '';
  const hdr = document.createElement('div');
  hdr.style.cssText = 'font-weight:700;margin-bottom:14px;color:#555';
  hdr.textContent = d.test.subject + ' | ' + d.total + ' ta qatnashuvchi';
  c.appendChild(hdr);
  if (d.rankings.length === 0) {
    const p = document.createElement('p');
    p.style.cssText = 'color:#999;text-align:center';
    p.textContent = "Hali natijalar yo'q";
    c.appendChild(p); return;
  }
  d.rankings.forEach(function(u, idx) {
    const rank = idx + 1;
    const mc = rank===1?'m1':rank===2?'m2':rank===3?'m3':'mx';
    const row = document.createElement('div');
    row.className = 'rank-row';
    const medal = document.createElement('div');
    medal.className = 'medal ' + mc;
    medal.textContent = rank;
    const info = document.createElement('div');
    info.style.flex = '1';
    const name = document.createElement('div');
    name.className = 'user-name';
    name.textContent = u.full_name;
    const stats = document.createElement('div');
    stats.className = 'user-stats';
    stats.textContent = u.correct_count + " ta to'g'ri javob";
    info.appendChild(name);
    info.appendChild(stats);
    const badge = document.createElement('div');
    badge.className = 'rush-badge';
    badge.textContent = u.rush_score.toFixed(2) + ' RUSH';
    row.appendChild(medal);
    row.appendChild(info);
    row.appendChild(badge);
    c.appendChild(row);
  });
}

function switchTab(t) {
  document.querySelectorAll('.tab').forEach(function(b, i){
    b.classList.toggle('active', (i===0&&t==='q')||(i===1&&t==='u'));
  });
  document.getElementById('qTab').classList.toggle('hidden', t !== 'q');
  document.getElementById('uTab').classList.toggle('hidden', t !== 'u');
}

function resetView() {
  hide('resultsSection'); show('inputSection');
  document.getElementById('codeInput').value = '';
}

function show(id) { document.getElementById(id).classList.remove('hidden'); }
function hide(id) { document.getElementById(id).classList.add('hidden'); }
</script>
</body>
</html>"""


# ================================================================
#                      FLASK API
# ================================================================
flask_app = Flask(__name__)

@flask_app.route("/health")
def health_check():
    return jsonify({"status": "ok", "service": "Milliy Sertifikat Bot"}), 200

# ── Statik sahifalar ─────────────────────────────────────────────
@flask_app.route("/webapp/user/test")
def page_user_test():
    return USER_TEST_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

@flask_app.route("/webapp/owner/create")
def page_owner_create():
    return OWNER_CREATE_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

@flask_app.route("/webapp/owner/results")
def page_owner_results():
    return OWNER_RESULTS_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

# ── Test ma'lumotlarini olish ─────────────────────────────────────
@flask_app.route("/api/test/<code>")
def api_get_test(code: str):
    code = code.upper()
    with get_db() as conn:
        test = conn.execute(
            "SELECT * FROM tests WHERE code=? COLLATE NOCASE", (code,)
        ).fetchone()
        if not test:
            return jsonify({"success": False, "message": "Test topilmadi!"}), 404
        if not test["is_saved"]:
            return jsonify({"success": False, "message": "Test hali tayyor emas!"}), 400

        status = check_test_status(test)
        if status == "pending":
            return jsonify({"success": False, "message": f"Test {fmt_dt(test['start_time'])} da boshlanadi."}), 400
        if status in ("expired", "invalid"):
            return jsonify({"success": False, "message": "Test vaqti tugagan! ⌛"}), 400

        return jsonify({
            "success": True,
            "test": {
                "code":         test["code"],
                "subject":      test["subject"],
                "open_count":   test["open_count"],
                "closed_count": test["closed_count"]
            }
        })

# ── Test topshirish (FIX #1: tg.sendData() → fetch() route) ──────
@flask_app.route("/api/test/submit", methods=["POST"])
def api_submit_test():
    # FIX #5: Server-side HMAC tekshiruvi
    user_info = get_verified_user()
    if not user_info:
        return jsonify({"success": False, "message": "Autentifikatsiya muvaffaqiyatsiz!"}), 401

    user_tg_id = user_info.get("id")
    if not user_tg_id:
        return jsonify({"success": False, "message": "Foydalanuvchi ID topilmadi!"}), 401

    data = request.get_json(force=True)
    if not data:
        return jsonify({"success": False, "message": "Ma'lumot yo'q!"}), 400

    test_code   = str(data.get("test_code", "")).strip().upper()
    raw_answers = data.get("answers", {})
    # FIX #10: key tipini int ga normallashtirish
    answers = {int(k): str(v).strip().upper() for k, v in raw_answers.items() if v}

    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE telegram_id=?", (user_tg_id,)
        ).fetchone()
        if not user:
            return jsonify({"success": False, "message": "Avval /start buyrug'ini yuboring!"}), 400

        test = conn.execute(
            "SELECT * FROM tests WHERE code=? COLLATE NOCASE", (test_code,)
        ).fetchone()
        if not test:
            return jsonify({"success": False, "message": "Test topilmadi!"}), 404

        status = check_test_status(test)
        if status == "pending":
            return jsonify({"success": False, "message": f"Test hali boshlanmagan! Boshlanish: {fmt_dt(test['start_time'])}"}), 400
        if status in ("expired", "invalid"):
            return jsonify({"success": False, "message": "Test vaqti tugagan! ⌛"}), 400

        existing = conn.execute(
            "SELECT id FROM submissions WHERE user_id=? AND test_id=?",
            (user["id"], test["id"])
        ).fetchone()
        if existing:
            return jsonify({"success": False, "message": "Siz bu testni allaqachon topshirgansiz!"}), 409

        questions = conn.execute(
            "SELECT * FROM questions WHERE test_id=? ORDER BY q_num", (test["id"],)
        ).fetchall()

        correct_count  = 0
        answer_records = []
        total_q        = test["open_count"] + test["closed_count"]

        for q in questions:
            qn          = q["q_num"]
            user_ans    = answers.get(qn, "")
            correct_ans = q["correct_ans"].strip().upper()
            is_correct  = 1 if user_ans == correct_ans and user_ans != "" else 0
            if is_correct:
                correct_count += 1
            answer_records.append((qn, user_ans, is_correct))

        cursor = conn.execute(
            "INSERT INTO submissions (user_id, test_id, correct_count) VALUES (?,?,?)",
            (user["id"], test["id"], correct_count)
        )
        sub_id = cursor.lastrowid

        conn.executemany(
            "INSERT INTO sub_answers (sub_id, q_num, user_ans, is_correct) VALUES (?,?,?,?)",
            [(sub_id, qn, ans, isc) for qn, ans, isc in answer_records]
        )

    pct = round(correct_count / total_q * 100) if total_q > 0 else 0

    # Foydalanuvchiga Telegram xabari (background thread — Flask'ni bloklamas)
    details = []
    for qn, user_ans, is_correct in answer_records[:20]:
        icon = "✅" if is_correct else "❌"
        details.append(f"{icon} {qn}-savol: {user_ans or '—'}")
    detail_text = "\n".join(details)
    if len(answer_records) > 20:
        detail_text += f"\n... va yana {len(answer_records)-20} ta savol"

    msg_text = (
        f"✅ *Javoblaringiz qabul qilindi!*\n\n"
        f"📚 Fan: {test['subject']}\n"
        f"🔑 Test kodi: `{test_code}`\n"
        f"📊 To'g'ri javoblar: *{correct_count}/{total_q}* ta ({pct}%)\n\n"
        f"📋 *Javoblar:*\n{detail_text}\n\n"
        f"🏆 RUSH ball ega natijalarni e'lon qilganda ma'lum bo'ladi."
    )
    send_tg_async(user_tg_id, msg_text)

    return jsonify({
        "success": True,
        "code":    test_code,
        "subject": test["subject"],
        "correct": correct_count,
        "total":   total_q,
        "pct":     pct
    })

# ── Test yaratish ─────────────────────────────────────────────────
@flask_app.route("/api/test/create", methods=["POST"])
def api_create_test():
    # FIX #5: initData header'dan owner tekshiruvi (body'dagi owner_tg_id emas!)
    user_info = get_verified_user()
    if not user_info:
        return jsonify({"success": False, "message": "Autentifikatsiya muvaffaqiyatsiz!"}), 401

    owner_tg_id = user_info.get("id")
    if owner_tg_id not in OWNER_IDS:
        logger.warning("Ruxsatsiz test yaratish urinishi: tg_id=%s", owner_tg_id)
        return jsonify({"success": False, "message": "Ruxsat yo'q!"}), 403

    data = request.get_json(force=True)
    if not data:
        return jsonify({"success": False, "message": "Ma'lumot yo'q!"}), 400

    code         = str(data.get("test_code", "")).strip().upper()
    open_count   = int(data.get("open_count",   0))
    closed_count = int(data.get("closed_count", 0))
    subject      = str(data.get("subject",      "")).strip()
    raw_answers  = data.get("answers", {})
    start_time   = str(data.get("start_time",   "")).strip()
    end_time     = str(data.get("end_time",     "")).strip()

    if not code or not subject or (open_count + closed_count == 0):
        return jsonify({"success": False, "message": "Ma'lumotlar to'liq emas!"}), 400

    # FIX #7: start_time/end_time server tomonda tekshiriladi
    if not start_time or not end_time:
        return jsonify({"success": False, "message": "Vaqt ma'lumotlari to'liq emas!"}), 400
    try:
        st_dt = parse_dt(start_time)
        et_dt = parse_dt(end_time)
    except Exception:
        return jsonify({"success": False, "message": "Vaqt formati noto'g'ri (ISO 8601 kerak)!"}), 400
    if et_dt <= st_dt:
        return jsonify({"success": False, "message": "Tugash vaqti boshlanishdan keyin bo'lishi kerak!"}), 400
    if et_dt <= now_utc():
        return jsonify({"success": False, "message": "Tugash vaqti o'tib ketgan!"}), 400

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM tests WHERE code=? COLLATE NOCASE", (code,)
        ).fetchone()
        if existing:
            return jsonify({"success": False, "message": f"{code} kodi allaqachon mavjud!"}), 409

        cursor = conn.execute("""
            INSERT INTO tests
              (code, subject, open_count, closed_count, owner_tg_id, start_time, end_time, is_saved)
            VALUES (?,?,?,?,?,?,?,1)
        """, (code, subject, open_count, closed_count, owner_tg_id, start_time, end_time))
        test_id = cursor.lastrowid

        # FIX #10: raw_answers key str bo'ladi — str(i) bilan olish
        rows = []
        for i in range(1, open_count + closed_count + 1):
            q_type = "open" if i <= open_count else "closed"
            ans    = str(raw_answers.get(str(i), "")).strip().upper()
            rows.append((test_id, i, q_type, ans))
        conn.executemany(
            "INSERT INTO questions (test_id, q_num, q_type, correct_ans) VALUES (?,?,?,?)", rows
        )

    start_str = fmt_dt(start_time)
    end_str   = fmt_dt(end_time)
    total_q   = open_count + closed_count

    msg_text = (
        f"🎉 *Test muvaffaqiyatli yaratildi!*\n\n"
        f"🔑 Kod: `{code}`\n"
        f"📚 Fan: {subject}\n"
        f"📊 Ochiq: {open_count} | Yopiq: {closed_count} | Jami: {total_q} ta\n"
        f"🕐 Boshlanish: {start_str}\n"
        f"🕑 Tugash: {end_str}\n\n"
        f"✅ Foydalanuvchilar belgilangan vaqtdan boshlab test topshira oladi!"
    )
    send_tg_async(owner_tg_id, msg_text)

    return jsonify({
        "success": True,
        "message": "Test saqlandi!",
        "code":    code,
        "subject": subject,
        "total":   total_q,
        "start":   start_str,
        "end":     end_str
    })

# ── Natijalar ─────────────────────────────────────────────────────
# FIX #18: Faqat owner ko'ra oladi (initData bilan tekshiriladi)
@flask_app.route("/api/results/<code>")
def api_get_results(code: str):
    # Owner tekshiruvi
    user_info = get_verified_user()
    if not user_info:
        return jsonify({"success": False, "message": "Autentifikatsiya muvaffaqiyatsiz!"}), 401
    if user_info.get("id") not in OWNER_IDS:
        return jsonify({"success": False, "message": "Ruxsat yo'q!"}), 403

    code = code.upper()
    with get_db() as conn:
        test = conn.execute(
            "SELECT * FROM tests WHERE code=? COLLATE NOCASE", (code,)
        ).fetchone()
        if not test:
            return jsonify({"success": False, "message": "Test topilmadi!"}), 404

        test_id = test["id"]
        # FIX #8: Faqat test tugagandan keyin va bir marta hisoblaydi
        maybe_calculate_rush(test_id, test)

        total = conn.execute(
            "SELECT COUNT(*) AS c FROM submissions WHERE test_id=?", (test_id,)
        ).fetchone()["c"]

        questions = conn.execute(
            "SELECT q_num, q_type FROM questions WHERE test_id=? ORDER BY q_num", (test_id,)
        ).fetchall()

        q_stats = []
        for q in questions:
            correct = conn.execute("""
                SELECT COUNT(*) AS c FROM sub_answers sa
                JOIN submissions s ON sa.sub_id = s.id
                WHERE s.test_id=? AND sa.q_num=? AND sa.is_correct=1
            """, (test_id, q["q_num"])).fetchone()["c"]
            q_stats.append({"q_num": q["q_num"], "q_type": q["q_type"], "correct": correct})

        rankings = conn.execute("""
            SELECT u.full_name, s.rush_score, s.correct_count
            FROM submissions s JOIN users u ON s.user_id = u.id
            WHERE s.test_id=?
            ORDER BY s.rush_score DESC, s.correct_count DESC
        """, (test_id,)).fetchall()

        return jsonify({
            "success":  True,
            "test":     {"code": test["code"], "subject": test["subject"]},
            "total":    total,
            "questions": q_stats,
            "rankings": [dict(r) for r in rankings]
        })

def run_flask():
    flask_app.run(
        host="0.0.0.0",
        port=FLASK_PORT,
        debug=False,
        use_reloader=False,
        threaded=True
    )

# ================================================================
#                    TELEGRAM BOT HANDLERS
# ================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with get_db() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE telegram_id=?", (user_id,)
        ).fetchone()
    if user:
        await show_main_menu(update)
    else:
        kb = [[KeyboardButton("📱 Kontaktni ulashish", request_contact=True)]]
        await update.message.reply_text(
            "🎓 *Milliy Sertifikat Test Botiga Xush Kelibsiz!*\n\n"
            "Davom etish uchun telefon raqamingizni ulashing 👇",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True)
        )

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update)

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact   = update.message.contact
    u         = update.effective_user
    phone     = contact.phone_number
    full_name = (
        f"{contact.first_name or ''} {contact.last_name or ''}".strip()
        or u.full_name or "Foydalanuvchi"
    )
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (telegram_id, phone, full_name, username)
            VALUES (?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE
            SET phone=excluded.phone, full_name=excluded.full_name
        """, (u.id, phone, full_name, u.username))

    await update.message.reply_text(
        f"✅ Xush kelibsiz, *{full_name}*!\n📱 Raqamingiz saqlandi.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await show_main_menu(update)

async def show_main_menu(update: Update):
    user_id  = update.effective_user.id
    is_owner = user_id in OWNER_IDS

    # FIX #1: InlineKeyboardButton(web_app=...) bilan WebApp ochiladi.
    # sendData() endi ishlatilmasligi sababli (fetch() ishlatiladi),
    # InlineKeyboardButton to'g'ri va to'liq ishlaydi.
    if is_owner:
        keyboard = [
            [InlineKeyboardButton("📝 Test Tekshirish",
                                  web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp/user/test"))],
            [InlineKeyboardButton("➕ Test Yaratish",
                                  web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp/owner/create"))],
            [InlineKeyboardButton("📊 Natijalar",
                                  web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp/owner/results"))],
        ]
        text = (
            "👑 *Ega Paneli*\n\n"
            "📝 *Test Tekshirish* – test kodini kiriting va javoblarni belgilang\n"
            "➕ *Test Yaratish* – yangi test yarating va vaqt belgilang\n"
            "📊 *Natijalar* – savollar va foydalanuvchilar bo'yicha statistika"
        )
    else:
        keyboard = [
            [InlineKeyboardButton("📝 Test Tekshirish",
                                  web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp/user/test"))],
        ]
        text = (
            "🎓 *Milliy Sertifikat Test Tizimi*\n\n"
            "Test tekshirish uchun quyidagi tugmani bosing.\n"
            "Test kodini va javoblaringizni kiriting."
        )

    # FIX #11: callback_query None bo'lishi mumkin — xavfsiz olish
    msg = None
    if update.message:
        msg = update.message
    elif update.callback_query:
        msg = update.callback_query.message

    if msg:
        await msg.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Oddiy matn xabarlari — ro'yxatdan o'tganini tekshiradi"""
    user_id = update.effective_user.id
    with get_db() as conn:
        user = conn.execute(
            "SELECT id FROM users WHERE telegram_id=?", (user_id,)
        ).fetchone()
    if user:
        await show_main_menu(update)
    else:
        await cmd_start(update, context)

# FIX #4: handle_webapp_data — tg.sendData() endi ishlatilmaydi
# Bu handler qoldirildi, lekin hech qachon ishga tushmasligi kerak.
# Agar kutilmagan holdа sendData() chaqirilsa, xabar loglanadi.
async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.web_app_data.data
    logger.warning(
        "Kutilmagan WebApp data keldi (bu handler chaqirilmasligi kerak): %r", raw
    )
    await update.message.reply_text(
        "⚠️ Eski versiya. Botni qayta ishga tushiring: /start"
    )

# ================================================================
#                           MAIN
# ================================================================
def main():
    init_db()

    # Flask server alohida daemon threadda
    flask_thread = threading.Thread(
        target=run_flask, daemon=True, name="FlaskThread"
    )
    flask_thread.start()
    logger.info("🌐 Flask server port %s da ishga tushdi.", FLASK_PORT)

    # Telegram bot
    bot_app = Application.builder().token(BOT_TOKEN).build()

    bot_app.add_handler(CommandHandler("start",  cmd_start))
    bot_app.add_handler(CommandHandler("menu",   cmd_menu))
    bot_app.add_handler(MessageHandler(filters.CONTACT,                   handle_contact))
    bot_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,   handle_text))

    logger.info("🤖 Bot polling rejimida ishga tushdi!")
    bot_app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
