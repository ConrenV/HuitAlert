# -*- coding: utf-8 -*-
import os
import sys
import time
import json
import sqlite3
import re
import html
import logging
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote, urlencode, urlunparse
from pathlib import Path

import requests
import feedparser
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# Cấu hình UTF-8 cho Windows Console
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / '.env'
load_dotenv(dotenv_path=env_path)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FB_COOKIE = os.getenv("FB_COOKIE", "").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 45))
PORT = int(os.getenv("PORT", 8080))
DB_PATH = str(BASE_DIR / 'huit_bot.db')

# ==============================================================================
# CẤU HÌNH CLOUD PERSISTENCE (CHỐNG MẤT DỮ LIỆU KHI RENDER RESTART/REDEPLOY)
# ==============================================================================
# Cách 1: Dùng GitHub Gist (Chỉ cần điền GITHUB_TOKEN và GIST_ID trên Render)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GIST_ID = os.getenv("GIST_ID", "").strip()
GIST_FILENAME = "huit_bot_cloud_db.json"

# Cách 2: Dùng Cloud PostgreSQL như Neon.tech / Supabase (Điền DATABASE_URL)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Tùy chọn: ID Telegram của Admin để dùng lệnh /announce (Nếu để trống thì ai dùng bot cũng gửi được)
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()

# ==============================================================================
# CẤU HÌNH PHIÊN BẢN & THÔNG BÁO CẬP NHẬT TỰ ĐỘNG (NHƯ HÌNH MẪU)
# Mỗi lần bạn nâng số APP_VERSION (VD: "1.1" -> "1.2") và sửa VERSION_CHANGELOG,
# Bot sẽ tự động gửi thông báo cập nhật cho toàn bộ người dùng đúng 1 lần!
# ==============================================================================
APP_VERSION = "1.1"
VERSION_CHANGELOG = [
    "LƯU TRỮ ĐÁM MÂY (CLOUD BACKUP) — KHÔNG BAO GIỜ MẤT DỮ LIỆU KHI RENDER CẬP NHẬT CODE",
    "CHẾ ĐỘ THÔNG BÁO TẤT CẢ BÀI VIẾT MỚI KẾT HỢP PHÂN LOẠI ĐIỂM RÈN LUYỆN RIÊNG BIỆT",
    "QUÉT SÂU 10 BÀI GẦN NHẤT & CHỐNG CACHE RSS — VƯỢT QUA MỌI BÀI GHIM TRÊN FANPAGE",
    "LỆNH /latest LẤY NGAY BÀI ĐĂNG MỚI NHẤT CỦA TẤT CẢ CÁC TRANG ĐANG THEO DÕI",
    "TÍNH NĂNG TỰ ĐỘNG THÔNG BÁO KHI BOT CÓ BẢN CẬP NHẬT MỚI (/changelog, /announce)",
]

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("❌ LỖI: Thiếu TELEGRAM_TOKEN hoặc GEMINI_API_KEY trong file .env hoặc Environment Variables!")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

ai_client = genai.Client(api_key=GEMINI_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


# ==============================================================================
# 1. UPTIMEROBOT & RENDER KEEP-ALIVE SERVER (v1.1)
# ==============================================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"🟢 HUIT DRL Bot v{APP_VERSION} is running 24/7!".encode("utf-8"))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        return

def start_keep_alive_server():
    try:
        server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logging.info(f"🌐 Keep-Alive Server (v{APP_VERSION}) đang lắng nghe tại port {PORT}")
    except Exception as e:
        logging.error(f"Không thể khởi động Web Server trên port {PORT}: {e}")


# ==============================================================================
# 2. HỆ THỐNG DATABASE HYBRID + ĐỒNG BỘ CLOUD TỰ ĐỘNG (GIST / POSTGRESQL / SQLITE)
# ==============================================================================
USE_POSTGRES = False
if DATABASE_URL:
    try:
        import psycopg2
        USE_POSTGRES = True
        logging.info("☁️ Đã kích hoạt chế độ lưu trữ Cloud PostgreSQL (DATABASE_URL)!")
    except ImportError:
        logging.warning("⚠️ Có DATABASE_URL nhưng chưa cài psycopg2-binary, chuyển về SQLite + Gist Cloud.")

_gist_sync_lock = threading.Lock()
_last_gist_sync_time = 0.0

def get_sqlite_connection():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def db_execute(query: str, params: tuple = (), fetch: str = None, commit: bool = False):
    """Hàm thực thi SQL tương thích cả SQLite lẫn Cloud PostgreSQL."""
    if USE_POSTGRES:
        pg_query = query.replace("?", "%s")
        if "INSERT OR IGNORE INTO" in pg_query:
            pg_query = pg_query.replace("INSERT OR IGNORE INTO", "INSERT INTO") + " ON CONFLICT DO NOTHING"
        conn = psycopg2.connect(DATABASE_URL)
        try:
            with conn.cursor() as c:
                c.execute(pg_query, params)
                res = None
                if fetch == "one":
                    res = c.fetchone()
                elif fetch == "all":
                    res = c.fetchall()
                if commit:
                    conn.commit()
                return res
        finally:
            conn.close()
    else:
        with get_sqlite_connection() as conn:
            c = conn.cursor()
            c.execute(query, params)
            res = None
            if fetch == "one":
                res = c.fetchone()
            elif fetch == "all":
                res = c.fetchall()
            if commit:
                conn.commit()
            return res

def init_db():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                         (id SERIAL PRIMARY KEY, chat_id BIGINT, feed_url TEXT, page_name TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS seen_posts
                         (post_id TEXT PRIMARY KEY, page_name TEXT, summary TEXT, post_link TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS user_settings
                         (chat_id BIGINT PRIMARY KEY, only_drl INTEGER DEFAULT 0)''')
            c.execute('''CREATE TABLE IF NOT EXISTS saved_posts
                         (id SERIAL PRIMARY KEY, chat_id BIGINT, page_name TEXT, summary TEXT, post_link TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS bot_metadata
                         (meta_key TEXT PRIMARY KEY, meta_value TEXT)''')
            conn.commit()
        conn.close()
    else:
        with get_sqlite_connection() as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, feed_url TEXT, page_name TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS seen_posts
                         (post_id TEXT PRIMARY KEY, page_name TEXT, summary TEXT, post_link TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS user_settings
                         (chat_id INTEGER PRIMARY KEY, only_drl INTEGER DEFAULT 0)''')
            c.execute('''CREATE TABLE IF NOT EXISTS saved_posts
                         (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, page_name TEXT, summary TEXT, post_link TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS bot_metadata
                         (meta_key TEXT PRIMARY KEY, meta_value TEXT)''')
            conn.commit()

def restore_db_from_gist():
    """Tải dữ liệu từ GitHub Gist về SQLite ngay khi Render vừa khởi động lại."""
    if USE_POSTGRES or not (GITHUB_TOKEN and GIST_ID):
        if not USE_POSTGRES:
            logging.info("ℹ️ Chưa cấu hình GITHUB_TOKEN/GIST_ID hoặc DATABASE_URL -> Đang chạy SQLite cục bộ.")
        return
    try:
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        resp = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers, timeout=12)
        if resp.status_code != 200:
            logging.error(f"⚠️ Không thể đọc Gist Backup (HTTP {resp.status_code}). Kiểm tra lại GITHUB_TOKEN & GIST_ID.")
            return

        files = resp.json().get("files", {})
        raw_content = None
        if GIST_FILENAME in files:
            raw_content = files[GIST_FILENAME].get("content", "{}")
        elif files:
            first_file = next(iter(files.values()))
            raw_content = first_file.get("content", "{}")

        if not raw_content:
            return
        data = json.loads(raw_content)
        if not isinstance(data, dict) or not data:
            logging.info("☁️ Gist Cloud hiện đang trống, sẽ tự động đẩy dữ liệu mới lên khi có thay đổi.")
            return

        with get_sqlite_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions")
            for row in data.get("subscriptions", []):
                c.execute("INSERT INTO subscriptions (id, chat_id, feed_url, page_name) VALUES (?, ?, ?, ?)", tuple(row))

            c.execute("DELETE FROM user_settings")
            for row in data.get("user_settings", []):
                c.execute("INSERT INTO user_settings (chat_id, only_drl) VALUES (?, ?)", tuple(row))

            c.execute("DELETE FROM saved_posts")
            for row in data.get("saved_posts", []):
                c.execute("INSERT INTO saved_posts (id, chat_id, page_name, summary, post_link) VALUES (?, ?, ?, ?, ?)", tuple(row))

            c.execute("DELETE FROM seen_posts")
            for row in data.get("seen_posts", []):
                c.execute("INSERT OR IGNORE INTO seen_posts (post_id, page_name, summary, post_link) VALUES (?, ?, ?, ?)", tuple(row))

            c.execute("DELETE FROM bot_metadata")
            for row in data.get("bot_metadata", []):
                c.execute("INSERT OR REPLACE INTO bot_metadata (meta_key, meta_value) VALUES (?, ?)", tuple(row))

            conn.commit()
        logging.info(
            f"✅ Đã khôi phục dữ liệu thành công từ GitHub Gist Cloud! "
            f"({len(data.get('subscriptions', []))} trang theo dõi, {len(data.get('saved_posts', []))} mục đã lưu)"
        )
    except Exception as e:
        logging.error(f"Lỗi khôi phục dữ liệu từ Gist: {e}")

def backup_db_to_gist(force: bool = False):
    """Đẩy toàn bộ dữ liệu SQLite lên GitHub Gist để bảo toàn vĩnh viễn."""
    global _last_gist_sync_time
    if USE_POSTGRES or not (GITHUB_TOKEN and GIST_ID):
        return
    now = time.time()
    if not force and (now - _last_gist_sync_time < 8):
        return

    def _sync_worker():
        global _last_gist_sync_time
        with _gist_sync_lock:
            try:
                with get_sqlite_connection() as conn:
                    c = conn.cursor()
                    c.execute("SELECT id, chat_id, feed_url, page_name FROM subscriptions")
                    subs = c.fetchall()
                    c.execute("SELECT chat_id, only_drl FROM user_settings")
                    settings = c.fetchall()
                    c.execute("SELECT id, chat_id, page_name, summary, post_link FROM saved_posts")
                    saved = c.fetchall()
                    c.execute("SELECT post_id, page_name, summary, post_link FROM seen_posts ORDER BY rowid DESC LIMIT 600")
                    seen = c.fetchall()
                    c.execute("SELECT meta_key, meta_value FROM bot_metadata")
                    meta = c.fetchall()

                payload_data = {
                    "updated_at": int(time.time()),
                    "app_version": APP_VERSION,
                    "subscriptions": subs,
                    "user_settings": settings,
                    "saved_posts": saved,
                    "seen_posts": seen,
                    "bot_metadata": meta,
                }
                headers = {
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.v3+json"
                }
                body = {
                    "description": f"HUIT DRL Bot v{APP_VERSION} Cloud Database Backup",
                    "files": {
                        GIST_FILENAME: {
                            "content": json.dumps(payload_data, ensure_ascii=False, indent=2)
                        }
                    }
                }
                resp = requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=headers, json=body, timeout=12)
                if resp.status_code == 200:
                    _last_gist_sync_time = time.time()
                else:
                    logging.warning(f"Gist Backup HTTP {resp.status_code}: {resp.text[:120]}")
            except Exception as e:
                logging.error(f"Gist Sync Error: {e}")

    threading.Thread(target=_sync_worker, daemon=True).start()

def get_meta_value(key: str) -> str:
    row = db_execute("SELECT meta_value FROM bot_metadata WHERE meta_key=?", (key,), fetch="one")
    return row[0] if row else ""

def set_meta_value(key: str, val: str):
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO bot_metadata (meta_key, meta_value) VALUES (%s, %s) "
                "ON CONFLICT (meta_key) DO UPDATE SET meta_value = EXCLUDED.meta_value",
                (key, val)
            )
            conn.commit()
        conn.close()
    else:
        db_execute("INSERT OR REPLACE INTO bot_metadata (meta_key, meta_value) VALUES (?, ?)", (key, val), commit=True)
        backup_db_to_gist(force=True)

def get_all_known_chat_ids() -> list:
    """Lấy danh sách tất cả người dùng từng tương tác để gửi thông báo cập nhật."""
    ids = set()
    for table in ["subscriptions", "user_settings", "saved_posts"]:
        rows = db_execute(f"SELECT DISTINCT chat_id FROM {table}", fetch="all") or []
        for r in rows:
            if r and r[0]:
                ids.add(int(r[0]))
    if ADMIN_CHAT_ID and ADMIN_CHAT_ID.lstrip("-").isdigit():
        ids.add(int(ADMIN_CHAT_ID))
    return list(ids)


# ==============================================================================
# 3. TỐI ƯU CRAWL RSS, CHỐNG CACHE, QUÉT COMMENT & GIẢI MÃ QR TRONG ẢNH
# ==============================================================================
def is_direct_facebook_url(url: str) -> bool:
    u = url.lower()
    return ("facebook.com" in u or "fb.com" in u) and not any(
        r in u for r in ["rss", "xml", "feed", "atom"]
    )

def clean_extracted_url(url: str) -> str:
    if not url:
        return ""
    url = html.unescape(url).strip()
    try:
        parsed = urlparse(url)
        if "facebook.com" in parsed.netloc and parsed.path == "/l.php":
            qs = parse_qs(parsed.query)
            if "u" in qs and qs["u"]:
                url = unquote(qs["u"][0])
    except Exception:
        pass
    return url.rstrip('.,;)]}\'"\\')

def normalize_post_unique_id(entry) -> str:
    raw_id = entry.get('id') or entry.get('guid') or entry.get('link') or entry.get('title', '')
    raw_id = str(raw_id).strip()
    if raw_id.startswith("http"):
        try:
            parsed = urlparse(raw_id)
            qs = parse_qs(parsed.query)
            keep_params = {}
            for k in ["story_fbid", "id", "fbid"]:
                if k in qs:
                    keep_params[k] = qs[k][0]
            new_query = urlencode(keep_params) if keep_params else ""
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', new_query, ''))
        except Exception:
            return raw_id
    return raw_id

def is_useful_external_link(url: str) -> bool:
    u = url.lower()
    if any(ignore in u for ignore in ["facebook.com/photo", "facebook.com/hashtag", "fbcdn.net", "fetchrss.com", "rss.app"]):
        return False
    target_domains = [
        "forms.gle", "docs.google.com/forms", "forms.office.com", "microsoft.com",
        "zalo.me", "bit.ly", "tinyurl.com", "shorturl.at", "cutt.ly", "rebrand.ly",
        "gg.gg", "s.net.vn", "link.huit.edu.vn", "huit.edu.vn", "drive.google.com",
        "canva.com", "docs.google.com/spreadsheets", "docs.google.com/document"
    ]
    return any(d in u for d in target_domains)

def decode_qr_from_image_url(img_url: str) -> list:
    qr_links = []
    if not img_url or not img_url.startswith("http"):
        return qr_links
    try:
        api_url = "https://api.qrserver.com/v1/read-qr-code/"
        resp = requests.get(api_url, params={"fileurl": img_url}, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                for symbol in data[0].get("symbol", []):
                    qr_data = symbol.get("data")
                    if qr_data:
                        urls = re.findall(r'(https?://[^\s<>"\'\]\)]+)', qr_data)
                        for u in urls:
                            qr_links.append(clean_extracted_url(u))
    except Exception as e:
        logging.debug(f"QR Decode skip: {e}")
    return qr_links

def extract_full_entry_data(entry):
    html_parts = []
    if entry.get('title'):
        html_parts.append(str(entry.get('title')))
    if entry.get('description'):
        html_parts.append(str(entry.get('description')))
    if entry.get('summary') and entry.get('summary') != entry.get('description'):
        html_parts.append(str(entry.get('summary')))
    if entry.get('content'):
        for c_item in entry.get('content', []):
            if isinstance(c_item, dict) and c_item.get('value'):
                html_parts.append(str(c_item.get('value')))

    combined_html = "\n".join(html_parts)
    soup = BeautifulSoup(combined_html, "html.parser")

    for br in soup.find_all(["br", "p", "div"]):
        br.append("\n")

    raw_a_links = [a['href'] for a in soup.find_all('a', href=True)]
    images = [
        img['src'] for img in soup.find_all('img', src=True)
        if not img['src'].lower().endswith('.gif') and "emoji" not in img['src'].lower()
    ]

    for media in entry.get('media_content', []) + entry.get('enclosures', []):
        if isinstance(media, dict):
            m_url = media.get('url') or media.get('href')
            if m_url and m_url.startswith("http") and m_url not in images:
                images.append(m_url)

    text = soup.get_text()
    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    regex_urls = re.findall(r'(https?://[^\s<>"\'\]\)]+)', text)

    cleaned_links = []
    for link in raw_a_links + regex_urls:
        clean_l = clean_extracted_url(link)
        if clean_l.startswith("http") and is_useful_external_link(clean_l):
            cleaned_links.append(clean_l)

    return text, list(dict.fromkeys(cleaned_links)), list(dict.fromkeys(images))

def scrape_links_and_comments_sync(post_url: str, existing_images: list):
    found_links = []
    all_images = list(existing_images)

    if post_url:
        urls_to_try = []
        if "www.facebook.com" in post_url:
            urls_to_try.append(post_url.replace("www.facebook.com", "mbasic.facebook.com"))
            urls_to_try.append(post_url.replace("www.facebook.com", "m.facebook.com"))
        elif "facebook.com" in post_url and "mbasic." not in post_url:
            urls_to_try.append(post_url.replace("facebook.com", "mbasic.facebook.com"))
        urls_to_try.append(post_url)

        patterns = [
            r'https?://forms\.gle/[a-zA-Z0-9_-]+',
            r'https?://docs\.google\.com/forms/[^\s"<>\\]+',
            r'https?://forms\.office\.com/[^\s"<>\\]+',
            r'https?://zalo\.me/[a-zA-Z0-9_/-]+',
            r'https?://bit\.ly/[a-zA-Z0-9_-]+',
            r'https?://tinyurl\.com/[a-zA-Z0-9_-]+',
            r'https?://shorturl\.at/[a-zA-Z0-9_-]+',
            r'https?://cutt\.ly/[a-zA-Z0-9_-]+',
            r'https?://l\.facebook\.com/l\.php\?u=[^\s"<>\\]+'
        ]

        req_headers = HEADERS.copy()
        if FB_COOKIE:
            req_headers["Cookie"] = FB_COOKIE

        for target_url in urls_to_try[:2]:
            try:
                resp = requests.get(target_url, headers=req_headers, timeout=7)
                if resp.status_code != 200:
                    continue
                raw_html = html.unescape(resp.text.replace("\\/", "/"))

                for pat in patterns:
                    for m in re.findall(pat, raw_html):
                        cleaned = clean_extracted_url(m)
                        if cleaned.startswith("http") and is_useful_external_link(cleaned):
                            found_links.append(cleaned)

                soup = BeautifulSoup(raw_html, "html.parser")
                for img in soup.find_all("img", src=True):
                    src = img["src"]
                    if "scontent" in src and src not in all_images:
                        all_images.append(src)

                if found_links:
                    break
            except Exception:
                pass

    for img_url in all_images[:4]:
        qr_urls = decode_qr_from_image_url(img_url)
        for q_url in qr_urls:
            if q_url.startswith("http"):
                found_links.append(q_url)

    return list(dict.fromkeys(found_links)), all_images[:4]

def fetch_feed_sync(feed_url: str):
    try:
        sep = "&" if "?" in feed_url else "?"
        fresh_url = f"{feed_url}{sep}_cb={int(time.time())}"
        resp = requests.get(fresh_url, headers=HEADERS, timeout=12)
        if resp.status_code == 200 and resp.content:
            parsed = feedparser.parse(resp.content)
            if getattr(parsed, "entries", None):
                return parsed
        resp_orig = requests.get(feed_url, headers=HEADERS, timeout=12)
        return feedparser.parse(resp_orig.content)
    except Exception as e:
        logging.warning(f"Fetch RSS fallback ({feed_url}): {e}")
        return feedparser.parse(feed_url)


# ==============================================================================
# 4. LOGIC PHÂN TÍCH CẢ BÀI THƯỜNG LẪN BÀI ĐIỂM RÈN LUYỆN
# ==============================================================================
def heuristic_drl_check(text: str, links: list) -> bool:
    t = text.lower()
    strong_keywords = [
        "điểm rèn luyện", "diem ren luyen", "đrl", "drl",
        "cộng điểm", "cong diem", "điểm cộng", "diem cong",
        "mục i", "mục ii", "mục iii", "mục iv", "mục v",
        "điều 1", "điều 2", "điều 3", "điều 4", "điều 5",
        "quyền lợi: cộng", "check-in", "checkin",
        "điểm danh", "diem danh", "khảo sát", "khao sat", "quét mã qr"
    ]
    if any(k in t for k in strong_keywords):
        return True

    has_form = any(
        any(f in l.lower() for f in ["forms.gle", "docs.google.com/forms", "forms.office.com", "bit.ly", "tinyurl"])
        for l in links
    )
    activity_keywords = [
        "đăng ký tham gia", "hội thảo", "workshop", "talkshow", "tình nguyện",
        "hiến máu", "cổ vũ", "cuộc thi", "seminar", "tập huấn", "chuyên đề"
    ]
    if has_form and any(ak in t for ak in activity_keywords):
        return True

    return False

def generate_ai_response_with_fallback(contents: list):
    models_to_try = [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-3.0-flash",
    ]
    last_err = None
    for model_name in models_to_try:
        try:
            return ai_client.models.generate_content(model=model_name, contents=contents)
        except Exception as e:
            last_err = e
            continue
    raise last_err

def analyze_post_sync(text: str, links: list, image_urls=None):
    contents = []
    if image_urls:
        for img_url in image_urls[:2]:
            try:
                img_resp = requests.get(img_url, headers=HEADERS, timeout=6)
                if img_resp.status_code == 200 and 'image' in img_resp.headers.get('Content-Type', ''):
                    mime_type = img_resp.headers.get('Content-Type').split(';')[0]
                    contents.append(types.Part.from_bytes(data=img_resp.content, mime_type=mime_type))
            except Exception:
                pass

    display_text = text if text.strip() else "(Bài đăng đính kèm hình ảnh/video, hãy đọc nội dung trên ảnh nếu có)"

    prompt = (
        "Bạn là trợ lý thông báo tin tức & săn Điểm Rèn Luyện (ĐRL) cho sinh viên trường Đại học Công Thương TP.HCM (HUIT).\n"
        f"Nội dung bài đăng:\n\"\"\"{display_text}\"\"\"\n"
        f"Các đường link thu thập được (từ bài viết, QR & Comment): {links}\n\n"
        "NHIỆM VỤ:\n"
        "1. Dòng đầu tiên BẮT BUỘC chỉ ghi [CO_DRL] hoặc [KHONG_DRL].\n"
        "   - Ghi [CO_DRL] nếu bài viết hoặc chữ trên ảnh poster nói về: cộng điểm rèn luyện (ĐRL), cộng điểm Mục I-V, "
        "tuyển tình nguyện viên, hiến máu, hội thảo, workshop, talkshow, cuộc thi học thuật, khảo sát sinh viên, "
        "hoặc sự kiện có link/mã QR đăng ký tham gia.\n"
        "   - Ghi [KHONG_DRL] nếu là thông báo học vụ chung, lịch nghỉ, tin tức, chúc mừng, thông tin CLB hoặc bài viết thường.\n"
        "2. Nếu trên ảnh poster có in đường link rút gọn (như bit.ly/..., forms.gle/...) mà chưa có trong danh sách link, "
        "hãy ghi thêm dòng: [LINK_ANH: https://...] ở dòng thứ 2.\n"
        "3. Tóm tắt rõ ràng, súc tích cho sinh viên theo mẫu sau (KHÔNG dùng dấu * hay định dạng Markdown):\n"
        "   - Nếu là [CO_DRL]:\n"
        "     📌 Hoạt động: (Tên sự kiện/hoạt động)\n"
        "     ⏰ Thời gian / Địa điểm: (Đọc kỹ trong bài và trên ảnh Poster)\n"
        "     🎁 Quyền lợi: (Ghi rõ số ĐRL được cộng, thuộc Mục nào hoặc quyền lợi khác)\n"
        "     ⚠️ Lưu ý: (Yêu cầu chính: điền form, quét QR, vào nhóm Zalo, trang phục...)\n"
        "   - Nếu là [KHONG_DRL] (Bài đăng thông thường):\n"
        "     📌 Chủ đề: (Tiêu đề/Nội dung chính của bài viết)\n"
        "     📝 Tóm tắt: (Tóm tắt ngắn gọn 2-3 ý quan trọng nhất trong bài hoặc trong ảnh)\n"
        "     ℹ️ Phân loại: Tin tức / Thông báo chung (Không đề cập cộng ĐRL)"
    )
    contents.append(prompt)

    is_drl_fallback = heuristic_drl_check(text, links)
    extra_ai_links = []

    try:
        response = generate_ai_response_with_fallback(contents)
        raw_out = (response.text or "").replace("*", "").replace("#", "").strip()

        found_in_img = re.findall(r'\[LINK_ANH:\s*(https?://[^\s\]]+)\]', raw_out)
        for u in found_in_img:
            cleaned_u = clean_extracted_url(u)
            if cleaned_u.startswith("http"):
                extra_ai_links.append(cleaned_u)
        raw_out = re.sub(r'\[LINK_ANH:[^\]]+\]', '', raw_out).strip()

        ai_says_drl = "[CO_DRL]" in raw_out
        final_is_drl = ai_says_drl or is_drl_fallback

        clean_summary = raw_out.replace("[CO_DRL]", "").replace("[KHONG_DRL]", "").strip()
        if not clean_summary:
            clean_summary = f"📌 Nội dung: {display_text[:250]}"
        return final_is_drl, html.escape(clean_summary), extra_ai_links

    except Exception as e:
        logging.error(f"AI Error: {e}")
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        title = lines[0][:100] if lines else "Bài đăng mới từ trang theo dõi"
        short_body = "\n".join(lines[1:4])[:280] if len(lines) > 1 else (text[:280] if text else "Xem chi tiết tại link bài viết gốc bên dưới.")
        reward_text = "Có khả năng cộng ĐRL / Hoạt động sự kiện" if is_drl_fallback else "Thông báo / Bài đăng mới"
        fallback_summary = (
            f"📌 Chủ đề: {title}\n"
            f"📝 Nội dung: {short_body}\n"
            f"🏷️ Phân loại: {reward_text}"
        )
        return is_drl_fallback, html.escape(fallback_summary), []


# ==============================================================================
# 5. ĐỊNH DẠNG THÔNG BÁO CẬP NHẬT GIỐNG HỆT HÌNH MẪU
# ==============================================================================
def build_update_notification_message(items: list = None, custom_text: str = None, version: str = None) -> str:
    """
    Tạo nội dung thông báo cập nhật đúng chuẩn hình mẫu:
    Đã thêm:
    - MỤC 1
    - MỤC 2

    👉 Vào /menu để xem và trải nghiệm ngay nhé!
    """
    if custom_text:
        lines = [l.strip() for l in custom_text.strip().split("\n") if l.strip()]
        if len(lines) == 1 and not lines[0].startswith("-"):
            header_part = f"Đã thêm {html.escape(lines[0])}"
            body_part = ""
        else:
            header_part = "Đã thêm:"
            formatted_lines = [
                f"- {html.escape(l.lstrip('-•* ').strip())}" for l in lines
            ]
            body_part = "\n" + "\n".join(formatted_lines)
        return (
            f"<b>{header_part}</b>{body_part}\n\n"
            f"👉 Vào /menu hoặc /list để xem và sử dụng ngay nhé!"
        )

    changelog_items = items or VERSION_CHANGELOG
    ver_str = f" (Bản cập nhật v{version})" if version else ""
    bullet_lines = "\n".join([f"- {html.escape(item)}" for item in changelog_items])
    return (
        f"<b>Đã thêm{ver_str}:</b>\n"
        f"{bullet_lines}\n\n"
        f"👉 Vào /menu để xem và trải nghiệm ngay nhé!"
    )

async def broadcast_version_update_on_startup(application: Application):
    """Tự động gửi thông báo cập nhật tới toàn bộ người dùng khi nâng APP_VERSION."""
    last_notified = get_meta_value("last_notified_version")
    if last_notified == APP_VERSION:
        return

    chat_ids = get_all_known_chat_ids()
    if not chat_ids:
        # Lưu lại version để khi người dùng đầu tiên bấm /start thì không bị lỗi,
        # nhưng nếu chưa có user nào thì chờ lúc có user hoặc ghi nhận sẵn.
        set_meta_value("last_notified_version", APP_VERSION)
        return

    msg = build_update_notification_message(version=APP_VERSION)
    sent = 0
    for cid in chat_ids:
        try:
            await application.bot.send_message(
                chat_id=cid,
                text=msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            sent += 1
        except Exception as e:
            logging.warning(f"Không thể gửi thông báo update tới {cid}: {e}")

    set_meta_value("last_notified_version", APP_VERSION)
    logging.info(f"📣 Đã gửi thông báo cập nhật v{APP_VERSION} tới {sent}/{len(chat_ids)} người dùng!")


# ==============================================================================
# 6. GIAO DIỆN MENU, NÚT BẤM & LỆNH CLICK TRỰC TIẾP (/menu, /list, /saved...)
# ==============================================================================
async def safe_edit_message(query, text, reply_markup=None):
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logging.error(f"Telegram BadRequest: {e}")
    except Exception as e:
        logging.error(f"Edit message error: {e}")

def get_user_drl_mode(chat_id: int) -> int:
    row = db_execute("SELECT only_drl FROM user_settings WHERE chat_id=?", (chat_id,), fetch="one")
    if row is None:
        db_execute("INSERT OR IGNORE INTO user_settings (chat_id, only_drl) VALUES (?, 0)", (chat_id,), commit=True)
        backup_db_to_gist()
        return 0
    return row[0]

def get_cloud_status_label() -> str:
    if USE_POSTGRES:
        return "☁️ Cloud PostgreSQL (An toàn 100%)"
    if GITHUB_TOKEN and GIST_ID:
        return "☁️ GitHub Gist Cloud (An toàn 100%)"
    return "⚠️ Cục bộ (Cần thêm GITHUB_TOKEN & GIST_ID)"

def get_main_menu_keyboard(chat_id: int):
    current_drl = get_user_drl_mode(chat_id)
    drl_label = (
        "🎯 Chế độ: Chỉ báo bài ĐRL (Bấm để nhận TẤT CẢ)"
        if current_drl == 1
        else "📢 Chế độ: Báo TẤT CẢ bài mới + lọc ĐRL (Bấm để đổi)"
    )

    keyboard = [
        [InlineKeyboardButton("📋 Quản lý trang theo dõi", callback_data="menu_list")],
        [InlineKeyboardButton("📌 Tủ ĐRL & Bài viết đã lưu", callback_data="menu_saved")],
        [InlineKeyboardButton(drl_label, callback_data="menu_toggle_drl")],
        [
            InlineKeyboardButton("⚡ Quét bài mới ngay", callback_data="menu_force_check"),
            InlineKeyboardButton("🆕 Xem bài cuối các trang", callback_data="menu_latest_posts")
        ],
        [
            InlineKeyboardButton("♻️ Cập nhật v" + APP_VERSION, callback_data="menu_changelog"),
            InlineKeyboardButton("📖 Hướng dẫn RSS", callback_data="menu_help")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_welcome_text(chat_id: int = None):
    mode_badge = "📢 <b>NHẬN TẤT CẢ BÀI VIẾT (Có gắn nhãn ĐRL riêng)</b>"
    if chat_id is not None and get_user_drl_mode(chat_id) == 1:
        mode_badge = "🎯 <b>CHỈ NHẬN BÀI CÓ ĐIỂM RÈN LUYỆN</b>"

    return (
        f"🎓 <b>BOT THEO DÕI & SĂN ĐRL HUIT v{APP_VERSION}</b> 🎓\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔔 Trạng thái: {mode_badge}\n"
        f"💾 Dữ liệu: <b>{get_cloud_status_label()}</b>\n\n"
        "⚡ <b>Lệnh bấm nhanh (chạm vào chữ xanh):</b>\n"
        "👉 /list — Xem & quản lý trang theo dõi\n"
        "👉 /add — Thêm nhanh trang RSS mới\n"
        "👉 /scan — Quét các bài mới chưa đọc ngay lập tức\n"
        "👉 /latest — Ép gửi lại bài mới nhất của từng trang\n"
        "👉 /drl — Đổi chế độ: Nhận tất cả bài ↔ Chỉ nhận ĐRL\n"
        "👉 /saved — Mở tủ sự kiện & bài viết đã lưu\n"
        "👉 /changelog — Xem thông báo cập nhật mới nhất\n\n"
        "👇 <b>Hoặc chọn trên bảng điều khiển:</b>"
    )

def build_subscription_list_view(chat_id: int):
    rows = db_execute("SELECT id, page_name, feed_url FROM subscriptions WHERE chat_id=?", (chat_id,), fetch="all") or []

    keyboard = []
    has_fb_warning = False
    if not rows:
        text = (
            "📭 <b>DANH SÁCH TRỐNG</b>\n"
            "Bạn chưa theo dõi trang nào.\n"
            "👉 Bấm vào /add hoặc nút <b>➕ Thêm trang mới</b> bên dưới!"
        )
    else:
        items = []
        for idx, (sub_id, name, url) in enumerate(rows, 1):
            safe_name = html.escape(name)
            safe_url = html.escape(url[:32])
            if is_direct_facebook_url(url):
                has_fb_warning = True
                status_badge = "⚠️ (Link FB trực tiếp - Cần đổi sang RSS)"
            else:
                status_badge = "✅"

            items.append(f"{idx}. <b>{safe_name}</b> {status_badge}\n   🔗 <code>{safe_url}...</code>")
            keyboard.append([
                InlineKeyboardButton(f"✏️ Sửa #{idx}", callback_data=f"edit_name_{sub_id}"),
                InlineKeyboardButton(f"🔗 Link #{idx}", callback_data=f"edit_url_{sub_id}"),
                InlineKeyboardButton(f"🗑️ Xóa #{idx}", callback_data=f"del_sub_{sub_id}")
            ])

        warning_box = (
            "\n\n🚨 <i>CẢNH BÁO: Trang có dấu ⚠️ đang dùng link Facebook trực tiếp nên sẽ bị Facebook chặn. "
            "Hãy tạo link từ FetchRSS.com hoặc RSS.app rồi bấm nút <b>🔗 Link</b> để cập nhật!</i>"
        ) if has_fb_warning else ""
        text = (
            f"📋 <b>DANH SÁCH TRANG ({len(rows)} trang đang theo dõi)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(items)
            + warning_box
            + "\n\n👈 Bấm /add để thêm trang | /menu về trang chủ"
        )

    keyboard.append([InlineKeyboardButton("➕ Thêm trang mới", callback_data="add_sub_prompt")])
    keyboard.append([InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")])
    return text, InlineKeyboardMarkup(keyboard)

def build_saved_posts_view(chat_id: int):
    rows = db_execute(
        "SELECT id, page_name, summary, post_link FROM saved_posts WHERE chat_id=? ORDER BY id DESC LIMIT 10",
        (chat_id,),
        fetch="all"
    ) or []

    keyboard = []
    if not rows:
        text = (
            "📌 <b>TỦ LƯU TRỮ</b>\n"
            "Bạn chưa lưu sự kiện hay bài viết nào.\n\n"
            "👈 Bấm /menu để về bảng điều khiển hoặc /scan để quét bài mới!"
        )
    else:
        items = []
        for idx, (saved_id, page, summary, link) in enumerate(rows, 1):
            safe_page = html.escape(page)
            first_line = html.escape(summary.split("\n")[0]) if summary else "Bài viết đã lưu"
            safe_link = html.escape(link, quote=True)
            items.append(f"<b>{idx}. [{safe_page}]</b> {first_line}\n🔗 <a href=\"{safe_link}\">Mở bài viết gốc</a>")
            keyboard.append([InlineKeyboardButton(f"🗑️ Xóa mục #{idx}", callback_data=f"del_saved_{saved_id}")])
        text = (
            "📌 <b>BÀI VIẾT & SỰ KIỆN ĐÃ LƯU (10 mục gần nhất)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n\n".join(items)
            + "\n\n👈 Bấm /menu để về trang chủ | /list xem danh sách trang"
        )

    keyboard.append([InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")])
    return text, InlineKeyboardMarkup(keyboard)

def get_add_sub_instruction_text():
    return (
        "➕ <b>THÊM TRANG THEO DÕI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Gõ theo định dạng (hỗ trợ gửi nhiều dòng cùng lúc):\n"
        "<code>Tên_Trang Link_RSS</code>\n\n"
        "Ví dụ:\n<code>Đoàn Trường HUIT https://fetchrss.com/rss/...</code>\n\n"
        "👉 Gửi vào khung chat (hoặc bấm /menu để hủy):"
    )

def get_help_text():
    return (
        "📖 <b>HƯỚNG DẪN LẤY LINK RSS & LƯU DỮ LIỆU CLOUD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>1. Cách tạo link RSS theo dõi Fanpage:</b>\n"
        "• Vào <a href='https://fetchrss.com'>fetchrss.com</a> hoặc <a href='https://rss.app'>rss.app</a>, dán link Fanpage Facebook vào để lấy link có đuôi <code>.xml</code> hoặc <code>/rss/...</code> rồi dùng lệnh /add.\n\n"
        "<b>2. Cách chống mất dữ liệu khi Render cập nhật:</b>\n"
        "• Trên Render -> mục <b>Environment</b>, chỉ cần thêm <code>GITHUB_TOKEN</code> (có quyền gist) và <code>GIST_ID</code> (hoặc <code>DATABASE_URL</code> của Neon.tech).\n"
        "• Bot sẽ tự động sao lưu mọi trang theo dõi lên đám mây và tự khôi phục ngay khi khởi động lại!\n\n"
        "<b>3. Lệnh gửi thông báo cập nhật (như hình mẫu):</b>\n"
        "• Gõ <code>/announce Nội dung mới...</code> để phát thông báo tới người dùng.\n\n"
        "👈 Bấm /menu để về lại bảng điều khiển."
    )


# ==============================================================================
# 7. CÁC COMMAND HANDLER CHO PHÉP BẤM TRỰC TIẾP TRONG CHAT
# ==============================================================================
async def start_or_menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    chat_id = update.effective_chat.id
    get_user_drl_mode(chat_id)
    msg = get_welcome_text(chat_id)
    kb = get_main_menu_keyboard(chat_id)
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.HTML)
    elif update.callback_query:
        await update.callback_query.answer()
        await safe_edit_message(update.callback_query, msg, kb)

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    chat_id = update.effective_chat.id
    text, kb = build_subscription_list_view(chat_id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["action"] = "add_sub"
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy", callback_data="menu_list")]])
    await update.message.reply_text(get_add_sub_instruction_text(), reply_markup=cancel_kb, parse_mode=ParseMode.HTML)

async def saved_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    chat_id = update.effective_chat.id
    text, kb = build_saved_posts_view(chat_id)
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    wait_msg = await update.message.reply_text("⚡ Đang quét sâu 10 bài gần nhất của từng trang (vượt qua cả bài ghim)...", parse_mode=ParseMode.HTML)
    sent_count = await run_feed_check_core(context.bot, target_chat_id=chat_id, force_latest=False)
    await wait_msg.edit_text(
        f"✅ <b>Đã hoàn tất quét bài mới!</b> (Phát hiện và gửi <b>{sent_count}</b> bài chưa đọc).\n"
        "💡 <i>Nếu muốn ép bot gửi lại bài mới nhất của các trang (kể cả bài đã đọc rồi), hãy bấm vào</i> /latest.\n\n"
        "👈 Vào /menu hoặc /list để quản lý tiếp nhé!",
        parse_mode=ParseMode.HTML
    )

async def latest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    wait_msg = await update.message.reply_text("🆕 Đang lấy bài đăng mới nhất từ tất cả các trang bạn theo dõi...", parse_mode=ParseMode.HTML)
    sent_count = await run_feed_check_core(context.bot, target_chat_id=chat_id, force_latest=True)
    await wait_msg.edit_text(
        f"✅ <b>Đã gửi {sent_count} bài mới nhất từ các trang của bạn!</b>\n\n"
        "👈 Vào /menu hoặc /list để quản lý tiếp nhé!",
        parse_mode=ParseMode.HTML
    )

async def toggle_drl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    current = get_user_drl_mode(chat_id)
    new_val = 0 if current == 1 else 1
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO user_settings (chat_id, only_drl) VALUES (%s, %s) "
                "ON CONFLICT (chat_id) DO UPDATE SET only_drl = EXCLUDED.only_drl",
                (chat_id, new_val)
            )
            conn.commit()
        conn.close()
    else:
        db_execute("INSERT OR REPLACE INTO user_settings (chat_id, only_drl) VALUES (?, ?)", (chat_id, new_val), commit=True)
        backup_db_to_gist(force=True)

    mode_str = (
        "🎯 <b>CHỈ NHẬN BÀI ĐIỂM RÈN LUYỆN</b>"
        if new_val == 1
        else "📢 <b>NHẬN TẤT CẢ BÀI ĐĂNG MỚI (Có gắn nhãn phân biệt bài ĐRL)</b>"
    )
    kb = get_main_menu_keyboard(chat_id)
    await update.message.reply_text(
        f"🔄 Đã chuyển sang chế độ: {mode_str}\n\n{get_welcome_text(chat_id)}",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )

async def changelog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hiển thị thông báo cập nhật y hệt hình mẫu."""
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")]])
    msg = build_update_notification_message(version=APP_VERSION)
    await update.message.reply_text(msg, reply_markup=back_kb, parse_mode=ParseMode.HTML)

async def announce_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Lệnh gửi thông báo cập nhật tùy chỉnh tới tất cả người dùng theo đúng mẫu trong ảnh:
    Ví dụ: /announce Đã thêm tính năng quét QR siêu tốc
    """
    chat_id = update.effective_chat.id
    if ADMIN_CHAT_ID and str(chat_id) != ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Bạn không có quyền dùng lệnh phát thông báo hệ thống.")
        return

    raw_text = update.message.text or ""
    custom_body = re.sub(r'^/announce(@\w+)?\s*', '', raw_text, flags=re.IGNORECASE).strip()

    if not custom_body:
        msg = build_update_notification_message(version=APP_VERSION)
    else:
        msg = build_update_notification_message(custom_text=custom_body)

    target_ids = get_all_known_chat_ids()
    if chat_id not in target_ids:
        target_ids.append(chat_id)

    sent = 0
    for cid in target_ids:
        try:
            await context.bot.send_message(
                chat_id=cid,
                text=msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            sent += 1
        except Exception as e:
            logging.warning(f"Lỗi gửi announce tới {cid}: {e}")

    if len(target_ids) > 1:
        await update.message.reply_text(f"✅ Đã phát thông báo cập nhật tới {sent} người dùng!")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")]])
    await update.message.reply_text(
        get_help_text(),
        reply_markup=back_kb,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


# ==============================================================================
# 8. XỬ LÝ INLINE BUTTON & TIN NHẮN VĂN BẢN
# ==============================================================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    chat_id = query.message.chat_id
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")]])

    try:
        if data == "menu_list":
            await query.answer()
            context.user_data.clear()
            text, kb = build_subscription_list_view(chat_id)
            await safe_edit_message(query, text, kb)

        elif data.startswith("del_sub_"):
            await query.answer("Đã xóa trang!")
            sub_id = int(data.split("_")[2])
            db_execute("DELETE FROM subscriptions WHERE id=? AND chat_id=?", (sub_id, chat_id), commit=True)
            backup_db_to_gist(force=True)
            text, kb = build_subscription_list_view(chat_id)
            await safe_edit_message(query, "✅ Đã xóa thành công (Đã đồng bộ Cloud)!\n\n" + text, kb)

        elif data.startswith("edit_name_"):
            await query.answer()
            sub_id = int(data.split("_")[2])
            row = db_execute("SELECT page_name FROM subscriptions WHERE id=? AND chat_id=?", (sub_id, chat_id), fetch="one")
            if not row:
                return
            context.user_data["action"] = "rename_sub"
            context.user_data["target_sub_id"] = sub_id
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy", callback_data="menu_list")]])
            await safe_edit_message(
                query,
                f"✏️ Đổi tên cho trang (Hiện tại: <b>{html.escape(row[0])}</b>):\n\n👉 Gõ tên mới vào khung chat:",
                cancel_kb
            )

        elif data.startswith("edit_url_"):
            await query.answer()
            sub_id = int(data.split("_")[2])
            context.user_data["action"] = "change_url_sub"
            context.user_data["target_sub_id"] = sub_id
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy", callback_data="menu_list")]])
            await safe_edit_message(query, "🔗 Dán Link FetchRSS / RSS.app mới vào khung chat:", cancel_kb)

        elif data == "add_sub_prompt":
            await query.answer()
            context.user_data["action"] = "add_sub"
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy", callback_data="menu_list")]])
            await safe_edit_message(query, get_add_sub_instruction_text(), cancel_kb)

        elif data == "menu_saved":
            await query.answer()
            context.user_data.clear()
            text, kb = build_saved_posts_view(chat_id)
            await safe_edit_message(query, text, kb)

        elif data.startswith("del_saved_"):
            await query.answer("Đã xóa khỏi tủ!")
            saved_id = int(data.split("_")[2])
            db_execute("DELETE FROM saved_posts WHERE id=? AND chat_id=?", (saved_id, chat_id), commit=True)
            backup_db_to_gist(force=True)
            text, kb = build_saved_posts_view(chat_id)
            await safe_edit_message(query, text, kb)

        elif data == "save_this_post":
            msg_text = query.message.text or ""
            lines = [l.strip() for l in msg_text.split("\n") if l.strip()]
            page_name = lines[0].split("từ ")[-1] if lines and "từ " in lines[0] else "HUIT"
            summary = "\n".join(lines[2:6]) if len(lines) > 2 else msg_text[:200]
            post_link = "https://www.facebook.com"
            if query.message.reply_markup:
                for row in query.message.reply_markup.inline_keyboard:
                    for btn in row:
                        if btn.url:
                            post_link = btn.url
                            break
                    if post_link != "https://www.facebook.com":
                        break

            dup = db_execute("SELECT 1 FROM saved_posts WHERE chat_id=? AND post_link=?", (chat_id, post_link), fetch="one")
            if dup:
                await query.answer("⚠️ Bài viết này đã có trong Tủ của bạn rồi!", show_alert=True)
                return

            db_execute(
                "INSERT INTO saved_posts (chat_id, page_name, summary, post_link) VALUES (?, ?, ?, ?)",
                (chat_id, page_name, summary, post_link),
                commit=True
            )
            backup_db_to_gist(force=True)
            await query.answer("🌟 Đã lưu vào Tủ thành công (Đã sao lưu Cloud)!", show_alert=True)

        elif data == "menu_toggle_drl":
            current = get_user_drl_mode(chat_id)
            new_val = 0 if current == 1 else 1
            if USE_POSTGRES:
                conn = psycopg2.connect(DATABASE_URL)
                with conn.cursor() as c:
                    c.execute(
                        "INSERT INTO user_settings (chat_id, only_drl) VALUES (%s, %s) "
                        "ON CONFLICT (chat_id) DO UPDATE SET only_drl = EXCLUDED.only_drl",
                        (chat_id, new_val)
                    )
                    conn.commit()
                conn.close()
            else:
                db_execute("INSERT OR REPLACE INTO user_settings (chat_id, only_drl) VALUES (?, ?)", (chat_id, new_val), commit=True)
                backup_db_to_gist(force=True)

            status_msg = (
                "🎯 Chỉ nhận thông báo có Điểm Rèn Luyện"
                if new_val == 1
                else "📢 Nhận TẤT CẢ bài đăng mới (vẫn gắn nhãn ĐRL riêng)"
            )
            await query.answer(f"Đã chuyển sang: {status_msg}")
            new_kb = get_main_menu_keyboard(chat_id)
            await safe_edit_message(query, get_welcome_text(chat_id), new_kb)

        elif data == "menu_force_check":
            await query.answer("⚡ Đang quét các bài mới chưa đọc...", show_alert=False)
            sent_count = await run_feed_check_core(context.bot, target_chat_id=chat_id, force_latest=False)
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ Đã hoàn tất quét bài mới! (Đã gửi <b>{sent_count}</b> bài mới).\n"
                    "👉 Nếu muốn xem lại bài mới nhất của từng trang (kể cả đã đọc), bấm /latest.\n"
                    "👈 Vào /menu để mở lại bảng điều khiển."
                ),
                parse_mode=ParseMode.HTML
            )

        elif data == "menu_latest_posts":
            await query.answer("🆕 Đang lấy bài mới nhất của các trang...", show_alert=False)
            sent_count = await run_feed_check_core(context.bot, target_chat_id=chat_id, force_latest=True)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Đã gửi <b>{sent_count}</b> bài mới nhất từ các trang bạn theo dõi!\n👈 Vào /menu để mở lại bảng điều khiển.",
                parse_mode=ParseMode.HTML
            )

        elif data == "menu_changelog":
            await query.answer()
            msg = build_update_notification_message(version=APP_VERSION)
            await safe_edit_message(query, msg, back_kb)

        elif data == "menu_help":
            await query.answer()
            await safe_edit_message(query, get_help_text(), back_kb)

        elif data == "menu_back":
            await query.answer()
            context.user_data.clear()
            msg = get_welcome_text(chat_id)
            kb = get_main_menu_keyboard(chat_id)
            await safe_edit_message(query, msg, kb)

    except Exception as e:
        logging.error(f"Menu error: {e}")

async def handle_user_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("action")
    if not action or not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text_input = update.message.text.strip()

    if action == "rename_sub":
        sub_id = context.user_data.get("target_sub_id")
        new_name = text_input[:45]
        db_execute("UPDATE subscriptions SET page_name=? WHERE id=? AND chat_id=?", (new_name, sub_id, chat_id), commit=True)
        backup_db_to_gist(force=True)
        context.user_data.clear()
        text, kb = build_subscription_list_view(chat_id)
        await update.message.reply_text(
            f"✅ Đã đổi tên thành: <b>{html.escape(new_name)}</b>\n\n{text}",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )

    elif action == "change_url_sub":
        sub_id = context.user_data.get("target_sub_id")
        new_url = text_input.split()[0].strip()
        db_execute("UPDATE subscriptions SET feed_url=? WHERE id=? AND chat_id=?", (new_url, sub_id, chat_id), commit=True)
        backup_db_to_gist(force=True)
        context.user_data.clear()
        text, kb = build_subscription_list_view(chat_id)
        await update.message.reply_text(
            f"✅ Đã cập nhật Link mới!\n\n{text}",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )

    elif action == "add_sub":
        lines = re.split(r'[\r\n]+', text_input)
        context.user_data.clear()
        wait_msg = await update.message.reply_text("⏳ Đang kiểm tra RSS và thêm trang...", parse_mode=ParseMode.HTML)

        added_pages = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[-1].startswith("http"):
                feed_url = parts[-1].strip()
                page_name = " ".join(parts[:-1]).strip()[:45]
                try:
                    feed = await asyncio.to_thread(fetch_feed_sync, feed_url)
                    db_execute(
                        "INSERT INTO subscriptions (chat_id, feed_url, page_name) VALUES (?, ?, ?)",
                        (chat_id, feed_url, page_name),
                        commit=True
                    )
                    # Đánh dấu các bài cũ từ vị trí số 2 trở đi là đã đọc, chừa lại bài mới nhất (index 0) để gửi ngay
                    if feed and getattr(feed, "entries", None):
                        for entry in feed.entries[1:12]:
                            norm_id = normalize_post_unique_id(entry)
                            if norm_id:
                                db_execute(
                                    "INSERT OR IGNORE INTO seen_posts (post_id, page_name, summary, post_link) VALUES (?, ?, ?, ?)",
                                    (f"{chat_id}_{norm_id}", page_name, entry.get('title', ''), entry.get('link', '')),
                                    commit=True
                                )
                    added_pages.append(page_name)
                except Exception as e:
                    logging.error(f"Add sub error: {e}")

        backup_db_to_gist(force=True)
        text, kb = build_subscription_list_view(chat_id)

        # Hiển thị thông báo "Đã thêm: ..." giống hệt phong cách trong tấm hình
        if added_pages:
            added_notification = build_update_notification_message(
                custom_text="\n".join([f"- {p}" for p in added_pages])
            )
            await wait_msg.edit_text(
                f"{added_notification}\n\n{text}",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            await run_feed_check_core(context.bot, target_chat_id=chat_id, force_latest=False)
        else:
            await wait_msg.edit_text(
                f"⚠️ Không thêm được trang nào. Hãy kiểm tra lại cú pháp: <code>Tên_Trang Link_RSS</code>\n\n{text}",
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )


# ==============================================================================
# 9. TIẾN TRÌNH QUÉT BÀI ĐỊNH KỲ (QUÉT SÂU 10 BÀI - VƯỢT BÀI GHIM - CLOUD SYNC)
# ==============================================================================
async def run_feed_check_core(bot, target_chat_id=None, force_latest=False) -> int:
    total_sent = 0
    if target_chat_id:
        subs = db_execute("SELECT chat_id, feed_url, page_name FROM subscriptions WHERE chat_id=?", (target_chat_id,), fetch="all") or []
    else:
        subs = db_execute("SELECT chat_id, feed_url, page_name FROM subscriptions", fetch="all") or []

    settings_rows = db_execute("SELECT chat_id, only_drl FROM user_settings", fetch="all") or []
    settings_map = dict(settings_rows)

    if not subs:
        return 0

    feed_subscribers = {}
    for chat_id, feed_url, page_name in subs:
        if is_direct_facebook_url(feed_url):
            continue
        feed_subscribers.setdefault(feed_url, []).append((chat_id, page_name))

    for feed_url, subscribers in feed_subscribers.items():
        feed = await asyncio.to_thread(fetch_feed_sync, feed_url)
        if not feed or not getattr(feed, "entries", None):
            continue

        entries_to_process = feed.entries[:1] if force_latest else list(reversed(feed.entries[:10]))

        for entry in entries_to_process:
            norm_post_id = normalize_post_unique_id(entry)
            raw_post_id = entry.get('id') or entry.get('link')
            if not norm_post_id:
                continue

            unseen_subscribers = []
            for chat_id, page_name in subscribers:
                unique_key_norm = f"{chat_id}_{norm_post_id}"
                unique_key_raw = f"{chat_id}_{raw_post_id}"
                if force_latest:
                    unseen_subscribers.append((chat_id, page_name, unique_key_norm))
                else:
                    seen_norm = db_execute("SELECT 1 FROM seen_posts WHERE post_id=?", (unique_key_norm,), fetch="one")
                    seen_raw = db_execute("SELECT 1 FROM seen_posts WHERE post_id=?", (unique_key_raw,), fetch="one")
                    if not seen_norm and not seen_raw:
                        unseen_subscribers.append((chat_id, page_name, unique_key_norm))

            if not unseen_subscribers:
                continue

            clean_text, extracted_links, image_urls = extract_full_entry_data(entry)
            post_link = clean_extracted_url(entry.get('link', ''))

            comment_and_qr_links, updated_images = await asyncio.to_thread(
                scrape_links_and_comments_sync, post_link, image_urls
            )
            all_links = list(dict.fromkeys(extracted_links + comment_and_qr_links))

            is_drl, summary_html, ai_img_links = await asyncio.to_thread(
                analyze_post_sync, clean_text, all_links, updated_images
            )
            all_links = list(dict.fromkeys(all_links + ai_img_links))

            for chat_id, page_name, unique_key in unseen_subscribers:
                only_drl = settings_map.get(chat_id, 0)

                if not force_latest and only_drl == 1 and not is_drl:
                    continue

                keyboard = []
                if post_link and post_link.startswith("http"):
                    keyboard.append([InlineKeyboardButton("🌐 Mở bài viết gốc", url=post_link)])

                for idx, link in enumerate(all_links[:6], 1):
                    if link != post_link and link.startswith("http"):
                        l_lower = link.lower()
                        is_form = any(f in l_lower for f in ["forms.gle", "docs.google.com/forms", "office.com", "bit.ly", "tinyurl", "shorturl", "cutt.ly"])
                        is_zalo = "zalo.me" in l_lower
                        label = f"📝 ĐIỀN FORM / ĐĂNG KÝ ({idx})" if is_form else (f"💬 NHÓM ZALO ({idx})" if is_zalo else f"🔗 Link / QR ({idx})")
                        keyboard.append([InlineKeyboardButton(label, url=link)])

                keyboard.append([InlineKeyboardButton("🌟 Lưu bài viết / sự kiện này", callback_data="save_this_post")])
                reply_markup = InlineKeyboardMarkup(keyboard)

                badge = "🔥 <b>[SĂN ĐIỂM RÈN LUYỆN]</b>" if is_drl else "📢 <b>[BÀI ĐĂNG MỚI]</b>"
                safe_page_name = html.escape(page_name)

                quick_nav_footer = "\n\n👈 Vào /menu để điều khiển | /drl đổi chế độ lọc | /scan quét tiếp"
                message = (
                    f"{badge} từ <b>{safe_page_name}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{summary_html}"
                    f"{quick_nav_footer}"
                )

                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True
                    )
                    if USE_POSTGRES:
                        conn = psycopg2.connect(DATABASE_URL)
                        with conn.cursor() as c:
                            c.execute(
                                "INSERT INTO seen_posts (post_id, page_name, summary, post_link) VALUES (%s, %s, %s, %s) "
                                "ON CONFLICT (post_id) DO UPDATE SET summary = EXCLUDED.summary",
                                (unique_key, page_name, summary_html, post_link)
                            )
                            conn.commit()
                        conn.close()
                    else:
                        db_execute(
                            "INSERT OR REPLACE INTO seen_posts (post_id, page_name, summary, post_link) VALUES (?, ?, ?, ?)",
                            (unique_key, page_name, summary_html, post_link),
                            commit=True
                        )
                    total_sent += 1
                except Exception as e:
                    logging.error(f"Send message error (chat_id={chat_id}): {e}")

    if total_sent > 0:
        backup_db_to_gist()
    return total_sent

async def check_feeds_job(context: ContextTypes.DEFAULT_TYPE):
    await run_feed_check_core(context.bot, force_latest=False)


# ==============================================================================
# 10. ĐĂNG KÝ MENU LỆNH & TỰ ĐỘNG THÔNG BÁO KHI BOT CẬP NHẬT
# ==============================================================================
async def post_init_setup(application: Application):
    commands = [
        BotCommand("start", f"🎓 Mở bảng điều khiển Bot v{APP_VERSION}"),
        BotCommand("menu", "🎛️ Mở nhanh Menu điều khiển"),
        BotCommand("list", "📋 Quản lý danh sách trang đang theo dõi"),
        BotCommand("add", "➕ Thêm nhanh trang RSS mới"),
        BotCommand("scan", "⚡ Quét tất cả bài mới chưa đọc ngay lập tức"),
        BotCommand("latest", "🆕 Lấy ngay bài đăng mới nhất của từng trang"),
        BotCommand("drl", "🔄 Đổi chế độ: Nhận tất cả bài ↔ Chỉ bài ĐRL"),
        BotCommand("saved", "📌 Mở tủ sự kiện & bài viết đã lưu"),
        BotCommand("changelog", "♻️ Xem các tính năng vừa cập nhật"),
        BotCommand("announce", "📣 Phát thông báo cập nhật mới"),
        BotCommand("help", "📖 Hướng dẫn lấy link RSS & Cloud Backup"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logging.info(f"✅ Đã kích hoạt Menu lệnh v{APP_VERSION} thành công!")
        # Tự động gửi thông báo "Đã thêm: ..." tới người dùng nếu vừa nâng cấp phiên bản
        await broadcast_version_update_on_startup(application)
    except Exception as e:
        logging.error(f"Lỗi cài đặt post_init: {e}")

if __name__ == '__main__':
    init_db()
    restore_db_from_gist()
    start_keep_alive_server()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init_setup)
        .build()
    )

    app.add_handler(CommandHandler(["start", "menu"], start_or_menu_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("saved", saved_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("latest", latest_cmd))
    app.add_handler(CommandHandler("drl", toggle_drl_cmd))
    app.add_handler(CommandHandler("changelog", changelog_cmd))
    app.add_handler(CommandHandler("announce", announce_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_text_input))

    app.job_queue.run_repeating(check_feeds_job, interval=CHECK_INTERVAL, first=5)

    print(f"🚀 Bot HUIT v{APP_VERSION} (Cloud Backup + Auto Update Changelog + Báo tất cả bài + Lọc ĐRL) đang chạy 24/7...")
    app.run_polling(drop_pending_updates=True)