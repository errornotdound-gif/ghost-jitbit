#!/usr/bin/env python3
import sys, json, time, sqlite3, requests
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
import re

DB = "/opt/ghost/ghost.db"

def job_update(jid, **kw):
    sets = ",".join(f"{k}=?" for k in kw)
    with sqlite3.connect(DB) as c:
        c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*kw.values(), jid))

def job_get(jid):
    with sqlite3.connect(DB) as c:
        r = c.execute("SELECT status,total,sent,failed,done,start,error FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r: return None
    return {"status": r[0], "total": r[1], "sent": r[2], "failed": r[3], "done": r[4], "start": r[5], "error": r[6]}

def tg_send(token, chat, text):
    if not token or not chat: return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception:
        pass

def letter_clean(html):
    html = re.sub(r'<div[^>]*class="[^"]*jitbit[^"]*"[^>]*>.*?</div>', '', html, flags=re.DOTALL|re.I)
    html = re.sub(r'Powered by Jitbit.*?(?:</a>|</div>|</p>|$)', '', html, flags=re.DOTALL|re.I)
    html = re.sub(r'<a[^>]*href="[^"]*jitbit[^"]*"[^>]*>.*?</a>', '', html, flags=re.DOTALL|re.I)
    html = re.sub(r'🚀.*?Jitbit Helpdesk', '', html, flags=re.I)
    html = re.sub(r'-- reply above this line --', '', html, flags=re.I)
    html = re.sub(r'reply above this line', '', html, flags=re.I)
    hide = """<style type="text/css">
div[style*="color:#bbb"], div[style*="color: #bbb"], div[style*="color:#BBB"] { display:none !important; visibility:hidden !important; height:0 !important; max-height:0 !important; overflow:hidden !important; font-size:0 !important; line-height:0 !important; mso-hide:all !important; }
p[style*="color:#888"], p[style*="color: #888"], p[style*="color:#999"] { display:none !important; visibility:hidden !important; height:0 !important; max-height:0 !important; overflow:hidden !important; font-size:0 !important; line-height:0 !important; mso-hide:all !important; }
.jitbit-powered, .footer, [class*="powered"], [id*="powered"] { display:none !important; }
</style>
<!--[if !mso]><!-->
<style>
div[style*="color:#bbb"] { display:none !important; }
p[style*="color:#888"] { display:none !important; }
</style>
<!--<![endif]-->
"""
    html = hide + html
    return html.strip()

class SetupAgent:
    def __init__(self, base, user, pw):
        self.base = base.rstrip("/")
        self.user = user
        self.pw = pw
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "GHOST/1.0"
    def login(self):
        for _ in range(2):
            r = self.s.get(f"{self.base}/User/Login", timeout=20)
            m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', r.text)
            if not m: continue
            r = self.s.post(f"{self.base}/User/Login", data={"Username": self.user, "Password": self.pw, "__RequestVerificationToken": m.group(1), "rememberMe": "true"}, allow_redirects=False, timeout=20)
            if r.status_code in (302, 303) and "Login" not in r.headers.get("Location", ""):
                return True
        raise RuntimeError("Login failed")

def run(job_id, base, token, cat, rule, wuser, wpw, emails, subject, html, sname, semail, tg_token, tg_chat):
    job_update(job_id, status="running", start=time.time())
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
        if semail: fields["FromAddress"] = semail
        agent.s.post(url, data=fields, timeout=20)
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
                r = requests.get(f"{base}/helpdesk/api/UserByEmail", params={"email": email}, headers={"Authorization": f"Bearer {token}"}, timeout=15)
                uid = None
                if r.status_code == 200:
                    try: uid = r.json().get("UserID")
                    except: pass
                if not uid:
                    r = requests.post(f"{base}/helpdesk/api/CreateUser", headers={"Authorization": f"Bearer {token}"}, data={"email": email, "sendWelcomeEmail": "false"}, timeout=15)
                    if r.status_code == 200 and r.text.strip().isdigit():
                        uid = int(r.text.strip())
                if not uid:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                r = requests.post(f"{base}/helpdesk/api/ticket", headers={"Authorization": f"Bearer {token}"}, data={
                    "categoryId": cat, "subject": subject, "body": html, "priorityId": 0,
                    "userId": uid, "origin": 3, "suppressConfirmation": "false"
                }, timeout=20)
                if r.status_code < 400: return True
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                    continue
            except Exception:
                time.sleep(0.8 * (attempt + 1))
        return False

    sent = failed = done = last_k = 0
    group = 20
    for i in range(0, len(emails), group):
        j=job_get(job_id)
        if j and j["status"]=="stopped":
            return
        chunk = emails[i:i+group]
        with ThreadPoolExecutor(max_workers=min(4, len(chunk))) as ex:
            results = list(ex.map(one, chunk))
        for ok in results:
            if ok: sent += 1
            else: failed += 1
            done += 1
        job_update(job_id, sent=sent, failed=failed, done=done)
        k = sent // 1000
        if k > last_k:
            last_k = k
            tg_send(tg_token, tg_chat, f"<b>GHOST</b>\n{k}K done\n{sent} sent · {failed} failed")
        time.sleep(0.4)
    job_update(job_id, status="finished", sent=sent, failed=failed, done=done)
    start = job_get(job_id)["start"] or time.time()
    mins = (time.time() - start) / 60
    tg_send(tg_token, tg_chat, f"<b>GHOST finished</b>\n{sent} sent successfully\n{failed} failed\n{mins:.1f} minutes\nlist complete")

if __name__ == "__main__":
    data = json.load(open(sys.argv[1]))
    import os; os.remove(sys.argv[1])
    run(**data)
