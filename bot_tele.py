# -*- coding: utf-8 -*-
import os
import sys
import sqlite3
import re
import html
import logging
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, unquote
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
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
}


# ==============================================================================
# 1. UPTIMEROBOT & RENDER KEEP-ALIVE SERVER
# ==============================================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("🟢 HUIT DRL Bot v2.5 PRO is running 24/7!".encode("utf-8"))

    def do_HEAD(self):
        # Quan trọng: UptimeRobot bản miễn phí mặc định gửi HEAD request
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        # Ẩn log ping của UptimeRobot để đỡ rối console
        return

def start_keep_alive_server():
    try:
        server = HTTPServer(('0.0.0.0', PORT), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logging.info(f"🌐 Keep-Alive Server cho UptimeRobot đang lắng nghe tại port {PORT}")
    except Exception as e:
        logging.error(f"Không thể khởi động Web Server trên port {PORT}: {e}")


# ==============================================================================
# 2. QUẢN LÝ DATABASE (CHỐNG LOCK & TỐI ƯU HIỆU NĂNG)
# ==============================================================================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, feed_url TEXT, page_name TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS seen_posts
                     (post_id TEXT PRIMARY KEY, page_name TEXT, summary TEXT, post_link TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_settings
                     (chat_id INTEGER PRIMARY KEY, only_drl INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS saved_posts
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, page_name TEXT, summary TEXT, post_link TEXT)''')
        conn.commit()


# ==============================================================================
# 3. TỐI ƯU CRAWL FACEBOOK, QUÉT COMMENT & GIẢI MÃ QR TRONG ẢNH
# ==============================================================================
def is_direct_facebook_url(url: str) -> bool:
    u = url.lower()
    return ("facebook.com" in u or "fb.com" in u) and not any(
        r in u for r in ["rss", "xml", "feed", "atom"]
    )

def clean_extracted_url(url: str) -> str:
    """Giải mã link bị bọc bởi l.facebook.com/l.php?u=... và xóa ký tự thừa."""
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

def is_useful_external_link(url: str) -> bool:
    """Kiểm tra xem link có phải là form đăng ký, link rút gọn hoặc nhóm Zalo hay không."""
    u = url.lower()
    if any(ignore in u for ignore in ["facebook.com/photo", "facebook.com/hashtag", "fbcdn.net", "fetchrss.com"]):
        return False
    target_domains = [
        "forms.gle", "docs.google.com/forms", "forms.office.com", "microsoft.com",
        "zalo.me", "bit.ly", "tinyurl.com", "shorturl.at", "cutt.ly", "rebrand.ly",
        "gg.gg", "s.net.vn", "link.huit.edu.vn", "huit.edu.vn", "drive.google.com",
        "canva.com"
    ]
    return any(d in u for d in target_domains)

def decode_qr_from_image_url(img_url: str) -> list:
    """Giải mã QR Code trực tiếp từ URL ảnh bằng goQR API (nhẹ, không cần cài thư viện C++ trên Render)."""
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

def parse_entry_content(html_content: str):
    if not html_content:
        return "", [], []
    soup = BeautifulSoup(html_content, "html.parser")

    for br in soup.find_all(["br", "p", "div"]):
        br.append("\n")

    raw_a_links = [a['href'] for a in soup.find_all('a', href=True)]
    images = [
        img['src'] for img in soup.find_all('img', src=True)
        if not img['src'].lower().endswith('.gif') and "emoji" not in img['src'].lower()
    ]

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
    """
    1. Quét sâu vào bài gốc & khung bình luận (mbasic/mobile) để lấy link form dưới comment.
    2. Quét mã QR từ tất cả ảnh trong bài viết và ảnh dưới phần bình luận.
    """
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

                # 1. Bóc tách các link văn bản trong bài và comment
                for pat in patterns:
                    for m in re.findall(pat, raw_html):
                        cleaned = clean_extracted_url(m)
                        if cleaned.startswith("http") and is_useful_external_link(cleaned):
                            found_links.append(cleaned)

                # 2. Lấy thêm ảnh đính kèm dưới comment (nếu có) để quét QR
                soup = BeautifulSoup(raw_html, "html.parser")
                for img in soup.find_all("img", src=True):
                    src = img["src"]
                    if "scontent" in src and src not in all_images:
                        all_images.append(src)

                if found_links:
                    break
            except Exception:
                pass

    # 3. Giải mã QR từ tối đa 4 bức ảnh đầu tiên (Poster chính + ảnh comment)
    for img_url in all_images[:4]:
        qr_urls = decode_qr_from_image_url(img_url)
        for q_url in qr_urls:
            if q_url.startswith("http"):
                found_links.append(q_url)

    return list(dict.fromkeys(found_links)), all_images[:4]

def fetch_feed_sync(feed_url: str):
    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=10)
        return feedparser.parse(resp.content)
    except Exception:
        return feedparser.parse(feed_url)


# ==============================================================================
# 4. NÂNG CẤP LOGIC PHÂN TÍCH & LỌC ĐIỂM RÈN LUYỆN (AI + QR VISION + HEURISTIC)
# ==============================================================================
def heuristic_drl_check(text: str, links: list) -> bool:
    """Bộ lọc dự phòng cực nhạy nếu AI quá tải hoặc bài viết dùng từ viết tắt."""
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

def analyze_post_sync(text: str, links: list, image_urls=None):
    contents = []
    # Gửi tối đa 2 ảnh poster vào Gemini để đọc thông tin trên ảnh và đường link in trên ảnh
    if image_urls:
        for img_url in image_urls[:2]:
            try:
                img_resp = requests.get(img_url, headers=HEADERS, timeout=6)
                if img_resp.status_code == 200 and 'image' in img_resp.headers.get('Content-Type', ''):
                    mime_type = img_resp.headers.get('Content-Type').split(';')[0]
                    contents.append(types.Part.from_bytes(data=img_resp.content, mime_type=mime_type))
            except Exception:
                pass

    prompt = (
        "Bạn là trợ lý săn Điểm Rèn Luyện (ĐRL) thông minh cho sinh viên trường Đại học Công Thương TP.HCM (HUIT).\n"
        f"Nội dung bài đăng:\n\"\"\"{text}\"\"\"\n"
        f"Các đường link thu thập được (bao gồm cả giải mã từ QR & Comment): {links}\n\n"
        "NHIỆM VỤ:\n"
        "1. Dòng đầu tiên BẮT BUỘC chỉ ghi [CO_DRL] hoặc [KHONG_DRL].\n"
        "   - Ghi [CO_DRL] nếu bài viết hoặc chữ trên ảnh poster nói về: cộng điểm rèn luyện (ĐRL), cộng điểm Mục I-V, "
        "tuyển tình nguyện viên, hiến máu, hội thảo, workshop, talkshow, cuộc thi học thuật, khảo sát sinh viên, "
        "hoặc sự kiện có link/mã QR đăng ký tham gia.\n"
        "   - Ghi [KHONG_DRL] nếu chỉ là bài chúc mừng, vinh danh sự kiện đã kết thúc, tin tức thời sự chung.\n"
        "2. Nếu trên ảnh poster có in đường link rút gọn (như bit.ly/..., forms.gle/...) mà chưa có trong danh sách link, "
        "hãy ghi thêm dòng: [LINK_ANH: https://...] ở dòng thứ 2.\n"
        "3. Tóm tắt rõ ràng theo mẫu sau (KHÔNG dùng dấu * hay định dạng Markdown):\n"
        "📌 Hoạt động: (Tên sự kiện/thông báo ngắn gọn)\n"
        "⏰ Thời gian / Địa điểm: (Đọc kỹ cả trong bài viết lẫn chữ trên ảnh Poster)\n"
        "🎁 Quyền lợi: (Ghi rõ số ĐRL được cộng, thuộc Mục nào hoặc quyền lợi khác)\n"
        "⚠️ Lưu ý: (Yêu cầu chính: điền form, quét QR, vào nhóm Zalo, mặc đồng phục...)"
    )
    contents.append(prompt)

    is_drl_fallback = heuristic_drl_check(text, links)
    extra_ai_links = []

    try:
        response = ai_client.models.generate_content(model='gemini-2.5-flash', contents=contents)
        raw_out = (response.text or "").replace("*", "").replace("#", "").strip()

        # Tách link do AI đọc được từ chữ trên ảnh poster (nếu có)
        found_in_img = re.findall(r'\[LINK_ANH:\s*(https?://[^\s\]]+)\]', raw_out)
        for u in found_in_img:
            cleaned_u = clean_extracted_url(u)
            if cleaned_u.startswith("http"):
                extra_ai_links.append(cleaned_u)
        raw_out = re.sub(r'\[LINK_ANH:[^\]]+\]', '', raw_out).strip()

        ai_says_drl = "[CO_DRL]" in raw_out
        final_is_drl = ai_says_drl or is_drl_fallback

        clean_summary = raw_out.replace("[CO_DRL]", "").replace("[KHONG_DRL]", "").strip()
        return final_is_drl, html.escape(clean_summary), extra_ai_links

    except Exception as e:
        logging.error(f"AI Error: {e}")
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        title = lines[0][:90] if lines else "Thông báo mới từ trang"
        short_body = "\n".join(lines[1:4])[:250] if len(lines) > 1 else text[:250]
        reward_text = "Có khả năng cộng ĐRL / Hoạt động sự kiện" if is_drl_fallback else "Xem chi tiết trong bài"
        fallback_summary = (
            f"📌 Hoạt động: {title}\n"
            f"📝 Tóm tắt: {short_body}...\n"
            f"🎁 Quyền lợi: {reward_text}"
        )
        return is_drl_fallback, html.escape(fallback_summary), []


# ==============================================================================
# 5. GIAO DIỆN MENU, NÚT BẤM & LỆNH CLICK TRỰC TIẾP (/menu, /list, /saved...)
# ==============================================================================
async def safe_edit_message(query, text, reply_markup=None):
    """Chống lỗi 'Message is not modified' khi người dùng bấm liên tục 1 nút."""
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

def get_main_menu_keyboard(chat_id: int):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT only_drl FROM user_settings WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
    current_drl = row[0] if row else 0

    drl_label = "🟢 Chế độ: Chỉ nhận bài ĐRL" if current_drl == 1 else "⚪ Chế độ: Nhận tất cả bài"

    keyboard = [
        [InlineKeyboardButton("📋 Quản lý trang theo dõi", callback_data="menu_list")],
        [InlineKeyboardButton("📌 Tủ ĐRL đã lưu", callback_data="menu_saved")],
        [InlineKeyboardButton(drl_label, callback_data="menu_toggle_drl")],
        [InlineKeyboardButton("⚡ Quét bài mới ngay lập tức", callback_data="menu_force_check")],
        [InlineKeyboardButton("📖 Hướng dẫn lấy link FetchRSS", callback_data="menu_help")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_welcome_text():
    return (
        "🎓 <b>BOT SĂN ĐIỂM RÈN LUYỆN HUIT v2.5 PRO</b> 🎓\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <i>Tự động canh bài mới, giải mã QR, quét Comment & lọc ĐRL 24/7.</i>\n\n"
        "⚡ <b>Lệnh bấm nhanh (chạm vào chữ xanh):</b>\n"
        "👉 /list — Xem & quản lý trang theo dõi\n"
        "👉 /add — Thêm nhanh trang RSS mới\n"
        "👉 /saved — Mở tủ sự kiện ĐRL đã lưu\n"
        "👉 /scan — Quét bài mới ngay lập tức\n"
        "👉 /drl — Bật/Tắt chế độ chỉ nhận bài ĐRL\n"
        "👉 /help — Hướng dẫn tạo link RSS\n\n"
        "👇 <b>Hoặc chọn trên bảng điều khiển:</b>"
    )

def build_subscription_list_view(chat_id: int):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, page_name, feed_url FROM subscriptions WHERE chat_id=?", (chat_id,))
        rows = c.fetchall()

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
                status_badge = "⚠️"
            else:
                status_badge = "✅"

            items.append(f"{idx}. <b>{safe_name}</b> {status_badge} | 🔗 <code>{safe_url}...</code>")
            keyboard.append([
                InlineKeyboardButton(f"✏️ Sửa #{idx}", callback_data=f"edit_name_{sub_id}"),
                InlineKeyboardButton(f"🔗 Link #{idx}", callback_data=f"edit_url_{sub_id}"),
                InlineKeyboardButton(f"🗑️ Xóa #{idx}", callback_data=f"del_sub_{sub_id}")
            ])

        warning_box = (
            "\n\n🚨 <i>Lưu ý: Trang có dấu ⚠️ đang dùng link Facebook trực tiếp nên sẽ bị chặn. "
            "Hãy bấm nút <b>Link</b> để đổi sang link RSS (FetchRSS / RSS.app)!</i>"
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
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, page_name, summary, post_link FROM saved_posts WHERE chat_id=? ORDER BY id DESC LIMIT 10",
            (chat_id,)
        )
        rows = c.fetchall()

    keyboard = []
    if not rows:
        text = (
            "📌 <b>TỦ ĐRL ĐÃ LƯU</b>\n"
            "Bạn chưa lưu sự kiện nào.\n\n"
            "👈 Bấm /menu để về bảng điều khiển hoặc /scan để quét bài mới!"
        )
    else:
        items = []
        for idx, (saved_id, page, summary, link) in enumerate(rows, 1):
            safe_page = html.escape(page)
            first_line = html.escape(summary.split("\n")[0]) if summary else "Sự kiện ĐRL"
            safe_link = html.escape(link, quote=True)
            items.append(f"<b>{idx}. [{safe_page}]</b> {first_line}\n🔗 <a href=\"{safe_link}\">Mở bài viết gốc</a>")
            keyboard.append([InlineKeyboardButton(f"🗑️ Xóa mục #{idx}", callback_data=f"del_saved_{saved_id}")])
        text = (
            "📌 <b>SỰ KIỆN ĐÃ LƯU (Tối đa 10 mục gần nhất)</b>\n"
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
        "📖 <b>HƯỚNG DẪN LẤY LINK FETCHRSS / RSS.APP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "1. Truy cập <a href='https://fetchrss.com'>fetchrss.com</a> hoặc <a href='https://rss.app'>rss.app</a> và đăng nhập.\n"
        "2. Copy link Fanpage/Khoa/CLB của trường HUIT trên Facebook dán vào ô tạo RSS.\n"
        "3. Copy đường link có đuôi <code>.xml</code> hoặc <code>/rss/...</code> rồi bấm vào /add để dán vào Bot.\n\n"
        "👈 Bấm /menu để về lại bảng điều khiển."
    )


# ==============================================================================
# 6. CÁC COMMAND HANDLER CHO PHÉP BẤM TRỰC TIẾP TRONG CHAT (/start, /menu, /list...)
# ==============================================================================
async def start_or_menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    chat_id = update.effective_chat.id
    msg = get_welcome_text()
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
    wait_msg = await update.message.reply_text("⚡ Đang quét toàn bộ các trang theo dõi...", parse_mode=ParseMode.HTML)
    await run_feed_check_core(context.bot, target_chat_id=chat_id)
    await wait_msg.edit_text(
        "✅ <b>Đã hoàn tất quét bài mới!</b>\n"
        "Nếu có bài chưa đọc, bot đã gửi ở trên.\n\n"
        "👈 Vào /menu hoặc /list để quản lý tiếp nhé!",
        parse_mode=ParseMode.HTML
    )

async def toggle_drl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT only_drl FROM user_settings WHERE chat_id=?", (chat_id,))
        row = c.fetchone()
        current = row[0] if row else 0
        new_val = 0 if current == 1 else 1
        c.execute("INSERT OR REPLACE INTO user_settings (chat_id, only_drl) VALUES (?, ?)", (chat_id, new_val))
        conn.commit()

    mode_str = "🟢 <b>CHỈ NHẬN BÀI ĐIỂM RÈN LUYỆN</b>" if new_val == 1 else "⚪ <b>NHẬN TẤT CẢ BÀI ĐĂNG MỚI</b>"
    kb = get_main_menu_keyboard(chat_id)
    await update.message.reply_text(
        f"🔄 Đã chuyển sang chế độ: {mode_str}\n\n{get_welcome_text()}",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")]])
    await update.message.reply_text(
        get_help_text(),
        reply_markup=back_kb,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


# ==============================================================================
# 7. XỬ LÝ INLINE BUTTON & TIN NHẮN VĂN BẢN
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
            with get_db_connection() as conn:
                conn.execute("DELETE FROM subscriptions WHERE id=? AND chat_id=?", (sub_id, chat_id))
                conn.commit()
            text, kb = build_subscription_list_view(chat_id)
            await safe_edit_message(query, "✅ Đã xóa thành công!\n\n" + text, kb)

        elif data.startswith("edit_name_"):
            await query.answer()
            sub_id = int(data.split("_")[2])
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT page_name FROM subscriptions WHERE id=? AND chat_id=?", (sub_id, chat_id))
                row = c.fetchone()
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
            await safe_edit_message(query, "🔗 Dán Link FetchRSS / RSS mới vào khung chat:", cancel_kb)

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
            with get_db_connection() as conn:
                conn.execute("DELETE FROM saved_posts WHERE id=? AND chat_id=?", (saved_id, chat_id))
                conn.commit()
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

            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT 1 FROM saved_posts WHERE chat_id=? AND post_link=?",
                    (chat_id, post_link)
                )
                if c.fetchone():
                    await query.answer("⚠️ Bài viết này đã có trong Tủ của bạn rồi!", show_alert=True)
                    return
                c.execute(
                    "INSERT INTO saved_posts (chat_id, page_name, summary, post_link) VALUES (?, ?, ?, ?)",
                    (chat_id, page_name, summary, post_link)
                )
                conn.commit()
            await query.answer("🌟 Đã lưu sự kiện vào Tủ ĐRL thành công!", show_alert=True)

        elif data == "menu_toggle_drl":
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute("SELECT only_drl FROM user_settings WHERE chat_id=?", (chat_id,))
                row = c.fetchone()
                current = row[0] if row else 0
                new_val = 0 if current == 1 else 1
                c.execute("INSERT OR REPLACE INTO user_settings (chat_id, only_drl) VALUES (?, ?)", (chat_id, new_val))
                conn.commit()

            status_msg = "Chỉ nhận thông báo có Điểm Rèn Luyện" if new_val == 1 else "Nhận tất cả thông báo mới"
            await query.answer(f"Đã chuyển sang: {status_msg}")
            new_kb = get_main_menu_keyboard(chat_id)
            await safe_edit_message(query, get_welcome_text(), new_kb)

        elif data == "menu_force_check":
            await query.answer("⚡ Đang quét toàn bộ các trang...", show_alert=False)
            await run_feed_check_core(context.bot, target_chat_id=chat_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text="✅ Đã hoàn tất quét bài mới! Nếu có bài chưa đọc, bot đã gửi ở trên.\n👈 Vào /menu để mở lại bảng điều khiển.",
                parse_mode=ParseMode.HTML
            )

        elif data == "menu_help":
            await query.answer()
            await safe_edit_message(query, get_help_text(), back_kb)

        elif data == "menu_back":
            await query.answer()
            context.user_data.clear()
            msg = get_welcome_text()
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
        with get_db_connection() as conn:
            conn.execute("UPDATE subscriptions SET page_name=? WHERE id=? AND chat_id=?", (new_name, sub_id, chat_id))
            conn.commit()
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
        with get_db_connection() as conn:
            conn.execute("UPDATE subscriptions SET feed_url=? WHERE id=? AND chat_id=?", (new_url, sub_id, chat_id))
            conn.commit()
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

        added_count = 0
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
                    with get_db_connection() as conn:
                        c = conn.cursor()
                        c.execute(
                            "INSERT INTO subscriptions (chat_id, feed_url, page_name) VALUES (?, ?, ?)",
                            (chat_id, feed_url, page_name)
                        )
                        # Đánh dấu các bài cũ từ vị trí số 2 trở đi là đã đọc, chừa lại bài mới nhất (index 0) để bot báo ngay
                        for entry in feed.entries[1:6]:
                            post_id = entry.get('id') or entry.get('link')
                            if post_id:
                                c.execute(
                                    "INSERT OR IGNORE INTO seen_posts VALUES (?, ?, ?, ?)",
                                    (f"{chat_id}_{post_id}", page_name, entry.get('title', ''), entry.get('link', ''))
                                )
                        conn.commit()
                    added_count += 1
                except Exception as e:
                    logging.error(f"Add sub error: {e}")

        text, kb = build_subscription_list_view(chat_id)
        await wait_msg.edit_text(
            f"✅ Đã thêm thành công <b>{added_count}</b> trang!\n\n{text}",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )


# ==============================================================================
# 8. TIẾN TRÌNH QUÉT BÀI ĐỊNH KỲ (GOM NHÓM THEO FEED + QR + COMMENT)
# ==============================================================================
async def run_feed_check_core(bot, target_chat_id=None):
    with get_db_connection() as conn:
        c = conn.cursor()
        if target_chat_id:
            c.execute("SELECT chat_id, feed_url, page_name FROM subscriptions WHERE chat_id=?", (target_chat_id,))
        else:
            c.execute("SELECT chat_id, feed_url, page_name FROM subscriptions")
        subs = c.fetchall()

        c.execute("SELECT chat_id, only_drl FROM user_settings")
        settings_map = dict(c.fetchall())

    if not subs:
        return

    feed_subscribers = {}
    for chat_id, feed_url, page_name in subs:
        if is_direct_facebook_url(feed_url):
            continue
        feed_subscribers.setdefault(feed_url, []).append((chat_id, page_name))

    for feed_url, subscribers in feed_subscribers.items():
        feed = await asyncio.to_thread(fetch_feed_sync, feed_url)
        if not feed or not getattr(feed, "entries", None):
            continue

        for entry in reversed(feed.entries[:3]):
            post_id = entry.get('id') or entry.get('link')
            if not post_id:
                continue

            unseen_subscribers = []
            with get_db_connection() as conn:
                c = conn.cursor()
                for chat_id, page_name in subscribers:
                    unique_key = f"{chat_id}_{post_id}"
                    c.execute("SELECT 1 FROM seen_posts WHERE post_id=?", (unique_key,))
                    if not c.fetchone():
                        unseen_subscribers.append((chat_id, page_name, unique_key))

            if not unseen_subscribers:
                continue

            # 1. Bóc tách HTML từ RSS
            raw_html = entry.get('description', '') or entry.get('summary', '') or entry.get('title', '')
            clean_text, extracted_links, image_urls = parse_entry_content(raw_html)
            post_link = clean_extracted_url(entry.get('link', ''))

            # 2. Quét sâu vào bài gốc để lấy link trong Comment & giải mã QR từ tất cả ảnh
            comment_and_qr_links, updated_images = await asyncio.to_thread(
                scrape_links_and_comments_sync, post_link, image_urls
            )
            all_links = list(dict.fromkeys(extracted_links + comment_and_qr_links))

            # 3. Phân tích bằng Gemini 2.5 Flash (đọc cả chữ trên ảnh Poster) + Bộ lọc Heuristic
            is_drl, summary_html, ai_img_links = await asyncio.to_thread(
                analyze_post_sync, clean_text, all_links, updated_images
            )
            all_links = list(dict.fromkeys(all_links + ai_img_links))

            # 4. Gửi thông báo cho từng người dùng
            for chat_id, page_name, unique_key in unseen_subscribers:
                only_drl = settings_map.get(chat_id, 0)

                with get_db_connection() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO seen_posts VALUES (?, ?, ?, ?)",
                        (unique_key, page_name, summary_html, post_link)
                    )
                    conn.commit()

                if only_drl == 1 and not is_drl:
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

                keyboard.append([InlineKeyboardButton("🌟 Lưu sự kiện này", callback_data="save_this_post")])
                reply_markup = InlineKeyboardMarkup(keyboard)

                badge = "🔥 <b>[SĂN ĐIỂM RÈN LUYỆN]</b>" if is_drl else "📢 <b>[BÀI ĐĂNG MỚI]</b>"
                safe_page_name = html.escape(page_name)
                
                # Dòng lệnh có thể click trực tiếp giống như ảnh mẫu (/menu, /saved, /scan)
                quick_nav_footer = "\n\n👈 Vào /menu để điều khiển | /saved xem tủ ĐRL | /scan quét tiếp"
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
                except Exception as e:
                    logging.error(f"Send message error (chat_id={chat_id}): {e}")

async def check_feeds_job(context: ContextTypes.DEFAULT_TYPE):
    await run_feed_check_core(context.bot)


# ==============================================================================
# 9. ĐĂNG KÝ MENU LỆNH TỰ ĐỘNG & KHỞI CHẠY ỨNG DỤNG
# ==============================================================================
async def post_init_setup(application: Application):
    """Tự động đăng ký danh sách lệnh để hiển thị nút Menu xanh và gợi ý khi gõ '/'."""
    commands = [
        BotCommand("start", "🎓 Khởi động & Mở bảng điều khiển chính"),
        BotCommand("menu", "🎛️ Mở nhanh Menu điều khiển"),
        BotCommand("list", "📋 Quản lý danh sách trang đang theo dõi"),
        BotCommand("add", "➕ Thêm nhanh trang RSS mới"),
        BotCommand("saved", "📌 Mở tủ sự kiện ĐRL đã lưu"),
        BotCommand("scan", "⚡ Quét bài đăng mới ngay lập tức"),
        BotCommand("drl", "🔄 Bật/Tắt chế độ chỉ nhận bài ĐRL"),
        BotCommand("help", "📖 Hướng dẫn lấy link RSS Facebook"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logging.info("✅ Đã kích hoạt Menu lệnh (/start, /menu, /list, /saved...) thành công!")
    except Exception as e:
        logging.error(f"Lỗi cài đặt BotCommand: {e}")

if __name__ == '__main__':
    init_db()
    start_keep_alive_server()

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init_setup)
        .build()
    )

    # Đăng ký tất cả các lệnh có thể bấm trực tiếp trong tin nhắn
    app.add_handler(CommandHandler(["start", "menu"], start_or_menu_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("saved", saved_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("drl", toggle_drl_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_text_input))

    app.job_queue.run_repeating(check_feeds_job, interval=CHECK_INTERVAL, first=5)

    print("🚀 Bot HUIT PRO v2.5 (QR + Comment + Clickable Commands + UptimeRobot) đang chạy 24/7...")
    app.run_polling(drop_pending_updates=True)