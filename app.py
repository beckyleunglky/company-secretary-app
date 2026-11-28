# === [PART 1/3] START ===
import os, re, json, base64, threading, itertools, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import pytz, requests
from flask import Flask, request, jsonify, render_template_string

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GAS_WEBAPP_URL = os.environ.get("GAS_WEBAPP_URL")
APP_PIN = os.environ.get("APP_PIN", "5678")

app = Flask(__name__)
WRITE_LOCK = threading.Lock()
PENDING_ACTIONS = {}
CHAT_MEMORY = {}

def push_chat(uid, role, txt):
    hist = CHAT_MEMORY.setdefault(uid, [])
    hist.append(f"{role}: {txt}")
    CHAT_MEMORY[uid] = list(itertools.islice(reversed(hist), 0, 12))[::-1]

def col(row, idx):
    return next(itertools.islice(row, idx, idx + 1), "")

def norm_date(val):
    s = str(val).strip()
    if "T" in s and s.endswith("Z"):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00")) + timedelta(hours=8)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    m = re.search(r"(\d{4})[-/\s](\d{1,2})[-/\s](\d{1,2})", s)
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else s

def parse_num(val):
    m = re.search(r"[-+]?[\d.]+", str(val))
    return float(m.group()) if m else 0.0

def fetch_sheet(name):
    try:
        r = requests.get(GAS_WEBAPP_URL, params={"sheet": name}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            if isinstance(d, list):
                return name, d
            if isinstance(d, dict):
                return name, d.get("values") or d.get("data") or []
    except Exception:
        pass
    return name, []

def read_all_sheets():
    sheets = ["Maintenance", "Purchasing"]
    with ThreadPoolExecutor(max_workers=2) as ex:
        return dict(ex.map(fetch_sheet, sheets))

def get_all_history_trimmed(history, cur_date):
    try:
        cutoff = (datetime.strptime(cur_date, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = "2000-01-01"
    trimmed = {}
    for k, rows in history.items():
        valid = []
        for r in rows:
            if not isinstance(r, list) or len(r) == 0:
                continue
            d_s = norm_date(col(r, 0))
            if re.match(r"^\d{4}-\d{2}-\d{2}$", d_s) and d_s >= cutoff:
                rest = list(itertools.islice(r, 1, None))
                valid.append([d_s] + rest)
        trimmed[k] = list(itertools.islice(valid, 0, 300))
    return trimmed

def search_history_records(history, query_text):
    clean_q = re.sub(r"(幾時|邊日|幾號|日期|換過|維修過|買過|採購過|紀錄|記錄|的|嘅|呀|呢|[？?，,。.\s])", " ", query_text).strip().lower()
    tokens = [t for t in clean_q.split() if len(t) >= 2]
    if not tokens:
        return []
    matches = []
    for sheet_name, rows in history.items():
        for r in rows:
            if not isinstance(r, list) or len(r) < 3:
                continue
            d_str = norm_date(col(r, 0))
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d_str):
                continue
            row_str = " | ".join(str(x).strip() for x in r).lower()
            if any(tok in row_str for tok in tokens):
                rest = list(itertools.islice(r, 1, None))
                row_disp = [d_str] + rest
                matches.append(f"[{sheet_name}] " + " | ".join(str(x).strip() for x in row_disp))
    return list(itertools.islice(matches, 0, 30))

def compute_cf_stats(history, cur_date):
    cur_month = "".join(itertools.islice(cur_date, 0, 7))
    maint_month, maint_total = 0.0, 0.0
    maint_by_dept = {}
    for r in history.get("Maintenance", []):
        if isinstance(r, list) and len(r) >= 5:
            d_str = norm_date(col(r, 0))
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d_str):
                continue
            dept = str(col(r, 1)).strip() or "其他"
            fee = parse_num(col(r, 4))
            maint_total += fee
            if d_str.startswith(cur_month):
                maint_month += fee
                maint_by_dept[dept] = round(maint_by_dept.get(dept, 0.0) + fee, 1)

    purch_month, purch_total = 0.0, 0.0
    for r in history.get("Purchasing", []):
        if isinstance(r, list) and len(r) >= 5:
            d_str = norm_date(col(r, 0))
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d_str):
                continue
            amt = parse_num(col(r, 4))
            purch_total += amt
            if d_str.startswith(cur_month):
                purch_month += amt

    return {
        "maint_month": round(maint_month, 1),
        "purch_month": round(purch_month, 1),
        "total_month": round(maint_month + purch_month, 1),
        "maint_all_time": round(maint_total, 1),
        "purch_all_time": round(purch_total, 1),
        "maint_by_dept_month": maint_by_dept
    }

def write_rows(sheet, rows):
    with WRITE_LOCK:
        try:
            if not rows:
                return True
            res = requests.post(GAS_WEBAPP_URL, json={"action": "append", "sheet": sheet, "rows": rows}, timeout=15)
            return res.status_code == 200
        except Exception:
            return False
# === [PART 1/3] END ===



# === [PART 2/3] START ===
CF_PROMPT = """你係專業公司行政秘書（管理 CF record 資料庫）。嚴格輸出純 JSON（不加任何多餘解釋文字）：
{"intent": "record 或 query", "missing": ["漏填必填欄名"], "writes": [{"sheet": "Maintenance 或 Purchasing", "rows": [["欄1", "欄2"]]}], "reply": ""}
規則：
1. Maintenance 維修表（嚴格 6 欄）：日期(YYYY-MM-DD) | 維修部門 | 項目 | 位置 | 費用(純數字如 450 或 0) | 維修人。
   - 必填欄位：維修部門、項目、費用、維修人（「位置」為選填，若用戶無提供位置，直接填 "" 空字串即可，絕不列入 missing；若說明「免費」或「內部維修無費」則費用填 0）。
   - 日期若無特別指明，預設使用系統提供的今日日期。
2. Purchasing 採購表（嚴格 5 欄）：日期(YYYY-MM-DD) | 項目 | 數量(純數字) | 單價(純數字) | 總金額(純數字)。
   - 必填欄位：項目、數量、單價、總金額。只要提供「數量＋單價」或「數量＋總金額」，必須自動互相計算補齊另一欄（例如 5 箱每箱 180 -> 數量 5, 單價 180, 總金額 900），絕不列入 missing；若連單價同總金額都無寫，將「單價/總金額」列入 missing。
3. 若用戶同時報告多項維修或採購（或上傳單據相片包含多項），可於 rows 輸出多列，或同時輸出 Maintenance 與 Purchasing 兩張表的 writes。
4. 若用戶正在補充剛才漏填的資料或修改預覽內容，結合「待確認上文」輸出更新後的完整 writes，並將 missing 設為 []。
5. 若 intent 為 query（查詢過往維修日期、採購歷史、本月開支或行政建議），必須仔細核對「關鍵字搜尋命中紀錄」與「過去365日完整明細」（不分英文大小寫），列出所有符合項目的日期、部門、項目、費用及維修人，用專業簡潔廣東話回答。"""

def get_live_groq_models(g_host, headers, need_vision):
    try:
        m_url = urllib.parse.urlunparse(("https", g_host, "/openai/v1/models", "", "", ""))
        mr = requests.get(m_url, headers=headers, timeout=10)
        if mr.status_code == 200:
            ids = [x.get("id", "") for x in mr.json().get("data", []) if x.get("id")]
            bad = ("whisper", "guard", "tts", "embed", "rerank", "orpheus")
            chat_ids = [m for m in ids if not any(b in m.lower() for b in bad)]
            if need_vision:
                vis = [m for m in chat_ids if any(k in m.lower() for k in ("vision", "scout", "maverick", "llava"))]
                return list(dict.fromkeys(vis + chat_ids))
            top = [m for m in chat_ids if any(k in m.lower() for k in ("70b", "llama-4", "qwen", "gpt-oss"))]
            return list(dict.fromkeys(top + chat_ids))
    except Exception:
        pass
    return []

def get_live_gemini_models(m_host, m_key):
    try:
        m_url = urllib.parse.urlunparse(("https", m_host, "/v1beta/models", "", f"key={m_key}", ""))
        mr = requests.get(m_url, timeout=10)
        if mr.status_code == 200:
            res = []
            for item in mr.json().get("models", []):
                if "generateContent" in item.get("supportedGenerationMethods", []):
                    name = item.get("name", "").replace("models/", "")
                    if "flash" in name.lower() and "image" not in name.lower() and "tts" not in name.lower():
                        res.append(name)
            return list(dict.fromkeys(reversed(res)))
    except Exception:
        pass
    return ["gemini-flash-latest"]

def call_ai(prompt_text, img_b64_list=None):
    import time
    errs = []
    g_key = os.environ.get("GROQ_API_KEY")
    m_key = os.environ.get("GEMINI_API_KEY")

    if g_key:
        g_host = "".join(["api", ".groq", ".com"])
        groq_url = urllib.parse.urlunparse(("https", g_host, "/openai/v1/chat/completions", "", "", ""))
        headers = {"Authorization": f"Bearer {g_key}", "Content-Type": "application/json"}
        content = [{"type": "text", "text": prompt_text}]
        for item in (img_b64_list or []):
            content.append({"type": "image_url", "image_url": {"url": f"data:{item.get('mime','image/jpeg')};base64,{item.get('data','')}"}})
        for m_id in itertools.islice(get_live_groq_models(g_host, headers, bool(img_b64_list)), 0, 4):
            try:
                payload = {
                    "model": m_id,
                    "messages": [{"role": "system", "content": CF_PROMPT}, {"role": "user", "content": content if img_b64_list else prompt_text}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1
                }
                r = requests.post(groq_url, headers=headers, json=payload, timeout=20)
                if r.status_code == 200:
                    raw = r.json().get("choices", [{}]).pop(0).get("message", {}).get("content", "")
                    s, e = raw.find("{"), raw.rfind("}")
                    if s != -1 and e > s:
                        return json.loads(raw[s:e+1])
                else:
                    errs.append(f"Groq({m_id}):{r.status_code}")
            except Exception as ex:
                errs.append(f"Groq:{str(ex)}")

    if m_key:
        m_host = "".join(["generativelanguage", ".googleapis", ".com"])
        parts = [{"text": CF_PROMPT + "\n\n" + prompt_text}]
        for item in (img_b64_list or []):
            parts.append({"inline_data": {"mime_type": item.get("mime", "image/jpeg"), "data": item.get("data", "")}})
        for model in itertools.islice(get_live_gemini_models(m_host, m_key), 0, 3):
            try:
                url = urllib.parse.urlunparse(("https", m_host, f"/v1beta/models/{model}:generateContent", "", f"key={m_key}", ""))
                body = {"contents": [{"parts": parts}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}}
                r = requests.post(url, json=body, timeout=25)
                if r.status_code == 200:
                    cands = r.json().get("candidates", [])
                    if cands:
                        txt = cands.pop(0).get("content", {}).get("parts", [{}]).pop(0).get("text", "")
                        s, e = txt.find("{"), txt.rfind("}")
                        if s != -1 and e > s:
                            return json.loads(txt[s:e+1])
                else:
                    errs.append(f"Gemini({model}):{r.status_code}")
                    time.sleep(1)
            except Exception as ex:
                errs.append(f"Gemini:{str(ex)}")

    raise Exception("AI 服務繁忙 (" + ", ".join(errs) + ")")

def process_message(user_id, text, img_b64_list=None):
    hk_now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    cur_date, cur_time = hk_now.strftime("%Y-%m-%d"), hk_now.strftime("%H:%M")
    clean_cmd = text.strip().lower()

    if clean_cmd in ["新話題", "重置", "clear", "reset"]:
        PENDING_ACTIONS.pop(user_id, None)
        CHAT_MEMORY.pop(user_id, None)
        return "🧹 已清空對話記憶，請開始新話題！"

    if clean_cmd in ["ok", "確認", "好", "入表", "yes"]:
        pending = PENDING_ACTIONS.pop(user_id, None)
        if not pending:
            return "目前沒有待確認的公司紀錄。"
        ok_all = all(write_rows(w.get("sheet"), w.get("rows", [])) for w in pending if w.get("sheet") and w.get("rows"))
        rep = "✅ 已正式寫入 CF record！" if ok_all else "⚠️ 寫入失敗，請檢查連線。"
        push_chat(user_id, "用戶", text)
        push_chat(user_id, "秘書", rep)
        return rep

    if clean_cmd in ["取消", "cancel", "唔要", "放棄"]:
        PENDING_ACTIONS.pop(user_id, None)
        rep = "🗑️ 已取消該筆待確認紀錄。"
        push_chat(user_id, "用戶", text)
        push_chat(user_id, "秘書", rep)
        return rep

    history = read_all_sheets()
    stats = compute_cf_stats(history, cur_date)
    all_records = get_all_history_trimmed(history, cur_date)
    matched_rows = search_history_records(history, text)
    prev_pending = PENDING_ACTIONS.get(user_id)
    recent_chat = "\n".join(CHAT_MEMORY.get(user_id, []))

    prompt = f"香港時間：{cur_date} {cur_time}\n全表背景統計：{json.dumps(stats, ensure_ascii=False)}\n關鍵字搜尋命中紀錄：{json.dumps(matched_rows, ensure_ascii=False)}\n過去365日完整明細：{json.dumps(all_records, ensure_ascii=False)}\n最近對話上下文：\n{recent_chat}\n待確認上文：{json.dumps(prev_pending, ensure_ascii=False)}\n用戶輸入：{text}"
    res = call_ai(prompt, img_b64_list)

    if res.get("intent") == "query":
        rep = res.get("reply") or "收到。"
    elif res.get("missing"):
        PENDING_ACTIONS[user_id] = res.get("writes") or prev_pending
        rep = f"⚠️ 請先補充以下資料再入表：**{ '、'.join(res.get('missing')) }**"
    elif res.get("writes"):
        PENDING_ACTIONS[user_id] = res.get("writes")
        pretty = [f"• [{w.get('sheet','')}] " + " | ".join(str(x) for x in r) for w in res.get("writes") for r in w.get("rows", [])]
        rep = f"📋 **預覽確認（回覆 `ok` 入表 / `取消`）：**\n" + "\n".join(pretty)
    else:
        rep = res.get("reply") or "⚠️ 無法識別內容，請補充維修或採購細節。"

    push_chat(user_id, "用戶", text or "[相片]")
    push_chat(user_id, "秘書", rep)
    return rep
# === [PART 2/3] END ===



# === [PART 3/3] START ===
HTML_CF = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#f472b6">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="秘書">
<link rel="icon" type="image/png" href="/icon.png">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="manifest" href="/manifest.json">
<title>秘書</title><style>
*{box-sizing:border-box}
html,body{margin:0;padding:0;width:100%;height:100dvh;max-height:100dvh;overflow:hidden;position:fixed;inset:0;font-family:-apple-system,sans-serif;background:#0f172a;color:#f8fafc;display:flex;flex-direction:column}
#chat{flex:1;min-height:0;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:12px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:85%;padding:10px 14px;border-radius:14px;white-space:pre-wrap;line-height:1.45;font-size:15px}
.u{align-self:flex-end;background:#0284c7}.b{align-self:flex-start;background:#1e293b;border:1px solid #334155}
.bar{flex-shrink:0;display:flex;gap:6px;padding:8px 12px;overflow-x:auto;background:#1e293b}.chip{background:#334155;color:#fff;border:none;padding:6px 12px;border-radius:16px;font-size:13px;white-space:nowrap;cursor:pointer}
.chip.tpl{background:#0369a1;color:#fff;font-weight:600}
.inp{flex-shrink:0;display:flex;gap:8px;padding:8px 10px calc(8px + env(safe-area-inset-bottom));background:#1e293b;align-items:center}input[type=text]{flex:1;min-width:0;padding:10px 12px;border-radius:20px;border:none;background:#0f172a;color:#fff;font-size:15px}
button.send{flex-shrink:0;background:#0284c7;color:#fff;border:none;padding:10px 16px;border-radius:20px;font-weight:600;cursor:pointer}
</style></head><body>
<div id="chat"><div class="msg b">🏢 秘書已就緒（CF record），直接輸入維修／採購紀錄，或按 📷 上傳單據／現場相片（最多 3 張）。</div></div>
<div class="bar">
  <button class="chip" onclick="quick('ok')">✅ 確認</button>
  <button class="chip" onclick="quick('取消')">🗑️ 取消</button>
  <button class="chip tpl" onclick="fillMaintTpl()">🔧 維修</button>
  <button class="chip" onclick="quick('新話題')">🧹 新話題</button>
  <button class="chip" onclick="quick('本月維修同採購各用咗幾多錢？')">📊 本月統計</button>
  <button class="chip" onclick="quick('按部門列出本月維修明細')">🛠️ 部門維修分析</button>
</div>
<div class="inp"><label style="font-size:22px;cursor:pointer;padding:2px 4px;flex-shrink:0">📷<input type="file" id="imgs" accept="image/*" multiple hidden></label>
<input type="text" id="txt" placeholder="輸入維修、採購或查詢..." onkeydown="if(event.key==='Enter')sendMsg()">
<button class="send" onclick="sendMsg()">發送</button></div>
<script>
let pin = localStorage.getItem("cf_pin") || prompt("請輸入公司秘書 PIN 密碼：");
if(pin) localStorage.setItem("cf_pin", pin);

async function checkAuth(){
  try {
    let r = await fetch("/api/cf_dash?pin=" + encodeURIComponent(pin || ""));
    if(r.status === 401){ localStorage.removeItem("cf_pin"); alert("PIN 錯誤"); location.reload(); }
  } catch(e) {}
}

function addMsg(t, cls){let d=document.createElement("div");d.className="msg "+cls;d.innerText=t;let c=document.getElementById("chat");c.appendChild(d);c.scrollTop=c.scrollHeight;return d;}
function quick(t){document.getElementById("txt").value=t;sendMsg();}

function fillMaintTpl(){
  let inp = document.getElementById("txt");
  inp.value = "維修部門： | 項目： | 位置： | 費用： | 維修人：";
  inp.focus();
}

async function sendMsg(){
  let inp=document.getElementById("txt"), fileList=Array.from(document.getElementById("imgs").files).slice(0,3), val=inp.value.trim();
  if(!val && fileList.length === 0) return;
  addMsg(val || "[傳送 " + fileList.length + " 張單據/相片]", "u"); inp.value="";
  let imgs=[];
  for(const f of fileList){
    let b64 = await new Promise(res=>{let fr=new FileReader();fr.onload=()=>res(fr.result.split(",").pop());fr.readAsDataURL(f);});
    imgs.push({mime: f.type || "image/jpeg", data: b64});
  }
  document.getElementById("imgs").value="";
  let waitMsg = addMsg("⏳ 處理中...", "b");
  try {
    let r = await fetch("/api/cf_chat", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pin:pin,text:val,imgs:imgs})});
    let d = await r.json();
    waitMsg.remove();
    addMsg(d.reply || d.error || "無回應", "b");
  } catch(e) {
    waitMsg.innerText = "⚠️ 連線錯誤：" + e.message;
  }
}

checkAuth();
</script></body></html>"""

def build_cf_png():
    import zlib, struct, math
    w, h = 192, 192
    cx, cy = 112.0, 82.0
    pts = []
    for i in range(10):
        ang = math.radians(-90 + i * 36)
        r = 56.0 if i % 2 == 0 else 25.0
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    gems = [(cx + 42.0 * math.cos(math.radians(-90 + k * 72)), cy + 42.0 * math.sin(math.radians(-90 + k * 72))) for k in range(5)]

    def seg_dist(px, py, x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        l2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2)) if l2 > 0 else 0.0
        return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            col_rgb = (255, 251, 235)
            min_star = min(seg_dist(x, y, pts[i][0], pts[i][1], pts[(i + 1) % 10][0], pts[(i + 1) % 10][1]) for i in range(10))
            if min_star <= 12.5:
                col_rgb = (244, 154, 194)
            for gx, gy in gems:
                if math.hypot(x - gx, y - gy) <= 5.2:
                    col_rgb = (239, 68, 108)
            if seg_dist(x, y, 76, 128, 34, 152) <= 13.5 or seg_dist(x, y, 68, 144, 48, 160) <= 10.5:
                col_rgb = (244, 154, 194)
            if seg_dist(x, y, 70, 134, 42, 148) <= 1.8 or seg_dist(x, y, 74, 143, 50, 156) <= 1.8:
                col_rgb = (134, 239, 172)
            for sx, sy in [(32, 148), (44, 157), (56, 163)]:
                if math.hypot(x - sx, y - sy) <= 2.6:
                    col_rgb = (253, 224, 71)
            hx = (x - 82.0) / 21.0
            hy = -(y - 122.0) / 21.0
            if (hx * hx + hy * hy - 1.0) ** 3 - hx * hx * (hy ** 3) <= 0.0:
                col_rgb = (253, 224, 71)
            raw.extend((col_rgb[0], col_rgb[1], col_rgb[2], 255))

    def chunk(tag, data):
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", zlib.crc32(tag + data) & 0xffffffff)
    ihdr = struct.pack("!IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")

def check_pin(req_pin):
    expected = str(APP_PIN or "").strip()
    return (not expected) or (str(req_pin or "").strip() == expected)

@app.route("/icon.png")
def cf_icon():
    from flask import Response
    return Response(build_cf_png(), mimetype="image/png")

@app.route("/manifest.json")
def cf_manifest():
    return jsonify({
        "name": "秘書",
        "short_name": "秘書",
        "start_url": "/app",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#f472b6",
        "icons": [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"}]
    })

@app.route("/")
@app.route("/app")
def index():
    return render_template_string(HTML_CF)

@app.route("/api/cf_dash")
def api_cf_dash():
    if not check_pin(request.args.get("pin")):
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"ok": True})

@app.route("/api/cf_chat", methods=["POST"])
def api_cf_chat():
    data = request.get_json(silent=True) or {}
    if not check_pin(data.get("pin")):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        rep = process_message("cf_admin", data.get("text", ""), data.get("imgs", []))
        return jsonify({"reply": rep})
    except Exception as e:
        return jsonify({"reply": f"⚠️ 系統錯誤：{str(e)}"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
# === [PART 3/3] END ===




