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

import io
import os
import platform
import re
import subprocess
import sys
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

FILE_EXT_PATTERN = re.compile(
    r"\.(pdf|jpe?g|png|gif|webp|bmp|svg|docx?|xlsx?|pptx?|mp4|mov|zip)(\?|$)",
    re.IGNORECASE,
)
URL_IN_TEXT_PATTERN = re.compile(r'https?://[^\s"\'<>\\]+')

BROWSER_COOKIE_LOADERS = {
    "Chrome": browser_cookie3.chrome,
    "Firefox": browser_cookie3.firefox,
    "Edge": browser_cookie3.edge,
    "Brave": browser_cookie3.brave,
}


# ---------------------------------------------------------------------------
# Session / auth
# ---------------------------------------------------------------------------

def build_session(browser_name: str, domain: str) -> requests.Session:
    loader = BROWSER_COOKIE_LOADERS[browser_name]
    cookiejar = loader(domain_name=domain)  # reads cookies straight from local browser storage
    session = requests.Session()
    session.cookies.update(cookiejar)
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        }
    )
    return session


def looks_logged_out(html: str) -> bool:
    lowered = html.lower()
    signals = ["login", "log in", "sign in", "sso", "session expired", "unauthorized"]
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


def extract_attachments(html: str, base_url: str):
    soup = BeautifulSoup(html, "html.parser")
    found = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if FILE_EXT_PATTERN.search(href):
            abs_url = urljoin(base_url, href)
            found[abs_url] = {
                "url": abs_url,
                "label": guess_label(a) or a.get_text(strip=True) or "file",
            }

    for img in soup.find_all("img", src=True):
        src = img["src"]
        abs_url = urljoin(base_url, src)
        if abs_url not in found:
            found[abs_url] = {"url": abs_url, "label": guess_label(img) or "image"}

    for script in soup.find_all("script"):
        text = script.string or ""
        if not text:
            continue
        for m in URL_IN_TEXT_PATTERN.finditer(text):
            url = m.group(0).rstrip('\\",)')
            if FILE_EXT_PATTERN.search(url) and url not in found:
                found[url] = {"url": url, "label": "file"}

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

def download_all(session: requests.Session, files, dest_folder: Path):
    dest_folder.mkdir(parents=True, exist_ok=True)
    results = []
    progress = st.progress(0.0, text="Downloading...")
    for i, f in enumerate(files):
        url = f["url"]
        try:
            resp = session.get(url, timeout=60)
            resp.raise_for_status()
            original_name = sanitize_filename(urlparse(url).path.split("/")[-1] or f"{f['label']}_{i}")
            dest_path = dest_folder / original_name
            # avoid overwriting a different file that happens to share a name
            counter = 1
            while dest_path.exists():
                stem, dot, ext = original_name.rpartition(".")
                dest_path = dest_folder / (f"{stem}_{counter}.{ext}" if dot else f"{original_name}_{counter}")
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
            session = build_session(browser_name, domain)
        except Exception as e:
            st.error(
                f"Couldn't read cookies from {browser_name} ({e}). "
                f"Make sure {browser_name} is installed and you've logged into the panel there at least once."
            )
            st.stop()

        try:
            resp = session.get(panel_url, timeout=30)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            st.error(f"Couldn't fetch the panel page: {e}")
            st.stop()

    if not session.cookies or looks_logged_out(html):
        st.error(
            f"You don't look logged into the panel in {browser_name} right now. "
            f"Please open {browser_name}, log into the panel normally, then click Download again."
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
