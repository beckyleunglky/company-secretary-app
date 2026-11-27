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
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
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

def filter_recent_3_days(history, cur_date):
    try:
        base_dt = datetime.strptime(cur_date, "%Y-%m-%d")
        valid_dates = {(base_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)}
    except Exception:
        valid_dates = {cur_date}
    trimmed = {}
    for k, rows in history.items():
        recent = [r for r in rows if isinstance(r, list) and len(r) > 0 and norm_date(col(r, 0)) in valid_dates]
        trimmed[k] = list(itertools.islice(reversed(recent), 0, 25))[::-1]
    return trimmed

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
5. 若 intent 為 query（查詢本月維修費、採購開支、各部門統計或行政建議），writes 與 missing 設為 []，reply 用專業簡潔廣東話回答。"""

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
    recent_3d = filter_recent_3_days(history, cur_date)
    prev_pending = PENDING_ACTIONS.get(user_id)
    recent_chat = "\n".join(CHAT_MEMORY.get(user_id, []))

    prompt = f"香港時間：{cur_date} {cur_time}\n全表背景統計：{json.dumps(stats, ensure_ascii=False)}\n最近3日明細：{json.dumps(recent_3d, ensure_ascii=False)}\n最近對話上下文：\n{recent_chat}\n待確認上文：{json.dumps(prev_pending, ensure_ascii=False)}\n用戶輸入：{text}"
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
HTML_CF_APP = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Company Secretary | CF record</title><style>
body{font-family:-apple-system,sans-serif;background:#0f172a;color:#f8fafc;margin:0;display:flex;flex-direction:column;height:100vh}
.top{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:12px;background:#1e293b;text-align:center}
.card{background:#0f172a;padding:8px;border-radius:10px}.val{font-size:15px;font-weight:700;color:#34d399}.lbl{font-size:11px;color:#94a3b8}
#chat{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:85%;padding:10px 14px;border-radius:14px;white-space:pre-wrap;line-height:1.45;font-size:15px}
.u{align-self:flex-end;background:#059669}.b{align-self:flex-start;background:#1e293b;border:1px solid #334155}
.bar{display:flex;gap:6px;padding:8px 12px;overflow-x:auto;background:#1e293b}.chip{background:#334155;color:#fff;border:none;padding:6px 12px;border-radius:16px;font-size:13px;white-space:nowrap}
.inp{display:flex;gap:8px;padding:10px;background:#1e293b}input[type=text]{flex:1;padding:10px;border-radius:20px;border:none;background:#0f172a;color:#fff;font-size:15px}
button.send{background:#059669;color:#fff;border:none;padding:0 16px;border-radius:20px;font-weight:600}
</style></head><body>
<div class="top"><div class="card"><div class="val" id="m_fee">--</div><div class="lbl">本月維修($)</div></div>
<div class="card"><div class="val" id="p_fee">--</div><div class="lbl">本月採購($)</div></div>
<div class="card"><div class="val" id="t_fee">--</div><div class="lbl">本月總支出($)</div></div></div>
<div id="chat"><div class="msg b">💼 公司行政秘書已就緒（CF record），直接輸入維修／採購紀錄，或按 📷 上傳單據／現場相片（最多 3 張）。</div></div>
<div class="bar"><button class="chip" onclick="quick('ok')">✅ ok 入表</button><button class="chip" onclick="quick('取消')">🗑️ 取消</button>
<button class="chip" onclick="quick('新話題')">🧹 新話題</button>
<button class="chip" onclick="quick('本月維修同採購各用咗幾多錢？')">📊 本月統計</button><button class="chip" onclick="quick('邊個部門本月最多維修費？')">🔧 部門維修分析</button></div>
<div class="inp"><label style="font-size:22px;cursor:pointer;padding:4px">📷<input type="file" id="imgs" accept="image/*" multiple hidden></label>
<input type="text" id="txt" placeholder="輸入維修、採購或查詢..." onkeydown="if(event.key==='Enter')sendMsg()">
<button class="send" onclick="sendMsg()">發送</button></div>
<script>
let pin = localStorage.getItem("cf_sec_pin") || prompt("請輸入公司秘書專屬 PIN 密碼：");
if(pin) localStorage.setItem("cf_sec_pin", pin);
async function loadDash(){
  let r = await fetch("/api/dash?pin="+encodeURIComponent(pin));
  if(r.status===401){localStorage.removeItem("cf_sec_pin");alert("PIN 錯誤");location.reload();return;}
  let d = await r.json();
  document.getElementById("m_fee").innerText = "$" + d.maint_month;
  document.getElementById("p_fee").innerText = "$" + d.purch_month;
  document.getElementById("t_fee").innerText = "$" + d.total_month;
}
function addMsg(t, cls){let d=document.createElement("div");d.className="msg "+cls;d.innerText=t;let c=document.getElementById("chat");c.appendChild(d);c.scrollTop=c.scrollHeight;}
function quick(t){document.getElementById("txt").value=t;sendMsg();}
async function sendMsg(){
  let inp=document.getElementById("txt"), files=document.getElementById("imgs").files, val=inp.value.trim();
  if(!val && !files.length) return;
  addMsg(val || "[傳送 "+files.length+" 張相片]", "u"); inp.value="";
  let imgs=[];
  for(let i=0;i<Math.min(files.length,3);i++){
    let b64 = await new Promise(res=>{let fr=new FileReader();fr.onload=()=>res(fr.result.split(",").pop());fr.readAsDataURL(files.item(i));});
    imgs.push({mime: files.item(i).type || "image/jpeg", data: b64});
  }
  document.getElementById("imgs").value="";
  let r = await fetch("/api/chat", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pin:pin,text:val,images:imgs})});
  let d = await r.json(); addMsg(d.reply || d.error, "b"); loadDash();
}
loadDash();
</script></body></html>"""

@app.route("/")
def home():
    return "Company Secretary API Online", 200

@app.route("/app")
def web_app():
    return render_template_string(HTML_CF_APP)

@app.route("/api/dash")
def api_dash():
    if request.args.get("pin") != APP_PIN:
        return jsonify({"error": "Unauthorized"}), 401
    cur_date = datetime.now(pytz.timezone("Asia/Hong_Kong")).strftime("%Y-%m-%d")
    return jsonify(compute_cf_stats(read_all_sheets(), cur_date))

@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.json or {}
    if body.get("pin") != APP_PIN:
        return jsonify({"error": "PIN 錯誤"}), 401
    try:
        rep = process_message("cf_user", body.get("text", ""), body.get("images", []))
        return jsonify({"reply": rep})
    except Exception as e:
        return jsonify({"reply": f"⚠️ {str(e)}"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
# === [PART 3/3] END ===



