import os
import re
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
import time, uuid
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import requests
from bs4 import BeautifulSoup
import hashlib

app = Flask(__name__)
app.secret_key = "ghost-multi-2026-secure"

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

@app.before_request
def gate():
    open_paths = ("/login", "/register", "/static")
    if request.path in open_paths or request.path.startswith("/static"):
        return
    if not session.get("user_id"):
        if request.path == "/send" and request.method == "POST":
            return jsonify({"error": "not logged in"}), 401
        return redirect("/login")
DB = os.path.join(os.path.dirname(__file__), "ghost.db")

NVIDIA_KEY = "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"
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1"

def init_db():
    with sqlite3.connect(DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created TEXT
        )""")
        # Migrate configs table
        c.execute("""CREATE TABLE IF NOT EXISTS configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            url TEXT,
            token TEXT,
            category_id TEXT,
            rule_id TEXT,
            web_user TEXT,
            web_pass TEXT,
            created TEXT,
            from_address TEXT,
            UNIQUE(user_id, name)
        )""")
        # Add user_id column if old table exists without it
        try:
            c.execute("ALTER TABLE configs ADD COLUMN user_id INTEGER")
        except:
            pass
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            status TEXT,
            total INTEGER,
            sent INTEGER,
            failed INTEGER,
            done INTEGER,
            start REAL,
            error TEXT
        )""")
        try:
            c.execute("ALTER TABLE jobs ADD COLUMN user_id INTEGER")
        except:
            pass
init_db()

def job_set(jid, **kw):
    with db() as c:
        cols = ",".join(kw.keys())
        vals = ",".join("?" for _ in kw)
        c.execute(f"INSERT OR REPLACE INTO jobs (id,{cols}) VALUES (?,{vals})", (jid, *kw.values()))

def job_update(jid, **kw):
    if not kw: return
    sets = ",".join(f"{k}=?" for k in kw)
    with db() as c:
        c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*kw.values(), jid))

def job_get(jid):
    with db() as c:
        r = c.execute("SELECT status,total,sent,failed,done,start,error FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r: return None
    return {"status": r[0], "total": r[1], "sent": r[2], "failed": r[3], "done": r[4], "start": r[5], "error": r[6]}

def db():
    return sqlite3.connect(DB)

def letter_clean(html):
    import re as _re
    hide = (
        '<style type="text/css">'
        'div[style*="color:#bbb"],div[style*=\'color:#bbb\']{display:none!important;max-height:0!important;height:0!important;overflow:hidden!important;font-size:0!important;line-height:0!important;mso-hide:all}'
        'div[style*="color:#bbb"]+br,div[style*=\'color:#bbb\']+br{display:none!important;height:0!important;mso-hide:all}'
        'p:has(a[href*="hd_powered_by"]),p[style*="color:#888"],p[style*="color:#999"]{display:none!important;max-height:0!important;height:0!important;overflow:hidden!important;font-size:0!important;line-height:0!important;mso-hide:all}'
        '.powered-by, .jitbit-footer, #poweredby {display:none!important}'
        '</style>'
    )
    raw = html.lstrip()
    if raw[:9].lower() == '<!doctype' or raw[:5].lower() == '<html':
        if _re.search(r'<head[^>]*>', html, _re.I):
            html = _re.sub(r'(<head[^>]*>)', r'\1' + hide, html, count=1, flags=_re.I)
        else:
            html = _re.sub(r'(<html[^>]*>)', r'\1<head>' + hide + '</head>', html, count=1, flags=_re.I)
        return '<!--html-->' + html
    return '<!--html-->' + hide + html

def nvidia_chat(messages):
    try:
        r = requests.post(NVIDIA_URL, headers={
            "Authorization": f"Bearer {NVIDIA_KEY}",
            "Content-Type": "application/json"
        }, json={"model": NVIDIA_MODEL, "messages": messages, "temperature": 0.2, "max_tokens": 512}, timeout=40)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
    except Exception:
        pass
    return None

class SetupAgent:
    def __init__(self, base_url, username, password):
        self.base = base_url.rstrip("/")
        self.user = username
        self.pw = password
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "GHOST-VPS/1.0"

    def login(self):
        for attempt in range(2):
            r = self.s.get(f"{self.base}/User/Login", timeout=20)
            m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text)
            if not m:
                continue
            r = self.s.post(f"{self.base}/User/Login", data={
                "Username": self.user,
                "Password": self.pw,
                "__RequestVerificationToken": m.group(1),
                "rememberMe": "true"
            }, allow_redirects=False, timeout=20)
            loc = r.headers.get("Location", "")
            if "TwoFactorAuth" in loc:
                raise RuntimeError("2FA_REQUIRED")
            if r.status_code in (302, 303) and "Login" not in loc:
                return True
            if "JitbitAuthCore" in r.headers.get("Set-Cookie", ""):
                return True
        raise RuntimeError("Login failed — check URL, username, password")

    def login_2fa(self, code):
        r = self.s.post(f"{self.base}/User/TwoFactorAuth", data={"code": code, "remember": "true"}, allow_redirects=False, timeout=20)
        loc = r.headers.get("Location", "")
        if r.status_code in (302, 303) and "TwoFactor" not in loc:
            return True
        raise RuntimeError("2FA code invalid")

    def get_token(self):
        for path in ["/User/Token", "/helpdesk/User/Token", "/Admin/API"]:
            try:
                r = self.s.get(f"{self.base}{path}", timeout=20)
                m = re.search(r'eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+', r.text)
                if m:
                    return m.group(0)
            except Exception:
                continue
        raise RuntimeError("Token not found — open Admin > API and generate one")

    def list_categories(self, token):
        r = requests.get(f"{self.base}/helpdesk/api/categories", headers={"Authorization": f"Bearer {token}"}, timeout=15)
        return r.json() if r.status_code == 200 else []

    def ensure_category(self, token):
        cats = self.list_categories(token)
        for c in cats:
            if "GHOST" in c.get("Name", "") or "General" in c.get("Name", ""):
                return str(c["CategoryID"])
        for path in ["/helpdesk/api/AddCategory", "/api/AddCategory"]:
            try:
                r = requests.post(f"{self.base}{path}", headers={"Authorization": f"Bearer {token}"}, data={"name": "GHOST Campaigns"}, timeout=15)
                if r.status_code == 200 and r.text.strip().isdigit():
                    return r.text.strip()
            except Exception:
                continue
        return str(cats[0]["CategoryID"]) if cats else "1"

    def ensure_rule(self, category_id):
        r = self.s.get(f"{self.base}/Admin/Rules", timeout=20)
        m = re.search(r'/Admin/EditRule/(\d+)[^>]*>Direct letter no delimiter', r.text)
        if m:
            return m.group(1)
        payload = {
            "name": "Direct letter no delimiter",
            "description": "GHOST auto",
            "TriggerType": "0",
            "hdnConditions": json.dumps([{"ConditionType": "CategoryCondition", "NOT": "false", "CategoryId": category_id}]),
            "hdnActions": json.dumps([
                {"ActionType": "NotifySubmitter", "NotificationSubject": "#Subject#", "NotificationText": "<!--html--><p>#Body#</p>"},
                {"ActionType": "AbortEmailAction"}
            ]),
            "hdnElseActions": "[]"
        }
        self.s.post(f"{self.base}/helpdesk/Admin/SaveRule/0", data=payload, allow_redirects=False, timeout=20)
        r2 = self.s.get(f"{self.base}/Admin/Rules", timeout=20)
        m = re.search(r'/Admin/EditRule/(\d+)[^>]*>Direct letter no delimiter', r2.text)
        return m.group(1) if m else "0"

    def fix_email_settings(self):
        try:
            url = f"{self.base}/Admin/Email"
            page = self.s.get(url, timeout=20)
            fields = {}
            from bs4 import BeautifulSoup as BS
            soup = BS(page.text, "html.parser")
            for inp in soup.select("input[name], textarea[name], select[name]"):
                n = inp.get("name")
                if not n:
                    continue
                if inp.name == "input" and inp.get("type") == "checkbox":
                    if inp.has_attr("checked"):
                        fields[n] = inp.get("value") or "true"
                else:
                    fields[n] = inp.get("value") or ""
            fields["UseFromNameForUserOriginatedMsgs"] = "true"
            if not fields.get("ReplyToAddress"):
                fields["ReplyToAddress"] = fields.get("FromAddress", "")
            self.s.post(url, data=fields, timeout=20)
        except Exception:
            pass

    def run(self):
        self.login()
        self.fix_email_settings()
        for _ in range(3):
            try:
                gs = self.s.get(f"{self.base}/Admin/GeneralSettings", timeout=20)
                from bs4 import BeautifulSoup as BS
                gsoup = BS(gs.text, "html.parser")
                gf = {}
                for inp in gsoup.select("input[name], textarea[name], select[name]"):
                    n = inp.get("name")
                    if not n: continue
                    if inp.get("type") == "checkbox":
                        if inp.has_attr("checked") or n == "HidePoweredBy":
                            gf[n] = inp.get("value") or "true"
                    else: gf[n] = inp.get("value") or ""
                gf["HidePoweredBy"] = "true"
                self.s.post(f"{self.base}/Admin/GeneralSettings", data=gf, timeout=20)
                # verify
                gs2 = self.s.get(f"{self.base}/Admin/GeneralSettings", timeout=20)
                if 'HidePoweredBy' in gs2.text and 'checked' in gs2.text:
                    break
            except Exception: pass
        token = self.get_token()
        cat = self.ensure_category(token)
        rule = self.ensure_rule(cat)
        from_addr = ""
        try:
            er = self.s.get(f"{self.base}/Admin/Email", timeout=20)
            from bs4 import BeautifulSoup as BS
            esoup = BS(er.text, "html.parser")
            fa = esoup.select_one("#FromAddress")
            if fa and fa.get("value"):
                from_addr = fa.get("value").strip()
            url = f"{self.base}/helpdesk/Admin/EditCategory/{cat}"
            page = self.s.get(url, timeout=20)
            fields = {}
            soup = BeautifulSoup(page.text, "html.parser")
            for inp in soup.select("input[name], textarea[name]"):
                fields[inp.get("name")] = inp.get("value") or inp.text or ""
            fields["FromName"] = "Support"
            if from_addr:
                fields["FromAddress"] = from_addr
            self.s.post(url, data=fields, timeout=20)
        except Exception:
            pass
        return {"url": self.base, "token": token, "category_id": cat, "rule_id": rule, "web_user": self.user, "web_pass": self.pw, "from_address": from_addr}

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db() as c:
            row = c.execute("SELECT id, password FROM users WHERE username=?", (username,)).fetchone()
        if row and row[1] == hash_pw(password):
            session["user_id"] = row[0]
            session["username"] = username
            return redirect("/")
        return render_template("login.html", error="wrong")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        invite = request.form.get("invite", "").strip()
        if invite != "HAVIJ26":
            return render_template("register.html", error="wrong invite code")
        if len(username) < 3 or len(password) < 4:
            return render_template("register.html", error="Username min 3 chars, password min 4")
        try:
            with db() as c:
                c.execute("INSERT INTO users (username, password, created) VALUES (?, ?, datetime('now'))",
                          (username, hash_pw(password)))
            return redirect("/login?registered=1")
        except sqlite3.IntegrityError:
            return render_template("register.html", error="Username already exists")
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        old = request.form.get("old", "")
        new = request.form.get("new", "")
        uid = session.get("user_id")
        if not uid:
            return redirect("/login")
        if len(new) < 4:
            return render_template("settings.html", error="password min 4")
        with db() as c:
            row = c.execute("SELECT password FROM users WHERE id=?", (uid,)).fetchone()
            if not row or row[0] != hash_pw(old):
                return render_template("settings.html", error="wrong current password")
            c.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(new), uid))
        return render_template("settings.html", ok="password updated")
    return render_template("settings.html")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if request.method == "POST":
        data = request.json
        try:
            agent = SetupAgent(data["url"], data["user"], data["password"])
            cfg = agent.run()
            name = data.get("name") or cfg["url"].split("//")[-1].split(".")[0]
            uid = session.get("user_id")
            with db() as c:
                c.execute("""INSERT OR REPLACE INTO configs (user_id,name,url,token,category_id,rule_id,web_user,web_pass,created,from_address)
                             VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)""",
                          (uid, name, cfg["url"], cfg["token"], cfg["category_id"], cfg["rule_id"], cfg["web_user"], cfg["web_pass"], cfg.get("from_address","")))
            summary = nvidia_chat([
                {"role": "system", "content": "One short sentence summary of this config."},
                {"role": "user", "content": json.dumps(cfg)}
            ]) or "Setup complete."
            return jsonify({"ok": True, "config": cfg, "name": name, "summary": summary})
        except RuntimeError as e:
            if str(e) == "2FA_REQUIRED":
                return jsonify({"ok": False, "error": "2FA_REQUIRED", "message": "enter 2FA code"}), 400
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    return render_template("setup.html")

@app.route("/setup-2fa", methods=["POST"])
def setup_2fa():
    data = request.json
    try:
        agent = SetupAgent(data["url"], data["user"], data["password"])
        agent.login_2fa(data["code"])
        cfg = agent.run()
        name = data.get("name") or cfg["url"].split("//")[-1].split(".")[0]
        uid = session.get("user_id")
        with db() as c:
            c.execute("""INSERT OR REPLACE INTO configs (user_id,name,url,token,category_id,rule_id,web_user,web_pass,created,from_address)
                         VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)""",
                      (uid, name, cfg["url"], cfg["token"], cfg["category_id"], cfg["rule_id"], cfg["web_user"], cfg["web_pass"], cfg.get("from_address","")))
        return jsonify({"ok": True, "config": cfg, "name": name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/configs-ui")
def configs_ui():
    return render_template("configs.html")

@app.route("/configs", methods=["GET", "POST", "DELETE"])
def configs():
    if request.method == "POST":
        d = request.json
        uid = session.get("user_id")
        with db() as c:
            c.execute("""INSERT OR REPLACE INTO configs (user_id,name,url,token,category_id,rule_id,web_user,web_pass,created,from_address)
                         VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)""",
                      (uid, d["name"], d["url"], d["token"], d["category_id"], d["rule_id"], d.get("web_user",""), d.get("web_pass",""), d.get("from_address","")))
        return jsonify({"ok": True})
    if request.method == "DELETE":
        name = request.args.get("name")
        uid = session.get("user_id")
        with db() as c:
            c.execute("DELETE FROM configs WHERE name=? AND user_id=?", (name, uid))
        return jsonify({"ok": True})
    with db() as c:
        uid = session.get("user_id")
        rows = c.execute("SELECT name,url,category_id,rule_id,created,from_address FROM configs WHERE user_id=?", (uid,)).fetchall()
    return jsonify([{"name": r[0], "url": r[1], "category_id": r[2], "rule_id": r[3], "created": r[4], "from_address": r[5] or ""} for r in rows])

def tg_send(token, chat, text):
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception:
        pass

def run_job(job_id, base, token, cat, rule, wuser, wpw, emails, subject, html, sname, semail, tg_token, tg_chat):
    job_update(job_id, status="running", start=time.time(), emails=json.dumps(emails), current_index=0)
    try:
        agent = SetupAgent(base, wuser, wpw)
        agent.login()
        url = f"{base}/helpdesk/Admin/EditCategory/{cat}"
        page = agent.s.get(url, timeout=20)
        fields = {}
        soup = BeautifulSoup(page.text, "html.parser")
        for inp in soup.select("input[name], textarea[name]"):
            fields[inp.get("name")] = inp.get("value") or inp.text or ""
        fields["FromName"] = sname[:80]
        try:
            er = agent.s.get(f"{base}/Admin/Email", timeout=20)
            ef = {}
            esoup = BeautifulSoup(er.text, "html.parser")
            for inp in esoup.select("input[name], textarea[name], select[name]"):
                n = inp.get("name")
                if not n: continue
                if inp.name == "input" and inp.get("type") == "checkbox":
                    if inp.has_attr("checked"): ef[n] = inp.get("value") or "true"
                else: ef[n] = inp.get("value") or ""
            ef["FromName"] = sname[:80]
            ef["EmailNotificationsEnabled"] = "true"
            ef["EnableTicketConfirmationNotification"] = "true"
            ef["UseFromNameForUserOriginatedMsgs"] = "true"
            agent.s.post(f"{base}/Admin/Email", data=ef, timeout=20)
        except Exception: pass
        if semail:
            fields["FromAddress"] = semail
        agent.s.post(url, data=fields, timeout=20)
        payload = {
            "name": "Direct letter no delimiter",
            "description": "GHOST",
            "TriggerType": "0",
            "hdnConditions": json.dumps([{"ConditionType": "CategoryCondition", "NOT": "false", "CategoryId": cat}]),
            "hdnActions": json.dumps([
                {"ActionType": "NotifySubmitter", "NotificationSubject": subject, "NotificationText": letter_clean(html)},
                {"ActionType": "AbortEmailAction"}
            ]),
            "hdnElseActions": "[]"
        }
        agent.s.post(f"{base}/helpdesk/Admin/SaveRule/{rule}", data=payload, allow_redirects=False, timeout=20)
    except Exception as e:
        job_update(job_id, status="error", error=str(e))
        return

    def one(email):
        for attempt in range(5):
            try:
                r = requests.get(f"{base}/helpdesk/api/UserByEmail", params={"email": email}, headers={"Authorization": f"Bearer {token}"}, timeout=12)
                uid = None
                if r.status_code == 200:
                    try: uid = r.json().get("UserID")
                    except: pass
                if not uid:
                    r = requests.post(f"{base}/helpdesk/api/CreateUser", headers={"Authorization": f"Bearer {token}"}, data={"email": email, "sendWelcomeEmail": "false"}, timeout=12)
                    if r.status_code == 200 and r.text.strip().isdigit():
                        uid = int(r.text.strip())
                if not uid:
                    time.sleep(0.4)
                    continue
                r = requests.post(f"{base}/helpdesk/api/ticket", headers={"Authorization": f"Bearer {token}"}, data={
                    "categoryId": cat, "subject": subject, "body": html, "priorityId": 0,
                    "userId": uid, "origin": 3, "suppressConfirmation": "false"
                }, timeout=15)
                if r.status_code < 400:
                    return True
                if r.status_code == 429:
                    time.sleep(1.5)
                    continue
            except Exception:
                time.sleep(0.6)
                continue
        return False

    sent = 0
    failed = 0
    done = 0
    last_k = 0
    group = 10
    workers = 3
    for i in range(0, len(emails), group):
        chunk = emails[i:i+group]
        with ThreadPoolExecutor(max_workers=min(workers, len(chunk))) as ex:
            results = list(ex.map(one, chunk))
        for ok in results:
            if ok: sent += 1
            else: failed += 1
            done += 1
        job_update(job_id, sent=sent, failed=failed, done=done, current_index=i+len(chunk))
        time.sleep(1.5)
        k = sent // 1000
        if k > last_k:
            last_k = k
            tg_send(tg_token, tg_chat, f"<b>GHOST</b>\n{k}K done\n{sent} sent · {failed} failed")
    job_update(job_id, status="finished", sent=sent, failed=failed, done=done)
    mins = (time.time() - job_get(job_id)["start"]) / 60
    tg_send(tg_token, tg_chat, f"<b>GHOST finished</b>\n{sent} sent successfully\n{failed} failed\n{mins:.1f} minutes\nlist complete")

@app.route("/send", methods=["GET", "POST"])
def send():
    if request.method == "POST":
        d = request.json
        with db() as c:
            uid = session.get("user_id")
            row = c.execute("SELECT url,token,category_id,rule_id,web_user,web_pass,from_address FROM configs WHERE name=? AND user_id=?", (d["name"], uid)).fetchone()
        if not row:
            return jsonify({"error": "config not found"}), 404
        base, token, cat, rule, wuser, wpw, stored_from = row
        emails = [e.strip().lower() for e in d.get("emails","").splitlines() if "@" in e]
        if not emails:
            return jsonify({"error": "no emails"}), 400
        subject = d.get("subject","Update")
        html = d.get("html","<p>Hello</p>")
        sname = d.get("sender_name","Support")
        semail = d.get("sender_email","") or stored_from or ""
        tg_token = d.get("tg_token","")
        tg_chat = d.get("tg_chat","")
        job_id = str(uuid.uuid4())[:8]
        job_set(job_id, status="queued", total=len(emails), sent=0, failed=0, done=0, start=time.time(), error="")
        payload = json.dumps({
            "job_id": job_id, "base": base, "token": token, "cat": cat, "rule": rule,
            "wuser": wuser, "wpw": wpw, "emails": emails, "subject": subject, "html": html,
            "sname": sname, "semail": semail, "tg_token": tg_token, "tg_chat": tg_chat
        })
        import subprocess
        jobfile = f"/tmp/ghost-job-{job_id}.json"
        with open(jobfile, "w") as f:
            f.write(payload)
        subprocess.Popen(["/opt/ghost/venv/bin/python3", "/opt/ghost/worker.py", jobfile],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True, "job": job_id, "total": len(emails)})
    return render_template("send.html")

@app.route("/status/<job_id>")
def status(job_id):
    j = job_get(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    return jsonify(j)

@app.route("/jobs")
def jobs_list():
    with db() as c:
        rows = c.execute("SELECT id,status,total,sent,failed,done FROM jobs ORDER BY start DESC LIMIT 20").fetchall()
    return jsonify([{"id": r[0], "status": r[1], "total": r[2], "sent": r[3], "failed": r[4], "done": r[5]} for r in rows])

@app.route("/job/<job_id>/stop", methods=["POST"])
def job_stop(job_id):
    job_update(job_id, status="stopped")
    return jsonify({"ok": True})

@app.route("/job/<job_id>/resume", methods=["POST"])
def job_resume(job_id):
    j = job_get(job_id)
    if not j or j["status"] not in ("stopped", "error"):
        return jsonify({"error": "not resumable"}), 400
    job_update(job_id, status="queued")
    return jsonify({"ok": True})

@app.route("/job/<job_id>/delete", methods=["POST"])
def job_delete(job_id):
    with db() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return jsonify({"ok": True})

@app.route("/jobs/delete-all", methods=["POST"])
def jobs_delete_all():
    with db() as c:
        c.execute("DELETE FROM jobs")
    return jsonify({"ok": True})

@app.route("/jobs-ui")
def jobs_ui():
    return render_template("jobs.html")

def run_clean(url, token):
    closed = 0
    for page in range(1, 50):
        try:
            r = requests.get(f"{url}/helpdesk/api/tickets", params={"page": page, "count": 100}, headers={"Authorization": f"Bearer {token}"}, timeout=15)
            if r.status_code != 200:
                break
            tickets = r.json()
            if not tickets:
                break
            for t in tickets:
                tid = t.get("IssueID") or t.get("id")
                if not tid:
                    continue
                requests.post(f"{url}/helpdesk/api/SetTicketStatus", headers={"Authorization": f"Bearer {token}"}, data={"id": tid, "statusId": 3}, timeout=10)
                closed += 1
            if len(tickets) < 100:
                break
        except Exception:
            break
    return closed

@app.route("/clean", methods=["POST"])
def clean():
    name = request.json.get("name")
    with db() as c:
        row = c.execute("SELECT url,token FROM configs WHERE name=?", (name,)).fetchone()
    if not row:
        return jsonify({"error": "config not found"}), 404
    url, token = row
    closed = run_clean(url, token)
    return jsonify({"ok": True, "closed": closed})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
