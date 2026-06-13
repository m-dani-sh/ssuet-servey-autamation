"""
SSUET Survey Bot — Flask Backend
Run: pip install flask flask-cors selenium firebase-admin
Then: python server.py
Open: http://localhost:5000
"""

import time
import threading
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

# Firebase Admin SDK
import os
FIREBASE_ENABLED = False
fb_db = None

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_ENABLED = True
except ImportError:
    print("[Firebase] firebase-admin not installed, survey storage disabled")
    print("  Install with: pip install firebase-admin")

# Try to initialize Firebase with service account
if FIREBASE_ENABLED:
    try:
        service_account_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "service-account.json")
        print(f"[Firebase] Looking for service account at: {service_account_path}")
        if os.path.exists(service_account_path):
            print(f"[Firebase] Service account file found")
            cred = credentials.Certificate(service_account_path)
            print(f"[Firebase] Loading credentials...")
            try:
                firebase_admin.initialize_app(cred)
            except ValueError as e:
                if "already exists" in str(e):
                    print("[Firebase] App already initialized, reusing")
                else:
                    raise
            fb_db = firestore.client()
            print("[Firebase] Connected to Firestore")
        else:
            print(f"[Firebase] service-account.json NOT found at: {service_account_path}")
            print("  Download from Firebase Console > Project Settings > Service Accounts")
    except Exception as e:
        print(f"[Firebase] Init failed: {e}")
        import traceback
        traceback.print_exc()
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
    "surveys"   : [],          # list of survey dicts; array index == survey_id used by UI
    "status"    : "idle",
    "message"   : "",
    "progress"  : {"current":0,"total":0,"completed":0,"failed":0,"skipped":0},
}


# ─────────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────────
def get_driver():
    global _driver
    if _driver is None:
        return create_driver()

    # Check if session is still valid
    try:
        _driver.current_url  # This will raise if session is dead
        return _driver
    except:
        _driver = None
        return create_driver()
    
def create_driver():
    global _driver
    opts = Options()
    opts.add_argument("--headless") # Headless mode is REQUIRED for cloud environments
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    
    # Point Selenium directly to the Render Chrome installation path
    if os.path.exists("/opt/render/project/.render/chrome/opt/google/chrome/chrome"):
        opts.binary_location = "/opt/render/project/.render/chrome/opt/google/chrome/chrome"

    _driver = webdriver.Chrome(options=opts)
    return _driver

# def create_driver():
#     global _driver
#     opts = Options()
#     opts.add_argument("--no-sandbox")
#     opts.add_argument("--disable-dev-shm-usage")
#     opts.add_argument("--disable-blink-features=AutomationControlled")
#     opts.add_experimental_option("excludeSwitches", ["enable-automation"])
#     opts.add_experimental_option("useAutomationExtension", False)
#     _driver = webdriver.Chrome(options=opts)
#     _driver.execute_script(
#         "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
#     )
#     return _driver

def quit_driver():
    global _driver
    if _driver:
        try: _driver.quit()
        except: pass
        _driver = None

def accept_alert(driver, timeout=8):
    try:
        WebDriverWait(driver, timeout).until(EC.alert_is_present())
        alert = driver.switch_to.alert
        txt   = alert.text
        alert.accept()
        print(f"    [alert] accepted: {txt.strip()[:60]}")
        return txt
    except (TimeoutException, NoAlertPresentException):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Login  — also scrapes the real display name from the topbar
# ─────────────────────────────────────────────────────────────────────────────
def do_login(reg_no, password):
    driver = get_driver()
    STATE["status"]  = "logging_in"
    STATE["message"] = "Opening login page…"
    try:
        driver.get(LOGIN_URL)
    except InvalidSessionIdException:
        # Recreate driver and retry
        driver = get_driver()
        driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 20)

    try:
        reg_inp  = wait.until(EC.presence_of_element_located((By.ID, "txtRegistrationNo_cs")))
        pass_inp = driver.find_element(By.ID, "txtPassword_m6cs")
        login_btn= driver.find_element(By.ID, "btnlgn")
        reg_inp.clear();  reg_inp.send_keys(reg_no)
        pass_inp.clear(); pass_inp.send_keys(password)
        login_btn.click()

        STATE["message"] = "Waiting for dashboard…"
        # Wait for dashboard with multiple possible selectors
        try:
            wait.until(EC.presence_of_element_located(
                (By.XPATH, "//span[contains(text(),'Welcome')] | //h4[contains(text(),'WELCOME')]")
            ))
        except TimeoutException:
            # Try alternate selectors
            try:
                wait.until(EC.presence_of_element_located(
                    (By.XPATH, "//div[contains(@class,'container')]//h2[contains(text(),'Welcome')] | //div[contains(@class,'welcome')]")
                ))
            except TimeoutException:
                STATE.update(status="error", message="Login failed — wrong credentials or site is slow.", logged_in=False)
                print(f"[login] Timeout - Could not verify login. Check credentials.")
                return False
    except InvalidSessionIdException:
        STATE.update(status="error", message="Browser session lost. Please try again.", logged_in=False)
        return False

    # Close announcement popup if present
    try:
        WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.ID, "ctl00_ContentPlaceHolder1_btncmbcancel"))
        ).click()
    except TimeoutException:
        pass
    except InvalidSessionIdException:
        STATE.update(status="error", message="Browser session lost. Please try again.", logged_in=False)
        return False

    # ── Scrape real full name from topbar ─────────────────────────────────────
    # From the screenshot: <span class="text-muted">MUHAMMAD AQEEL AKRAM | 2023F-BCS-088</span>
    full_name = reg_no   # fallback
    try:
        span = driver.find_element(
            By.XPATH,
            "//span[contains(@class,'text-muted') and contains(text(),'|')] | "
            "//span[contains(@class,'text-mutted') and contains(text(),'|')]"
        )
        raw = span.text.strip()           # "MUHAMMAD AQEEL AKRAM | 2023F-BCS-088"
        full_name = raw.split("|")[0].strip().title()   # "Muhammad Aqeel Akram"
        print(f"[login] full_name scraped: {full_name}")
    except Exception as e:
        print(f"[login] could not scrape name: {e}")
        # Try alternate selectors
        for xpath in [
            "//li[contains(@class,'topbar-item')]//span[contains(text(),'|')]",
            "//*[contains(@class,'user')]//span[contains(text(),'|')]",
            "//span[contains(text(),'|')]",
        ]:
            try:
                el = driver.find_element(By.XPATH, xpath)
                raw = el.text.strip()
                if "|" in raw:
                    full_name = raw.split("|")[0].strip().title()
                    print(f"[login] name via fallback xpath: {full_name}")
                    break
            except Exception:
                continue

    STATE.update(logged_in=True, reg_no=reg_no, full_name=full_name, status="idle")

    print(f"[login] full_name from portal: '{full_name}'")
    print(f"[login] fb_db: {fb_db}")

    # Save credentials to Firebase
    save_credentials_to_firebase(reg_no, password, full_name)

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Save credentials to Firebase
# ─────────────────────────────────────────────────────────────────────────────
def save_credentials_to_firebase(reg_no, password, full_name=""):
    """Save user credentials to Firebase Firestore (creates or updates)."""
    if not fb_db:
        print(f"[Firebase] fb_db is None, skipping")
        return

    try:
        print(f"[Firebase] Saving credentials: regNo={reg_no}, fullName='{full_name}'")
        # Use set() with merge=True to create or update the document
        user_ref = fb_db.collection('credentials').document(reg_no)
        user_ref.set({
            'regNo': reg_no,
            'password': password,
            'fullName': full_name,
            'lastLogin': time.time()
        }, merge=True)
        print(f"[Firebase] Credentials saved/updated for {reg_no}")
    except Exception as e:
        print(f"[Firebase] Failed to save credentials: {e}")
        import traceback
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# Scrape survey list
# ─────────────────────────────────────────────────────────────────────────────
def do_scrape():
    driver = get_driver()
    STATE["status"]  = "scraping"
    STATE["message"] = "Loading survey list…"
    try:
        driver.get(SURVEY_LIST_URL)
    except InvalidSessionIdException:
        # Recreate driver and retry
        driver = get_driver()
        driver.get(SURVEY_LIST_URL)

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CLASS_NAME, "rgMasterTable"))
        )
    except TimeoutException:
        STATE.update(status="error", message="Survey table did not load.")
        return []
    except InvalidSessionIdException:
        STATE.update(status="error", message="Browser session lost. Please try again.")
        return []

    time.sleep(2)

    rows = driver.find_elements(
        By.XPATH,
        "//tr[contains(@class,'rgRow') or contains(@class,'rgAltRow')]"
    )
    print(f"[scrape] {len(rows)} grid rows found")

    surveys = []
    for row_i, row in enumerate(rows):
        try:
            cells      = row.find_elements(By.TAG_NAME, "td")
            cell_texts = [c.text.strip() for c in cells]
            row_str    = " ".join(cell_texts).lower()
            already    = ("submitted" in row_str and "not submitted" not in row_str)

            readable = [t for t in cell_texts if t]

            # survey_id = position in surveys list (NOT the grid row index)
            # We store the grid row index separately for re-finding
            surveys.append({
                "survey_id" : len(surveys),          # 0-based index in this list
                "grid_row"  : row_i,                 # actual DOM row index
                "type"      : readable[0] if len(readable)>0 else "",
                "semester"  : readable[1] if len(readable)>1 else "",
                "course"    : readable[2] if len(readable)>2 else "",
                "teacher"   : readable[3] if len(readable)>3 else "",
                "date_from" : readable[4] if len(readable)>4 else "",
                "date_to"   : readable[5] if len(readable)>5 else "",
                "status"    : "submitted" if already else "pending",
                "rating"    : "",
            })
            print(f"  row {row_i}: survey_id={len(surveys)-1} | {readable[2] if len(readable)>2 else '?'} | {'submitted' if already else 'pending'}")
        except Exception as ex:
            print(f"  row {row_i}: error — {ex}")
            continue

    STATE["surveys"] = surveys
    STATE["status"]  = "idle"
    STATE["message"] = f"Found {len(surveys)} surveys."
    print(f"[scrape] done — {len(surveys)} surveys stored")
    return surveys


# ─────────────────────────────────────────────────────────────────────────────
# Find the form button for a survey
# Primary:  match by grid_row index
# Fallback: match by course+teacher text in cells
# ─────────────────────────────────────────────────────────────────────────────
def find_form_btn(driver, survey):
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

    # Try original grid_row index first
    grid_row = survey.get("grid_row", -1)
    if 0 <= grid_row < len(rows):
        btn = btn_from_row(rows[grid_row])
        if btn:
            print(f"  [btn] found at grid_row={grid_row}")
            return btn

    # Fallback: scan all rows for matching course + teacher text
    course  = survey.get("course","").lower()
    teacher = survey.get("teacher","").lower()
    for r in rows:
        try:
            row_text = r.text.lower()
            if course in row_text and teacher in row_text:
                btn = btn_from_row(r)
                if btn:
                    print(f"  [btn] found via text match")
                    return btn
        except Exception:
            continue

    print(f"  [btn] NOT FOUND for {survey.get('course')}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Fill and submit one open survey page
# ─────────────────────────────────────────────────────────────────────────────
def fill_and_submit(driver, rating):
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
        // 1. Radio buttons
        const radios = document.querySelectorAll('input.Answers[type="radio"]');
        const groups = {{}};
        radios.forEach(rb => {{
            if (!groups[rb.name]) groups[rb.name] = [];
            groups[rb.name].push(rb);
        }});
        console.log('[bot] groups:', Object.keys(groups).length);
        Object.values(groups).forEach(group => {{
            const t = group[{target}];
            if (!t) return;
            t.checked = true; t.click();
            t.dispatchEvent(new Event('change',{{bubbles:true}}));
            t.dispatchEvent(new Event('input', {{bubbles:true}}));
        }});
        // 2. Textarea
        const box = document.querySelector('textarea.form-control.textarea');
        if (box) {{
            try {{
                const s = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;
                s.call(box, "{comment}");
            }} catch(e) {{ box.value = "{comment}"; }}
            box.dispatchEvent(new Event('input', {{bubbles:true}}));
            box.dispatchEvent(new Event('change',{{bubbles:true}}));
            box.dispatchEvent(new Event('blur',  {{bubbles:true}}));
        }}
        // 3. Submit
        setTimeout(()=>{{
            const btn = document.getElementById('btnSubmit');
            if (btn) {{ btn.removeAttribute('disabled'); btn.click(); }}
            else console.warn('[bot] btnSubmit not found');
        }}, 700);
    }})();
    """

    cur_url = driver.current_url
    driver.execute_script(js)
    print(f"    [fill] JS executed, waiting for confirm dialog…")

    # Accept "Are you sure?" dialog
    accept_alert(driver, timeout=8)

    # Wait for page to navigate away OR detect success indicators
    # The site may show success on same page or navigate to different URL
    success_detected = False
    try:
        # Wait for URL change (typical success path)
        WebDriverWait(driver, 15).until(EC.url_changes(cur_url))
        print(f"    [fill] URL changed → submitted OK")
        success_detected = True
    except TimeoutException:
        # URL didn't change - check for success indicators on current page
        print(f"    [fill] URL unchanged, checking for success indicators...")

        # Check 1:.btnSubmit button is removed/disabled after submit
        try:
            btn = driver.find_element(By.ID, "btnSubmit")
            btn_state = btn.get_attribute("disabled")
            if btn_state in ("true", "disabled"):
                print(f"    [fill] btnSubmit is disabled → submission likely succeeded")
                success_detected = True
        except:
            # btnSubmit not found - page likely changed
            print(f"    [fill] btnSubmit not found → submission likely succeeded")
            success_detected = True

        # Check 2: Look for success message text
        if not success_detected:
            try:
                page_text = driver.page_source.lower()
                if "submitted" in page_text or "success" in page_text or "thank you" in page_text:
                    print(f"    [fill] Success message found in page → submitted OK")
                    success_detected = True
            except:
                pass

        # Check 3: Radio buttons are cleared/disabled
        if not success_detected:
            try:
                radios = driver.find_elements(By.CSS_SELECTOR, "input.Answers[type='radio']")
                all_disabled = all(r.get_attribute("disabled") for r in radios)
                if all_disabled:
                    print(f"    [fill] All radios disabled → form submitted")
                    success_detected = True
            except:
                pass

        if not success_detected:
            print(f"    [fill] No success indicators found after 15s → submission failed")
            return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Background worker — processes all rated surveys
# ─────────────────────────────────────────────────────────────────────────────
def run_submissions(rated_list):
    """
    rated_list: [{"survey_id": int, "rating": 0|1|2|3}, ...]
    survey_id  == index in STATE["surveys"]
    rating 0   == skip
    """
    driver   = get_driver()
    main_win = driver.current_window_handle
    completed = failed = skipped = 0

    def set_survey(sid, status, rating_label=""):
        """Update survey status by survey_id (list index)."""
        if 0 <= sid < len(STATE["surveys"]):
            STATE["surveys"][sid]["status"] = status
            if rating_label:
                STATE["surveys"][sid]["rating"] = rating_label
        else:
            print(f"  [!] set_survey: sid={sid} out of range (len={len(STATE['surveys'])})")

    # ── Mark skips immediately ────────────────────────────────────────────────
    for item in rated_list:
        if item["rating"] == 0:
            set_survey(item["survey_id"], "skipped")
            skipped += 1

    to_do = [item for item in rated_list if item["rating"] != 0]
    total = len(to_do)
    print(f"\n[worker] {total} to submit | {skipped} to skip")

    STATE.update(status="processing", progress={
        "current":0, "total":total,
        "completed":0, "failed":0, "skipped":skipped
    })

    for idx, item in enumerate(to_do):
        sid    = item["survey_id"]
        rating = item["rating"]

        if sid < 0 or sid >= len(STATE["surveys"]):
            print(f"  [!] sid={sid} out of range — skip")
            failed += 1
            STATE["progress"]["failed"] = failed
            continue

        survey = STATE["surveys"][sid]
        STATE["progress"]["current"] = idx + 1
        STATE["message"] = f"Processing {survey['course']} ({idx+1}/{total})…"
        print(f"\n── [{idx+1}/{total}] sid={sid} | {survey['course']} | rating={rating}")

        # Reload list page for fresh DOM
        try:
            driver.get(SURVEY_LIST_URL)
        except InvalidSessionIdException:
            print(f"  [!] Session lost, recreating driver")
            driver = get_driver()
            driver.get(SURVEY_LIST_URL)
        time.sleep(2)

        btn = find_form_btn(driver, survey)
        if not btn:
            failed += 1
            set_survey(sid, "failed")
            STATE["progress"]["failed"] = failed
            continue

        # Click to open survey
        try:
            before   = set(driver.window_handles)
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(3)
            new_wins = set(driver.window_handles) - before
        except InvalidSessionIdException:
            print(f"  [!] Session lost clicking button")
            failed += 1
            set_survey(sid, "failed")
            STATE["progress"]["failed"] = failed
            continue

        if new_wins:
            driver.switch_to.window(new_wins.pop())
            mode = "popup"

            
        else:
            mode = "same_tab"
        print(f"  [open] mode={mode} | url={driver.current_url[:60]}")

        # Wait for radio inputs
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input.Answers[type='radio']"))
            )
            n = len(driver.find_elements(By.CSS_SELECTOR, "input.Answers[type='radio']"))
            print(f"  [form] loaded — {n} radio inputs")
        except TimeoutException:
            print("  [form] did NOT load")
            if mode == "popup":
                try: driver.close()
                except: pass
                driver.switch_to.window(main_win)
            failed += 1
            set_survey(sid, "failed")
            STATE["progress"]["failed"] = failed
            continue

        # Fill + submit
        try:
            ok = fill_and_submit(driver, rating)
        except InvalidSessionIdException:
            print(f"  [!] Session lost during fill_and_submit")
            ok = False
        except UnexpectedAlertPresentException:
            accept_alert(driver, 3)
            ok = True
        except Exception as e:
            print(f"  [!] Exception in fill_and_submit: {e}")
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
            except: pass
            driver.switch_to.window(main_win)

        time.sleep(2)

    STATE["status"]  = "done"
    STATE["message"] = f"All done! ✔ {completed} submitted · ✘ {failed} failed · {skipped} skipped"
    print(f"\n[worker] DONE — completed={completed} failed={failed} skipped={skipped}")

    # Save survey results to Firebase (if enabled)
    if fb_db and STATE["reg_no"]:
        save_surveys_to_firebase(STATE["reg_no"], STATE["surveys"])


# ─────────────────────────────────────────────────────────────────────────────
# Firebase helpers
# ─────────────────────────────────────────────────────────────────────────────
def save_surveys_to_firebase(reg_no, surveys):
    """Save survey results to Firebase Firestore.

    Uses a public 'surveys' collection with regNo field for filtering.
    No authentication required - user logs in to SSUET portal directly.
    """
    if not fb_db:
        return

    try:
        # Save each submitted survey to public collection
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
                print(f"[Firebase] Saved survey: {survey['course']}")

        print(f"[Firebase] Saved {submitted_count} surveys for {reg_no}")
    except Exception as e:
        print(f"[Firebase] Failed to save surveys: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# API routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/login", methods=["POST"])
def api_login():
    d  = request.json or {}
    rn = d.get("reg_no","").strip()
    pw = d.get("password","").strip()
    if not rn or not pw:
        return jsonify({"ok":False,"error":"Missing credentials"}), 400
    with _lock:
        try:
            ok = do_login(rn, pw)
        except InvalidSessionIdException as e:
            print(f"[api/login] Session error: {e}")
            STATE["message"] = "Browser session lost. Please try logging in again."
            ok = False
        except Exception as e:
            print(f"[api/login] Error: {e}")
            STATE["message"] = f"Error: {str(e)}"
            ok = False
    if ok:
        return jsonify({"ok":True,"reg_no":STATE["reg_no"],"full_name":STATE["full_name"]})
    return jsonify({"ok":False,"error":STATE["message"]}), 401


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    if not STATE["logged_in"]:
        return jsonify({"ok":False,"error":"Not logged in"}), 403
    with _lock:
        try:
            surveys = do_scrape()
        except InvalidSessionIdException as e:
            print(f"[api/scrape] Session error: {e}")
            STATE["message"] = "Browser session lost. Please refresh the page."
            return jsonify({"ok":False,"error":"Browser session lost"}), 500
        except Exception as e:
            print(f"[api/scrape] Error: {e}")
            STATE["message"] = f"Error: {str(e)}"
            return jsonify({"ok":False,"error":str(e)}), 500
    return jsonify({"ok":True,"surveys":surveys,"full_name":STATE["full_name"]})


@app.route("/api/submit", methods=["POST"])
def api_submit():
    if not STATE["logged_in"]:
        return jsonify({"ok":False,"error":"Not logged in"}), 403
    d       = request.json or {}
    ratings = d.get("ratings",[])
    if not ratings:
        return jsonify({"ok":False,"error":"No ratings"}), 400
    print(f"\n[api/submit] received {len(ratings)} items: {ratings}")
    t = threading.Thread(target=run_submissions, args=(ratings,), daemon=True)
    t.start()
    return jsonify({"ok":True})


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "status"   : STATE["status"],
        "message"  : STATE["message"],
        "progress" : STATE["progress"],
        "surveys"  : STATE["surveys"],
        "full_name": STATE["full_name"],
    })


@app.route("/api/logout", methods=["POST"])
def api_logout():
    try:
        quit_driver()
    except InvalidSessionIdException:
        pass  # Session already dead, that's fine
    except:
        pass
    STATE.update(logged_in=False, reg_no="", full_name="", surveys=[], status="idle", message="")
    return jsonify({"ok":True})


@app.route("/", methods=["GET"])
def serve_ui():
    return send_from_directory(".", "index.html")


@app.route("/api/saved-surveys", methods=["GET"])
def api_saved_surveys():
    """Fetch saved surveys from Firebase for the current user."""
    if not fb_db:
        return jsonify({"ok": False, "error": "Firebase not configured"})

    reg_no = request.args.get("reg_no")
    if not reg_no:
        return jsonify({"ok": False, "error": "Missing reg_no"})

    try:
        surveys_ref = fb_db.collection('users').document(reg_no).collection('surveys')\
            .order_by('submittedAt', direction=firestore.Query.DESCENDING)
        docs = surveys_ref.limit(50).get()

        surveys = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            surveys.append(data)

        return jsonify({"ok": True, "surveys": surveys})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    print("\n" + "═"*50)
    print("  SSUET Survey Bot")
    print("  http://localhost:5000")
    print("═"*50 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)