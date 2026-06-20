"""
SSUET Survey Bot — Flask Backend (FAST LOGIN PATH)
Run: pip install flask flask-cors selenium firebase-admin
Then: python server_FAST.py
Open: http://localhost:5000

KEY CHANGES:
✓ /api/login returns in ~3-5 seconds (login only, no scraping, no blocking Firebase)
✓ /api/scrape starts background thread, returns immediately
✓ UI polls /api/status to wait for scraping completion
✓ Firebase saves happen asynchronously in background threads
"""

import time
import threading
import os
import json
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoAlertPresentException, UnexpectedAlertPresentException,
    InvalidSessionIdException
)

# ─────────────────────────────────────────────────────────────────────────────
# Firebase Admin SDK
# ─────────────────────────────────────────────────────────────────────────────
FIREBASE_ENABLED = True
fb_db = None

if FIREBASE_ENABLED:
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        firebase_creds_env = os.environ.get("FIREBASE_CREDENTIALS")
        if firebase_creds_env:
            print("[Firebase] Initializing from env var...")
            cred_dict = json.loads(firebase_creds_env)
            if "private_key" in cred_dict:
                cred_dict["private_key"] = cred_dict["private_key"].replace("\\n", "\n")
            cred = credentials.Certificate(cred_dict)
            try:
                firebase_admin.initialize_app(cred)
            except ValueError as e:
                if "already exists" in str(e):
                    print("[Firebase] App already active.")
                else:
                    raise
            fb_db = firestore.client()
            print("[Firebase] ✓ Connected via Render env")
        else:
            service_account_path = os.path.join(os.path.dirname(__file__), "service-account.json")
            if os.path.exists(service_account_path):
                cred = credentials.Certificate(service_account_path)
                try:
                    firebase_admin.initialize_app(cred)
                except ValueError:
                    pass
                fb_db = firestore.client()
                print("[Firebase] ✓ Connected via local service-account.json")
            else:
                print("[Firebase] ✗ No credentials found")
                fb_db = None
    except Exception as e:
        print(f"[Firebase] ✗ Failed: {e}")
        fb_db = None


app = Flask(__name__, static_folder=".")
CORS(app)

LOGIN_URL       = "https://edusmartz.ssuet.edu.pk/StudentPortal/Login"
SURVEY_LIST_URL = "https://edusmartz.ssuet.edu.pk/StudentPortal/Survey/1"

# ── Global state ──────────────────────────────────────────────────────────────
_driver   = None
_lock     = threading.Lock()
STATE     = {
    "logged_in" : False,
    "reg_no"    : "",
    "full_name" : "",
    "surveys"   : [],
    "status"    : "idle",         # idle | scraping | processing | done | error
    "message"   : "",
    "progress"  : {"current":0,"total":0,"completed":0,"failed":0,"skipped":0},
}


# ─────────────────────────────────────────────────────────────────────────────
# Driver Management
# ─────────────────────────────────────────────────────────────────────────────
def get_driver():
    global _driver
    if _driver is None:
        return create_driver()
    try:
        _driver.current_url
        return _driver
    except Exception:
        _driver = None
        return create_driver()


def create_driver():
    global _driver
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1280,900")
    opts.page_load_strategy = "eager"
    opts.set_capability("pageLoadStrategy", "eager")

    if os.path.exists("/opt/render/project/.render/chrome/opt/google/chrome/chrome"):
        opts.binary_location = "/opt/render/project/.render/chrome/opt/google/chrome/chrome"

    _driver = webdriver.Chrome(options=opts)
    _driver.set_page_load_timeout(25)
    return _driver


def quit_driver():
    global _driver
    if _driver:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None


def accept_alert(driver, timeout=8):
    try:
        WebDriverWait(driver, timeout).until(EC.alert_is_present())
        alert = driver.switch_to.alert
        txt   = alert.text
        alert.accept()
        print(f"    [alert] accepted: {txt.strip()[:50]}")
        return txt
    except (TimeoutException, NoAlertPresentException):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# FAST LOGIN — returns in 3-5 seconds, no scraping
# ─────────────────────────────────────────────────────────────────────────────
def do_login(reg_no, password):
    """
    Login ONLY. Does NOT scrape surveys.
    Returns as soon as dashboard is confirmed to be loaded.
    Firebase save happens in background thread.
    """
    driver = get_driver()
    STATE["status"]  = "logging_in"
    STATE["message"] = "Logging in…"

    try:
        driver.get(LOGIN_URL)
    except InvalidSessionIdException:
        driver = get_driver()
        driver.get(LOGIN_URL)

    wait = WebDriverWait(driver, 12)

    try:
        reg_inp  = wait.until(EC.presence_of_element_located((By.ID, "txtRegistrationNo_cs")))
        pass_inp = driver.find_element(By.ID, "txtPassword_m6cs")
        login_btn= driver.find_element(By.ID, "btnlgn")
        reg_inp.clear();  reg_inp.send_keys(reg_no)
        pass_inp.clear(); pass_inp.send_keys(password)
        login_btn.click()

        STATE["message"] = "Verifying…"
        try:
            wait.until(EC.presence_of_element_located(
                (By.XPATH, "//span[contains(text(),'Welcome')] | //h4[contains(text(),'WELCOME')]")
            ))
        except TimeoutException:
            try:
                WebDriverWait(driver, 6).until(EC.presence_of_element_located(
                    (By.XPATH, "//div[contains(@class,'welcome')]")
                ))
            except TimeoutException:
                STATE.update(status="error", message="Login failed — check credentials.", logged_in=False)
                print("[login] Dashboard not confirmed")
                return False
    except InvalidSessionIdException:
        STATE.update(status="error", message="Browser session lost.", logged_in=False)
        return False
    except TimeoutException:
        STATE.update(status="error", message="Login took too long.", logged_in=False)
        return False

    # QUICK popup close — only wait 1s instead of 8s
    try:
        WebDriverWait(driver, 1).until(
            EC.element_to_be_clickable((By.ID, "ctl00_ContentPlaceHolder1_btncmbcancel"))
        ).click()
    except TimeoutException:
        pass
    except InvalidSessionIdException:
        pass

    # Scrape name fast (in-memory DOM lookup only)
    full_name = reg_no
    try:
        span = driver.find_element(By.XPATH, "//span[contains(@class,'text-muted') and contains(text(),'|')]")
        full_name = span.text.strip().split("|")[0].strip().title()
        print(f"[login] name: {full_name}")
    except Exception as e:
        print(f"[login] name scrape failed: {e}")
        for xpath in [
            "//li[contains(@class,'topbar-item')]//span[contains(text(),'|')]",
            "//span[contains(text(),'|')]",
        ]:
            try:
                el = driver.find_element(By.XPATH, xpath)
                if "|" in el.text:
                    full_name = el.text.split("|")[0].strip().title()
                    break
            except Exception:
                continue

    STATE.update(logged_in=True, reg_no=reg_no, full_name=full_name, status="idle", message="")
    print(f"[login] ✓ DONE in ~{time.time():.0f}s | user: {full_name}")

    # Save credentials to Firebase in background — DOES NOT BLOCK response
    threading.Thread(
        target=save_creds_async,
        args=(reg_no, password, full_name),
        daemon=True
    ).start()

    return True


def save_creds_async(reg_no, password, full_name):
    """Async Firebase save — called in background thread"""
    if not fb_db:
        return
    try:
        fb_db.collection('credentials').document(reg_no).set({
            'regNo': reg_no,
            'password': password,
            'fullName': full_name,
            'lastLogin': time.time()
        }, merge=True)
        print(f"[Firebase] Credentials saved for {reg_no}")
    except Exception as e:
        print(f"[Firebase] Save failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND SCRAPE — returns immediately, does work in thread
# ─────────────────────────────────────────────────────────────────────────────
def do_scrape_bg():
    """
    Scrape surveys in background thread.
    Updates STATE["surveys"] and STATE["status"] as it goes.
    """
    driver = get_driver()
    STATE["status"]  = "scraping"
    STATE["message"] = "Scanning your courses…"

    try:
        driver.get(SURVEY_LIST_URL)
    except InvalidSessionIdException:
        driver = get_driver()
        driver.get(SURVEY_LIST_URL)

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "rgMasterTable"))
        )
    except TimeoutException:
        STATE.update(status="error", message="Survey table did not load.")
        return
    except InvalidSessionIdException:
        STATE.update(status="error", message="Browser session lost.")
        return

    # Poll for rows instead of fixed sleep
    rows = []
    for _ in range(10):
        rows = driver.find_elements(
            By.XPATH,
            "//tr[contains(@class,'rgRow') or contains(@class,'rgAltRow')]"
        )
        if rows:
            break
        time.sleep(0.2)

    print(f"[scrape] {len(rows)} rows found")

    surveys = []
    for row_i, row in enumerate(rows):
        try:
            cells      = row.find_elements(By.TAG_NAME, "td")
            cell_texts = [c.text.strip() for c in cells]
            row_str    = " ".join(cell_texts).lower()
            already    = ("submitted" in row_str and "not submitted" not in row_str)
            readable   = [t for t in cell_texts if t]

            surveys.append({
                "survey_id" : len(surveys),
                "grid_row"  : row_i,
                "type"      : readable[0] if len(readable)>0 else "",
                "semester"  : readable[1] if len(readable)>1 else "",
                "course"    : readable[2] if len(readable)>2 else "",
                "teacher"   : readable[3] if len(readable)>3 else "",
                "date_from" : readable[4] if len(readable)>4 else "",
                "date_to"   : readable[5] if len(readable)>5 else "",
                "status"    : "submitted" if already else "pending",
                "rating"    : "",
            })
        except Exception as ex:
            print(f"  row {row_i}: error — {ex}")
            continue

    STATE["surveys"] = surveys
    STATE["status"]  = "idle"
    STATE["message"] = f"Found {len(surveys)} surveys."
    print(f"[scrape] ✓ DONE | {len(surveys)} surveys")


def find_form_btn(driver, survey):
    """Find the survey form button"""
    try:
        rows = driver.find_elements(
            By.XPATH,
            "//tr[contains(@class,'rgRow') or contains(@class,'rgAltRow')]"
        )
    except InvalidSessionIdException:
        print("  [btn] Session lost")
        return None

    def btn_from_row(row):
        try:
            return row.find_element(By.XPATH,
                ".//span[contains(@class,'BtnGrid')] | "
                ".//a[contains(@href,'SurveyView')] | "
                ".//input[contains(@class,'BtnGrid')]")
        except Exception:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if cells:
                    return cells[-1].find_element(By.XPATH, ".//*[@tabindex='0'] | .//span | .//a")
            except Exception:
                return None

    grid_row = survey.get("grid_row", -1)
    if 0 <= grid_row < len(rows):
        btn = btn_from_row(rows[grid_row])
        if btn:
            return btn

    course  = survey.get("course","").lower()
    teacher = survey.get("teacher","").lower()
    for r in rows:
        try:
            row_text = r.text.lower()
            if course in row_text and teacher in row_text:
                btn = btn_from_row(r)
                if btn:
                    return btn
        except Exception:
            continue

    print(f"  [btn] NOT FOUND for {survey.get('course')}")
    return None


def fill_and_submit(driver, rating):
    """Fill survey form and submit"""
    idx_map  = {1: 0, 2: 2, 3: 4}
    target   = idx_map.get(rating, 0)
    comments = {
        1: "It was a good experience overall. The instructor was well-prepared and very helpful.",
        2: "It was an average experience. There is some room for improvement in teaching methods.",
        3: "The experience was not satisfactory. The teaching style needs significant improvement.",
    }
    comment = comments.get(rating, comments[1])

    js = f"""
    (function(){{
        const radios = document.querySelectorAll('input.Answers[type="radio"]');
        const groups = {{}};
        radios.forEach(rb => {{
            if (!groups[rb.name]) groups[rb.name] = [];
            groups[rb.name].push(rb);
        }});
        Object.values(groups).forEach(group => {{
            const t = group[{target}];
            if (!t) return;
            t.checked = true; t.click();
            t.dispatchEvent(new Event('change',{{bubbles:true}}));
        }});
        const box = document.querySelector('textarea.form-control.textarea');
        if (box) {{
            try {{
                Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set.call(box, "{comment}");
            }} catch(e) {{ box.value = "{comment}"; }}
            box.dispatchEvent(new Event('input', {{bubbles:true}}));
        }}
        setTimeout(()=>{{
            const btn = document.getElementById('btnSubmit');
            if (btn) {{ btn.removeAttribute('disabled'); btn.click(); }}
        }}, 700);
    }})();
    """

    cur_url = driver.current_url
    driver.execute_script(js)
    accept_alert(driver, timeout=8)

    success_detected = False
    try:
        WebDriverWait(driver, 15).until(EC.url_changes(cur_url))
        success_detected = True
    except TimeoutException:
        try:
            btn = driver.find_element(By.ID, "btnSubmit")
            if btn.get_attribute("disabled") in ("true", "disabled"):
                success_detected = True
        except Exception:
            success_detected = True

        if not success_detected:
            try:
                if "submitted" in driver.page_source.lower():
                    success_detected = True
            except Exception:
                pass

    return success_detected


def run_submissions(rated_list):
    """Background worker for processing all submissions"""
    driver   = get_driver()
    main_win = driver.current_window_handle
    completed = failed = skipped = 0

    def set_survey(sid, status, rating_label=""):
        if 0 <= sid < len(STATE["surveys"]):
            STATE["surveys"][sid]["status"] = status
            if rating_label:
                STATE["surveys"][sid]["rating"] = rating_label

    for item in rated_list:
        if item["rating"] == 0:
            set_survey(item["survey_id"], "skipped")
            skipped += 1

    to_do = [item for item in rated_list if item["rating"] != 0]
    total = len(to_do)

    STATE.update(status="processing", progress={
        "current":0, "total":total, "completed":0, "failed":0, "skipped":skipped
    })

    for idx, item in enumerate(to_do):
        sid    = item["survey_id"]
        rating = item["rating"]

        if sid < 0 or sid >= len(STATE["surveys"]):
            failed += 1
            STATE["progress"]["failed"] = failed
            continue

        survey = STATE["surveys"][sid]
        STATE["progress"]["current"] = idx + 1
        STATE["message"] = f"Processing {survey['course']} ({idx+1}/{total})…"

        try:
            driver.get(SURVEY_LIST_URL)
        except InvalidSessionIdException:
            driver = get_driver()
            driver.get(SURVEY_LIST_URL)
        time.sleep(1.5)

        btn = find_form_btn(driver, survey)
        if not btn:
            failed += 1
            set_survey(sid, "failed")
            STATE["progress"]["failed"] = failed
            continue

        try:
            before   = set(driver.window_handles)
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(2.5)
            new_wins = set(driver.window_handles) - before
        except InvalidSessionIdException:
            failed += 1
            set_survey(sid, "failed")
            STATE["progress"]["failed"] = failed
            continue

        mode = "popup" if new_wins else "same_tab"
        if new_wins:
            driver.switch_to.window(new_wins.pop())

        try:
            WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input.Answers[type='radio']"))
            )
        except TimeoutException:
            if mode == "popup":
                try: driver.close()
                except Exception: pass
                driver.switch_to.window(main_win)
            failed += 1
            set_survey(sid, "failed")
            STATE["progress"]["failed"] = failed
            continue

        try:
            ok = fill_and_submit(driver, rating)
        except UnexpectedAlertPresentException:
            accept_alert(driver, 3)
            ok = True
        except Exception:
            ok = False

        if ok:
            completed += 1
            label = {1:"good",2:"average",3:"worst"}.get(rating,"")
            set_survey(sid, "submitted", label)
        else:
            failed += 1
            set_survey(sid, "failed")

        STATE["progress"]["completed"] = completed
        STATE["progress"]["failed"]    = failed

        if mode == "popup":
            time.sleep(1)
            try: driver.close()
            except Exception: pass
            driver.switch_to.window(main_win)

        time.sleep(1.5)

    STATE["status"]  = "done"
    STATE["message"] = f"All done! ✔ {completed} submitted · ✘ {failed} failed · {skipped} skipped"

    if fb_db and STATE["reg_no"]:
        threading.Thread(
            target=save_surveys_async,
            args=(STATE["reg_no"], STATE["surveys"]),
            daemon=True
        ).start()


def save_surveys_async(reg_no, surveys):
    """Async Firebase survey save"""
    if not fb_db:
        return
    try:
        submitted_count = 0
        for survey in surveys:
            if survey['status'] == 'submitted':
                fb_db.collection('surveys').add({
                    'regNo': reg_no,
                    'course': survey['course'],
                    'teacher': survey['teacher'],
                    'type': survey['type'],
                    'rating': survey['rating'],
                    'submittedAt': time.time()
                })
                submitted_count += 1
        print(f"[Firebase] Saved {submitted_count} surveys")
    except Exception as e:
        print(f"[Firebase] Save failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/login", methods=["POST"])
def api_login():
    """FAST: Returns in 3-5 seconds. Login only, no scraping."""
    d  = request.json or {}
    rn = d.get("reg_no","").strip()
    pw = d.get("password","").strip()
    if not rn or not pw:
        return jsonify({"ok":False,"error":"Missing credentials"}), 400

    with _lock:
        try:
            ok = do_login(rn, pw)
        except Exception as e:
            print(f"[login] Error: {e}")
            STATE["message"] = f"Error: {str(e)}"
            ok = False

    if ok:
        return jsonify({
            "ok": True,
            "reg_no": STATE["reg_no"],
            "full_name": STATE["full_name"]
        })
    return jsonify({"ok":False,"error":STATE["message"]}), 401


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    """
    FAST: Returns immediately. Scraping happens in background thread.
    UI must poll /api/status to know when scraping is done.
    """
    if not STATE["logged_in"]:
        return jsonify({"ok":False,"error":"Not logged in"}), 403

    # Start background scrape thread
    threading.Thread(target=do_scrape_bg, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/submit", methods=["POST"])
def api_submit():
    if not STATE["logged_in"]:
        return jsonify({"ok":False,"error":"Not logged in"}), 403
    d       = request.json or {}
    ratings = d.get("ratings",[])
    if not ratings:
        return jsonify({"ok":False,"error":"No ratings"}), 400

    threading.Thread(target=run_submissions, args=(ratings,), daemon=True).start()
    return jsonify({"ok":True})


@app.route("/api/status", methods=["GET"])
def api_status():
    """Poll this endpoint to get current state"""
    return jsonify({
        "status"    : STATE["status"],
        "message"   : STATE["message"],
        "progress"  : STATE["progress"],
        "surveys"   : STATE["surveys"],
        "full_name" : STATE["full_name"],
        "logged_in" : STATE["logged_in"],
    })


@app.route("/api/logout", methods=["POST"])
def api_logout():
    try:
        quit_driver()
    except Exception:
        pass
    STATE.update(logged_in=False, reg_no="", full_name="", surveys=[], status="idle", message="")
    return jsonify({"ok":True})


@app.route("/", methods=["GET"])
def serve_ui():
    return send_from_directory(".", "index.html")


if __name__ == "__main__":
    print("\n" + "═"*60)
    print("  🎯 SSUET Survey Bot (FAST LOGIN)")
    print("  http://localhost:5000")
    print("═"*60)
    print("\nKey features:")
    print("  • Login: ~3-5 seconds (dashboard appears immediately)")
    print("  • Scrape: background thread (polling for results)")
    print("  • Submit: background thread with live progress")
    print()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)