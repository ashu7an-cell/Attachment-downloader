"""
Sales Panel Bulk Downloader
----------------------------
Paste a sales panel link, click Download, and every attachment (brochure,
images, etc.) is saved straight into a folder on your laptop.

No Selenium, no browser automation, no manual cookie copying. The app
reads your existing login session directly from your browser's local
cookie storage (the same way the browser itself would) and uses that to
fetch and download - so it only works if you're already logged into the
panel in that browser.

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

def build_session(browser_name: str, panel_url: str, custom_cookie_path: str = "") -> requests.Session:
    loader = BROWSER_COOKIE_LOADERS[browser_name]
    # No domain filter: browser-cookie3's domain filter does a substring match that
    # misses cookies stored against a parent domain (e.g. ".99acres.com" vs
    # "www.99acres.com"). Loading everything is fast and 'requests' only ever
    # sends the cookies that actually match the domain of each request anyway.
    explicit_path = custom_cookie_path.strip() or None

    try:
        cookiejar = loader(cookie_file=explicit_path) if explicit_path else loader()
    except Exception as primary_error:
        # Auto-fallback for Firefox: try known profile locations
        # browser_cookie3 doesn't check (Microsoft Store install, snap, etc.)
        if browser_name == "Firefox" and not explicit_path:
            fallback_path = locate_firefox_cookie_file()
            if fallback_path:
                cookiejar = loader(cookie_file=str(fallback_path))
            else:
                raise RuntimeError(
                    f"{primary_error}. Auto-detection also failed - if you know where your "
                    f"Firefox profile lives, paste the path to its cookies.sqlite file into "
                    f"'Custom cookie file path' in the sidebar."
                ) from primary_error
        else:
            raise

    session = requests.Session()
    session.cookies.update(cookiejar)
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            # Several CDNs / attachment endpoints check Referer and will 403
            # a bare request without it.
            "Referer": panel_url,
        }
    )
    return session


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

with st.sidebar:
    st.header("Settings")
    browser_name = st.selectbox("Browser you're logged into the panel with", list(BROWSER_COOKIE_LOADERS.keys()))
    custom_cookie_path = st.text_input(
        "Custom cookie file path (optional)",
        value="",
        help=(
            "Only needed if auto-detection fails (e.g. Firefox installed via the Microsoft "
            "Store). Point this at the browser's cookies file - for Firefox that's "
            "cookies.sqlite inside your profile folder, e.g. "
            "%APPDATA%\\Mozilla\\Firefox\\Profiles\\xxxx.default-release\\cookies.sqlite"
        ),
    )
    dest_root = st.text_input("Save downloads to", value=str(DEFAULT_DOWNLOAD_ROOT))
    st.caption("A subfolder is created per batch automatically.")

panel_url = st.text_input(
    "Sales panel link",
    placeholder="https://www.99acres.com/opspanel/sales-agent-doc-view-panel?batchId=SALES_6a9e6ecddf034b06fecd73d6",
)

download_clicked = st.button("⬇️ Download all attachments", type="primary", disabled=not panel_url)

if download_clicked:
    domain = urlparse(panel_url).netloc

    with st.spinner("Reading your login and fetching the panel..."):
        try:
            session = build_session(browser_name, panel_url, custom_cookie_path)
        except Exception as e:
            st.error(
                f"Couldn't read cookies from {browser_name} ({e}). "
                f"Make sure {browser_name} is installed and you've logged into the panel there at least once. "
                f"If auto-detection keeps failing, try the 'Custom cookie file path' field in the sidebar, "
                f"or switch to Chrome/Edge/Brave if you're logged into the panel there too."
            )
            st.stop()

        matched_cookies = cookies_for_domain(session, domain)

        try:
            resp = session.get(panel_url, timeout=30, allow_redirects=True)
            html = resp.text
        except Exception as e:
            st.error(f"Couldn't fetch the panel page: {e}")
            st.stop()

    with st.expander("Diagnostics (open this if you get a login error)"):
        st.write(f"Cookies found for `{domain}`: **{len(matched_cookies)}**")
        if matched_cookies:
            st.caption(", ".join(c.name for c in matched_cookies))
        st.write(f"HTTP status: **{resp.status_code}**")
        st.write(f"Final URL after redirects: `{resp.url}`")
        st.write(f"Response length: **{len(html)}** characters")
        st.code(html[:1500])

    if not matched_cookies:
        st.error(
            f"No cookies found for {domain} in {browser_name}. Either you're not logged in there, "
            f"or {browser_name} couldn't be read (see Diagnostics above for what was found). "
            f"Open {browser_name}, confirm the panel loads without asking you to log in, then retry."
        )
        st.stop()

    if looks_logged_out(html, resp.status_code, resp.url):
        st.error(
            "Cookies were found, but the fetched page still looks like a login screen. "
            "Check Diagnostics above — if the response is very short, the panel may render its content "
            "with JavaScript after page load, which a plain request can't see. If so, let me know and "
            "we'll need a different approach (e.g. finding the underlying data API)."
        )
        st.stop()

    attachments = extract_attachments(html, panel_url)

    with st.expander("Debug: raw HTML fetched (first 3000 chars)"):
        st.code(html[:3000])

    if not attachments:
        st.warning(
            "No attachments detected. Open the debug section above, search for the file type you "
            "expect (e.g. .pdf), and share that snippet so detection can be tuned to this panel."
        )
        st.stop()

    st.success(f"Found {len(attachments)} attachment(s). Downloading...")

    batch_id = batch_id_from_url(panel_url)
    dest_folder = Path(dest_root) / sanitize_filename(batch_id)

    start = time.time()
    results = download_all(session, attachments, dest_folder)
    elapsed = time.time() - start

    ok = [r for r in results if r[1] is not None]
    failed = [r for r in results if r[1] is None]

    st.success(f"✅ Downloaded {len(ok)}/{len(results)} file(s) in {elapsed:.1f}s to:\n\n`{dest_folder}`")

    if failed:
        st.warning(f"{len(failed)} file(s) failed:")
        for url, _, err in failed:
            st.caption(f"- {url} — {err}")

    if st.button("📂 Open folder"):
        open_folder(dest_folder)
