# main.py
# Standard library imports
import asyncio
import base64
import csv
import glob
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta
import html
from io import BytesIO
from urllib.parse import urljoin
from typing import Optional
from telegram.ext import Application
# Local application imports
import config  # make sure this has TWO_CAPTCHA_API_KEY
import os

# Base folder for per-alias downloads
_download_base = os.path.join(os.getcwd(), "downloads")
# Map profile_dir → its dedicated download folder
_profile_downloads: dict[str, str] = {}
os.makedirs(_download_base, exist_ok=True)

import logging
# ─── set up root logger ───
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─── define a filter to drop HTTP OK messages ───
class ExcludeHttpOkFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "HTTP Request:" not in msg or "200 OK" not in msg

# ─── file handler ───
file_handler = logging.FileHandler("autobot.log", encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_handler.addFilter(ExcludeHttpOkFilter())
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s %(module)s:%(lineno)d — %(message)s"
))

# ─── console handler (optional) ───
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
))

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# ─── silence httpx INFO-level “HTTP Request” logs ───
logging.getLogger("httpx").setLevel(logging.WARNING)

# Third-party imports
import requests

# Telegram imports
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Selenium imports
from selenium import webdriver
from selenium.webdriver.remote.remote_connection import RemoteConnection
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    UnexpectedAlertPresentException,
)

# Tweak Selenium’s HTTP connection pool
RemoteConnection.pool_connections = 30
RemoteConnection.pool_maxsize = 50


# track last‐bot‐message time per alias
last_active: dict[str, datetime] = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- pool of up to 10 pre-warmed AutoBank profiles ---
MAX_PROFILES = 10
BASE_PROFILE_DIR = os.path.join(os.path.expanduser("~"), "chrome-profiles")
os.makedirs(BASE_PROFILE_DIR, exist_ok=True)

# --- NEW GLOBALS for “always-up” profiles ---
_drivers: dict[str, webdriver.Chrome] = {}    # profile_dir → Chrome instance
_active: dict[str, bool]          = {}        # profile_dir → is alias running?

# create exactly 10 empty profile dirs (you can manually sign in to each)
PROFILE_DIRS = [
    os.path.join(BASE_PROFILE_DIR, f"profile{i}")
    for i in range(1, MAX_PROFILES + 1)
]
for d in PROFILE_DIRS:
    os.makedirs(d, exist_ok=True)

# pool state
_free_profiles = PROFILE_DIRS.copy()       # list of unused profile-dirs
_profile_assignments = {}                  # alias -> profile-dir

# ─── after your existing “from telegram.ext import …” block ───

# track users mid‐flow in the KGB custom‐date sequence
pending_kgb: dict[int, dict] = {}

async def run_kgb(update, context, alias, from_dt=None, to_dt=None):
    """Start a KGBWorker by reusing a pre-warmed Chrome driver."""
    msg_target = update.message or update.callback_query.message

    # pre-checks
    if alias not in creds:
        return await msg_target.reply_text(f"❌ Unknown alias “{alias}”.")
    if alias in _profile_assignments:
        return await msg_target.reply_text(f"❌ Already running “{alias}”.")
    if not _free_profiles:
        return await msg_target.reply_text("❌ Maximum of 10 concurrent sessions reached.")

    # reserve credentials and profile
    cred    = creds[alias]
    profile = _free_profiles.pop(0)
    driver  = _drivers[profile]
    _active[profile]            = True
    _profile_assignments[alias] = profile

    # use the profile’s dedicated download folder
    download_folder = _profile_downloads[profile]

    # spawn the worker with the existing driver + its profile download folder
    worker = KGBWorker(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        alias=alias,
        cred=cred,
        loop=asyncio.get_running_loop(),
        driver=driver,
        download_folder=download_folder,
        profile_dir=profile,
    )

    # attach custom dates if provided
    if from_dt and to_dt:
        worker.from_dt = from_dt
        worker.to_dt   = to_dt

    workers[alias] = worker
    worker.start()

    # notify user
    msg = f"Started *{alias}* in profile *{profile}*"
    if from_dt:
        msg += f" from {from_dt.strftime('%d/%m/%Y')} to {to_dt.strftime('%d/%m/%Y')}"
    await msg_target.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def kgb_button(update, context):
    """CallbackQueryHandler for “Default / Custom” buttons."""
    q = update.callback_query
    await q.answer()
    _, alias, choice = q.data.split("|")
    user_id = update.effective_user.id

    if choice == "default":
        # fire off the normal logic
        return await run_kgb(update, context, alias)

    # custom branch → start asking dates
    pending_kgb[user_id] = {"alias": alias, "stage": "from"}
    await q.message.reply_text(
        "✏️ Enter *FROM* date (dd/mm/yyyy or dd/mm/yy):",
        parse_mode=ParseMode.MARKDOWN,
    )
    
def solve_captcha_with_2captcha(image_bytes, min_len=None, max_len=None, regsense=True):
    try:
        b64_image = base64.b64encode(image_bytes).decode('utf-8')
        data = {
            'method': 'base64',
            'key': config.TWO_CAPTCHA_API_KEY,
            'body': base64.b64encode(image_bytes).decode('utf-8'),
            'json': 1
        }
        if regsense:
            data['regsense'] = 1
        if min_len:
            data['min_len'] = min_len
        if max_len:
            data['max_len'] = max_len

        r = requests.post('http://2captcha.com/in.php', data=data).json()
        if r.get("status") != 1:
            raise Exception(f"Upload failed: {r.get('request')}")

        captcha_id = r.get("request")

        for _ in range(30):
            time.sleep(5)
            res = requests.get(f"http://2captcha.com/res.php?key={config.TWO_CAPTCHA_API_KEY}&action=get&id={captcha_id}&json=1").json()
            if res.get("status") == 1:
                return res["request"], captcha_id
            if res.get("request") != "CAPCHA_NOT_READY":
                raise Exception(f"2Captcha error: {res['request']}")
        raise TimeoutError("2Captcha timed out")
    except Exception:
        return None, None

def report_bad_captcha(captcha_id):
    try:
        requests.get(f"http://2captcha.com/res.php?key={config.TWO_CAPTCHA_API_KEY}&action=reportbad&id={captcha_id}")
    except:
        pass

def load_credentials():
    creds = {}
    with open(config.CREDENTIALS_CSV, newline="") as f:
        for row in csv.DictReader(f):
            creds[row["alias"]] = row
    return creds

async def add_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.lower().startswith("/add "):
        await update.message.reply_text(
            "Usage:\n"
            "  /add alias,username,password,account_number   (TMB)\n"
            "or\n"
            "  /add alias,login_id,user_id,password,account_number   (corporate)"
        )
        return

    parts = [p.strip() for p in text[5:].split(",")]
    if len(parts) == 4:
        alias, username, password, account_number = parts
        login_id = ""
        user_id = ""
    elif len(parts) == 5:
        alias, login_id, user_id, password, account_number = parts
        username = ""
    else:
        await update.message.reply_text(
            "❌ Invalid format.\n"
            "Use 4 fields for TMB or 5 fields for corporate."
        )
        return

    global creds
    if alias in creds:
        await update.message.reply_text(f"❌ Alias *{alias}* already exists.", parse_mode=ParseMode.MARKDOWN)
        return

    csv_path = config.CREDENTIALS_CSV
    # only make parent dir if there is one
    parent = os.path.dirname(csv_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    is_new = not os.path.exists(csv_path) or os.stat(csv_path).st_size == 0
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "alias", "login_id", "user_id", "username", "password", "account_number"
        ])
        if is_new:
            writer.writeheader()
        writer.writerow({
            "alias": alias,
            "login_id": login_id,
            "user_id": user_id,
            "username": username,
            "password": password,
            "account_number": account_number,
        })

    creds = load_credentials()
    await update.message.reply_text(
        f"✅ Added alias *{alias}*.", parse_mode=ParseMode.MARKDOWN
    )


from selenium.webdriver.support.ui import Select

class TMBWorker(threading.Thread):


    def _send_screenshots(self):
        """
        Capture a screenshot of each open tab (TMB + AutoBank if present)
        and send it into the Telegram chat.
        """
        for handle in self.driver.window_handles:
            try:
                self.driver.switch_to.window(handle)
                png = self.driver.get_screenshot_as_png()
                bio = BytesIO(png)
                tab_name = "TMB" if handle == self.tmb_window else "AutoBank"
                coro = self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=bio,
                    caption=f"[{self.alias}] 📸 {tab_name} screenshot",
                )
                asyncio.run_coroutine_threadsafe(coro, self.loop)
            except Exception:
                # if one screenshot fails, keep going with the rest
                continue


    def __init__(
        self,
        bot,
        chat_id,
        alias,
        cred,
        loop,
        driver: Optional[webdriver.Chrome] = None,
        download_folder: Optional[str]      = None,
        profile_dir: Optional[str]         = None,
    ):
        super().__init__(daemon=True)
        self.bot          = bot
        self.chat_id      = chat_id
        self.alias        = alias
        self.cred         = cred
        self.loop         = loop
        self.captcha_code = None
        self.logged_in    = False
        self._stop_event  = threading.Event()
        self.tmb_window   = None

        # ─── reuse injected Chrome if provided ───
        self.reused_driver = driver is not None
        if self.reused_driver:
            self.driver       = driver
            self.download_dir = download_folder
            return

        # ─── otherwise, spin up a fresh Chrome instance ───
        # (A) per-alias download folder under "./downloads/<alias>"
        download_root = os.path.join(os.getcwd(), "downloads", alias)
        os.makedirs(download_root, exist_ok=True)
        self.download_dir = download_root

        opts = webdriver.ChromeOptions()
        opts.add_argument(f"--user-data-dir={profile_dir}")
        opts.add_argument("--start-maximized")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        prefs = {
            "download.default_directory": download_root,
            "download.prompt_for_download": False,
            "profile.default_content_setting_values.automatic_downloads": 1,
        }
        opts.add_experimental_option("prefs", prefs)

        self.driver = webdriver.Chrome(options=opts)
        # clear cookies & cache
        self.driver.execute_cdp_cmd('Network.clearBrowserCookies', {})
        self.driver.execute_cdp_cmd('Network.clearBrowserCache', {})
        self.driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")


    def stop(self):
        # signal the worker loop to exit
        self._stop_event.set()

        if self.logged_in:
            try:
                # switch to the TMB tab
                if self.tmb_window in self.driver.window_handles:
                    self.driver.switch_to.window(self.tmb_window)
                else:
                    self.driver.switch_to.window(self.driver.window_handles[0])

                self._send("🚪 Logging out…")
                btn = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable((By.LINK_TEXT, "Log Out"))
                )
                btn.click()

                # wait for either logout-confirmation element
                WebDriverWait(self.driver, 5).until(lambda d: (
                    d.find_elements(By.ID, "HDisplay2.Re3.C2") or
                    d.find_elements(By.XPATH, "//font[contains(text(),'You have been logged out')]")
                ))

                if self.driver.find_elements(By.ID, "HDisplay2.Re3.C2"):
                    self._send("✅ You have successfully logged out")
                else:
                    self._send("✅ You have been logged out for Application Security")
            except Exception:
                pass

        # always capture screenshots for debugging
        try:
            self._send_screenshots()
        except Exception:
            pass

        if self.reused_driver:
            # recycle the shared Chrome: open a new AutoBank tab, close all others
            main = self.driver.window_handles[0]
            self.driver.switch_to.window(main)
            self.driver.execute_script(
                "window.open('https://autobank.payatom.in/operator_index.php');"
            )
            for handle in self.driver.window_handles[:-1]:
                self.driver.switch_to.window(handle)
                self.driver.close()
            self.driver.switch_to.window(self.driver.window_handles[0])
        else:
            try:
                self.driver.quit()
            except Exception:
                pass


    def _send_msg(self, text):
        # update last‐active timestamp
        last_active[self.alias] = datetime.now()
        return asyncio.run_coroutine_threadsafe(
            self.bot.send_message(
                chat_id=self.chat_id,
                text=f"[{self.alias}] {text}",
                parse_mode=ParseMode.MARKDOWN,
            ),
            self.loop,
        )
        
    def run(self):
        self._send_msg("🚀 Starting TMB automation")
        retry_count = 0

        while not self._stop_event.is_set():
            try:
                self._login()
                retry_count = 0  # reset after success

                while not self._stop_event.is_set():
                    self._balance_and_pages_and_download()
                    time.sleep(60)

                break  # exit if stop was requested

            except Exception as e:
                retry_count += 1
                try:
                    self._send_screenshots()
                except:
                    pass
                self._send_msg(f"⚠️ Error: {e!r}\nRetrying {retry_count}/5…")

                if retry_count > 5:
                    self._send_msg(f"❌ Too many failures. Stopping this alias.")
                    self.stop()
                    break
                self._retry()


    def _retry(self):
        """Logout of TMB, then cycle to a fresh tab and reset state—no login here."""
        try:
            if self.tmb_window in self.driver.window_handles:
                self.driver.switch_to.window(self.tmb_window)
            else:
                self.driver.switch_to.window(self.driver.window_handles[0])

            btn = WebDriverWait(self.driver, 5).until(
                EC.element_to_be_clickable((By.LINK_TEXT, "Log Out"))
            )
            btn.click()

            WebDriverWait(self.driver, 5).until(lambda d: (
                d.find_elements(By.ID, "HDisplay2.Re3.C2") or
                d.find_elements(By.XPATH, "//font[contains(text(),'You have been logged out')]")
            ))

            if self.driver.find_elements(By.ID, "HDisplay2.Re3.C2"):
                self._send_msg("✅ You have successfully logged out")
            else:
                self._send_msg("✅ You have been logged out for Application Security")
        except Exception:
            pass

        old_tabs = set(self.driver.window_handles)
        self.driver.execute_script("window.open('about:blank','_blank');")
        new_tab = (set(self.driver.window_handles) - old_tabs).pop()

        for h in old_tabs:
            try:
                self.driver.switch_to.window(h)
                self.driver.close()
            except:
                pass

        self.driver.switch_to.window(new_tab)
        self.tmb_window = new_tab

        self.captcha_code = None
        self.logged_in   = False
        self._send_msg("🔄 Retrying login…")


    def _login(self):
        #self._send_msg("Navigating to tmbnet.in…")
        self.driver.get("https://www.tmbnet.in/")

        #self._send_msg("Clicking Net Banking Login…")
        WebDriverWait(self.driver, 10).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Net Banking Login"))
        ).click()

        #self._send_msg("Clicking Continue to Login…")
        try:
            WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "button.login-button.btn-tmb-primary")
                )
            ).click()
        except TimeoutException:
            btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//button[contains(., 'Continue to Login')]")
                )
            )
            btn.click()

        #self._send_msg("Waiting for login form…")
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.NAME, "AuthenticationFG.USER_PRINCIPAL"))
        )

        #self._send_msg("Filling credentials…")
        self.driver.find_element(By.NAME, "AuthenticationFG.USER_PRINCIPAL").send_keys(self.cred["username"])
        self.driver.find_element(By.NAME, "AuthenticationFG.ACCESS_CODE").send_keys(self.cred["password"])

        #self._send_msg("Fetching captcha…")
        img = WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.ID, "IMAGECAPTCHA")))
        bio = BytesIO(img.screenshot_as_png)

        self._send_msg("🤖 Trying to auto-solve CAPTCHA via 2Captcha…")
        solution, self._captcha_id = solve_captcha_with_2captcha(bio.getvalue())

        if solution:
            self.captcha_code = solution
            self._send_msg(f"✅ Auto-solved: `{solution}`")
        else:
            asyncio.run_coroutine_threadsafe(
                self.bot.send_photo(chat_id=self.chat_id, photo=bio, caption=f"[{self.alias}] 🔐 Please solve this captcha"),
                self.loop,
            )
            self._send_msg("⚠️ 2Captcha failed. Waiting for your reply…")
            while not self.captcha_code and not self._stop_event.is_set():
                time.sleep(0.5)

        if self._stop_event.is_set():
            return


        #self._send_msg("Submitting captcha…")
        self.driver.find_element(By.NAME, "AuthenticationFG.VERIFICATION_CODE").send_keys(self.captcha_code)
        self.driver.find_element(By.ID, "VALIDATE_CREDENTIALS").click()
        try:
            err_div = WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.redbg[role='alert']"))
            )
            if "enter the characters" in err_div.text.lower() and hasattr(self, "_captcha_id"):
                self._send_msg("❌ CAPTCHA incorrect — reporting to 2Captcha and retrying…")
                report_bad_captcha(self._captcha_id)
                self._retry()
                return
        except TimeoutException:
            pass  # no error element → proceed

        # once we're truly in, wait for the Account Summary link
        WebDriverWait(self.driver, 20).until(
            EC.element_to_be_clickable((By.ID, "Account_Summary"))
        )
        # remember this handle for later logout
        self.tmb_window = self.driver.current_window_handle

        self._send_msg("✅ Logged in!")
        self.logged_in = True



    def _click_account_summary(self):
        #self._send_msg("🔎 [DEBUG] enter _click_account_summary")
        attempts = [
            ("sidebar-ID",        By.ID,           "Account_Summary"),
            ("sidebar-linkText",  By.LINK_TEXT,    "Account Summary"),
            ("sidebar-CSS",       By.CSS_SELECTOR, "a.Loginbluelink[title='Account Summary']"),
            ("topnav-ID",         By.ID,           "Balance--Transaction-Info_Account-Summary"),
            ("topnav-linkText",   By.LINK_TEXT,    "Account Summary"),
        ]
        for name, by, loc in attempts:
            try:
                #self._send_msg(f"🔎 [DEBUG] looking for {name} ({by}={loc})")
                el = WebDriverWait(self.driver, 15).until(
                    EC.element_to_be_clickable((by, loc))
                )
                #self._send_msg(f"⚙️ [DEBUG] found {name}, attempting click…")
                self.driver.execute_script("arguments[0].scrollIntoView(true);", el)
                try:
                    el.click()
                except:
                    try:
                        ActionChains(self.driver).move_to_element(el).click().perform()
                    except:
                        self.driver.execute_script("arguments[0].click();", el)
                #self._send_msg(f"✅ [DEBUG] clicked {name}")
                return
            except TimeoutException as e:
                self._send_msg(f"⚠️ [DEBUG] {name} not clickable: {e}")
                continue

        raise TimeoutException("Could not find ANY Account Summary link to click")
        
    from selenium.common.exceptions import ElementClickInterceptedException
    from selenium.webdriver import ActionChains
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    from selenium.webdriver.support.ui import Select
    def _click_account_summary(self):
        #self._send_msg("🔎 [DEBUG] enter _click_account_summary")

        # Always start by scrolling back to the very top
        self.driver.execute_script("window.scrollTo(-50,-100);")
        time.sleep(0.5)  # let any sticky headers collapse

        attempts = [
            ("sidebar-ID",        By.ID,           "Account_Summary"),
            ("sidebar-linkText",  By.LINK_TEXT,    "Account Summary"),
            ("sidebar-CSS",       By.CSS_SELECTOR, "a.Loginbluelink[title='Account Summary']"),
            ("topnav-ID",         By.ID,           "Balance--Transaction-Info_Account-Summary"),
            ("topnav-linkText",   By.LINK_TEXT,    "Account Summary"),
        ]

        for name, by, loc in attempts:
            try:
                #self._send_msg(f"🔎 [DEBUG] looking for {name} ({by}={loc})")

                # scroll the candidate into view at the top
                el = WebDriverWait(self.driver, 15).until(
                    EC.element_to_be_clickable((by, loc))
                )
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'start'});",
                    el
                )
                time.sleep(0.3)
                self.driver.execute_script("window.scrollTo(-50,-100);")
                # try three click methods
                try:
                    el.click()
                except:
                    try:
                        ActionChains(self.driver).move_to_element(el).click().perform()
                    except:
                        self.driver.execute_script("arguments[0].click();", el)

                #self._send_msg(f"✅ [DEBUG] clicked {name}")
                return
            except TimeoutException as e:
                self._send_msg(f"⚠️ [DEBUG] {name} not clickable: {e}")
                continue

        raise TimeoutException("Could not find ANY Account Summary link to click")

    def _balance_and_pages_and_download(self):
        # 1) Go back to the Account Summary screen
        #self._send_msg("➡️ Navigating to Account Summary…")
        try:
            self._click_account_summary()
        except Exception as e:
            self._send_msg(f"❌ Failed to click Account Summary: {e!r}")
            raise

        # 2) Wait for “My Accounts” heading
        WebDriverWait(self.driver, 20).until(
            EC.presence_of_element_located((By.XPATH, "//h1[text()='My Accounts']"))
        )

        # 3) Grab the balance
        row = WebDriverWait(self.driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#SummaryList tr.listwhiterow"))
        )
        balance = row.find_elements(By.TAG_NAME, "td")[2].text
        self._send_msg(f" 💰: {balance}")
        self.last_balance = balance
        # 4) Click Account Statement
        #self._send_msg("Clicking Account Statement…")
        stmt_link = WebDriverWait(self.driver, 20).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Account Statement"))
        )
        self.driver.execute_script("arguments[0].scrollIntoView(true);", stmt_link)
        try:
            stmt_link.click()
        except:
            self.driver.execute_script("arguments[0].click();", stmt_link)
        WebDriverWait(self.driver, 30).until(
            EC.text_to_be_present_in_element((By.CSS_SELECTOR, "#PgHeading h1"), "My Transactions")
        )

        # 5) Click Search
        #self._send_msg("Waiting for Search button…")
        btn = WebDriverWait(self.driver, 30).until(
            EC.element_to_be_clickable((By.ID, "SEARCH"))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
        try:
            btn.click()
        except ElementClickInterceptedException:
            self._send_msg("⚠️ Click intercepted; trying ActionChains…")
            try:
                ActionChains(self.driver).move_to_element(btn).click().perform()
            except:
                self._send_msg("⚠️ Falling back to JS click")
                self.driver.execute_script("arguments[0].click();", btn)

        # 6) Get total pages
        # 6) Determine total pages, or default to 1 if not shown
        try:
            page_info = WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located(
                    (By.XPATH, "//*[contains(text(),'Page') and contains(text(),'of')]")
                )
            ).text
            match = re.search(r"Page\s+\d+\s+of\s+(\d+)", page_info)
            pages = match.group(1) if match else "1"
        except TimeoutException:
            pages = "1"
        self._send_msg(f"Pages: {pages}")
        
        # 7) Select XLS format
        #self._send_msg("Selecting XLS format…")
        sel = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "select[id$='.OUTFORMAT']"))
        )
        dropdown = Select(sel)
        try:
            dropdown.select_by_visible_text("XLS")
        except NoSuchElementException:
            dropdown.select_by_value("4")

        # 8) Click Download
        #self._send_msg("Clicking Download…")
        for by, loc in (
            (By.NAME, "Action.CUSTOM_GENERATE_REPORTS"),
            (By.ID,   "okButton"),
            (By.XPATH, "//input[@value='Download']"),
        ):
            try:
                dl_btn = WebDriverWait(self.driver, 15).until(
                    EC.element_to_be_clickable((by, loc))
                )
                dl_btn.click()
                break
            except TimeoutException:
                continue
        else:
            raise TimeoutException("Could not find any Download button to click")

        # 9) Wait for the .xls to finish downloading
        download_dir = self.download_dir
        timeout = 60
        end_time = time.time() + timeout
        xls_file = None

        while time.time() < end_time:
            files = os.listdir(download_dir)
            # ignore any in-progress .tmp/.crdownload files
            candidates = [f for f in files if not f.lower().endswith(('.tmp', '.crdownload'))]
            if candidates:
                # pick the most recent fully-downloaded file
                xls_file = max(candidates, key=lambda f: os.path.getctime(os.path.join(download_dir, f)))
                full_path = os.path.join(download_dir, xls_file)
                if os.path.exists(full_path):
                    break
            time.sleep(1)

        if not xls_file:
            raise TimeoutException("Timed out waiting for XLS download to finish")

        #self._send_msg(f"✅ Downloaded file: {xls_file}")

        # 10) Upload to AutoBank
        try:
            self._upload_to_autobank(full_path)
        except Exception as e:
            self._send_msg(f"❌ AutoBank upload failed: {e!r}")
            raise
        from selenium.webdriver.support.ui import Select

    def _upload_to_autobank(self, statement_path):
        driver = self.driver
        wait = WebDriverWait(driver, 20)
        original = driver.current_window_handle

        # Open AutoBank in a new tab
        driver.execute_script("window.open();")
        new_tab = [h for h in driver.window_handles if h != original][0]
        driver.switch_to.window(new_tab)

        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                # 1) Start from the login page each time
                driver.get("https://autobank.payatom.in/operator_index.php")

                # 2) Click SIGN IN if present (else assume already logged in)
                try:
                    login_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "a.auth-form-btn"))
                    )
                    login_btn.click()
                except TimeoutException:
                    self._send_msg("🔄 AutoBank: already logged in")

                # 3) Wait for dashboard sidebar
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "nav.sidebar")))

                # 4) Go to upload page
                driver.get("https://autobank.payatom.in/bankupload.php")
                wait.until(EC.presence_of_element_located((By.ID, "drop-zone")))

                # 5) Select TMB, fill account and file
                Select(driver.find_element(By.ID, "bank")) \
                    .select_by_visible_text("TMB")
                acct = driver.find_element(By.ID, "account_number")
                acct.clear()
                acct.send_keys(self.cred["account_number"])
                driver.find_element(By.ID, "file_input").send_keys(statement_path)

                # 6) Wait for success alert
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".swal2-icon-success")))

                # Success!
                self._send_msg(f"✅ AutoBank upload succeeded (attempt {attempt}/{max_attempts})")
                self._send(f"[DEBUG] XLS path: {statement_path})")
                break

            except Exception as e:
                # Capture screenshots of both tabs on every failure
                try:
                    self._send_screenshots()
                except:
                    pass

                self._send_msg(f"⚠️ AutoBank upload failed (attempt {attempt}/{max_attempts}): {e!r}")
                if attempt < max_attempts:
                    # retry from step 1 without full logout
                    continue

                # After final failure: close upload tab, switch back, and re-raise
                driver.close()
                driver.switch_to.window(original)
                raise

        # Cleanup on success: close upload tab and return to main window
        driver.close()
        driver.switch_to.window(original)

# --- insert after your TMBWorker definition: ---
class IOBWorker(threading.Thread):
    """
    Handles both IOB‐personal and IOB‐corporate.
    Suffix is determined by alias: alias.endswith('_iob') or '_iobcorp'
    """
    def __init__(
        self,
        bot,
        chat_id,
        alias,
        cred,
        loop,
        driver: Optional[webdriver.Chrome] = None,
        download_folder: Optional[str]      = None,
        profile_dir: Optional[str]         = None,
    ):
        super().__init__(daemon=True)
        self.bot          = bot
        self.chat_id      = chat_id
        self.alias        = alias
        self.cred         = cred
        self.loop         = loop
        self.captcha_code = None
        self.logged_in    = False
        self.retry_count  = 0
        self.stop_evt     = threading.Event()

        # ─── reuse injected Chrome if provided ───
        self.reused_driver = driver is not None
        if self.reused_driver:
            self.driver       = driver
            self.download_dir = download_folder
            self.profile      = None
            return

        # ─── otherwise, spin up a fresh Chrome instance ───
        self.profile = profile_dir

        # (A) per-alias download folder under "./downloads/<alias>"
        download_root = os.path.join(os.getcwd(), "downloads", alias)
        os.makedirs(download_root, exist_ok=True)
        self.download_dir = download_root

        opts = webdriver.ChromeOptions()
        opts.add_argument(f"--user-data-dir={profile_dir}")
        opts.add_argument("--start-maximized")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--ignore-certificate-errors")
        opts.add_argument("--allow-insecure-localhost")
        opts.add_argument("--ignore-ssl-errors")

        # (B) keep your prefs for download
        prefs = {
            "download.default_directory": download_root,
            "download.prompt_for_download": False,
            "profile.default_content_setting_values.automatic_downloads": 1,
        }
        opts.add_experimental_option("prefs", prefs)

        self.driver = webdriver.Chrome(options=opts)

        
    def _send(self, text):
        # update last‐active timestamp
        last_active[self.alias] = datetime.now()
        return asyncio.run_coroutine_threadsafe(
            self.bot.send_message(
                chat_id=self.chat_id,
                text=f"[{self.alias}] {text}",
                parse_mode=ParseMode.MARKDOWN,
            ),
            self.loop,
        )


    def _screenshot_tabs(self):
        for h in self.driver.window_handles:
            try:
                self.driver.switch_to.window(h)
                png = self.driver.get_screenshot_as_png()
                bio = BytesIO(png)
                which = "IOB" if h == self.iob_win else "AutoBank"
                coro = self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=bio,
                    caption=f"[{self.alias}] 📸 {which} screenshot",
                )
                asyncio.run_coroutine_threadsafe(coro, self.loop)
            except: pass

    def _retry(self):
        """Cycle to a fresh tab, close all the old ones, reset state—no login here."""
        old_handles = set(self.driver.window_handles)

        # 1) Open a new blank tab
        self.driver.execute_script("window.open('about:blank','_blank');")
        new_handle = (set(self.driver.window_handles) - old_handles).pop()

        # 2) Close every old tab
        for h in old_handles:
            try:
                self.driver.switch_to.window(h)
                self.driver.close()
            except:
                pass

        # 3) Switch into the fresh tab
        self.driver.switch_to.window(new_handle)

        # 4) Reset for a clean login next loop
        self.captcha_code = None
        self.logged_in   = False
        self._send(f"🔄 Cycling tabs—will retry login ({self.retry_count}/5)…")  # correct helper :contentReference[oaicite:2]{index=2}

        
    def run(self):
        # Notify start
        self._send("🚀 Starting IOB automation")

        # Outer loop: handles (re)login + retry logic
        while not self.stop_evt.is_set():
            try:
                # Attempt login
                self._login()
                self.retry_count = 0

                # If login succeeds, enter steady-state upload/balance cycle
                while not self.stop_evt.is_set():
                    self._download_and_upload_statement()
                    self._balance_enquiry()
                    self.retry_count = 0
                    time.sleep(60)
                break  # external stop requested

            except Exception as e:
                # Bump retry count
                self.retry_count += 1

                # Give up after 5 attempts
                if self.retry_count > 5:
                    self._send(f"❌ Failed {self.retry_count} times—stopping alias.")
                    return self.stop()

                # Otherwise screenshot, notify, and cycle tabs
                try:
                    self._screenshot_tabs()
                except:
                    pass
                self._send(f"⚠️ Error: {e!r}\nRetrying {self.retry_count}/5…")
                self._retry()
                # Loop back to try login again

        # Clean shutdown if stop_evt was set
        self.stop()
        
    def _login(self):
        # 1) Open the IOB login page
        self.driver.get("https://www.iobnet.co.in/ibanking/html/index.html")

        # 2) Click “Continue to Internet Banking Home Page”
        WebDriverWait(self.driver, 20).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Continue to Internet Banking Home Page"))
        ).click()

        # 3) Choose personal vs corporate
        role_text = "Corporate Login" if self.alias.endswith("_iobcorp") else "Personal Login"
        WebDriverWait(self.driver, 20).until(
            EC.element_to_be_clickable((By.LINK_TEXT, role_text))
        ).click()

        # 4) Fill in credentials
        if role_text.startswith("Corporate"):
            self.driver.find_element(By.NAME, "loginId").send_keys(self.cred["login_id"])
            self.driver.find_element(By.NAME, "userId").send_keys(self.cred["user_id"])
            self.driver.find_element(By.NAME, "password").send_keys(self.cred["password"])
        else:
            self.driver.find_element(By.NAME, "loginId").send_keys(self.cred["username"])
            self.driver.find_element(By.NAME, "password").send_keys(self.cred["password"])

        # 5) Grab the captcha image & send it to Telegram
        img = WebDriverWait(self.driver, 20).until(
            EC.presence_of_element_located((By.ID, "captchaimg"))
        )
        bio = BytesIO(img.screenshot_as_png)

        self._send("🤖 Trying to auto-solve CAPTCHA via 2Captcha…")
        solution, self._captcha_id = solve_captcha_with_2captcha(
            bio.getvalue(), min_len=6, max_len=6, regsense=True
        )

        if solution:
            self.captcha_code = solution.upper()  # force to uppercase only for IOB
            self._send(f"✅ Auto-solved: `{self.captcha_code}` (converted to uppercase)")
        else:
            asyncio.run_coroutine_threadsafe(
                self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=bio,
                    caption=f"[{self.alias}] 🔐 Please solve captcha"
                ),
                self.loop,
            )
            self._send("⚠️ 2Captcha failed. Waiting for your reply…")
            while not self.captcha_code and not self.stop_evt.is_set():
                time.sleep(0.5)

        if self.stop_evt.is_set():
            return

        
        # 7) Fill in the captcha (note name="captchaid", not "captchajid")
        field = self.driver.find_element(By.NAME, "captchaid")
        field.clear()
        field.send_keys(self.captcha_code)
        
        # 8) Submit
        self.driver.find_element(By.ID, "btnSubmit").click()
        try:
            err_span = WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.otpmsg span.red"))
            )
            if "captcha entered is incorrect" in err_span.text.lower() and hasattr(self, "_captcha_id"):
                self._send("❌ CAPTCHA wrong — reporting to 2Captcha and retrying…")
                report_bad_captcha(self._captcha_id)
                self._retry()
                return
        except TimeoutException:
            pass  # no error shown → continue
                
        # 9) Wait until the main menu loads, then mark ourselves logged-in
        WebDriverWait(self.driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "nav.accordian"))
        )
        self.iob_win = self.driver.current_window_handle
        self.logged_in = True
        self._send("✅ Logged in!")


    from datetime import datetime, timedelta
    import os, time
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import ElementClickInterceptedException
    def _download_and_upload_statement(self):
        # 1) Navigate to “Account statement”
        WebDriverWait(self.driver, 10).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Account statement"))
        ).click()

        # 2) Pick the right account
        acct_sel = WebDriverWait(self.driver, 10).until(
            EC.element_to_be_clickable((By.ID, "accountNo"))
        )
        dropdown = Select(acct_sel)
        for opt in dropdown.options:
            if opt.text.startswith(self.cred["account_number"]):
                dropdown.select_by_visible_text(opt.text)
                break

        # 3) Compute from/to dates
        now = datetime.now()
        from_dt = now - timedelta(days=1) if now.hour < 6 else now
        to_dt   = now
        from_str = from_dt.strftime("%m/%d/%Y")
        to_str   = to_dt.strftime("%m/%d/%Y")

        # 4) Fill the “From Date”
        from_input = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.ID, "fromDate"))
        )
        # remove readonly and set value via JS
        self.driver.execute_script("arguments[0].removeAttribute('readonly')", from_input)
        self.driver.execute_script("arguments[0].value = arguments[1]", from_input, from_str)

        # 5) Fill the “To Date”
        to_input = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.ID, "toDate"))
        )
        self.driver.execute_script("arguments[0].removeAttribute('readonly')", to_input)
        self.driver.execute_script("arguments[0].value = arguments[1]", to_input, to_str)

        # 6) Click “View” (note: it’s an <input> not a <button>)
        view_btn = WebDriverWait(self.driver, 10).until(
            EC.element_to_be_clickable((By.ID, "accountstatement_view"))
        )
        self.driver.execute_script("arguments[0].scrollIntoView(true);", view_btn)
        view_btn.click()
        time.sleep(10)
        # 7) Wait for the CSV export button to appear, scroll to it…
        csv_btn = WebDriverWait(self.driver, 30).until(
            EC.element_to_be_clickable((By.ID, "accountstatement_csvAcctStmt"))
        )
        # a) scroll so it’s roughly centered (avoids sticky headers)
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", csv_btn
        )
        time.sleep(5)  # give any animations a moment

        # b) try the normal click, but if it’s still intercepted, do a JS click
        try:
            csv_btn.click()
        except ElementClickInterceptedException:
            self.driver.execute_script("arguments[0].click();", csv_btn)
        # 8) Wait for the download to finish
        download_dir = self.download_dir
        end_time = time.time() + 60
        csv_path = None
        while time.time() < end_time:
            files = [f for f in os.listdir(download_dir) if f.lower().endswith(".csv")]
            if files:
                csv_path = os.path.join(
                    download_dir,
                    max(files, key=lambda f: os.path.getctime(os.path.join(download_dir, f)))
                )
                self._send(f"[DEBUG1] XLS path: {csv_path})")
                break
            self._send(f"[DEBUG2] XLS path: {csv_path})")
            time.sleep(1)
        if not csv_path:
            raise TimeoutException("Timed out waiting for IOB CSV download")

        # 9) Open a new tab for AutoBank and remember the old one
        # ————— (9) Robust AutoBank upload with 5 retries —————
        original_handle = self.driver.current_window_handle
        max_attempts    = 5
        autobank_handle = None

        for attempt in range(1, max_attempts + 1):
            # On first try, open a new tab; thereafter reuse it
            if attempt == 1:
                self.driver.execute_script("window.open('about:blank');")
                handles = self.driver.window_handles
                autobank_handle = [h for h in handles if h != original_handle][-1]

            self.driver.switch_to.window(autobank_handle)
            #self._send(f"🔄 AutoBank upload attempt {attempt}/{max_attempts} starting…")

            try:
                # 9a) Go to login page
                self.driver.get("https://autobank.payatom.in/operator_index.php")

                # 9b) Click SIGN IN if present
                try:
                    WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "a.auth-form-btn"))
                    ).click()
                except TimeoutException:
                    pass  # already logged in

                # 9c) Wait for dashboard sidebar
                WebDriverWait(self.driver, 60).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "nav.sidebar"))
                )

                # 9d) Go to upload page
                self.driver.get("https://autobank.payatom.in/bankupload.php")
                WebDriverWait(self.driver, 20).until(
                    EC.presence_of_element_located((By.ID, "drop-zone"))
                )

                # 9e) Fill form
                Select(WebDriverWait(self.driver, 60).until(
                    EC.presence_of_element_located((By.ID, "bank"))
                )).select_by_visible_text("IOB")

                acct_field = WebDriverWait(self.driver, 60).until(
                    EC.element_to_be_clickable((By.ID, "account_number"))
                )
                acct_field.clear()
                acct_field.send_keys(self.cred["account_number"])

                file_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.ID, "file_input"))
                )
                file_input.send_keys(csv_path)

                # 9f) Wait for success
                WebDriverWait(self.driver, 30).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, ".swal2-icon-success"))
                )

                self._send(f"✅ AutoBank upload succeeded on attempt {attempt}/{max_attempts}")
                self._send(f"[DEBUG] XLS path: {csv_path})")
                break

            except Exception as e:
                # screenshot both tabs
                try:
                    self._screenshot_tabs()
                except:
                    pass

                err_name = type(e).__name__
                self._send(f"⚠️ AutoBank upload failed on attempt {attempt}/{max_attempts}: {err_name}: {e}")

                if attempt == max_attempts:
                    # final failure → close tab & re-raise
                    self.driver.close()
                    self.driver.switch_to.window(original_handle)
                    raise
                # otherwise brief pause then retry
                time.sleep(2)

        # 10) Cleanup: ensure we're back on the IOB tab
        if self.driver.current_window_handle != original_handle:
            self.driver.close()
            self.driver.switch_to.window(original_handle)

        #self._send("✅ Uploaded statement to AutoBank")

    def _balance_enquiry(self):
        # 1) Make sure we're on the IOB tab
        self.driver.switch_to.window(self.iob_win)

        # 2) Scroll back to the very top (so the nav is visible)
        self.driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)

        # 3) Locate the Balance Enquiry link
        balance_link = WebDriverWait(self.driver, 60).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Balance Enquiry"))
        )

        # 4) Scroll it into view (center of viewport)
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center'});", balance_link
        )
        time.sleep(0.3)

        # 5) Click (with JS fallback)
        try:
            balance_link.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", balance_link)

        # 6) Now scrape the popup as before
        acctno = self.cred["account_number"]
        link = WebDriverWait(self.driver, 180).until(
            EC.element_to_be_clickable(
                (By.XPATH, f"//a[contains(@href,'getBalance') and contains(.,'{acctno}')]")
            )
        )
        link.click()
        time.sleep(30)
        tbl = WebDriverWait(self.driver, 180).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "#dialogtbl table tr.querytr td")
            )
        )
        available = tbl.text.strip()
        self._send(f"💰: {available}")
        self.last_balance = available
        # 1) Remove the modal overlay & dialog so nothing blocks clicks
        self.driver.execute_script("""
            document.querySelectorAll('.ui-widget-overlay, #dialogtbl').forEach(el => el.remove());
        """)

        # 2) Click “Account Statement” again (or just navigate there directly)
        WebDriverWait(self.driver, 180).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Account statement"))
        ).click()
    def _logout(self):
        try:
            self.stop_evt.set()
            self.driver.switch_to.window(self.iob_win)
            self._send("🚪 Logging out…")
            self.driver.set_page_load_timeout(5)
            self.driver.get("https://www.iobnet.co.in/ibanking/logout.do?mode=USERCLICK")
            # only quit if we created this browser ourselves
            if not self.reused_driver:
                self.driver.quit()
            self._send("✅ Logged out")
        except Exception:
            pass
        finally:
            self.stop_evt.set()
            # 5) pop the profile assignment, return it to the pool, and mark inactive
            profile = _profile_assignments.pop(self.alias, None)
            if profile:
                _free_profiles.append(profile)
                _active[profile] = False
            # only quit again if necessary
            if not self.reused_driver:
                try:
                    self.driver.quit()
                except:
                    pass

    def stop(self):
        self.stop_evt.set()
        if self.logged_in:
            try:
                self._send("🚪 Logging out…")
                self.driver.set_page_load_timeout(5)
                self.driver.get("https://www.iobnet.co.in/ibanking/logout.do?mode=USERCLICK")
                self._send("✅ Logged out")
            except Exception:
                pass

        try:
            self._screenshot_tabs()
        except Exception:
            pass

        if self.reused_driver:
            # open a fresh AutoBank tab and close all others
            self.driver.switch_to.window(self.driver.window_handles[0])
            self.driver.execute_script(
                "window.open('https://autobank.payatom.in/operator_index.php');"
            )
            # close every tab except the new one
            for handle in self.driver.window_handles[:-1]:
                self.driver.switch_to.window(handle)
                self.driver.close()
            # switch to the remaining AutoBank tab
            self.driver.switch_to.window(self.driver.window_handles[0])
            # 5) pop the profile assignment, return it to the pool, and mark inactive
            profile = _profile_assignments.pop(self.alias, None)
            if profile:
                _free_profiles.append(profile)
                _active[profile] = False            
        else:
            try:
                self.driver.quit()
            except Exception:
                pass
 
class KGBWorker(threading.Thread):
    """
    Automates Kerala Gramin Bank (KGB) login → balance check → statement download → AutoBank upload
    """

    def __init__(
        self,
        bot,
        chat_id,
        alias,
        cred,
        loop,
        driver: webdriver.Chrome,
        download_folder: str,
        profile_dir: Optional[str] = None,
    ):
        super().__init__(daemon=True)
        self.bot          = bot
        self.chat_id      = chat_id
        self.alias        = alias
        self.cred         = cred
        self.loop         = loop
        self.captcha_code = None
        self.logged_in    = False
        self.retry_count  = 0
        self.stop_evt     = threading.Event()

        # ─── always use the injected driver ───
        assert driver, "KGBWorker requires a Chrome driver instance"
        self.driver       = driver
        self.download_dir = download_folder

    def _retry(self):
        """Cycle to a fresh tab, close the old ones, reset state—no login here."""
        old_handles = set(self.driver.window_handles)

        # 1) Open a new blank tab
        self.driver.execute_script("window.open('about:blank','_blank');")
        new_handle = (set(self.driver.window_handles) - old_handles).pop()

        # 2) Close every old tab
        for h in old_handles:
            try:
                self.driver.switch_to.window(h)
                self.driver.close()
            except:
                pass

        # 3) Switch into the fresh tab
        self.driver.switch_to.window(new_handle)

        # 4) Reset for a clean login next loop
        self.captcha_code = None
        self.logged_in   = False
        self._send(f"🔄 Cycling tabs—will retry login ({self.retry_count}/5)…")
        
    def _send(self, text: str):
        # update last‐active timestamp
        last_active[self.alias] = datetime.now()
        return asyncio.run_coroutine_threadsafe(
            self.bot.send_message(
                chat_id=self.chat_id,
                text=f"[{self.alias}] {text}",
                parse_mode=ParseMode.MARKDOWN,
            ),
            self.loop,
        )

    def run(self):
        # initial notification
        self._send("🚀 Starting KGB automation")

        # outer loop: handles login + retry logic
        while not self.stop_evt.is_set():
            try:
                # 1) Attempt login
                self._login()
                self.retry_count = 0

                # 2) If login succeeds, do the normal cycle
                while not self.stop_evt.is_set():
                    self._read_balance_and_navigate_to_statement()
                    self._download_and_upload_statement()
                    self.retry_count = 0
                    time.sleep(60)

                break  # if stop_evt set, exit cleanly

            except Exception as e:
                # bump retry count
                self.retry_count += 1

                # too many failures → stop permanently
                if self.retry_count > 5:
                    self._send(f"❌ Failed {self.retry_count} times—stopping alias.")
                    return self.stop()

                # otherwise screenshot, notify, and cycle tabs
                try:    self._screenshot_tabs()
                except: pass
                self._send(f"⚠️ Error: {e!r}\nRetrying {self.retry_count}/5…")
                self._retry()
                # loop back to try login again

        # if externally stopped, ensure logout/quit
        self.stop()

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    def _login(self):
        """
        Step 1: Go to KGB Netbanking login page, solve CAPTCHA, click Login to reach 2nd‐factor page.
        """
        # 1a) Open the login URL
        self.driver.get("https://netbanking.keralagbank.com/")
        time.sleep(5)
        # 1b) Wait for User ID input:  (e1)
        WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.ID, "AuthenticationFG.USER_PRINCIPAL"))
        )
        time.sleep(5)
        # 1c) Fill in User ID (alias.login_id)
        self.driver.find_element(By.ID, "AuthenticationFG.USER_PRINCIPAL")\
            .send_keys(self.cred["username"])
        time.sleep(5)
        # 1d) Wait for CAPTCHA image and try auto-solve
        img = WebDriverWait(self.driver, 15).until(
            EC.presence_of_element_located((By.ID, "IMAGECAPTCHA"))
        )
        bio = BytesIO(img.screenshot_as_png)
        time.sleep(5)
        self._send("🤖 Trying to auto-solve CAPTCHA via 2Captcha…")
        solution, self._captcha_id = solve_captcha_with_2captcha(bio.getvalue())
        if solution:
            self.captcha_code = solution
            self._send(f"✅ Auto-solved: `{solution}`")
        else:
            asyncio.run_coroutine_threadsafe(
                self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=bio,
                    caption=f"[{self.alias}] 🔐 Please solve this CAPTCHA"
                ),
                self.loop
            )
            self._send("⚠️ 2Captcha failed. Waiting for your CAPTCHA reply…")
            while not self.captcha_code and not self.stop_evt.is_set():
                time.sleep(5)
            if self.stop_evt.is_set():
                return
            

        time.sleep(5)
        # 1f) Fill in CAPTCHA (e3) and click Login (e4)
        self.driver.find_element(By.ID, "AuthenticationFG.VERIFICATION_CODE")\
            .send_keys(self.captcha_code)
        time.sleep(5)
        # 1f) (Find & click whichever STU_VALIDATE_CREDENTIALS button is visible)
        login_buttons = self.driver.find_elements(By.ID, "STU_VALIDATE_CREDENTIALS")
        login_btn = None
        for btn in login_buttons:
            if btn.is_displayed() and btn.is_enabled():
                login_btn = btn
                break
        if login_btn is None:
            raise TimeoutException("Could not find a visible/enabled STU_VALIDATE_CREDENTIALS button.")

        # Scroll into view + attempt a normal click, fallback to JS click if blocked
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", login_btn)
        time.sleep(0.2)
        try:
            login_btn.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", login_btn)
        time.sleep(5)
        # 🧠 Check for CAPTCHA error message under span.errorCodeWrapper
        try:
            error_text_elem = WebDriverWait(self.driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "span.errorCodeWrapper p[style='display: inline']")
                )
            )
            if "enter the characters" in error_text_elem.text.lower() and hasattr(self, "_captcha_id"):
                self._send("❌ Login failed — reporting bad CAPTCHA to 2Captcha.")
                report_bad_captcha(self._captcha_id)
                self._retry()
                return
        except TimeoutException:
            pass  # No error found — proceed


        # ─── Now we’re on the 2nd‐factor page ───
        # 1) Instead of waiting for #messageContent to go away, simply wait for
        #    the visible <span class="span-checkbox"> to be clickable.
        time.sleep(5)
        checkbox_span = WebDriverWait(self.driver, 30).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "span.span-checkbox"))
        )

        # Scroll it into view, then click it
        self.driver.execute_script(
            "arguments[0].scrollIntoView({ block: 'center' });",
            checkbox_span
        )
        time.sleep(5)
        try:
            checkbox_span.click()
        except Exception:
            # If “.click()” is blocked for some reason, fallback to JS click:
            self.driver.execute_script("arguments[0].click();", checkbox_span)


        # ─── Now that checkbox is checked, fill and submit the password ───
        pwd = self.driver.find_element(By.ID, "AuthenticationFG.ACCESS_CODE")
        pwd.send_keys(self.cred["password"])

        # 1i) Submit the second‐factor form by re‐using the same “Login” button or just pressing Enter.
        #     There is no separate “Submit” button here—filling the checkbox + password
        #     and pressing ENTER will work. We can send an ENTER key:
        pwd.send_keys("\n")
        time.sleep(5)
        # 1j) Wait until the main Dashboard (with “Account Balances” or a breadcrumb) appears.
        #     We know we’re logged in when the “Account Statement” link (on the left nav) is clickable.
        WebDriverWait(self.driver, 60).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Account Statement"))
        )
        self._send("✅ Logged in to KGB Netbanking!")
        self.logged_in = True
        # Remember this handle for logout
        self.kgb_win = self.driver.current_window_handle

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import time
    import asyncio

    def _read_balance_and_navigate_to_statement(self):
        """
        Step 2: On the landing page, click “Account Statement” from the left nav,
                wait for the Account Balances Summary, scrape the Available Balance,
                send it to Telegram, then click on the account‐nickname link to reach
                the statement‐download page.
        """

        # 2a) Make sure we’re on the main KGB window
        self.driver.switch_to.window(self.kgb_win)

        # 2b) Click the left‐nav “Account Statement” (LINK_TEXT exactly “Account Statement”).
        acct_stmt_link = WebDriverWait(self.driver, 15).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Account Statement"))
        )
        # Scroll it into view & click
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", acct_stmt_link)
        time.sleep(0.2)
        try:
            acct_stmt_link.click()
        except:
            # Fallback to JS click if normal click fails
            self.driver.execute_script("arguments[0].click();", acct_stmt_link)

        # 2e’) Wait for the account summary table to load
        WebDriverWait(self.driver, 20).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "table tbody tr"))
        )

        acct_no = self.cred["account_number"].strip()
        rows = self.driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

        for row in rows:
            tds = row.find_elements(By.TAG_NAME, "td")
            if not tds:
                continue
            if tds[0].text.strip() == acct_no:
                # ─── scrape the Available balance from *this* row ─────────────────
                try:
                    balance_span = row.find_element(
                        By.CSS_SELECTOR, "span.hwgreentxt.amountRightAlign"
                    )
                    available_balance = balance_span.text.strip()
                except NoSuchElementException:
                    # fallback: parse it out of the 4th cell’s text
                    text = tds[3].text.splitlines()[-1]      # "Available: INR 16,473.66"
                    available_balance = text.split()[-1]     # "16,473.66"
                # send that balance to Telegram
                asyncio.run_coroutine_threadsafe(
                    self.bot.send_message(
                        chat_id=self.chat_id,
                        text=f"[{self.alias}] 💰: {available_balance}"
                    ),
                    self.loop
                )
                self.last_balance = available_balance
                # ────────────────────────────────────────────────────────────────

                # ─── now click the matching Account Nickname link ────────────────
                try:
                    acct_link = row.find_element(
                        By.XPATH, ".//a[@title='Account Nickname']"
                    )
                except NoSuchElementException:
                    acct_link = tds[1].find_element(By.TAG_NAME, "a")

                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", acct_link
                )
                time.sleep(0.2)
                try:
                    acct_link.click()
                except:
                    self.driver.execute_script("arguments[0].click();", acct_link)
                # ────────────────────────────────────────────────────────────────

                break
        else:
            raise RuntimeError(f"Account number {acct_no!r} not found on the summary page")

        # At this point, you should be on the “Account Statement” download page.
        # From here you can proceed to choose date ranges, click “Search,” and then download CSV/XLS.

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
    import time, os
    from datetime import datetime, timedelta
    from selenium.common.exceptions import TimeoutException

    def _download_and_upload_statement(self):
        """
        Step 3: On the “Account Statement” page (after clicking the nickname link),
                fill “From”/“To” dates, click Search, force-select “XLS” in the hidden <select>,
                click OK to download, then upload to AutoBank.
        """

        # 3a) Wait for the “From Date” field (ID ending with .FROM_TXN_DATE) to appear
        WebDriverWait(self.driver, 180).until(
            EC.presence_of_element_located(
                (By.ID, "PageConfigurationMaster_RXACBSW__1:TransactionHistoryFG.FROM_TXN_DATE")
            )
        )

        # 3b) Compute “From” / “To” dates
        # 3b) Compute “From” / “To” dates (custom if present, else original logic)
        if hasattr(self, "from_dt") and hasattr(self, "to_dt"):
            dt_from, dt_to = self.from_dt, self.to_dt
        else:
            now = datetime.now()
            # ← your original 6 AM cutoff
            dt_from = now - timedelta(days=1) if now.hour < 6 else now
            dt_to   = now

        # keep the exact same names your fill-in code expects
        from_str = dt_from.strftime("%d/%m/%Y")
        to_str   = dt_to.strftime("%d/%m/%Y")



        # 3c) Fill “From Date”
        from_input = self.driver.find_element(
            By.ID, "PageConfigurationMaster_RXACBSW__1:TransactionHistoryFG.FROM_TXN_DATE"
        )
        self.driver.execute_script("arguments[0].removeAttribute('readonly')", from_input)
        self.driver.execute_script("arguments[0].value = arguments[1]", from_input, from_str)

        # 3d) Fill “To Date”
        to_input = self.driver.find_element(
            By.ID, "PageConfigurationMaster_RXACBSW__1:TransactionHistoryFG.TO_TXN_DATE"
        )
        self.driver.execute_script("arguments[0].removeAttribute('readonly')", to_input)
        self.driver.execute_script("arguments[0].value = arguments[1]", to_input, to_str)
        time.sleep(5)
        # 3e) Click “Search”
        search_btn = WebDriverWait(self.driver, 60).until(
            EC.element_to_be_clickable((By.ID, "PageConfigurationMaster_RXACBSW__1:SEARCH"))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", search_btn)
        time.sleep(2)
        attempts = 0
        while attempts < 3:
            try:
                search_btn.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", search_btn)

            # ⏳ Give it time to load fully
            time.sleep(30)

            # ❗ Check for error message indicating no transactions
            try:
                err_box = self.driver.find_element(By.CSS_SELECTOR, "div.error-box, .errormessages")
                if "do not exist for the account" in err_box.text:
                    self._send(f"⚠️ No transactions found, retrying search... ({attempts+1}/3)")
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", search_btn)
                    time.sleep(2)
                    attempts += 1
                    continue
            except:
                break  # No error box means success — break loop
        else:
            self._send("❌ Tried 3 times but no transactions found. Logging out.")
            self._screenshot_tabs()
            self._logout()
            return

        # ─── small pagination‐fix: if “1 – 5 of 500” then jump to page 101 ───
        # … after your SEARCH click, before STEP 1 …

#        try:
#            status_label = WebDriverWait(self.driver, 5).until(
#                EC.presence_of_element_located((
#                    By.CSS_SELECTOR,
#                    "span.text.pagination-status label.simple-text.pagination-status"
#                ))
#            )
#        except TimeoutException:
#            # pagination status not present → skip our 500-check entirely
 #           pass
 #       else:
  #          if "of 500" in status_label.text:
   #             self._send(
    #                "Notice: Transaction count exceeds 500. Automatically navigating to page 101 to retrieve remaining entries."
     #           )
#
 #               # enter page 101
  #              page_input = WebDriverWait(self.driver, 10).until(
   #                 EC.presence_of_element_located((
    #                    By.ID,
     #                   "PageConfigurationMaster_RXACBSW__1:TransactionHistoryFG.OpTransactionListing_REQUESTED_PAGE_NUMBER"
      #              ))
       #         )
        #        page_input.clear()
         #       page_input.send_keys("101")
#
 #               # click the “GO” button
  #              go_btn = WebDriverWait(self.driver, 10).until(
   #                 EC.element_to_be_clickable((
    #                    By.ID,
     #                   "PageConfigurationMaster_RXACBSW__1:Action.OpTransactionListing.GOTO_PAGE__"
      #              ))
       #         )
        #        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", go_btn)
         #       go_btn.click()
          #      time.sleep(30)
        # ─── STEP 1: after clicking SEARCH, take a screenshot & dump dropdown-wrapper HTML ───
        #self.driver.save_screenshot("step1_search.png")
        #print("STEP 1: Screenshot saved as step1_search.png")


        # Wait for the hidden <select> to exist (so we know the widget is rendered):
        WebDriverWait(self.driver, 60).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'select[name="TransactionHistoryFG.OUTFORMAT"]')
            )
        )

        # Find the wrapper DIV around the dropdown
        try:
            wrapper = self.driver.find_element(
                By.XPATH,
                "//select[@name='TransactionHistoryFG.OUTFORMAT']/ancestor::div[contains(@class,'dropdownexpandalbe_download')]"
            )
        except Exception as e:
            print("ERROR: Cannot find dropdown-wrapper DIV:", e)
            raise
        # Print out its entire outerHTML for inspection:
        wrapper_html = wrapper.get_attribute("outerHTML")
        print("STEP 1: dropdown-wrapper outerHTML:", wrapper_html)

        # Print out the <select>’s current value before doing anything:
        select_elem = wrapper.find_element(
            By.CSS_SELECTOR,
            'select[name="TransactionHistoryFG.OUTFORMAT"]'
        )
        # Print out its entire outerHTML for inspection:
        wrapper_html = wrapper.get_attribute("outerHTML")
        print("\nSTEP 1: dropdown-wrapper outerHTML:\n", wrapper_html, "\n")

        # Print out the <select>’s current value before doing anything:
        #select_elem = wrapper.find_element(By.CSS_SELECTOR, 'select[name="TransactionHistoryFG.OUTFORMAT"]')
        #print("STEP 1: <select> current value =", select_elem.get_attribute("value"))

        # ─── (D) SCROLL wrapper into view and JS‐click the <input.select-dropdown> ───
        self.driver.execute_script("arguments[0].scrollIntoView({block:'end'});", wrapper)
        time.sleep(0.3)
        # Take another screenshot now that we’ve scrolled down:
        #self.driver.save_screenshot("step2_wrapper_in_view.png")
        #print("STEP 2: Screenshot after scrolling wrapper into view: step2_wrapper_in_view.png")

        try:
            dropdown_input = wrapper.find_element(By.CSS_SELECTOR, "input.select-dropdown")
        except Exception as e:
            print("ERROR: Could not find input.select-dropdown inside wrapper:", e)
            raise

        # Print its data-activates attribute before clicking:
        da = dropdown_input.get_attribute("data-activates")
        print("STEP 2: dropdown_input data-activates =", da)

        # JS‐click it (normal .click() may fail):
        self.driver.execute_script("arguments[0].click();", dropdown_input)
        time.sleep(0.5)

        # Screenshot after opening the dropdown:
        self.driver.save_screenshot("step3_dropdown_open.png")
        print("STEP 3: Screenshot after JS-click on dropdown_input: step3_dropdown_open.png")

        # ─── (E) WAIT FOR THE GENERATED UL TO EXIST (by ID) ───
        if not da:
            print("ERROR: data-activates was empty—dropdown never opened.")
            raise RuntimeError("data-activates is empty")

        ul_locator = (By.ID, da)
        try:
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(ul_locator)
            )
        except Exception as e:
            print("ERROR: Timeout waiting for UL with id =", da, "→", e)
            # Dump the page source for further debugging:
            page_src = self.driver.page_source
            with open("step4_page_source.html", "w", encoding="utf-8") as f:
                f.write(page_src)
            print("STEP 4: page source saved to step4_page_source.html")
            raise

        # Grab the UL element:
        visible_ul = self.driver.find_element(By.ID, da)

        # Print out the UL’s outerHTML and each <li>’s text:
        ul_html = visible_ul.get_attribute("outerHTML")
        print("\nSTEP 4: <ul> outerHTML:\n", ul_html, "\n")

        li_elems = visible_ul.find_elements(By.TAG_NAME, "li")
        print("STEP 4: Found", len(li_elems), "<li> elements. Their visible texts are:")
        for idx, li in enumerate(li_elems, start=1):
            # Each <li> wraps a <span> inside. Use li.find_element to get that span’s text.
            try:
                span_text = li.find_element(By.TAG_NAME, "span").get_attribute("innerText").strip()
            except:
                span_text = "(unable to read span)"
            print(f"   li #{idx}: “{span_text}”")

        # Before clicking, print the <select>’s value again:
        print("STEP 4: <select> value before selecting XLS =", select_elem.get_attribute("value"))

        # ─── (F) SCROLL UL INTO VIEW, THEN JS‐CLICK THE <li> THAT’S “XLS” ───
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});",
            visible_ul
        )
        time.sleep(0.2)

        try:
            # Correct XPath to match the <span> inside <li>
            li_xls = visible_ul.find_element(
                By.XPATH, ".//li[.//span[normalize-space(text())='XLS']]"
            )
        except Exception as e:
            print("ERROR: Could not find LI with span text ‘XLS’: ", e)
            raise

        # Print out the found element’s tagName and text for sanity:
        print("STEP 5: Found li_xls.tagName =", li_xls.tag_name)
        try:
            print("STEP 5: Found li_xls span text =", li_xls.find_element(By.TAG_NAME, "span").get_attribute("innerText"))
        except:
            print("STEP 5: Could not read span text inside li_xls.")

        # JS‐click that <li>:
        self.driver.execute_script("arguments[0].click();", li_xls)
        time.sleep(0.3)

        # Screenshot after clicking “XLS”:
        self.driver.save_screenshot("step6_clicked_xls.png")
        print("STEP 6: Screenshot after clicking XLS: step6_clicked_xls.png")

        # After clicking “XLS,” print <select>’s new value:
        new_val = select_elem.get_attribute("value")
        print("STEP 6: <select> value after selecting XLS =", new_val)

        # ─── (G) CLICK “OK” TO START DOWNLOAD ───
        try:
            ok_btn = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable(
                    (By.ID, "PageConfigurationMaster_RXACBSW__1:GENERATE_REPORT")
                )
            )
        except Exception as e:
            print("ERROR: Could not locate or click OK button:", e)
            raise

        # Scroll OK into view and click
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", ok_btn)
        time.sleep(0.2)
        try:
            ok_btn.click()
        except:
            self.driver.execute_script("arguments[0].click();", ok_btn)

        # Screenshot immediately after clicking OK:
        self.driver.save_screenshot("step7_clicked_ok.png")
        print("STEP 7: Screenshot after clicking OK: step7_clicked_ok.png")

        # 3i) Wait up to 60 seconds for the new .xls to appear in our download folder:
        download_dir = self.download_dir
        end_time = time.time() + 60
        xls_path = None
        while time.time() < end_time:
            files = [f for f in os.listdir(download_dir) if f.lower().endswith(".xls")]
            if files:
                newest = max(files, key=lambda f: os.path.getctime(os.path.join(download_dir, f)))
                xls_path = os.path.join(download_dir, newest)
                break
            time.sleep(1)

        if not xls_path:
            raise TimeoutException("Timed out waiting for KGB .xls to finish downloading")

        # 3j) Open AutoBank in a new tab
        original_handle = self.driver.current_window_handle
        self.driver.execute_script("window.open();")
        new_tab = [h for h in self.driver.window_handles if h != original_handle][0]
        self.driver.switch_to.window(new_tab)

        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                # ← retry always starts here
                self.driver.get("https://autobank.payatom.in/operator_index.php")

                # 3k) Sign-in if needed
                try:
                    sign_in_btn = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "a.auth-form-btn"))
                    )
                    sign_in_btn.click()
                except TimeoutException:
                    pass  # already logged in

                # 3l) Go to upload page
                WebDriverWait(self.driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "nav.sidebar"))
                )
                self.driver.get("https://autobank.payatom.in/bankupload.php")
                WebDriverWait(self.driver, 20).until(
                    EC.presence_of_element_located((By.ID, "drop-zone"))
                )

                # 3m) Select KGB, fill account_number, attach file & wait for success
                Select(self.driver.find_element(By.ID, "bank")) \
                    .select_by_visible_text("Kerala Gramin Bank")
                acct = self.driver.find_element(By.ID, "account_number")
                acct.clear()
                acct.send_keys(self.cred["account_number"])
                self.driver.find_element(By.ID, "file_input").send_keys(xls_path)
                WebDriverWait(self.driver, 120).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".swal2-icon-success"))
                )

                # ✅ success: report and break
                self._send(f"✅ AutoBank upload succeeded (attempt {attempt}/{max_attempts})")
                self._send(f"[DEBUG] XLS path: {xls_path})")
                break

            except Exception as e:
                # 📸 capture both tabs on error
                try:
                    self._screenshot_tabs()
                except:
                    pass

                self._send(f"⚠️ AutoBank upload failed (attempt {attempt}/{max_attempts}): {e!r}")
                if attempt < max_attempts:
                    # transient → retry (loop re-GETs operator_index.php)
                    continue
                else:
                    # permanent → close and re-raise for full-login cycle
                    self.driver.close()
                    self.driver.switch_to.window(original_handle)
                    raise

        # cleanup after success
        self.driver.close()
        self.driver.switch_to.window(original_handle)
        self._send("✅ Uploaded statement to AutoBank")

        # 3o) Scroll to top and click “Account Statement” breadcrumb to return to Balances
        back_btn = WebDriverWait(self.driver, 10).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Account Statement"))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", back_btn)
        time.sleep(0.2)
        try:
            back_btn.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", back_btn)

        # 3p) Finally, wait for the “Available Balance” span to refresh
        refreshed_balance_span = WebDriverWait(self.driver, 20).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "span.hwgreentxt.amountRightAlign")
            )
        )
        updated_balance = refreshed_balance_span.text.strip()
        #self._send(f"Balance (after upload): {updated_balance}")

    def stop(self):
        """
        Gracefully stop the KGB worker:
          1) signal halt
          2) perform logout if logged in
          3) capture diagnostics screenshots
          4) recycle the shared Chrome: close old tabs, open a fresh AutoBank tab
          5) pop the profile assignment, return it to the pool, and mark inactive
        """
        # 1) signal halt
        self.stop_evt.set()

        # 2) if already logged in, log out
        if self.logged_in:
            try:
                self._logout()
            except Exception:
                pass

        # 3) capture screenshots for diagnostics
        try:
            self._screenshot_tabs()
        except Exception:
            pass

        # 4) recycle the shared browser
        self.driver.switch_to.window(self.driver.window_handles[0])
        self.driver.execute_script(
            "window.open('https://autobank.payatom.in/operator_index.php');"
        )
        for handle in self.driver.window_handles[:-1]:
            self.driver.switch_to.window(handle)
            self.driver.close()
        self.driver.switch_to.window(self.driver.window_handles[0])

        # 5) pop the profile assignment, return it to the pool, and mark inactive
        profile = _profile_assignments.pop(self.alias, None)
        if profile:
            _free_profiles.append(profile)
            _active[profile] = False
    

    def _logout(self):
        """
        Log out from KGB, then signal stop.
        """
        try:
            self.stop_evt.set()
            # switch back to the KGB tab
            self.driver.switch_to.window(self.kgb_win)

            self._send("🚪 Logged out of KGB.")
        except Exception:
            pass
        finally:
            # signal stop but do not quit the shared driver
            self.stop_evt.set()


    def _screenshot_tabs(self):
        """
        Capture all open tabs (KGB + AutoBank) and send into Telegram.
        """
        for h in self.driver.window_handles:
            try:
                self.driver.switch_to.window(h)
                png = self.driver.get_screenshot_as_png()
                bio = BytesIO(png)
                which = "KGB" if h == self.kgb_win else "AutoBank"
                coro = self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=bio,
                    caption=f"[{self.alias}] 📸 {which} screenshot",
                )
                asyncio.run_coroutine_threadsafe(coro, self.loop)
            except:
                continue
            
class IDBIWorker(threading.Thread):
    """
    Automates IDBI Bank:
      1) Login with CAPTCHA
      2) Scrape Available Balance & send to Telegram
      3) Drill into A/C Statement → download XLS every minute
      4) Upload to AutoBank as “IDBI”
      5) On any error: screenshot all tabs, retry up to 5×
    """
    def __init__(self, bot, chat_id, alias, cred, loop, profile_dir):
        super().__init__(daemon=True)
        self.bot         = bot
        self.chat_id     = chat_id
        self.alias       = alias
        self.cred        = cred             # expects keys: login_id (or username), password, account_number
        self.loop        = loop
        self.profile     = profile_dir
        self.captcha_code= None
        self.logged_in   = False
        self.stop_evt    = threading.Event()
        self.retry_count = 0

        # Per‐alias download folder
        download_root = os.path.join(os.getcwd(), "downloads", alias)
        os.makedirs(download_root, exist_ok=True)
        self.download_dir = download_root

        # Chrome with custom profile & download dir
        opts = webdriver.ChromeOptions()
        opts.add_argument(f"--user-data-dir={profile_dir}")
        opts.add_argument("--start-maximized")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_experimental_option("prefs", {
            "download.default_directory": download_root,
            "download.prompt_for_download": False,
            "profile.default_content_setting_values.automatic_downloads": 1,
        })

        self.driver = webdriver.Chrome(options=opts)
        # clear cookies/cache
        self.driver.execute_cdp_cmd('Network.clearBrowserCookies', {})
        self.driver.execute_cdp_cmd('Network.clearBrowserCache', {})
        self.driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")

    def _retry(self):
        """Close old tabs, open fresh one, reset login state."""
        old = set(self.driver.window_handles)
        self.driver.execute_script("window.open('about:blank','_blank');")
        new = (set(self.driver.window_handles) - old).pop()
        for h in old:
            try:
                self.driver.switch_to.window(h)
                self.driver.close()
            except:
                pass
        self.driver.switch_to.window(new)
        self.captcha_code = None
        self.logged_in   = False
        self._send(f"🔄 Cycling tabs—retry {self.retry_count}/5…")

    def _send(self, text: str):
        """Send a status message to Telegram."""
        last_active[self.alias] = datetime.now()
        return asyncio.run_coroutine_threadsafe(
            self.bot.send_message(
                chat_id=self.chat_id,
                text=f"[{self.alias}] {text}",
                parse_mode=ParseMode.MARKDOWN,
            ),
            self.loop,
        )

    def _screenshot_tabs(self):
        """Capture & send screenshots of every open tab (IDBI & AutoBank)."""
        for h in self.driver.window_handles:
            try:
                self.driver.switch_to.window(h)
                png = self.driver.get_screenshot_as_png()
                bio = BytesIO(png)
                which = "IDBI" if h == self.idbi_win else "AutoBank"
                coro = self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=bio,
                    caption=f"[{self.alias}] 📸 {which} screenshot",
                )
                asyncio.run_coroutine_threadsafe(coro, self.loop)
            except:
                continue

    def stop(self):
        """
        Gracefully stops the IDBI thread:
          1) signals the loop to exit
          2) quits Chrome
        """
        # 1) set the stop flag so any loops break out
        self.stop_evt.set()

        # 2) send a Telegram notice (optional)
        try:
            self._send("🚪 Stopping IDBI session and closing browser…")
        except Exception:
            pass

        # 3) tear down the browser
        try:
            self.driver.quit()
        except Exception:
            pass
            
    def run(self):
        self._send("🚀 Starting IDBI automation")

        # Outer retry loop
        while not self.stop_evt.is_set():
            try:
                # 1) Login + navigate into the statement page
                self._login()
                self.retry_count = 0
                self._read_balance_and_navigate_to_statement()

                # 2) Steady-state: download & upload every minute
                while not self.stop_evt.is_set():
                    self._download_and_upload_statement()
                    self.retry_count = 0
                    time.sleep(60)

                # clean exit if stop_evt was set
                break

            except Exception as e:
                self.retry_count += 1

                # give up permanently after 5 tries
                if self.retry_count > 5:
                    self._send(f"❌ Failed {self.retry_count} times—stopping alias.")
                    return self.stop()

                # capture screenshots, notify and cycle tabs
                try:    self._screenshot_tabs()
                except: pass
                self._send(f"⚠️ Error: {e!r}\nRetrying {self.retry_count}/5…")
                self._retry()
                # and then loop back to try logging in again

        # final cleanup once loop exits
        self.stop()
  
        
    def _login(self):
        """1) Go to login page, solve CAPTCHA, submit credentials."""
        self.driver.get("https://inet.idbibank.co.in/")
        wait = WebDriverWait(self.driver, 20)

        # (e1) Username
        wait.until(EC.presence_of_element_located((By.ID, "AuthenticationFG.USER_PRINCIPAL")))
        uid = self.cred.get("login_id") or self.cred.get("username")
        self.driver.find_element(By.ID, "AuthenticationFG.USER_PRINCIPAL").send_keys(uid)

        # (e2) CAPTCHA → screenshot → Telegram
        img = wait.until(EC.presence_of_element_located((By.ID, "IMAGECAPTCHA")))
        bio = BytesIO(img.screenshot_as_png)

        self._send("🤖 Trying to auto-solve CAPTCHA via 2Captcha…")
        solution, self._captcha_id = solve_captcha_with_2captcha(bio.getvalue())
        if solution:
            self.captcha_code = solution
            self._send(f"✅ Auto-solved CAPTCHA: `{solution}`")
        else:
            asyncio.run_coroutine_threadsafe(
                self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=bio,
                    caption=f"[{self.alias}] 🔐 Please solve this CAPTCHA",
                ),
                self.loop,
            )
            self._send("⚠️ 2Captcha failed. Waiting for your reply…")
            while not self.captcha_code and not self.stop_evt.is_set():
                time.sleep(0.5)
        if self.stop_evt.is_set():
            return

        # (e3 & e4) Fill CAPTCHA & Continue
        self.driver.find_element(By.ID, "AuthenticationFG.VERIFICATION_CODE")\
            .send_keys(self.captcha_code)
        btns = self.driver.find_elements(By.ID, "STU_VALIDATE_CREDENTIALS")
        cont = next((b for b in btns if b.is_displayed() and b.is_enabled()), None)
        if not cont:
            raise TimeoutException("Continue-to-Login button not found")
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", cont)
        time.sleep(0.2)
        try: cont.click()
        except: self.driver.execute_script("arguments[0].click();", cont)
        # Check for CAPTCHA error (e.g., invalid CAPTCHA warning)
        try:
            wrapper = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "span.errorCodeWrapper"))
            )
            all_p = wrapper.find_elements(By.TAG_NAME, "p")
            for p in all_p:
                if p.value_of_css_property("display") == "inline":
                    if "enter the characters" in p.text.lower() and hasattr(self, "_captcha_id"):
                        self._send("❌ CAPTCHA wrong — reporting to 2Captcha and retrying…")
                        report_bad_captcha(self._captcha_id)
                        self._retry()
                        return
        except TimeoutException:
            pass  # no visible error message → proceed
                
        # (e5) Password & submit
        wait.until(EC.presence_of_element_located((By.ID, "AuthenticationFG.ACCESS_CODE")))
        pwd = self.driver.find_element(By.ID, "AuthenticationFG.ACCESS_CODE")
        pwd.send_keys(self.cred["password"])
        # (e15) — wait for the styled checkbox “◻ I have read & accepted…”
        checkbox_span = WebDriverWait(self.driver, 20).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//input[@id='AuthenticationFG.TARGET_CHECKBOX']/following-sibling::span[contains(@class,'span-checkbox')]"
            ))
        )
        # scroll it into view and click
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", checkbox_span)
        time.sleep(0.2)
        try:
            checkbox_span.click()
        except Exception:
            # fallback to JS click if normal click is blocked
            self.driver.execute_script("arguments[0].click();", checkbox_span)
            
        time.sleep(2)
        pwd.send_keys("\n")  # submits form
        time.sleep(10)
        # 1) Try to send ENTER to dismiss your custom popup
        try:
            ActionChains(self.driver).send_keys(Keys.ENTER).perform()
        except UnexpectedAlertPresentException:
            # if a native alert popped up on enter, it'll be handled below
            pass

        # 2) Handle any native JS alert that might be present
        try:
            WebDriverWait(self.driver, 5).until(EC.alert_is_present())
            alert = self.driver.switch_to.alert
            alert.accept()
        except (TimeoutException, UnexpectedAlertPresentException):
            # no alert or already accepted
            pass

        # 3) Give the page a moment to start loading
        time.sleep(5)

        # 4) Now wait for the dashboard to appear, but catch any late alert
        try:
            WebDriverWait(self.driver, 20).until(
                EC.element_to_be_clickable((By.LINK_TEXT, "A/C Statement"))
            )
        except UnexpectedAlertPresentException:
            # if the alert shows up just as we wait for the link, accept then retry
            try:
                alert = self.driver.switch_to.alert
                alert.accept()
            except:
                pass
            # one more shot at waiting for the link
            WebDriverWait(self.driver, 20).until(
                EC.element_to_be_clickable((By.LINK_TEXT, "A/C Statement"))
            )

        self._send("✅ Logged in to IDBI Netbanking!")
        self.logged_in = True
        self.idbi_win  = self.driver.current_window_handle


    def _read_balance_and_navigate_to_statement(self):
        """2) On dashboard, wait for realign modal → match account_number → scrape balance → click its A/C Statement."""
        acct = self.cred["account_number"]
        self.driver.switch_to.window(self.idbi_win)

        # --- wait for the 'realign' overlay to disappear (if it appears) ---
        try:
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((
                    By.XPATH,
                    "//*[contains(text(),'Please wait while we realign dashboard to best fit in page')]"
                ))
            )
            WebDriverWait(self.driver, 120).until(
                EC.invisibility_of_element_located((
                    By.XPATH,
                    "//*[contains(text(),'Please wait while we realign dashboard to best fit in page')]"
                ))
            )
        except TimeoutException:
            pass  # proceed anyway

        # --- now wait for the row containing our exact account number ---
        account_xpath = (
            f"//span[normalize-space(text())='{acct}']"
            "/ancestor::tr"
        )
        row = WebDriverWait(self.driver, 60).until(
            EC.presence_of_element_located((By.XPATH, account_xpath))
        )
        time.sleep(1)  # let any final JS finish up

        # --- scrape the INR balance from that same <tr> ---
        bal = row.find_element(
            By.XPATH,
            ".//td[contains(normalize-space(.),'INR')]"
        ).text.strip()
        self.last_balance = bal                   
        self._send(f"💰 Balance: {bal}")

        # --- click the A/C Statement link in our row ---
        stmt = row.find_element(By.XPATH, ".//a[@title='A/C Statement']")
        WebDriverWait(self.driver, 20).until(
            EC.element_to_be_clickable((By.XPATH, ".//a[@title='A/C Statement']"))
        )
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", stmt)
        time.sleep(0.2)
        try:
            stmt.click()
        except:
            self.driver.execute_script("arguments[0].click();", stmt)
            
            
    def _download_and_upload_statement(self):
        """3) Wait for statement page, keep-alive, fill dates, click VIEW, then download XLS & upload."""
        # 3a) Wait up to 5 min for the “From” field to appear
        start = time.time()
        frm = None
        while time.time() - start < 300:
            try:
                frm = self.driver.find_element(By.NAME, "TransactionHistoryFG.FROM_TXN_DATE")
                break
            except:
                if time.time() - start > 60:
                    try:
                        self.driver.find_element(By.ID, "span_HREF_Notifications").click()
                    except:
                        pass
                time.sleep(5)
        if not frm:
            raise TimeoutException("Statement page did not load in time")

        # 3b) Compute and fill dates (as before)…
        now = datetime.now()
        fr_dt = now - timedelta(days=1) if now.hour < 5 else now
        to_dt = now
        fr_s, to_s = fr_dt.strftime("%d/%m/%Y"), to_dt.strftime("%d/%m/%Y")
        self.driver.execute_script("arguments[0].removeAttribute('readonly')", frm)
        frm.clear(); frm.send_keys(fr_s)
        to_elem = self.driver.find_element(By.NAME, "TransactionHistoryFG.TO_TXN_DATE")
        self.driver.execute_script("arguments[0].removeAttribute('readonly')", to_elem)
        to_elem.clear(); to_elem.send_keys(to_s)

        # 3c) Robust click of VIEW STATEMENT
        view_loc = (By.NAME, "Action.SEARCH")
        WebDriverWait(self.driver, 30).until(EC.element_to_be_clickable(view_loc))
        for _ in range(3):
            try:
                btn = self.driver.find_element(*view_loc)
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                btn.click()
                break
            except StaleElementReferenceException:
                time.sleep(1)
        else:
            raise TimeoutException("Could not click VIEW STATEMENT")
        time.sleep(5)
        # 3d) Wait for “Download:” to appear
        WebDriverWait(self.driver, 120).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "span.downloadtext"))
        )

        # 3e) Click “Download as XLS” specifically
        # wait for the Download label to show up
        WebDriverWait(self.driver, 120).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "span.downloadtext"))
        )

        # now locate the XLS-only button (e2)
        xls_btn = WebDriverWait(self.driver, 60).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//input[@name='Action.GENERATE_REPORT' and contains(@onclick,'setOutformat(4')]"
            ))
        )

        # scroll into view and click (with JS fallback)
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", xls_btn)
        time.sleep(0.2)
        try:
            xls_btn.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", xls_btn)


        # 3f) Wait for the file to land & upload as before…
        end = time.time() + 60
        xls_file = None
        while time.time() < end:
            files = [f for f in os.listdir(self.download_dir)
                     if not f.lower().endswith(('.tmp', '.crdownload'))]
            if files:
                xls_file = max(files, key=lambda f: os.path.getctime(os.path.join(self.download_dir, f)))
                full = os.path.join(self.download_dir, xls_file)
                if os.path.exists(full):
                    break
            time.sleep(1)
        if not xls_file:
            raise TimeoutException("Timed out waiting for XLS download")

        self._upload_to_autobank(full)
        
    def _upload_to_autobank(self, statement_path):
        driver = self.driver
        wait = WebDriverWait(driver, 20)
        original = driver.current_window_handle

        # 1) Open a new tab for AutoBank
        driver.execute_script("window.open();")
        new_tab = [h for h in driver.window_handles if h != original][-1]
        driver.switch_to.window(new_tab)

        max_attempts = 5
        for attempt in range(1, max_attempts+1):
            try:
                # a) Go to login page
                driver.get("https://autobank.payatom.in/operator_index.php")
                # b) Click SIGN IN if it’s there
                try:
                    login_btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "a.auth-form-btn"))
                    )
                    login_btn.click()
                except TimeoutException:
                    pass  # already logged in

                # c) Wait for the sidebar to confirm we’re in
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "nav.sidebar")))

                # d) Navigate to upload form
                driver.get("https://autobank.payatom.in/bankupload.php")
                wait.until(EC.presence_of_element_located((By.ID, "drop-zone")))

                # e) Select IDBI and attach the XLS
                Select(driver.find_element(By.ID, "bank")) \
                    .select_by_visible_text("IDBI")
                acct = driver.find_element(By.ID, "account_number")
                acct.clear()
                acct.send_keys(self.cred["account_number"])
                driver.find_element(By.ID, "file_input").send_keys(statement_path)

                # f) Wait for success
                wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".swal2-icon-success")))
                self._send(f"✅ AutoBank upload succeeded (attempt {attempt}/{max_attempts})")
                self._send(f"[DEBUG] XLS path: {statement_path})")
                break

            except Exception as e:
                # on error, screenshot & retry
                try:    self._screenshot_tabs()
                except: pass
                self._send(f"⚠️ AutoBank upload failed (attempt {attempt}/{max_attempts}): {e!r}")
                if attempt == max_attempts:
                    driver.close()
                    driver.switch_to.window(original)
                    raise
                time.sleep(2)

        # cleanup: close upload tab, back to main window
        if driver.current_window_handle != original:
            driver.close()
            driver.switch_to.window(original)

class IDFCWorker(threading.Thread):
    """
    Automates IDFC Bank:
      1) Login with OTP (via Telegram)
      2) Scrape Net Withdrawal balance & send to Telegram
      3) Download account statement (Excel) via date-picker every minute
      4) Upload to AutoBank as “IDFC”
      5) On error: screenshot all tabs, retry up to 5× (cycle tabs + relogin)
    """
    def __init__(self, bot, chat_id, alias, cred, loop, profile_dir):
        super().__init__(daemon=True)
        self.bot        = bot
        self.chat_id    = chat_id
        self.alias      = alias
        self.cred       = cred            # expects keys: username, password, account_number
        self.loop       = loop
        self.profile    = profile_dir
        self.otp_code   = None            # same injection logic as captcha
        self.logged_in  = False
        self.stop_evt   = threading.Event()
        self.retry_count= 0

        # per‐alias download folder
        download_root = os.path.join(os.getcwd(), "downloads", alias)
        os.makedirs(download_root, exist_ok=True)
        opts = webdriver.ChromeOptions()
        opts.add_argument(f"--user-data-dir={profile_dir}")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_experimental_option("prefs", {
            "download.default_directory": download_root,
            "download.prompt_for_download": False,
            "profile.default_content_setting_values.automatic_downloads": 1,
        })
        self.driver = webdriver.Chrome(options=opts)
        # clear cache/cookies
        self.driver.execute_cdp_cmd('Network.clearBrowserCookies', {})
        self.driver.execute_cdp_cmd('Network.clearBrowserCache', {})

    def _send(self, text: str):
        """Send status back to Telegram."""
        last_active[self.alias] = datetime.now()
        return asyncio.run_coroutine_threadsafe(
            self.bot.send_message(chat_id=self.chat_id, text=f"[{self.alias}] {text}"),
            self.loop,
        )

    def _retry(self):
        """On error: screenshot current tabs, then cycle tabs, reset state and notify."""
        # 1) screenshot all existing tabs before we close them
        for handle in list(self.driver.window_handles):
            try:
                self.driver.switch_to.window(handle)
                png = self.driver.get_screenshot_as_png()
                bio = BytesIO(png)
                which = "IDFC" if "idfc" in self.driver.current_url else "AutoBank"
                coro = self.bot.send_photo(
                    chat_id=self.chat_id,
                    photo=bio,
                    caption=f"[{self.alias}] 📸 {which} screenshot"
                )
                asyncio.run_coroutine_threadsafe(coro, self.loop)
            except:
                pass

        # 2) now cycle to a fresh blank tab
        old = set(self.driver.window_handles)
        self.driver.execute_script("window.open('about:blank');")
        new_tab = (set(self.driver.window_handles) - old).pop()

        # 3) close all the old tabs
        for h in old:
            try:
                self.driver.switch_to.window(h)
                self.driver.close()
            except:
                pass

        # 4) switch into the new blank tab and reset state
        self.driver.switch_to.window(new_tab)
        self.otp_code   = None
        self.logged_in  = False
        self.retry_count += 1
        self._send(f"⚠️ Error—retry {self.retry_count}/5…")
        
    def _login(self):
        wait = WebDriverWait(self.driver, 30)
        # e1–e2: username → Proceed
        self.driver.get("https://my.idfcfirstbank.com/login")
        wait.until(EC.presence_of_element_located((By.NAME, "customerUserName")))
        self.driver.find_element(By.NAME, "customerUserName")\
            .send_keys(self.cred["username"])
        self.driver.find_element(By.CSS_SELECTOR,
            "[data-testid='submit-button-id']").click()

        # e3–e4: password → Login Securely
        wait.until(EC.presence_of_element_located((By.ID, "login-password-input")))
        self.driver.find_element(By.ID, "login-password-input")\
            .send_keys(self.cred["password"])
        self.driver.find_element(By.CSS_SELECTOR,
            "[data-testid='login-button']").click()

        # e5–e6: get OTP via Telegram
        asyncio.run_coroutine_threadsafe(
            self.bot.send_message(chat_id=self.chat_id,
                text=f"[{self.alias}] 🔐 Enter your 6-digit OTP:"),
            self.loop,
        )
        self._send("Waiting for OTP…")
        start = time.time()
        while self.otp_code is None and not self.stop_evt.is_set():
            if time.time() - start > 300:  # 5-min expiry
                raise TimeoutException("OTP expired—restarting login")
            time.sleep(0.5)
        if self.stop_evt.is_set():
            return
        # inject OTP and submit
        self.driver.find_element(By.NAME, "otp").send_keys(self.otp_code)
        self.driver.find_element(By.CSS_SELECTOR,
            "[data-testid='verify-otp']").click()

        # ← NEW: wait for the inner span tab, not the <a>
        wait.until(EC.element_to_be_clickable((
            By.CSS_SELECTOR,
            "span[data-testid='Accounts']"
        )))

        self.logged_in = True
        self._send("✅ Logged in to IDFC")

    def _select_date(self, field_id: str, target: date):
        """Open the React datepicker for `field_id` and pick the given `target` day."""
        # 1) Click the readonly input to open the calendar widget
        inp = self.driver.find_element(By.ID, field_id)
        inp.click()

        # 2) Wait for the datepicker header (month/year dropdowns) to appear
        header = WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.CLASS_NAME, "react-datepicker__header"))
        )

        # 3) Grab the two <select> elements (month then year)
        selects = header.find_elements(By.TAG_NAME, "select")
        if len(selects) < 2:
            raise NoSuchElementException("Could not locate month/year selectors in datepicker")

        month_dropdown, year_dropdown = Select(selects[0]), Select(selects[1])
        # month_dropdown is zero-based: 0 = January
        month_dropdown.select_by_index(target.month - 1)
        # year_dropdown options are the full year strings
        year_dropdown.select_by_visible_text(str(target.year))

        # 4) Click the correct day cell (only days in the current month)
        day_xpath = (
            f"//div[contains(@class,'react-datepicker__day') "
            f"and not(contains(@class,'--outside-month')) and text()='{target.day}']"
        )
        WebDriverWait(self.driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, day_xpath))
        ).click()

    def _scrape_and_upload(self):
        wait = WebDriverWait(self.driver, 20)

        # e7: click Accounts
        self.driver.find_element(By.CSS_SELECTOR,
            "span[data-testid='Accounts']").click()

        # e8: scrape Net withdrawal balance
        bal = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "[data-testid='AccountEffectiveBalance-amount']"))
        ).text.strip()
        self._send(f"💰 {bal}")
        time.sleep(0.5)
        # e9–e14: download Excel via datepicker
        self.driver.find_element(By.CSS_SELECTOR,
            "[data-testid='download-statement-link']").click()
        time.sleep(0.5)
        # e10: choose Custom by clicking the label
        wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "label[for='AccountStatementDate-4']"))
        ).click()
        time.sleep(0.5)

        # select From = yesterday, To = today
        # select dates based on current time (Asia/Kolkata)
        now = datetime.now()  
        if now.hour < 5:
            frm_date = now.date() - timedelta(days=1)  # yesterday
            to_date  = now.date()                      # today
        else:
            frm_date = now.date()                      # today
            to_date  = now.date()                      # today

        self._select_date("custom-from-date", frm_date)
        time.sleep(5)
        self._select_date("custom-to-date", to_date)
        time.sleep(5)
        # ▶️ open the custom dropdown
        self.driver.find_element(By.ID, "select-account-statement-format").click()
        # ▶️ wait for and click the “Excel” option
        wait.until(EC.element_to_be_clickable((
            By.XPATH,
            "//ul[@id='select-account-statement-format-list']//span[text()='Excel']"
        ))).click()
        # ▶️ now click Download
        self.driver.find_element(By.CSS_SELECTOR, "[data-testid='PrimaryAction']").click()
        time.sleep(5)
        # wait for download to finish (same logic as KGBWorker)
        dl_dir, timeout = os.path.join(os.getcwd(),"downloads",self.alias), 60
        end = time.time() + timeout
        stmt = None
        while time.time()<end:
            files = [f for f in os.listdir(dl_dir)
                     if not f.endswith(('.tmp','.crdownload'))]
            if files:
                stmt = max(files, key=lambda f: os.path.getctime(os.path.join(dl_dir,f)))
                break
            time.sleep(1)
        if not stmt:
            raise TimeoutException("Statement download timed out")

        # upload to AutoBank
        self._upload_to_autobank(os.path.join(dl_dir, stmt))

        # e15: close the statement page
        self.driver.find_element(By.CSS_SELECTOR, "[aria-label='Cross']").click()
    
    def _upload_to_autobank(self, xls_path):
        original_handle = self.driver.current_window_handle
        # 1) open new tab
        self.driver.execute_script("window.open();")
        new_tab = [h for h in self.driver.window_handles if h != original_handle][0]
        self.driver.switch_to.window(new_tab)

        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                # go to AutoBank
                self.driver.get("https://autobank.payatom.in/operator_index.php")

                # sign-in if needed
                try:
                    sign_in = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "a.auth-form-btn"))
                    )
                    sign_in.click()
                except TimeoutException:
                    pass

                # navigate to upload page
                WebDriverWait(self.driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "nav.sidebar"))
                )
                self.driver.get("https://autobank.payatom.in/bankupload.php")
                WebDriverWait(self.driver, 20).until(
                    EC.presence_of_element_located((By.ID, "drop-zone"))
                )

                # select IDFC, fill account & attach file
                Select(self.driver.find_element(By.ID, "bank")) \
                    .select_by_visible_text("IDFC")
                acct = self.driver.find_element(By.ID, "account_number")
                acct.clear()
                acct.send_keys(self.cred["account_number"])
                self.driver.find_element(By.ID, "file_input")\
                    .send_keys(xls_path)

                # wait for success icon
                WebDriverWait(self.driver, 120).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".swal2-icon-success"))
                )

                self._send(f"✅ AutoBank upload succeeded (attempt {attempt}/{max_attempts})")
                self._send(f"[DEBUG] XLS path: {xls_path})")
                break

            except Exception as e:
                # on failure: screenshot + notify
                try:
                    self._screenshot_tabs()
                except:
                    pass
                self._send(f"⚠️ AutoBank upload failed (attempt {attempt}/{max_attempts}): {e!r}")
                if attempt < max_attempts:
                    continue
                else:
                    # give up & bubble up to full retry cycle
                    self.driver.close()
                    self.driver.switch_to.window(original_handle)
                    raise

        # cleanup: close upload tab, return to bank tab
        self.driver.close()
        self.driver.switch_to.window(original_handle)

        
    def run(self):
        self._send("🚀 Starting IDFC automation")
        while not self.stop_evt.is_set():
            try:
                self._login()
                self.retry_count = 0
                # steady-state loop
                while not self.stop_evt.is_set():
                    self._scrape_and_upload()
                    time.sleep(60)
                break
            except Exception as e:
                if self.retry_count >= 5:
                    self._send(f"❌ Failed {self.retry_count} times—stopping.")
                    break
                self._send(f"⚠️ {e!r}")
                self._retry()
        # cleanup
        try: self.driver.quit()
        except: pass

    def stop(self):
        self.stop_evt.set()
        try:
            self._send("🚪 Logging out...")
            # no explicit logout URL, so just quit
        except: pass
        try: self.driver.quit()
        except: pass

# extend your pool logic to accept both types:
_WORKER_CLASSES = {
    'tmb':    TMBWorker,      # TMB flow
    'iob':    IOBWorker,      # new personal IOB flow
    'iobcorp': IOBWorker,     # same class but will pick corporate branch
    'kgb':    KGBWorker,      # Kerala Gramin Bank flow
    'idbi':   IDBIWorker,     # new IDBI flow
    'idfc': IDFCWorker,       # IDFC bank flow
}
# --- Telegram bot management ---

workers = {}
creds = load_credentials()

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

async def on_startup(app: Application) -> None:
    await app.bot.delete_webhook(drop_pending_updates=True)

    # ensure base download directory exists
    os.makedirs(_download_base, exist_ok=True)

    # Launch one Chrome window per profile dir, each with its own download folder
    for profile in _free_profiles:
        # a) create a download folder named for this profile
        prof_name = os.path.basename(profile)
        download_folder = os.path.join(_download_base, prof_name)
        os.makedirs(download_folder, exist_ok=True)
        _profile_downloads[profile] = download_folder

        # b) start Chrome with that profile
        opts = webdriver.ChromeOptions()
        opts.add_argument(f"--user-data-dir={profile}")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        # ─── (B) ─── Give Chrome a unique download.default_directory ───
        prefs = {
            "download.default_directory": download_folder,
            "download.prompt_for_download": False,
            "profile.default_content_setting_values.automatic_downloads": 1,
        }
        opts.add_experimental_option("prefs", prefs)
        driver = webdriver.Chrome(options=opts)

        # c) instruct Chrome to dump all downloads into our profile folder
        driver.execute_cdp_cmd(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": download_folder}
        )
        driver.get("https://autobank.payatom.in/bankupload.php")
        # track driver and its state
        _drivers[profile] = driver
        _active[profile] = False

        # prompt user to log in
        await app.bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=(
                f"🔐 Please log in to AutoBank now in Chrome profile:\n"
                f"`{profile}`\n"
                f"Downloads will go to `{download_folder}`"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

    # 2) Start a background thread to refresh inactive profiles every 10m
    def _keep_alive_loop():
        while True:
            for prof, drv in _drivers.items():
                if not _active[prof]:
                    print(f"[KeepAlive] refreshing profile {prof}") 
                    try:
                        drv.switch_to.window(drv.window_handles[0])
                        drv.refresh()
                    except Exception:
                        pass
            time.sleep(600)
    threading.Thread(target=_keep_alive_loop, daemon=True).start()

    # 3) (Optional) your existing “restarted” message below…

    """
    This function runs immediately after `Application` has been built,
    but before it starts polling.  We use it to send our “bot restarted” message.
    """
    await app.bot.delete_webhook(drop_pending_updates=True)
    
    await app.bot.send_message(
    chat_id=config.TELEGRAM_CHAT_ID,
    parse_mode=ParseMode.MARKDOWN,
    text="""🎉 *We’ve Moved to the Cloud!*
AutoBot is now running on a faster, more reliable cloud platform.

🤖 *Bot Restarted & All Aliases Stopped*
• All operations have been reset.
• Please restart any aliases you need with `/run <alias>`.

⚡ *What’s New?*
• Faster responses
• Rock-solid stability
• Zero-downtime automatic updates

🔄 *2Captcha Auto-Solving is Live*🤖
• Image captchas auto-solved via 2Captcha.
• If solving fails, captchas are sent to Telegram for manual input.
• IOB captchas forced to UPPERCASE (6 characters only).
• Incorrect solutions auto-reported for refund.

🏦 *IDFC Bank Integration: Fully Operational*
Run `/run <alias>_idfc` to start.

🚨 *IDBI Services are CURRENTLY INOP* 🚨
We’re working round-the-clock to restore access ASAP.

Thank you for your patience and continued trust!"""
)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [ InlineKeyboardButton("❓ Need more help?", callback_data="more_help") ]
    ]
    short_text = (
        "🔷 *PayAtom Bot Quick Help* 🔷\n\n"
        "⚙️ *Core*:\n"
        "`/start`       • Welcome message\n"
        "`/help`        • This quick list\n\n"
        "-----------------------------------\n"
        "💾 *Alias Mgmt*:\n"
        "`/list`        • Show saved aliases\n"
        "`/add`         • Add an alias\n"
        "   • Usage: `/add alias,login_id,user_id,password,account_number`\n\n"
        "🚀 *Sessions*:\n"
        "`/run <alias>`   • Start automation\n"
        "`/stop <alias>`  • Stop automation\n"
        "`/stopall`       • Stop all sessions\n"
        "`/running`       • List running aliases\n"
        "`/active`        • Alive in last 3 min\n\n"
        "💰 *Balances & Reports*:\n"
        "`/balance`       • Show current balances\n"
        "`/report`        • Generate transaction report\n\n"
        "📂 *Fetch once*:\n"
        "`/file <alias>`  • Download last statement\n\n"
        "📸 *Diagnostics*:\n"
        "`/status <alias>` • Capture screenshots for an alias\n\n"
        "🔧 *Maintenance*:\n"
        "`/restart`       • Restart the bot\n\n"
        "⚠️ Tap below for full details, examples & limitations."
    )
    await update.message.reply_text(
        short_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def detailed_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()  # removes the “loading…” icon

    detailed = """
🔷 *PayAtom Automation Bot — Full Help* 🔷

👋 *Welcome!* This bot automates:
  • Balance checks  
  • Statement downloads (TMB, IOB personal/corp, KGB, IDBI)  
  • Upload to AutoBank  

────────────────────────
⚙️ *Core Commands*

• `/start`  
   – Sends a welcome & quick usage tip.

• `/help`  
   – Quick overview, with a “Need more help?” button.

────────────────────────
💾 *Alias Management*

• `/list`  
   – Lists all saved aliases.

• `/add alias,login_id,user_id,password,account_number`  
   – *IOB corporate example:*  
     `/add mycorp,LOGINID123,USERID456,passw0rd,1234567890`

• `/add alias,username,password,account_number`  
   – *TMB/KGB example:*  
     `/add mytmb,user123,pass123,0987654321`

────────────────────────
🚀 *Session Control*

• `/run <alias>`  
   – Start a background thread to login every minute, download & upload.  
   – E.g. `/run alice_tmb`

• `/stop <alias>`  
   – Gracefully log out and quit that alias’s browser.

• `/stopall`  
   – Stops _all_ running aliases at once.

• `/running`  
   – Shows which aliases are currently _spawned_.

• `/active`  
   – Shows which aliases have reported activity in the _last 3 minutes_.

────────────────────────
💰 *Balances & Reports*

• `/balance`  
   – Shows the most recent balance for each running alias.

• `/report`  
   – Scrapes and reports “Total Received” for each MID from your dashboard.

────────────────────────
📂 *One-off Fetch*

• `/file <alias>`  
   – Logs in once, grabs the latest statement & sends it to you.

────────────────────────
📸 *Diagnostics*

• `/status <alias>`  
   – Captures and sends screenshots of all open tabs for that alias.

────────────────────────
🔧 *Maintenance*

• `/restart`  
   – Prompts for confirmation & PIN, then restarts the bot process.

────────────────────────
🏦 *Bank-specific Notes & Examples*

1. **TMB** (`alias` ends in `_tmb`):  
   • Uses `https://www.tmbnet.in/` flows, selects “XLS” & uploads as `TMB`.  
   • Example alias: `alice_tmb`

2. **IOB personal** (`alias` ends in `_iob`):  
   • Logs in at `iobnet.co.in`, CSV export & uploads as `IOB`.  
   • Example: `bob_iob`

3. **IOB corporate** (`alias` ends in `_iobcorp`):  
   • Same as personal but with 5-field `/add`:  
     `alias,login_id,user_id,password,account_number`

4. **Kerala Gramin Bank** (`alias` ends in `_kgb`):  
   • Netbanking at `keralagbank.com`, XLS export & uploads as `Kerala Gramin Bank`.  
   • Example: `charlie_kgb`

5. **IDBI** (`alias` ends in `_idbi`):  
   • Netbanking at `inet.idbibank.co.in`, XLS export & uploads as `IDBI`.  
   • Example: `dave_idbi`

────────────────────────
⚠️ *Limitations*

• Max **10** concurrent sessions (Chrome profiles).  
• Captcha must be solved _manually_ in the Telegram group.  
• If session expires or errors > 5 times, that alias stops.  
• Statement window is last 24 h (IOB) or current view (others).  
• _No_ support for other banks yet.

If you hit any edge–case, drop a message here in the dev chat.
"""
    await update.callback_query.message.reply_text(
        detailed,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Payatom automation bot.\nUse /list to see aliases."
    )


async def list_aliases(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Display all configured aliases in alphabetical order,
    using HTML <code> tags for monospace formatting.
    """
    # Grab and sort
    aliases = sorted(creds.keys())
    if not aliases:
        await update.message.reply_text(
            "ℹ️ <b>No aliases are configured.</b>",
            parse_mode=ParseMode.HTML
        )
        return

    # Build the list with proper escaping
    lines = [
        f"• <code>{html.escape(alias)}</code>"
        for alias in aliases
    ]
    text = (
        "📝 <b>Available Aliases:</b>\n"
        + "\n".join(lines)
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

async def run_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /run <alias>")

    alias = context.args[0]
    # ── if it’s a KGB alias, offer Default vs Custom ──
    if alias.endswith("_kgb"):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Default", callback_data=f"kgb|{alias}|default"),
            InlineKeyboardButton("Custom",  callback_data=f"kgb|{alias}|custom"),
        ]])
        return await update.message.reply_text(
            "📅 KGB: use your usual dates or pick custom?",
            reply_markup=keyboard,
        )

    if alias not in creds:
        return await update.message.reply_text(f"❌ Unknown alias “{alias}”.")
    # pick the appropriate worker based on suffix
    for suffix, cls in _WORKER_CLASSES.items():
        if alias.endswith(f"_{suffix}"):
            WorkerClass = cls
            break
    else:
        return await update.message.reply_text(
            "❌ Alias must end in _tmb, _iob, _iobcorp, _idbi, _idfc or _kgb"
        )

    if alias in _profile_assignments:
        return await update.message.reply_text(f"❌ Already running “{alias}”.")
    if not _free_profiles:
        return await update.message.reply_text(
            "❌ Maximum of 10 concurrent sessions reached."
        )

    # Reserve a profile and grab its Chrome driver
    profile = _free_profiles.pop(0)
    _profile_assignments[alias] = profile
    driver = _drivers[profile]
    _active[profile] = True

    # Collapse to a single AutoBank tab
    main_handle = driver.window_handles[0]
    for handle in driver.window_handles[1:]:
        driver.switch_to.window(handle)
        driver.close()
    driver.switch_to.window(main_handle)

    # Use the profile’s dedicated download folder set at startup
    download_folder = _profile_downloads[profile]

    # Spawn the worker with the existing driver and download folder
    loop = asyncio.get_running_loop()
    worker = WorkerClass(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        alias=alias,
        cred=creds[alias],
        loop=loop,
        driver=driver,
        download_folder=download_folder,
        profile_dir=profile,
    )
    workers[alias] = worker
    worker.start()

    await update.message.reply_text(
        f"Started *{alias}* using profile `{os.path.basename(profile)}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"[KGB TEXT ENTRY] hit handle_text_message, pending_kgb global is {pending_kgb}")    
    user_id = update.effective_user.id
    text    = update.message.text.strip()

    # ―― 1) KGB custom‐date flow ――
    state = pending_kgb.get(user_id)
    if state:
        # try parsing dd/mm/YYYY or dd/mm/yy
        def parse_date(s: str):
            for fmt in ("%d/%m/%Y", "%d/%m/%y"):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    pass
            return None
        logger.info(f"[KGB] raw input for FROM‐date: {text!r}")
        dt = parse_date(text)
        if not dt:
            return await update.message.reply_text(
                "❌ Invalid format. Use dd/mm/yyyy or dd/mm/yy."
            )

        if state["stage"] == "from":
            logger.info(f"[KGB] 📅 Got FROM‐date {text!r}, parsed → {dt!r}")
            state["from_dt"] = dt
            state["stage"]   = "to"
            return await update.message.reply_text(
                "✏️ Now enter *TO* date (dd/mm/yyyy or dd/mm/yy):",
                parse_mode=ParseMode.MARKDOWN,
            )

        # stage == "to": fire off the worker
        alias   = state["alias"]
        from_dt = state["from_dt"]
        to_dt   = dt
        del pending_kgb[user_id]
        return await run_kgb(update, context, alias, from_dt, to_dt)

    # ―― 2) Only *after* we’ve given KGB a chance, catch random slashes ――
    if "/" in text:
        return await update.message.reply_text(
            "❌ No KGB operation pending. Please `/run <alias>` and tap *Custom* first.",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ―― 0) OTP input for any worker awaiting otp_code (IDFC, etc.)
    for w in workers.values():
        if hasattr(w, "otp_code") and w.otp_code is None and not w.logged_in:
            w.otp_code = text
            await update.message.reply_text(
                f"🔐 Got OTP `{text}`, continuing…",
                parse_mode=ParseMode.MARKDOWN
            )
            return

    # ―― 1) PIN for /restart
    if pending_restarts.get(user_id):
        if text == "2580":
            await update.message.reply_text("✅ Correct PIN. Restarting now…")
            await asyncio.sleep(1)
            for alias, worker in list(workers.items()):
                try: worker.stop()
                except: pass
            workers.clear()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            await update.message.reply_text("❌ Incorrect PIN. Restart cancelled.")
        pending_restarts.pop(user_id, None)
        return

    # ―― 2) CAPTCHA input for all running workers still logging in
    for w in workers.values():
        if hasattr(w, "captcha_code") and w.captcha_code is None and not w.logged_in:
            w.captcha_code = text
            await update.message.reply_text(
                f"🖼️ Got captcha `{text}`, continuing…",
                parse_mode=ParseMode.MARKDOWN
            )
            return

async def handle_captcha_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Catch any text reply (TMB or IOB alphanumeric captchas).
    """
    text = update.message.text.strip()
    for w in workers.values():
        # only inject if worker is waiting for a captcha
        if not w.logged_in:
            # TMBWorker has .captcha_code
            if hasattr(w, "captcha_code") and w.captcha_code is None:
                w.captcha_code = text
                await update.message.reply_text(
                    f"Got captcha `{text}`; continuing…",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            # IOBWorker now also has .captcha_code
            # (we unified it above)
            if hasattr(w, "captcha_code") and w.captcha_code is None:
                w.captcha_code = text
                await update.message.reply_text(
                    f"Got captcha `{text}`; continuing…",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

async def status_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /status <alias>")

    alias = context.args[0]
    worker = workers.get(alias)
    if not worker:
        return await update.message.reply_text(f"❌ `{alias}` is not currently running.", parse_mode=ParseMode.MARKDOWN)

    try:
        if hasattr(worker, "_send_screenshots"):
            worker._send_screenshots()
            await update.message.reply_text(f"📸 Capturing screenshots for `{alias}`…", parse_mode=ParseMode.MARKDOWN)
        elif hasattr(worker, "_screenshot_tabs"):
            worker._screenshot_tabs()
            await update.message.reply_text(f"📸 Capturing screenshots for `{alias}`…", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"❌ Screenshot method not available for `{alias}`.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error while capturing screenshots for `{alias}`: {e}")

async def stop_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Ensure they passed an alias
    if not context.args:
        await update.message.reply_text("Usage: /stop <alias>")
        return

    alias = context.args[0]
    # Remove from our running workers
    worker = workers.pop(alias, None)
    if not worker:
        await update.message.reply_text(f"`{alias}` not running")
        return

    # Release the Chrome profile
    profile = _profile_assignments.pop(alias, None)
    if profile:
        # put it back at the front so it’s the next one reused
        _free_profiles.insert(0, profile)

    # Now actually stop the thread/browser
    try:
        if hasattr(worker, "stop"):
            # TMBWorker.stop() will log out, quit Chrome, and set the flag
            worker.stop()
        else:
            # IOBWorker doesn’t have stop(); just flip its event flag
            worker.stop_evt.set()
            worker.stop()
    except Exception as e:
        logger.error(f"Error stopping {alias}: {e}", exc_info=True)
        await update.message.reply_text(f"⚠️ Error while stopping `{alias}`: {e}")

    await update.message.reply_text(f"✅ Stopped `{alias}`")



async def capture_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Special‐case: catch any pure 6-digit reply to handle both OTPs and TMB captchas
    so they don’t trigger the general text handler twice.
    """
    text = update.message.text.strip()
    if len(text) == 6 and text.isdigit():
        for w in workers.values():
            # 1) If a worker is logged out and waiting for OTP, inject it
            if hasattr(w, "otp_code") and w.otp_code is None and not w.logged_in:
                w.otp_code = text
                await update.message.reply_text(
                    f"🔐 Got OTP `{text}`, continuing…",
                    parse_mode=ParseMode.MARKDOWN
                )
                await update.message.delete()
                return

            # 2) Fallback: TMB/IOB captcha flow (if still unsigned in)
            if hasattr(w, "captcha_code") and w.captcha_code is None and not w.logged_in:
                w.captcha_code = text
                await update.message.reply_text(
                    f"🖼️ Got captcha `{text}`, continuing…",
                    parse_mode=ParseMode.MARKDOWN
                )
                await update.message.delete()
                return
                
async def running(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    List all aliases currently running and which profile they’re on.
    """
    if not workers:
        await update.message.reply_text("No aliases are currently running.")
        return

    lines = []
    for alias in workers:
        profile_dir = _profile_assignments.get(alias, "")
        profile = os.path.basename(profile_dir) if profile_dir else "<unknown>"
        lines.append(f"- `{alias}` on profile `{profile}`")

    await update.message.reply_text(
        "🚥 *Currently running:* \n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN
    )

async def active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    cutoff = now - timedelta(minutes=3)

    if not workers:
        return await update.message.reply_text("No aliases are currently running.")

    lines = []
    for alias in workers:
        last = last_active.get(alias)
        dot = "🟢" if (last and last >= cutoff) else "🔴"
        prof = _profile_assignments.get(alias, "")
        prof_name = os.path.basename(prof) if prof else "<unknown>"
        lines.append(f"{dot} `{alias}` on profile `{prof_name}`")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )

async def balance_all(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Show current balances for all running aliases in
    alphabetical order, with monospace formatting.
    """
    # Sort aliases
    aliases = sorted(workers.keys())
    if not aliases:
        await update.message.reply_text(
            "❌ <b>No aliases are running right now.</b>",
            parse_mode=ParseMode.HTML
        )
        return

    # Build each line: • `alias: balance` or retrieving…
    lines = []
    for alias in aliases:
        worker = workers[alias]
        bal = getattr(worker, "last_balance", None)
        status = bal if bal is not None else "retrieving…"
        # put both alias and status in monospace and escape any HTML
        line = f"• <code>{html.escape(alias)}: {html.escape(str(status))}</code>"
        lines.append(line)

    # Compose and send the reply
    message = "🏦 <b>Current balances:</b>\n" + "\n".join(lines)
    await update.message.reply_text(
        message,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )
    
async def file_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1️⃣ Parse & validate
    alias = context.args[0] if context.args else None
    if not alias:
        return await update.message.reply_text("Usage: /file <alias>")

    if alias not in creds:
        return await update.message.reply_text(f"❌ Unknown alias “{alias}”.")

    # 2️⃣ If already running, just grab latest download
    if alias in _profile_assignments:
        # instead of "downloads/…", build the full ~/autobot/downloads path
        dl_dir = os.path.expanduser(f"~/autobot/downloads/{alias}")
        files = glob.glob(os.path.join(dl_dir, "*"))
        if not files:
            return await update.message.reply_text(f"No files for `{alias}` yet.")
        latest = max(files, key=os.path.getctime)
        with open(latest, "rb") as fp:
            return await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=fp,
                filename=os.path.basename(latest),
            )


    # 3️⃣ Not running → tell them what to do
    # Turn that redundant second `if alias not in _profile_assignments:` into an else
    # Also: prefix the string with f so {alias} actually gets interpolated
    return await update.message.reply_text(
        f"❌ Alias `{alias}` is not running. Please run /run {alias} to start fetching."
    )




async def restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data="confirm_restart"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_restart")
        ]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "⚠️ Are you sure you want to restart the bot?",
        reply_markup=markup
    )
    
pending_restarts = {}  # alias → user_id for whom PIN is expected

async def handle_restart_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    await query.answer()

    if query.data == "confirm_restart":
        pending_restarts[user_id] = True
        await query.edit_message_text("🔐 Please enter the 4-digit PIN to confirm restart:")
    elif query.data == "cancel_restart":
        await query.edit_message_text("❎ Restart cancelled.")

async def handle_restart_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if pending_restarts.get(user_id) and text == "2580":
        await update.message.reply_text("✅ Correct PIN. Restarting now…")
        await asyncio.sleep(1)

        for alias, worker in list(workers.items()):
            try:
                worker.stop()
            except:
                pass
        workers.clear()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    elif pending_restarts.get(user_id):
        await update.message.reply_text("❌ Incorrect PIN. Restart cancelled.")
        pending_restarts.pop(user_id, None)

async def stop_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Stop *all* running aliases.  First send each through its logout routine,
    wait 10 seconds, then forcibly stop/quit each one and release their profiles.
    """
    if not workers:
        return await update.message.reply_text("Nothing to stop — no aliases are running.")

    # take snapshot
    to_stop = list(workers.items())   # list of (alias, worker)
    # clear our tables so new /run can reuse profiles immediately
    workers.clear()
    for alias, _ in to_stop:
        _profile_assignments.pop(alias, None)

    # 1) ask each worker to log themselves out
    for alias, worker in to_stop:
        try:
            # if they've got a dedicated logout method, use it
            if hasattr(worker, "_logout"):
                worker._logout()
            else:
                # fallback: stop() includes logout+quit
                worker.stop()
        except Exception as e:
            logger.error(f"Error during logout of {alias}: {e}", exc_info=True)

    # 2) wait a bit for those logout navigations to finish
    await asyncio.sleep(10)

    # 3) now force‐stop every worker (quit leftover browsers, set stop flags)
    for alias, worker in to_stop:
        try:
            worker.stop()
        except Exception:
            # ignore if already quit
            pass

    # 4) put all freed profiles back at the front so they’re used next
    #    (we popped them above; PROFILE_DIRS order remains)
    for _, worker in to_stop:
        profile = getattr(worker, "profile_dir", None) \
               or getattr(worker, "profile", None)
        if profile:
            _free_profiles.insert(0, profile)

    await update.message.reply_text("✅ All aliases have been logged out and stopped.")

    
def main():
    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(detailed_help_callback, pattern="^more_help$"))
    app.add_handler(CommandHandler("list", list_aliases))
    app.add_handler(CommandHandler("run", run_alias))
    app.add_handler(CommandHandler("running", running))
    app.add_handler(CommandHandler("active", active))    # ← add this line
    app.add_handler(CommandHandler("stopall", stop_all))
    app.add_handler(CommandHandler("balance", balance_all))
    app.add_handler(CommandHandler("file", file_alias))
    app.add_handler(CommandHandler("status", status_alias))
    app.add_handler(CommandHandler("restart", restart_bot))
    app.add_handler(CallbackQueryHandler(handle_restart_decision, pattern="^confirm_restart|cancel_restart$"))
    app.add_handler(CallbackQueryHandler(kgb_button, pattern=r"^kgb\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(CommandHandler("add", add_alias))
    app.add_handler(CommandHandler("stop", stop_alias))      
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^\d{6}$") & filters.Chat(config.TELEGRAM_CHAT_ID),
            capture_captcha,
        )
    )
    app.run_polling()


if __name__ == "__main__":
    main()
