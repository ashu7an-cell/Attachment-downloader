import os
import re
import time
import urllib.parse
import concurrent.futures
import requests
import streamlit as st
from playwright.sync_api import sync_playwright

st.set_page_config(page_title="99acres Downloader", page_icon="📥", layout="wide")
st.title("📥 99acres Fast Persistent Downloader")

SESSION_DIR = "./browser_session"

# --- Session State Persistence ---
if "download_dir" not in st.session_state:
    st.session_state.download_dir = "./downloads"
if "panel_url" not in st.session_state:
    st.session_state.panel_url = ""
if "last_processed_url" not in st.session_state:
    st.session_state.last_processed_url = ""

output_folder = st.sidebar.text_input(
    "Local Download Directory", 
    value=st.session_state.download_dir,
    key="download_dir_input"
)
st.session_state.download_dir = output_folder

content_wait_seconds = st.sidebar.number_input(
    "Max seconds to wait for panel content", value=45, min_value=10
)

panel_url = st.text_input(
    "Sales Panel Link (Paste & Press Enter to auto-download):",
    value=st.session_state.panel_url,
    placeholder="https://www.99acres.com/opspanel/sales-agent-doc-view-panel?batchId=...",
    key="panel_url_input"
)
st.session_state.panel_url = panel_url


class BrowserManager:
    """Owns all Playwright objects on a single worker thread."""

    def __init__(self):
        self.pw = None
        self.context = None
        self.page = None

    def open_or_navigate(self, url, session_dir):
        """Launches browser if not running, or navigates if already open."""
        if self.pw is None or self.context is None or self.page is None or self.page.is_closed():
            self.close()
            self.pw = sync_playwright().start()
            self.context = self.pw.chromium.launch_persistent_context(
                user_data_dir=session_dir,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

        self.page.goto(url)
        return self.page.url

    def current_url(self):
        if self.page is not None and not self.page.is_closed():
            return self.page.url
        return None

    def extract(self, timeout_s):
        if self.page is None or self.page.is_closed():
            raise RuntimeError("No open browser page.")

        deadline = time.time() + timeout_s
        media_urls = []
        while time.time() < deadline:
            full_html = self.page.content()

            cdn_pattern = r'https?://[a-zA-Z0-9.-]*imagecdn\.99acres\.com/[^\s"\'<>`\)\(\]\[]+'
            ext_pattern = r'https?://[^\s"\'<>`\)\(\]\[]+\.(?:pdf|mp4|jpg|jpeg|png|docx)[^\s"\'<>`\)\(\]\[]*'

            raw_matches = re.findall(cdn_pattern, full_html, re.IGNORECASE) + \
                          re.findall(ext_pattern, full_html, re.IGNORECASE)

            clean_urls = set()
            for url in raw_matches:
                clean_url = re.sub(r'["\'<>`\)\(\]\[;].*$', '', url)
                if "imagecdn.99acres.com" in clean_url or any(
                    clean_url.lower().endswith(e) for e in ('.pdf', '.mp4', '.jpg', '.png', '.docx')
                ):
                    clean_urls.add(clean_url)

            if clean_urls:
                media_urls = list(clean_urls)
                break
            time.sleep(2)

        return media_urls, self.page.url

    def close(self):
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.pw = None
        self.context = None
        self.page = None


def get_batch_subfolder(url):
    """Extracts batchId from URL to create a unique subfolder."""
    parsed = urllib.parse.urlparse(url)
    query_params = urllib.parse.parse_qs(parsed.query)

    if "batchId" in query_params and query_params["batchId"]:
        batch_id = query_params["batchId"][0]
        safe_batch_id = re.sub(r'[^a-zA-Z0-9_-]', '_', batch_id)
        return f"batch_{safe_batch_id}"

    return f"batch_download_{int(time.time())}"


# --- Persistent single-thread executor + manager ---
if "browser_executor" not in st.session_state:
    st.session_state.browser_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
if "browser_manager" not in st.session_state:
    st.session_state.browser_manager = BrowserManager()

executor = st.session_state.browser_executor
manager = st.session_state.browser_manager


def run(fn, *args, **kwargs):
    return executor.submit(fn, *args, **kwargs).result()


col1, col2 = st.columns(2)
with col1:
    retry_clicked = st.button("🔄 Retry Extraction / Download")
with col2:
    reset_clicked = st.button("🔁 Reset Browser")

status_box = st.empty()
url_box = st.empty()

# Function to run the full navigation + extraction process
def process_and_download(target_url):
    os.makedirs(SESSION_DIR, exist_ok=True)
    try:
        status_box.info("🌐 Opening/Navigating browser window...")
        current_url = run(manager.open_or_navigate, target_url, SESSION_DIR)
        url_box.caption(f"Current browser URL: {current_url}")

        status_box.info("⏳ Scanning the page for attachment links...")
        media_urls, final_url = run(manager.extract, content_wait_seconds)

        if media_urls:
            status_box.success(f"Found {len(media_urls)} attachment link(s)!")
            
            subfolder_name = get_batch_subfolder(final_url)
            target_download_dir = os.path.join(output_folder, subfolder_name)
            os.makedirs(target_download_dir, exist_ok=True)

            st.success(f"Downloading files to `{os.path.abspath(target_download_dir)}`...")
            progress_bar = st.progress(0)
            download_container = st.container()

            for idx, file_url in enumerate(media_urls):
                try:
                    clean_url = file_url.split('?')[0]
                    file_name = os.path.basename(urllib.parse.urlparse(clean_url).path)
                    if not file_name or '.' not in file_name:
                        file_name = f"attachment_{idx+1}.pdf"

                    file_path = os.path.join(target_download_dir, file_name)
                    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                    res = requests.get(file_url, headers=headers, stream=True, timeout=30)

                    if res.status_code == 200:
                        with open(file_path, "wb") as f:
                            for chunk in res.iter_content(chunk_size=8192):
                                f.write(chunk)
                        download_container.write(f"✅ Downloaded: **{file_name}**")
                    else:
                        download_container.write(f"❌ Failed: **{file_name}** (HTTP {res.status_code})")
                except Exception as err:
                    download_container.write(f"⚠️ Error downloading {file_url}: {err}")

                progress_bar.progress((idx + 1) / len(media_urls))

            st.balloons()
        else:
            status_box.warning(
                "No attachment links found within the time limit. If login is required, "
                "please log in inside the popup browser window and click '🔄 Retry Extraction'."
            )
    except Exception as e:
        st.error(f"Error: {e}")


# --- Reset Handler ---
if reset_clicked:
    try:
        run(manager.close)
    except Exception as e:
        st.warning(f"Cleanup warning: {e}")
    st.session_state.last_processed_url = ""
    status_box.info("Browser closed.")

# --- Auto-Trigger Logic on Link Input ---
clean_panel_url = panel_url.strip()
if clean_panel_url and clean_panel_url != st.session_state.last_processed_url:
    st.session_state.last_processed_url = clean_panel_url
    process_and_download(clean_panel_url)

# --- Manual Retry Handler ---
elif retry_clicked and clean_panel_url:
    process_and_download(clean_panel_url)

# Display current browser URL if browser is active
if not reset_clicked:
    try:
        current_url = run(manager.current_url)
        if current_url:
            url_box.caption(f"Current browser URL: {current_url}")
    except Exception:
        pass
