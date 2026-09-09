"""
Sales Panel Bulk Downloader
----------------------------
Paste a sales panel link, click Download, and every attachment (brochure,
images, etc.) is saved straight into a folder on your laptop.

This app can work in two ways:
1. Local mode: Reads cookies directly from your browser (Chrome, Firefox, Edge, Brave)
2. Cloud mode: You provide your cookies manually via a cookie file

Features:
- Automatically detects JavaScript-rendered pages and handles them with Selenium
- Extracts files from HTML, images, data attributes, and script tags
- Supports batch downloads with progress tracking

Run with:
    pip install -r requirements.txt
    streamlit run panel_downloader_app.py
"""

import mimetypes
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import browser_cookie3
import requests
import streamlit as st
from bs4 import BeautifulSoup

st.set_page_config(page_title="Sales Panel Bulk Downloader", layout="centered")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_DOWNLOAD_ROOT = Path.home() / "Downloads" / "SalesPanelAttachments"
PANEL_DOMAIN = "99acres.com"
PANEL_LOGIN_URL = "https://www.99acres.com/opspanel/login"

# Known file extensions - still the strongest signal when present.
FILE_EXT_PATTERN = re.compile(
    r"\.(pdf|jpe?g|png|gif|webp|bmp|svg|docx?|xlsx?|pptx?|mp4|mov|zip)(\?|$)",
    re.IGNORECASE,
)
URL_IN_TEXT_PATTERN = re.compile(r'https?://[^\s"\'<>\\]+')

# Fallback signal: many panels serve files through extension-less API
# endpoints. If a link's path or query string contains one of these words,
# treat it as a likely attachment even without a recognizable extension.
ATTACHMENT_KEYWORD_PATTERN = re.compile(
    r"(download|attachment|brochure|document|media|asset|file|cdn|export)",
    re.IGNORECASE,
)

# JSON-ish "key": "https://..." pairs where the key name suggests a file link,
# e.g. "fileUrl": "https://.../x", "docUrl": "...", "attachmentUrl": "..."
JSON_URL_KEY_PATTERN = re.compile(
    r'["\'](?:file|doc|attachment|brochure|image|img|media)?[uU]rl["\']\s*:\s*["\'](https?://[^"\']+)["\']'
)

BROWSER_COOKIE_LOADERS = {
    "Chrome": browser_cookie3.chrome,
    "Firefox": browser_cookie3.firefox,
    "Edge": browser_cookie3.edge,
    "Brave": browser_cookie3.brave,
}


# ---------------------------------------------------------------------------
# Profile auto-detection (fallback for when browser_cookie3 can't find it)
# ---------------------------------------------------------------------------

def _candidate_firefox_roots():
    """Known Firefox profile-root locations across OS + install methods.

    browser_cookie3 only checks the "normal" install location. It misses
    Firefox installed via the Microsoft Store on Windows (profiles live under
    a sandboxed Packages\\ folder) and Linux snap/flatpak installs.
    """
    system = platform.system()
    home = Path.home()
    candidates = []

    if system == "Windows":
        appdata = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        candidates.append(appdata / "Mozilla" / "Firefox" / "Profiles")

        localappdata = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        packages_dir = localappdata / "Packages"
        if packages_dir.exists():
            for pkg in packages_dir.glob("Mozilla.Firefox_*"):
                candidates.append(pkg / "LocalCache" / "Roaming" / "Mozilla" / "Firefox" / "Profiles")

    elif system == "Darwin":
        candidates.append(home / "Library" / "Application Support" / "Firefox" / "Profiles")

    else:  # Linux and friends
        candidates.append(home / ".mozilla" / "firefox")
        candidates.append(home / "snap" / "firefox" / "common" / ".mozilla" / "firefox")
        candidates.append(home / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox")

    return candidates


def locate_firefox_cookie_file():
    """Search known profile roots for cookies.sqlite, preferring default-release."""
    best = None
    for root in _candidate_firefox_roots():
        if not root.exists():
            continue
        matches = list(root.glob("*/cookies.sqlite"))
        for path in matches:
            rank = 0 if "default-release" in path.parent.name else 1 if "default" in path.parent.name else 2
            if best is None or rank < best[0]:
                best = (rank, path)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Session / auth
# ---------------------------------------------------------------------------

def try_build_session(browser_name: str, panel_url: str, custom_cookie_path: str = "") -> tuple:
    """Try to build a session and return (session, error_message). If successful, error_message is empty."""
    loader = BROWSER_COOKIE_LOADERS[browser_name]
    explicit_path = custom_cookie_path.strip() or None

    try:
        cookiejar = loader(cookie_file=explicit_path) if explicit_path else loader()
    except Exception as primary_error:
        # Auto-fallback for Firefox: try known profile locations
        if browser_name == "Firefox" and not explicit_path:
            fallback_path = locate_firefox_cookie_file()
            if fallback_path:
                try:
                    cookiejar = loader(cookie_file=str(fallback_path))
                except Exception as fallback_error:
                    return None, f"Firefox cookie detection failed: {fallback_error}"
            else:
                return None, f"{primary_error}. Auto-detection also failed."
        else:
            return None, str(primary_error)

    session = requests.Session()
    session.cookies.update(cookiejar)
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": panel_url,
        }
    )
    return session, ""


def build_session_from_cookies_file(cookies_file) -> tuple:
    """Build session from uploaded cookies.sqlite file (Firefox/Chrome format)."""
    try:
        import sqlite3
        
        # Save uploaded file temporarily
        temp_path = Path("/tmp/cookies_temp.sqlite")
        temp_path.write_bytes(cookies_file.read())
        
        # Load cookies using browser_cookie3 with the temp file
        # Try Firefox first, then Chrome format
        try:
            cookiejar = browser_cookie3.firefox(cookie_file=str(temp_path))
        except:
            try:
                cookiejar = browser_cookie3.chrome(cookie_file=str(temp_path))
            except Exception as e:
                return None, f"Could not read cookies file: {e}"
        
        session = requests.Session()
        session.cookies.update(cookiejar)
        session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            }
        )
        
        # Clean up
        temp_path.unlink(missing_ok=True)
        return session, ""
    except Exception as e:
        return None, f"Error reading cookies file: {e}"


def auto_detect_browser_with_cookies(panel_domain: str) -> str:
    """Try each browser in order and return the first one that has cookies for the panel domain."""
    for browser_name in BROWSER_COOKIE_LOADERS.keys():
        try:
            session, error = try_build_session(browser_name, f"https://{panel_domain}", "")
            if session is None:
                continue
            
            # Check if session has cookies for the panel domain
            matched_cookies = cookies_for_domain(session, panel_domain)
            if matched_cookies:
                return browser_name
        except Exception:
            continue
    
    return None


def cookies_for_domain(session: requests.Session, domain: str):
    """Cookies in the jar whose domain matches (loosely) the target site - for diagnostics only."""
    root = ".".join(domain.split(".")[-2:]) if domain.count(".") >= 1 else domain
    return [c for c in session.cookies if root in c.domain]


def looks_logged_out(html: str, status_code: int, final_url: str) -> bool:
    if status_code in (401, 403):
        return True
    lowered = html.lower()
    signals = ["login", "log in", "sign in", "sso", "session expired", "unauthorized"]
    url_lowered = final_url.lower()
    if any(s.replace(" ", "") in url_lowered for s in ("login", "sso", "signin")):
        return True
    # Weak heuristic: only flag if the page is short AND mentions a login-ish word,
    # since panel pages may legitimately contain the word "login" somewhere in a menu.
    return len(html) < 4000 and any(s in lowered for s in signals)


def fetch_with_selenium(session: requests.Session, url: str) -> tuple:
    """Fetch page with Selenium to render JavaScript. Returns (html, error_message)."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.firefox.options import Options as FirefoxOptions
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, WebDriverException
        
        # Try Chrome first (more common)
        driver = None
        try:
            options = ChromeOptions()
            options.add_argument("--headless")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            driver = webdriver.Chrome(options=options)
        except WebDriverException:
            # Fall back to Firefox if Chrome not available
            try:
                options = FirefoxOptions()
                options.add_argument("--headless")
                driver = webdriver.Firefox(options=options)
            except WebDriverException as e:
                return None, f"Selenium WebDriver not available: {e}. Install chromedriver or geckodriver."
        
        # Add cookies to driver
        driver.get(url)
        for cookie in session.cookies:
            try:
                driver.add_cookie({
                    'name': cookie.name,
                    'value': cookie.value,
                    'domain': cookie.domain,
                    'path': cookie.path or '/',
                })
            except Exception:
                pass  # Skip cookies that can't be added
        
        # Reload page with cookies
        driver.get(url)
        
        # Wait for page to load (up to 10 seconds)
        try:
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            pass  # Continue even if timeout
        
        # Give JavaScript time to render
        time.sleep(2)
        
        html = driver.page_source
        driver.quit()
        
        return html, ""
    except Exception as e:
        return None, f"Selenium rendering failed: {e}"


# ---------------------------------------------------------------------------
# Attachment discovery
# ---------------------------------------------------------------------------

def guess_label(tag) -> str:
    node = tag
    for _ in range(6):
        if node is None:
            break
        node = node.find_previous(["h1", "h2", "h3", "h4", "label", "strong", "span"])
        if node and node.get_text(strip=True):
            text = node.get_text(strip=True)
            if 0 < len(text) < 60:
                return text
    return ""


def _looks_like_attachment(url: str) -> bool:
    if FILE_EXT_PATTERN.search(url):
        return True
    return bool(ATTACHMENT_KEYWORD_PATTERN.search(url))


def extract_attachments(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    found = {}

    def add(url, label):
        if url not in found:
            found[url] = {"url": url, "label": label}

    # 1. Anchor tags - either a known extension, or a URL that otherwise
    #    smells like a download/attachment endpoint.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        abs_url = urljoin(base_url, href)
        if _looks_like_attachment(abs_url):
            add(abs_url, guess_label(a) or a.get_text(strip=True) or "file")

    # 2. Images - always candidates.
    for img in soup.find_all("img", src=True):
        abs_url = urljoin(base_url, img["src"])
        add(abs_url, guess_label(img) or "image")

    # 3. Any element carrying a data-* attribute that itself looks like a
    #    file URL or path (common pattern for JS-driven download buttons).
    for tag in soup.find_all(True):
        for attr_name, attr_val in tag.attrs.items():
            if not attr_name.startswith("data-") or not isinstance(attr_val, str):
                continue
            if attr_val.startswith("http") or attr_val.startswith("/"):
                abs_url = urljoin(base_url, attr_val)
                if _looks_like_attachment(abs_url):
                    add(abs_url, guess_label(tag) or "file")

    # 4. Script tags - scan full text (not just .string, which misses
    #    anything but a single uninterrupted text node), for both bare URLs
    #    and "xUrl": "..." JSON-style key/value pairs.
    for script in soup.find_all("script"):
        text = script.get_text() or ""
        if not text:
            continue
        for m in URL_IN_TEXT_PATTERN.finditer(text):
            url = m.group(0).rstrip('\\",)')
            if _looks_like_attachment(url):
                add(url, "file")
        for m in JSON_URL_KEY_PATTERN.finditer(text):
            url = m.group(1).rstrip('\\",)')
            add(urljoin(base_url, url), "file")

    return list(found.values())


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name or "file"


def batch_id_from_url(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    return qs.get("batchId", ["download"])[0]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _extension_from_response(resp, fallback_url: str) -> str:
    """Best-effort extension: Content-Disposition > Content-Type > URL path."""
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.IGNORECASE)
    if m:
        name = m.group(1)
        if "." in name:
            return "." + name.rsplit(".", 1)[-1]

    ext_match = re.search(r"\.(\w{2,5})(?:\?|$)", urlparse(fallback_url).path)
    if ext_match:
        return "." + ext_match.group(1)

    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    guessed = mimetypes.guess_extension(content_type) if content_type else None
    return guessed or ""


def download_all(session: requests.Session, files, dest_folder: Path):
    dest_folder.mkdir(parents=True, exist_ok=True)
    results = []
    progress = st.progress(0.0, text="Downloading...")
    for i, f in enumerate(files):
        url = f["url"]
        try:
            resp = session.get(url, timeout=60)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "").lower()
            if "text/html" in content_type and not url.lower().endswith((".html", ".htm")):
                # We asked for a file and got an HTML page back - almost
                # always means the session got logged out mid-run, or this
                # particular link needs a different auth path.
                raise ValueError("received an HTML page instead of a file (likely a login/redirect page)")

            base_name = urlparse(url).path.split("/")[-1] or f["label"] or f"file_{i}"
            base_name = sanitize_filename(base_name)
            if "." not in base_name:
                base_name += _extension_from_response(resp, url)

            dest_path = dest_folder / base_name
            counter = 1
            while dest_path.exists():
                stem, dot, ext = base_name.rpartition(".")
                dest_path = dest_folder / (f"{stem}_{counter}.{ext}" if dot else f"{base_name}_{counter}")
                counter += 1

            dest_path.write_bytes(resp.content)
            results.append((url, dest_path, None))
        except Exception as e:
            results.append((url, None, str(e)))
        progress.progress((i + 1) / len(files), text=f"Downloading... ({i+1}/{len(files)})")
    progress.empty()
    return results


def open_folder(path: Path):
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("📦 Sales Panel Bulk Downloader")
st.caption("Paste the panel link. Files are saved straight to a folder on your laptop.")

# Initialize session state
if "auto_detected_browser" not in st.session_state:
    st.session_state.auto_detected_browser = None
if "last_dest_root" not in st.session_state:
    st.session_state.last_dest_root = str(DEFAULT_DOWNLOAD_ROOT)

with st.sidebar:
    st.header("⚙️ Settings")
    
    # Check if running in cloud/remote environment
    is_remote = "/mount/src/" in os.getcwd() or "streamlit" in os.getcwd()
    
    if is_remote:
        st.info("🌐 **Cloud Mode** - Upload your cookies file")
        st.write("Since this is running on a server, you need to provide cookies manually:")
        
        # Option 1: Upload cookies.sqlite file
        uploaded_cookies = st.file_uploader(
            "📁 Upload cookies.sqlite from your browser",
            type=["sqlite", "db"],
            help="For Firefox: %APPDATA%\\Mozilla\\Firefox\\Profiles\\[profile]\\cookies.sqlite\nFor Chrome: %APPDATA%\\..\\Local\\Google\\Chrome\\User Data\\Default\\Cookies"
        )
        
        if uploaded_cookies:
            session_obj = None
            error_msg = ""
        else:
            session_obj = None
            error_msg = "Please upload your cookies.sqlite file"
    else:
        st.info("💻 **Local Mode** - Using your browser cookies")
        
        # Auto-detect browser with 99acres cookies
        if st.session_state.auto_detected_browser is None:
            st.info("🔍 Auto-detecting browser with 99acres login...")
            auto_detected = auto_detect_browser_with_cookies(PANEL_DOMAIN)
            st.session_state.auto_detected_browser = auto_detected or False  # False means tried and failed
        
        if st.session_state.auto_detected_browser:
            st.success(f"✅ Found active login in **{st.session_state.auto_detected_browser}**")
            browser_name = st.session_state.auto_detected_browser
            st.caption("Auto-detected - change below if needed")
            browser_name = st.selectbox(
                "Browser",
                list(BROWSER_COOKIE_LOADERS.keys()),
                index=list(BROWSER_COOKIE_LOADERS.keys()).index(browser_name),
            )
        else:
            st.warning("⚠️ No active 99acres login found in any browser")
            st.info("**How to fix:**")
            st.write("1. Open Chrome, Firefox, Edge, or Brave")
            st.write(f"2. Visit {PANEL_LOGIN_URL}")
            st.write("3. Log in with your credentials")
            st.write("4. Return here and try again")
            browser_name = st.selectbox(
                "Select browser manually",
                list(BROWSER_COOKIE_LOADERS.keys()),
            )
        
        uploaded_cookies = None
    
    custom_cookie_path = st.text_input(
        "Custom cookie file path (optional)",
        value="",
        help="For Firefox: %APPDATA%\\Mozilla\\Firefox\\Profiles\\xxxx.default-release\\cookies.sqlite",
    )
    
    dest_root = st.text_input(
        "Save downloads to",
        value=st.session_state.last_dest_root,
        help="Files will be organized in subfolders by batch.",
    )
    st.session_state.last_dest_root = dest_root

panel_url = st.text_input(
    "Sales panel link",
    placeholder="https://www.99acres.com/opspanel/sales-agent-doc-view-panel?batchId=SALES_6a9e6ecddf034b06fecd73d6",
)

download_clicked = st.button("⬇️ Download all attachments", type="primary", disabled=not panel_url)

if download_clicked:
    domain = urlparse(panel_url).netloc
    is_remote = "/mount/src/" in os.getcwd() or "streamlit" in os.getcwd()

    with st.spinner("Reading your login and fetching the panel..."):
        try:
            if is_remote and uploaded_cookies:
                # Cloud mode: use uploaded cookies file
                session, error = build_session_from_cookies_file(uploaded_cookies)
                if session is None:
                    raise RuntimeError(error)
            else:
                # Local mode: use browser cookies
                session, error = try_build_session(browser_name, panel_url, custom_cookie_path)
                if session is None:
                    raise RuntimeError(error)
        except Exception as e:
            st.error(
                f"❌ **Couldn't read cookies**: {e}\n\n"
                f"**Try this:**\n"
                f"1. Make sure your cookies file is valid\n"
                f"2. Visit {PANEL_LOGIN_URL} and log in\n"
                f"3. Upload your cookies.sqlite file"
            )
            st.stop()

        matched_cookies = cookies_for_domain(session, domain)

        try:
            resp = session.get(panel_url, timeout=30, allow_redirects=True)
            html = resp.text
        except Exception as e:
            st.error(f"❌ Couldn't fetch the panel page: {e}")
            st.stop()

    with st.expander("🔧 Diagnostics (if you get an error)"):
        st.write(f"**Cookies found for `{domain}`:** {len(matched_cookies)}")
        if matched_cookies:
            st.caption(", ".join(c.name for c in matched_cookies))
        st.write(f"**HTTP status:** {resp.status_code}")
        st.write(f"**Final URL:** `{resp.url}`")
        st.write(f"**Response length:** {len(html)} characters")
        st.code(html[:1500])

    if not matched_cookies:
        st.error(
            f"❌ **No login cookies found for {domain}**\n\n"
            f"**Fix:** Make sure your cookies.sqlite file contains 99acres.com login cookies"
        )
        st.stop()

    if looks_logged_out(html, resp.status_code, resp.url):
        st.info("🤖 **Page appears to be dynamically rendered with JavaScript. Attempting to load with Selenium...**")
        
        html, selenium_error = fetch_with_selenium(session, panel_url)
        
        if html is None:
            st.warning(
                f"⚠️ **Page uses JavaScript to render content**\n\n"
                f"Selenium error: {selenium_error}\n\n"
                f"**To fix:**\n"
                f"1. If running locally, install chromedriver: https://chromedriver.chromium.org/\n"
                f"2. If on Streamlit Cloud, JavaScript rendering is not yet available\n"
                f"3. Check Diagnostics section for the raw HTML to debug further"
            )
            st.stop()
        else:
            st.success("✅ Page loaded with Selenium successfully!")

    attachments = extract_attachments(html, panel_url)

    with st.expander("🔍 Debug: Raw HTML (first 3000 chars)"):
        st.code(html[:3000])

    if not attachments:
        st.warning(
            "❌ **No attachments detected on this page**\n\n"
            "Check the Debug section above and look for file URLs you expect to see."
        )
        st.stop()

    st.success(f"✅ Found {len(attachments)} attachment(s). Downloading...")

    batch_id = batch_id_from_url(panel_url)
    dest_folder = Path(dest_root) / sanitize_filename(batch_id)

    start = time.time()
    results = download_all(session, attachments, dest_folder)
    elapsed = time.time() - start

    ok = [r for r in results if r[1] is not None]
    failed = [r for r in results if r[1] is None]

    st.success(f"✅ Downloaded **{len(ok)}/{len(results)}** file(s) in **{elapsed:.1f}s**")
    st.code(str(dest_folder), language="plaintext")

    if failed:
        st.warning(f"⚠️ {len(failed)} file(s) failed:")
        for url, _, err in failed:
            st.caption(f"- {url}\n  - {err}")

    # Auto-open folder
    st.info("📂 Opening folder...")
    open_folder(dest_folder)
    st.success("✅ Folder opened! Check your file explorer.")
