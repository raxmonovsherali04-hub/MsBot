#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         MILLIY SERTIFIKAT TEST TEKSHIRUVCHI BOT              ║
║              RUSH Model Scoring System                        ║
║                                                              ║
║  O'zbekiston Milliy Sertifikat Imtihonlari tizimi            ║
╚══════════════════════════════════════════════════════════════╝

TALAB QILINADIGAN KUTUBXONALAR:
  pip install python-telegram-bot==20.7 flask apscheduler

ISHGA TUSHIRISH:
  1. BOT_TOKEN ni o'zgartiring
  2. OWNER_IDS ga o'z Telegram ID ingizni kiriting
  3. WEBAPP_URL ni o'z serveringiz URL ga o'zgartiring
  4. python bot.py
"""

import logging
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup,
    InlineKeyboardButton, InlineKeyboardMarkup,
    WebAppInfo, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)

# ================================================================
#                        SOZLAMALAR
# ================================================================
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
OWNER_IDS  = [int(x) for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
FLASK_PORT = int(os.environ.get("PORT", 5000))   # Railway PORT ni avtomatik o'rnatadi
TIMEZONE   = os.environ.get("TIMEZONE", "Asia/Tashkent")
DB_PATH    = os.environ.get("DB_PATH", "sertifikat.db")

# ── Ishga tushishdan oldin muhim o'zgaruvchilarni tekshirish ──
def _check_env():
    missing = []
    if not BOT_TOKEN:   missing.append("BOT_TOKEN")
    if not OWNER_IDS:   missing.append("OWNER_IDS")
    if not WEBAPP_URL:  missing.append("WEBAPP_URL")
    if missing:
        raise SystemExit(
            f"\n❌ ENV o'zgaruvchilar topilmadi: {', '.join(missing)}\n"
            "Railway → Variables bo'limiga qo'shing:\n"
            "  BOT_TOKEN=...\n  OWNER_IDS=123456789\n  WEBAPP_URL=https://...\n"
        )

_check_env()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================================================================
#                       MA'LUMOTLAR BAZASI
# ================================================================
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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

            CREATE TABLE IF NOT EXISTS tests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                code         TEXT    UNIQUE NOT NULL COLLATE NOCASE,
                subject      TEXT    NOT NULL,
                open_count   INTEGER NOT NULL DEFAULT 0,
                closed_count INTEGER NOT NULL DEFAULT 0,
                owner_id     INTEGER NOT NULL,
                start_time   TEXT,
                end_time     TEXT,
                is_saved     INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    DEFAULT (datetime('now'))
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
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (test_id) REFERENCES tests(id)
            );

            CREATE TABLE IF NOT EXISTS sub_answers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                sub_id       INTEGER NOT NULL,
                q_num        INTEGER NOT NULL,
                user_ans     TEXT,
                is_correct   INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (sub_id) REFERENCES submissions(id) ON DELETE CASCADE
            );
        """)
    logger.info("✅ Ma'lumotlar bazasi tayyor.")

# ================================================================
#                   RUSH MODEL SCORING
# ================================================================
def calculate_rush_scores(test_id: int):
    """
    RUSH Model:
    - Har bir savol uchun qancha kishi to'g'ri javob berganini hisoblaymiz
    - Kamroq topilgan savol → ko'proq ball (noyob javob qimmatroq)
    - Formula: weight_i = N / correct_count_i
    - Normalize: rush_score = (user_raw / max_possible) * 100
    """
    with get_db() as conn:
        subs = conn.execute(
            "SELECT id, user_id FROM submissions WHERE test_id = ?", (test_id,)
        ).fetchall()
        N = len(subs)
        if N == 0:
            return

        questions = conn.execute(
            "SELECT q_num FROM questions WHERE test_id = ? ORDER BY q_num", (test_id,)
        ).fetchall()
        q_nums = [q["q_num"] for q in questions]

        # Har bir savol uchun to'g'ri javoblar soni
        correct_counts = {}
        for qn in q_nums:
            cnt = conn.execute("""
                SELECT COUNT(*) AS c FROM sub_answers sa
                JOIN submissions s ON sa.sub_id = s.id
                WHERE s.test_id = ? AND sa.q_num = ? AND sa.is_correct = 1
            """, (test_id, qn)).fetchone()["c"]
            correct_counts[qn] = max(cnt, 1)  # 0 ga bo'lishni oldini olish

        # Maksimal mumkin bo'lgan raw score (barcha javob to'g'ri bo'lsa)
        max_raw = sum(N / correct_counts[qn] for qn in q_nums)
        if max_raw == 0:
            return

        # Har bir foydalanuvchining balini hisoblash
        for sub in subs:
            raw = 0.0
            for qn in q_nums:
                ans = conn.execute("""
                    SELECT is_correct FROM sub_answers
                    WHERE sub_id = ? AND q_num = ?
                """, (sub["id"], qn)).fetchone()
                if ans and ans["is_correct"]:
                    raw += N / correct_counts[qn]

            rush_score = round((raw / max_raw) * 100, 4)
            conn.execute(
                "UPDATE submissions SET rush_score = ? WHERE id = ?",
                (rush_score, sub["id"])
            )

# ================================================================
#                  VAQT YORDAMCHI FUNKSIYALAR
# ================================================================
UZ_TZ = ZoneInfo(TIMEZONE)

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_dt(s: str) -> datetime:
    """ISO string ni datetime ga aylantiradi"""
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s)

def fmt_dt(s: str) -> str:
    """ISO string → O'zbekcha format"""
    try:
        dt = parse_dt(s).astimezone(UZ_TZ)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return s

def check_test_status(test) -> str:
    """Test statusini qaytaradi: pending | active | expired"""
    now = now_utc()
    if now < test["start_time"]:
        return "pending"
    if now > test["end_time"]:
        return "expired"
    return "active"

# ================================================================
#                     HTML SAHIFALAR
# ================================================================

# ---- Foydalanuvchi: Test Tekshirish ----
USER_TEST_HTML = r"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Test Tekshirish</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--blue:#1a73e8;--green:#27ae60;--red:#e74c3c;--purple:#8e44ad;--orange:#e67e22}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#f0f2f5;color:#333;min-height:100vh}
.container{max-width:600px;margin:0 auto;padding:14px}
h2{text-align:center;color:var(--blue);margin-bottom:18px;font-size:20px}
.card{background:#fff;border-radius:14px;padding:18px;margin-bottom:14px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
input[type=text]{width:100%;padding:13px 14px;border:2px solid #dde0e8;border-radius:10px;font-size:16px;transition:.2s}
input[type=text]:focus{border-color:var(--blue);outline:none;box-shadow:0 0 0 3px rgba(26,115,232,.1)}
.btn{width:100%;padding:14px;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;margin-top:10px;transition:.2s}
.btn-blue{background:var(--blue);color:#fff}.btn-blue:hover{background:#1557b0}
.btn-green{background:var(--green);color:#fff}.btn-green:hover{background:#229954}
.q-block{background:#fff;border-radius:12px;padding:14px;margin-bottom:10px;box-shadow:0 1px 6px rgba(0,0,0,.07);border-left:4px solid var(--blue)}
.q-block.closed{border-left-color:var(--purple)}
.q-label{font-weight:700;color:var(--blue);margin-bottom:10px;font-size:15px;display:flex;align-items:center;gap:6px}
.q-label.closed-label{color:var(--purple)}
.opts{display:flex;gap:8px}
.opt-btn{flex:1;min-width:52px;padding:11px 4px;border:2px solid #dde0e8;border-radius:9px;background:#fff;cursor:pointer;font-size:16px;font-weight:700;text-align:center;transition:.15s}
.opt-btn:hover{border-color:var(--blue);background:#f0f5ff}
.opt-btn.sel{background:var(--blue);color:#fff;border-color:var(--blue)}
.closed-inp{width:100%;padding:11px 13px;border:2px solid #dde0e8;border-radius:9px;font-size:15px;margin-top:6px;transition:.2s}
.closed-inp:focus{border-color:var(--purple);outline:none}
.badge{display:inline-flex;align-items:center;gap:4px;padding:5px 12px;border-radius:20px;font-size:13px;font-weight:600}
.badge-blue{background:#e8f0fe;color:var(--blue)}
.badge-green{background:#e8f9ee;color:var(--green)}
.error{color:var(--red);font-size:13px;margin-top:6px}
.hidden{display:none!important}
.loading{text-align:center;padding:40px;color:#999;font-size:15px}
.section-hdr{font-weight:700;padding:6px 0 10px;color:#555;font-size:14px;border-bottom:1px dashed #eee;margin-bottom:10px}
.summary-bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.chip{background:#f5f7ff;border:1px solid #e0e6ff;border-radius:8px;padding:6px 12px;font-size:13px;color:#555}
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
    <button class="btn btn-green" onclick="submitAnswers()" style="margin-bottom:24px">
      ✅ Javoblarni Yuborish
    </button>
  </div>
</div>

<script>
const tg=window.Telegram.WebApp;
tg.expand();tg.ready();
tg.MainButton.hide();

let testData=null,answers={};

async function loadTest(){
  const code=document.getElementById('testCode').value.trim().toUpperCase();
  const errDiv=document.getElementById('codeErr');
  errDiv.textContent='';
  if(!code){errDiv.textContent='❗ Test kodini kiriting!';return;}
  show('loadingDiv');hide('step1');
  try{
    const r=await fetch('/api/test/'+code);
    const d=await r.json();
    hide('loadingDiv');
    if(!d.success){show('step1');errDiv.textContent='❌ '+d.message;return;}
    testData=d.test;
    renderQuestions(testData);
    show('step2');
  }catch(e){
    hide('loadingDiv');show('step1');
    errDiv.textContent='❌ Xatolik. Qaytadan urinib ko\'ring.';
  }
}

function renderQuestions(t){
  document.getElementById('subjectBadge').textContent='📚 '+t.subject;
  document.getElementById('openInfo').textContent='Ochiq: '+t.open_count+' ta';
  document.getElementById('closedInfo').textContent='Yopiq: '+t.closed_count+' ta';
  const c=document.getElementById('questionsContainer');
  c.innerHTML='';
  const total=t.open_count+t.closed_count;
  for(let i=1;i<=total;i++){
    const isOpen=i<=t.open_count;
    const blk=document.createElement('div');
    blk.className='q-block'+(isOpen?'':' closed');
    if(isOpen){
      blk.innerHTML=`
        <div class="q-label">🔵 ${i}-savol</div>
        <div class="opts">
          ${['A','B','C','D'].map(o=>`<button class="opt-btn" onclick="selOpt(${i},'${o}',this)">${o}</button>`).join('')}
        </div>`;
    }else{
      blk.innerHTML=`
        <div class="q-label closed-label">🟣 ${i}-savol <span style="font-weight:400;font-size:12px">(yopiq)</span></div>
        <input class="closed-inp" type="text" placeholder="Javobingizni kiriting..."
               oninput="answers[${i}]=this.value.trim().toUpperCase()"/>`;
    }
    c.appendChild(blk);
  }
}

function selOpt(qn,opt,btn){
  btn.closest('.opts').querySelectorAll('.opt-btn').forEach(b=>b.classList.remove('sel'));
  btn.classList.add('sel');
  answers[qn]=opt;
}

function submitAnswers(){
  if(!testData)return;
  const total=testData.open_count+testData.closed_count;
  const miss=[];
  for(let i=1;i<=total;i++){if(!answers[i]||answers[i]==='')miss.push(i);}
  if(miss.length>0){
    const names=miss.slice(0,5).join(', ')+(miss.length>5?'..':'');
    if(!confirm(`${miss.length} ta savol javobsiz qoldi (${names}).\nShunga qaramay yuborasizmi?`))return;
  }
  tg.sendData(JSON.stringify({action:'submit_test',test_code:testData.code,answers}));
}

function show(id){document.getElementById(id).classList.remove('hidden')}
function hide(id){document.getElementById(id).classList.add('hidden')}
</script>
</body>
</html>"""

# ---- Ega: Test Yaratish ----
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
.btn-ora{background:var(--orange);color:#fff}.btn-ora:hover{background:#d35400}
.btn-grn{background:var(--green);color:#fff}.btn-grn:hover{background:#229954}
.btn-gry{background:#95a5a6;color:#fff}.btn-gry:hover{background:#7f8c8d}
.q-block{background:#fafafa;border:1px solid #eee;border-radius:10px;padding:13px;margin-bottom:9px}
.q-lbl{font-weight:700;margin-bottom:9px;font-size:14px}
.opts{display:flex;gap:7px}
.opt-btn{flex:1;padding:10px 2px;border:2px solid #dde0e8;border-radius:8px;background:#fff;
  cursor:pointer;font-size:15px;font-weight:700;text-align:center;transition:.15s}
.opt-btn:hover{border-color:var(--green);background:#eafaf1}
.opt-btn.sel{background:var(--green);color:#fff;border-color:var(--green)}
.cl-inp{width:100%;padding:10px 12px;border:2px solid #dde0e8;border-radius:8px;font-size:14px;margin-top:6px}
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
          <label>Ochiq testlar soni</label>
          <input id="openCount" type="number" min="0" value="0"/>
        </div>
        <div class="fg">
          <label>Yopiq testlar soni</label>
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
        Yopilgandan keyin test kodi kiritilsa xabar yuboriladi.
      </div>
    </div>
    <button class="btn btn-gry" onclick="goStep(2)" style="margin-bottom:6px">← Orqaga</button>
    <button class="btn btn-grn" onclick="saveTest()">💾 Testni Saqlash</button>
  </div>
</div>

<script>
const tg=window.Telegram.WebApp;
tg.expand();tg.ready();

let meta={},answers={};

function show(id){document.getElementById(id).classList.remove('hidden')}
function hide(id){document.getElementById(id).classList.add('hidden')}

function goStep(n){
  [1,2,3].forEach(i=>{
    document.getElementById('step'+i).classList[i===n?'remove':'add']('hidden');
    document.getElementById('dot'+i).classList[i===n?'add':'remove']('active');
  });
}

function step1Next(){
  const code=document.getElementById('testCode').value.trim().toUpperCase();
  const open=parseInt(document.getElementById('openCount').value)||0;
  const closed=parseInt(document.getElementById('closedCount').value)||0;
  const subj=document.getElementById('subject').value.trim();
  const errDiv=document.getElementById('codeErr');
  errDiv.textContent='';
  if(!code){errDiv.textContent='Test kodi kiritilishi shart!';return;}
  if(!/^[A-Z0-9]{3,20}$/.test(code)){errDiv.textContent='Kod 3-20 ta harf/raqamdan iborat bo\'lsin!';return;}
  if(open+closed===0){alert('Kamida 1 ta savol bo\'lishi kerak!');return;}
  if(!subj){alert('Fan nomini kiriting!');return;}
  meta={code,open_count:open,closed_count:closed,subject:subj};
  renderAnswers();
  document.getElementById('metaInfo').innerHTML=
    `📋 <b>${code}</b> | 📚 ${subj} | Ochiq: ${open} | Yopiq: ${closed} | Jami: ${open+closed} ta`;
  goStep(2);
}

function renderAnswers(){
  const c=document.getElementById('answersContainer');
  c.innerHTML='';
  const{open_count:op,closed_count:cl}=meta;
  if(op>0){
    const h=document.createElement('div');
    h.className='sec-hdr sec-open';
    h.textContent='📗 Ochiq savollar (1-'+op+')';
    c.appendChild(h);
    for(let i=1;i<=op;i++){
      const blk=document.createElement('div');blk.className='q-block';
      blk.innerHTML=`<div class="q-lbl" style="color:#27ae60">${i}-savol</div>
        <div class="opts">${['A','B','C','D'].map(o=>
          `<button class="opt-btn${answers[i]===o?' sel':''}" onclick="selAns(${i},'${o}',this)">${o}</button>`
        ).join('')}</div>`;
      c.appendChild(blk);
    }
  }
  if(cl>0){
    const h2=document.createElement('div');
    h2.className='sec-hdr sec-closed';
    h2.style.marginTop='14px';
    h2.textContent='📘 Yopiq savollar ('+(op+1)+'-'+(op+cl)+')';
    c.appendChild(h2);
    for(let i=op+1;i<=op+cl;i++){
      const blk=document.createElement('div');blk.className='q-block';
      blk.innerHTML=`<div class="q-lbl" style="color:#8e44ad">${i}-savol <span style="font-weight:400;font-size:12px">(yopiq)</span></div>
        <input class="cl-inp" type="text" placeholder="To'g'ri javobni kiriting..."
               value="${answers[i]||''}"
               oninput="answers[${i}]=this.value.trim().toUpperCase()"/>`;
      c.appendChild(blk);
    }
  }
}

function selAns(qn,opt,btn){
  btn.closest('.opts').querySelectorAll('.opt-btn').forEach(b=>b.classList.remove('sel'));
  btn.classList.add('sel');answers[qn]=opt;
}

// Mahalliy vaqtni datetime-local input uchun formatlash (UTC emas!)
function localDT(d){
  const p=n=>String(n).padStart(2,'0');
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+
         'T'+p(d.getHours())+':'+p(d.getMinutes());
}

function step2Next(){
  const total=meta.open_count+meta.closed_count;
  const miss=[];
  for(let i=1;i<=total;i++){if(!answers[i]||!answers[i].trim())miss.push(i);}
  if(miss.length>0){alert('❌ Javobsiz savollar: '+miss.join(', '));return;}
  // Default vaqt — foydalanuvchi telefonining MAHALLIY vaqti (O‘zbekiston)
  const now=new Date();
  const def=new Date(now.getTime()+10*60000);
  const dt=document.getElementById('startTime');
  if(!dt.value) dt.value=localDT(def);
  dt.min=localDT(now);
  goStep(3);
}

function saveTest(){
  const st=document.getElementById('startTime').value;
  const dur=parseInt(document.getElementById('duration').value);
  if(!st){alert('Boshlanish vaqtini kiriting!');return;}
  if(!dur||dur<1){alert('Davomiylikni kiriting!');return;}
  const startDt=new Date(st);
  const endDt=new Date(startDt.getTime()+dur*60000);
  const payload={
    action:'create_test',
    ...meta,
    answers,
    start_time:startDt.toISOString(),
    end_time:endDt.toISOString()
  };
  tg.sendData(JSON.stringify(payload));
}
</script>
</body>
</html>"""

# ---- Ega: Natijalar ----
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
.m1{background:var(--gold);color:#333}
.m2{background:#bdc3c7;color:#333}
.m3{background:#cd7f32;color:#fff}
.mx{background:#ecf0f1;color:#888}
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

    <div id="qTab" class="card">
      <div id="qStats"></div>
    </div>

    <div id="uTab" class="card hidden">
      <div id="uRanks"></div>
    </div>

    <button class="btn btn-gry btn-full" onclick="resetView()">← Orqaga</button>
  </div>
</div>

<script>
const tg=window.Telegram.WebApp;
tg.expand();tg.ready();

async function loadResults(){
  const code=document.getElementById('codeInput').value.trim().toUpperCase();
  const err=document.getElementById('inputErr');err.textContent='';
  if(!code)return;
  show('loadDiv');hide('inputSection');
  try{
    const r=await fetch('/api/results/'+code);
    const d=await r.json();
    hide('loadDiv');
    if(!d.success){show('inputSection');err.textContent='❌ '+d.message;return;}
    renderAll(d);
    show('resultsSection');
  }catch(e){
    hide('loadDiv');show('inputSection');err.textContent='❌ Xatolik yuz berdi';
  }
}

function renderAll(d){
  document.getElementById('subjectChip').textContent='📚 '+d.test.subject;
  document.getElementById('codeChip').textContent='🔑 '+d.test.code;
  document.getElementById('totalBadge').textContent='👥 '+d.total+' ta qatnashuvchi';
  renderQStats(d);
  renderURanks(d);
}

function renderQStats(d){
  const c=document.getElementById('qStats');
  const N=d.total||1;
  c.innerHTML='<div style="font-weight:700;margin-bottom:14px;color:#555">Jami '+d.questions.length+' ta savol</div>';
  d.questions.forEach(q=>{
    const pct=N>0?Math.round(q.correct/N*100):0;
    const row=document.createElement('div');row.className='q-row';
    row.innerHTML=`<div class="q-row-hdr">
      <span class="q-num">${q.q_num}-savol <span style="color:#bbb;font-size:12px">(${q.q_type==='open'?'ochiq':'yopiq'})</span></span>
      <span class="q-cnt">${q.correct} ta ✓ (${pct}%)</span>
    </div>
    <div class="bar-bg"><div class="bar-fill" style="width:${pct}%"></div></div>`;
    c.appendChild(row);
  });
}

function renderURanks(d){
  const c=document.getElementById('uRanks');
  c.innerHTML=`<div style="font-weight:700;margin-bottom:14px;color:#555">
    ${d.test.subject} | ${d.total} ta qatnashuvchi
  </div>`;
  if(d.rankings.length===0){c.innerHTML+='<p style="color:#999;text-align:center">Hali natijalar yo\'q</p>';return;}
  d.rankings.forEach((u,idx)=>{
    const rank=idx+1;
    const mc=rank===1?'m1':rank===2?'m2':rank===3?'m3':'mx';
    const row=document.createElement('div');row.className='rank-row';
    row.innerHTML=`<div class="medal ${mc}">${rank}</div>
      <div style="flex:1">
        <div class="user-name">${u.full_name}</div>
        <div class="user-stats">${u.correct_count} ta to'g'ri javob</div>
      </div>
      <div class="rush-badge">${u.rush_score.toFixed(2)} RUSH</div>`;
    c.appendChild(row);
  });
}

function switchTab(t){
  document.querySelectorAll('.tab').forEach((b,i)=>b.classList.toggle('active',(i===0&&t==='q')||(i===1&&t==='u')));
  document.getElementById('qTab').classList.toggle('hidden',t!=='q');
  document.getElementById('uTab').classList.toggle('hidden',t!=='u');
}

function resetView(){
  hide('resultsSection');show('inputSection');
  document.getElementById('codeInput').value='';
}

function show(id){document.getElementById(id).classList.remove('hidden')}
function hide(id){document.getElementById(id).classList.add('hidden')}
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

@flask_app.route("/webapp/user/test")
def page_user_test():
    return USER_TEST_HTML

@flask_app.route("/webapp/owner/create")
def page_owner_create():
    return OWNER_CREATE_HTML

@flask_app.route("/webapp/owner/results")
def page_owner_results():
    return OWNER_RESULTS_HTML

@flask_app.route("/api/test/<code>")
def api_get_test(code):
    code = code.upper()
    with get_db() as conn:
        test = conn.execute("SELECT * FROM tests WHERE code=? COLLATE NOCASE", (code,)).fetchone()
        if not test:
            return jsonify({"success": False, "message": "Test topilmadi!"})
        if not test["is_saved"]:
            return jsonify({"success": False, "message": "Test hali tayyor emas!"})

        status = check_test_status(test)
        if status == "pending":
            return jsonify({"success": False, "message": f"Test {fmt_dt(test['start_time'])} da boshlanadi."})
        if status == "expired":
            return jsonify({"success": False, "message": "Test vaqti tugagan! ⌛"})

        return jsonify({
            "success": True,
            "test": {
                "code": test["code"],
                "subject": test["subject"],
                "open_count": test["open_count"],
                "closed_count": test["closed_count"]
            }
        })

@flask_app.route("/api/results/<code>")
def api_get_results(code):
    code = code.upper()
    with get_db() as conn:
        test = conn.execute("SELECT * FROM tests WHERE code=? COLLATE NOCASE", (code,)).fetchone()
        if not test:
            return jsonify({"success": False, "message": "Test topilmadi!"})

        test_id = test["id"]
        calculate_rush_scores(test_id)

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
                JOIN submissions s ON sa.sub_id=s.id
                WHERE s.test_id=? AND sa.q_num=? AND sa.is_correct=1
            """, (test_id, q["q_num"])).fetchone()["c"]
            q_stats.append({"q_num": q["q_num"], "q_type": q["q_type"], "correct": correct})

        rankings_raw = conn.execute("""
            SELECT u.full_name, s.rush_score, s.correct_count
            FROM submissions s JOIN users u ON s.user_id=u.id
            WHERE s.test_id=?
            ORDER BY s.rush_score DESC, s.correct_count DESC
        """, (test_id,)).fetchall()

        return jsonify({
            "success": True,
            "test": {"code": test["code"], "subject": test["subject"]},
            "total": total,
            "questions": q_stats,
            "rankings": [dict(r) for r in rankings_raw]
        })

def run_flask():
    # Railway PORT env ni o'rnatadi, biz uni FLASK_PORT ga yuklaymiz
    flask_app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False, threaded=True)

# ================================================================
#                    TELEGRAM BOT HANDLERS
# ================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE telegram_id=?", (user_id,)).fetchone()
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
    contact = update.message.contact
    u = update.effective_user
    phone = contact.phone_number
    full_name = (
        f"{contact.first_name or ''} {contact.last_name or ''}".strip()
        or u.full_name or "Foydalanuvchi"
    )
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (telegram_id, phone, full_name, username)
            VALUES (?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET phone=excluded.phone, full_name=excluded.full_name
        """, (u.id, phone, full_name, u.username))

    await update.message.reply_text(
        f"✅ Xush kelibsiz, *{full_name}*!\n📱 Raqamingiz saqlandi.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    await show_main_menu(update)

async def show_main_menu(update: Update):
    user_id = update.effective_user.id
    is_owner = user_id in OWNER_IDS

    if is_owner:
        keyboard = [
            [InlineKeyboardButton(
                "📝 Test Tekshirish",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp/user/test")
            )],
            [InlineKeyboardButton(
                "➕ Test Yaratish",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp/owner/create")
            )],
            [InlineKeyboardButton(
                "📊 Natijalar",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp/owner/results")
            )],
        ]
        text = (
            "👑 *Ega Paneli*\n\n"
            "📝 *Test Tekshirish* – test kodini kiriting va javoblarni belgilang\n"
            "➕ *Test Yaratish* – yangi test yarating va vaqt belgilang\n"
            "📊 *Natijalar* – savollar va foydalanuvchilar bo'yicha statistika"
        )
    else:
        keyboard = [
            [InlineKeyboardButton(
                "📝 Test Tekshirish",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp/user/test")
            )],
        ]
        text = (
            "🎓 *Milliy Sertifikat Test Tizimi*\n\n"
            "Test tekshirish uchun quyidagi tugmani bosing.\n"
            "Test kodini va javoblaringizni kiriting."
        )

    msg = update.message if update.message else update.callback_query.message
    await msg.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.web_app_data.data
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await update.message.reply_text("❌ Noto'g'ri ma'lumot!")
        return

    action = data.get("action")
    user_id = update.effective_user.id

    if action == "submit_test":
        await process_submit_test(update, data, user_id)
    elif action == "create_test":
        if user_id in OWNER_IDS:
            await process_create_test(update, data, user_id)
        else:
            await update.message.reply_text("❌ Sizda bu amalni bajarish uchun ruxsat yo'q!")
    else:
        await update.message.reply_text("❓ Noma'lum amal!")

async def process_submit_test(update: Update, data: dict, user_id: int):
    """Foydalanuvchi test javoblarini saqlash va natijani yuborish"""
    test_code = data.get("test_code", "").upper()
    raw_answers = data.get("answers", {})
    # JSON key larini int ga aylantirish
    answers = {int(k): str(v).strip().upper() for k, v in raw_answers.items() if v}

    with get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE telegram_id=?", (user_id,)
        ).fetchone()
        if not user:
            await update.message.reply_text(
                "❌ Avval /start buyrug'ini yuboring va raqamingizni ulashing!"
            )
            return

        test = conn.execute(
            "SELECT * FROM tests WHERE code=? COLLATE NOCASE", (test_code,)
        ).fetchone()
        if not test:
            await update.message.reply_text("❌ Test topilmadi!")
            return

        status = check_test_status(test)
        if status == "pending":
            await update.message.reply_text(
                f"⏳ Test hali boshlanmagan!\nBoshlanish: {fmt_dt(test['start_time'])}"
            )
            return
        if status == "expired":
            await update.message.reply_text("⌛ Test vaqti tugagan! Natijalar e'lon qilinishini kuting.")
            return

        # Qayta topshirishni tekshirish
        existing = conn.execute(
            "SELECT id FROM submissions WHERE user_id=? AND test_id=?",
            (user["id"], test["id"])
        ).fetchone()
        if existing:
            await update.message.reply_text(
                "⚠️ Siz bu testni allaqachon topshirgansiz!\n"
                "Natijalar ega tomonidan e'lon qilingandan so'ng ma'lum bo'ladi."
            )
            return

        # To'g'ri javoblarni olish
        questions = conn.execute(
            "SELECT * FROM questions WHERE test_id=? ORDER BY q_num", (test["id"],)
        ).fetchall()

        correct_count = 0
        answer_records = []
        total_q = test["open_count"] + test["closed_count"]

        for q in questions:
            qn = q["q_num"]
            user_ans = answers.get(qn, "")
            correct_ans = q["correct_ans"].strip().upper()
            is_correct = 1 if user_ans == correct_ans and user_ans != "" else 0
            if is_correct:
                correct_count += 1
            answer_records.append((qn, user_ans, is_correct))

        # Submission saqlash
        cursor = conn.execute("""
            INSERT INTO submissions (user_id, test_id, correct_count)
            VALUES (?,?,?)
        """, (user["id"], test["id"], correct_count))
        sub_id = cursor.lastrowid

        for qn, ans, is_correct in answer_records:
            conn.execute("""
                INSERT INTO sub_answers (sub_id, q_num, user_ans, is_correct)
                VALUES (?,?,?,?)
            """, (sub_id, qn, ans, is_correct))

        # Javoblar tafsiloti
        details = []
        for qn, user_ans, is_correct in answer_records:
            icon = "✅" if is_correct else "❌"
            details.append(f"{icon} {qn}-savol: {user_ans or '—'}")

        detail_text = "\n".join(details[:20])
        if len(details) > 20:
            detail_text += f"\n... va yana {len(details)-20} ta savol"

        pct = round(correct_count / total_q * 100) if total_q > 0 else 0

        await update.message.reply_text(
            f"✅ *Javoblaringiz qabul qilindi!*\n\n"
            f"📚 Fan: {test['subject']}\n"
            f"🔑 Test kodi: `{test_code}`\n"
            f"📊 To'g'ri javoblar: *{correct_count}/{total_q}* ta ({pct}%)\n\n"
            f"📋 *Javoblar:*\n{detail_text}\n\n"
            f"🏆 RUSH ball ega natijalarni e'lon qilganda ma'lum bo'ladi.",
            parse_mode="Markdown"
        )

async def process_create_test(update: Update, data: dict, owner_id: int):
    """Ega yangi test yaratish"""
    code         = data.get("test_code", "").upper()
    open_count   = int(data.get("open_count", 0))
    closed_count = int(data.get("closed_count", 0))
    subject      = data.get("subject", "").strip()
    raw_answers  = data.get("answers", {})
    start_time   = data.get("start_time", "")
    end_time     = data.get("end_time", "")

    if not code or not subject or (open_count + closed_count == 0):
        await update.message.reply_text("❌ Ma'lumotlar to'liq emas!")
        return

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM tests WHERE code=? COLLATE NOCASE", (code,)
        ).fetchone()
        if existing:
            await update.message.reply_text(
                f"❌ `{code}` kodi allaqachon mavjud! Boshqa kod tanlang.",
                parse_mode="Markdown"
            )
            return

        cursor = conn.execute("""
            INSERT INTO tests (code, subject, open_count, closed_count, owner_id, start_time, end_time, is_saved)
            VALUES (?,?,?,?,?,?,?,1)
        """, (code, subject, open_count, closed_count, owner_id, start_time, end_time))
        test_id = cursor.lastrowid

        for i in range(1, open_count + closed_count + 1):
            q_type = "open" if i <= open_count else "closed"
            ans = str(raw_answers.get(str(i), "")).strip().upper()
            conn.execute("""
                INSERT INTO questions (test_id, q_num, q_type, correct_ans)
                VALUES (?,?,?,?)
            """, (test_id, i, q_type, ans))

    start_str = fmt_dt(start_time)
    end_str   = fmt_dt(end_time)
    total_q   = open_count + closed_count

    await update.message.reply_text(
        f"🎉 *Test muvaffaqiyatli yaratildi!*\n\n"
        f"🔑 Kod: `{code}`\n"
        f"📚 Fan: {subject}\n"
        f"📊 Ochiq: {open_count} | Yopiq: {closed_count} | Jami: {total_q} ta\n"
        f"🕐 Boshlanish: {start_str}\n"
        f"🕑 Tugash: {end_str}\n\n"
        f"✅ Test foydalanuvchilar uchun ko'rsatilgan vaqtda avtomatik ochiladi!\n"
        f"👥 Foydalanuvchilar kodni kiritsalar test boshlanishini kutishlari kerak.",
        parse_mode="Markdown"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Oddiy matn xabarlari uchun"""
    user_id = update.effective_user.id
    with get_db() as conn:
        user = conn.execute("SELECT id FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    if user:
        await show_main_menu(update)
    else:
        await cmd_start(update, context)

# ================================================================
#                           MAIN
# ================================================================
def main():
    init_db()

    # Flask server ni alohida threadda ishga tushirish
    flask_thread = threading.Thread(target=run_flask, daemon=True, name="FlaskThread")
    flask_thread.start()
    logger.info(f"🌐 Flask server port {FLASK_PORT} da ishga tushdi.")

    # Telegram bot
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🤖 Bot polling rejimida ishga tushdi!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
