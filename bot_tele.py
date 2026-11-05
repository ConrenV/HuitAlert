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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
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
        self.wfile.write("🟢 HUIT DRL Bot is running 24/7!".encode("utf-8"))

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
# 3. TỐI ƯU CRAWL FACEBOOK & BÓC TÁCH LINK
# ==============================================================================
def is_direct_facebook_url(url: str) -> bool:
    u = url.lower()
    return ("facebook.com" in u or "fb.com" in u) and not any(
        r in u for r in ["rss", "xml", "feed", "atom"]
    )

def clean_extracted_url(url: str) -> str:
    """Giải mã link bị bọc bởi l.facebook.com/l.php?u=... và xóa tracking rác."""
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
    # Cắt bỏ phần ký tự thừa cuối chuỗi nếu dính dấu ngoặc hoặc dấu chấm câu
    url = url.rstrip('.,;)]}\'"')
    return url

def is_useful_external_link(url: str) -> bool:
    """Kiểm tra xem link có phải là form đăng ký, link rút gọn hoặc nhóm Zalo hay không."""
    u = url.lower()
    if any(ignore in u for ignore in ["facebook.com/photo", "facebook.com/hashtag", "fbcdn.net", "fetchrss.com"]):
        return False
    target_domains = [
        "forms.gle", "docs.google.com/forms", "forms.office.com", "microsoft.com",
        "zalo.me", "bit.ly", "tinyurl.com", "shorturl.at", "cutt.ly", "rebrand.ly",
        "gg.gg", "s.net.vn", "link.huit.edu.vn", "huit.edu.vn", "drive.google.com"
    ]
    return any(d in u for d in target_domains)

def parse_entry_content(html_content: str):
    if not html_content:
        return "", [], []
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Đổi thẻ <br> và <p> thành xuống dòng để giữ bố cục bài viết
    for br in soup.find_all(["br", "p", "div"]):
        br.append("\n")

    raw_a_links = [a['href'] for a in soup.find_all('a', href=True)]
    images = [img['src'] for img in soup.find_all('img', src=True) if not img['src'].endswith('.gif')]

    text = soup.get_text()
    # Thu gọn nhiều dòng trống liên tiếp
    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    regex_urls = re.findall(r'(https?://[^\s<>"\'\]\)]+)', text)
    
    cleaned_links = []
    for link in raw_a_links + regex_urls:
        clean_l = clean_extracted_url(link)
        if clean_l.startswith("http") and is_useful_external_link(clean_l):
            cleaned_links.append(clean_l)

    return text, list(dict.fromkeys(cleaned_links)), images

def scrape_links_sync(post_url: str):
    """Quét sâu vào bài gốc (ưu tiên mbasic.facebook.com) để tìm link form ẩn."""
    if not post_url:
        return []
    found_links = []
    urls_to_try = [post_url]
    
    # Chuyển sang mbasic để tải HTML tĩnh nhẹ hơn và dễ lấy link hơn
    if "www.facebook.com" in post_url:
        urls_to_try.insert(0, post_url.replace("www.facebook.com", "mbasic.facebook.com"))
    elif "facebook.com" in post_url and "mbasic." not in post_url:
        urls_to_try.insert(0, post_url.replace("facebook.com", "mbasic.facebook.com"))

    patterns = [
        r'https?://forms\.gle/[a-zA-Z0-9_-]+',
        r'https?://docs\.google\.com/forms/[^\s"<>\\]+',
        r'https?://forms\.office\.com/[^\s"<>\\]+',
        r'https?://zalo\.me/[a-zA-Z0-9_/-]+',
        r'https?://bit\.ly/[a-zA-Z0-9_-]+',
        r'https?://tinyurl\.com/[a-zA-Z0-9_-]+',
        r'https?://shorturl\.at/[a-zA-Z0-9_-]+',
        r'https?://l\.facebook\.com/l\.php\?u=[^\s"<>\\]+'
    ]

    for target_url in urls_to_try[:2]:
        try:
            resp = requests.get(target_url, headers=HEADERS, timeout=6)
            if resp.status_code != 200:
                continue
            raw_html = html.unescape(resp.text.replace("\\/", "/"))
            for pat in patterns:
                matches = re.findall(pat, raw_html)
                for m in matches:
                    cleaned = clean_extracted_url(m)
                    if cleaned.startswith("http"):
                        found_links.append(cleaned)
            if found_links:
                break
        except Exception:
            pass

    return list(dict.fromkeys(found_links))

def fetch_feed_sync(feed_url: str):
    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=10)
        return feedparser.parse(resp.content)
    except Exception:
        return feedparser.parse(feed_url)


# ==============================================================================
# 4. NÂNG CẤP LOGIC PHÂN TÍCH & LỌC ĐIỂM RÈN LUYỆN (AI + HEURISTIC)
# ==============================================================================
def heuristic_drl_check(text: str, links: list) -> bool:
    """Bộ lọc dự phòng cực nhạy nếu AI quá tải hoặc bài viết dùng từ viết tắt."""
    t = text.lower()
    strong_keywords = [
        "điểm rèn luyện", "diem ren luyen", "đrl", "drl",
        "cộng điểm", "cong diem", "điểm cộng", "diem cong",
        "mục i", "mục ii", "mục iii", "mục iv", "mục v",
        "điều 1", "điều 2", "điều 3", "điều 4", "điều 5",
        "quyền lợi: cộng", "체크인", "check-in", "checkin",
        "điểm danh", "diem danh", "khảo sát", "khao sat"
    ]
    if any(k in t for k in strong_keywords):
        return True

    # Nếu bài có kèm link Google Form / Microsoft Form VÀ có từ khóa hoạt động sinh viên
    has_form = any(
        any(f in l.lower() for f in ["forms.gle", "docs.google.com/forms", "forms.office.com"])
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
    if image_urls:
        for img_url in image_urls[:1]:
            try:
                img_resp = requests.get(img_url, headers=HEADERS, timeout=5)
                if img_resp.status_code == 200 and 'image' in img_resp.headers.get('Content-Type', ''):
                    mime_type = img_resp.headers.get('Content-Type').split(';')[0]
                    contents.append(types.Part.from_bytes(data=img_resp.content, mime_type=mime_type))
            except Exception:
                pass

    prompt = (
        "Bạn là trợ lý săn Điểm Rèn Luyện (ĐRL) thông minh cho sinh viên trường Đại học Công Thương TP.HCM (HUIT).\n"
        f"Nội dung bài đăng:\n\"\"\"{text}\"\"\"\n"
        f"Các đường link đính kèm: {links}\n\n"
        "NHIỆM VỤ:\n"
        "1. Dòng đầu tiên BẮT BUỘC chỉ ghi [CO_DRL] hoặc [KHONG_DRL].\n"
        "   - Ghi [CO_DRL] nếu bài viết nói về: cộng điểm rèn luyện (ĐRL), cộng điểm các Mục I, II, III, IV, V, "
        "tuyển tình nguyện viên, hiến máu, hội thảo, workshop, talkshow, cuộc thi học thuật, khảo sát sinh viên, "
        "hoặc sự kiện có link Form đăng ký tham gia / điểm danh.\n"
        "   - Ghi [KHONG_DRL] nếu chỉ là bài chúc mừng lễ/tết, vinh danh cá nhân đã qua, tin tức thời sự chung, "
        "hoặc thông báo nghỉ học không có hoạt động kèm theo.\n"
        "2. Từ dòng thứ 2, tóm tắt rõ ràng theo mẫu sau (KHÔNG dùng dấu * hay định dạng Markdown):\n"
        "📌 Hoạt động: (Tên sự kiện/thông báo ngắn gọn)\n"
        "⏰ Thời gian / Địa điểm: (Ghi cụ thể thời gian, hạn đăng ký hoặc địa điểm nếu có, nếu không ghi 'Xem chi tiết trong bài')\n"
        "🎁 Quyền lợi: (Ghi rõ số ĐRL được cộng, thuộc Mục nào hoặc quyền lợi khác)\n"
        "⚠️ Lưu ý: (Yêu cầu chính: điền form, vào nhóm Zalo, mặc đồng phục...)"
    )
    contents.append(prompt)

    is_drl_fallback = heuristic_drl_check(text, links)

    try:
        response = ai_client.models.generate_content(model='gemini-2.5-flash', contents=contents)
        raw_out = (response.text or "").replace("*", "").replace("#", "").strip()
        
        # Kết hợp kết quả của AI và bộ lọc Heuristic để không bỏ sót bài có Form ĐRL
        ai_says_drl = "[CO_DRL]" in raw_out
        final_is_drl = ai_says_drl or is_drl_fallback

        clean_summary = raw_out.replace("[CO_DRL]", "").replace("[KHONG_DRL]", "").strip()
        # Escape HTML để không bị lỗi Telegram ParseMode.HTML
        return final_is_drl, html.escape(clean_summary)

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
        return is_drl_fallback, html.escape(fallback_summary)


# ==============================================================================
# 5. GIAO DIỆN MENU & XỬ LÝ TƯƠNG TÁC TELEGRAM
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
        "🎓 <b>BOT SĂN ĐIỂM RÈN LUYỆN HUIT v2.0 PRO</b> 🎓\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <i>Hệ thống tự động canh bài mới, giải mã link ẩn và lọc sự kiện ĐRL 24/7.</i>\n\n"
        "👇 <b>Chọn chức năng bên dưới:</b>"
    )

def build_subscription_list_view(chat_id: int):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT id, page_name, feed_url FROM subscriptions WHERE chat_id=?", (chat_id,))
        rows = c.fetchall()

    keyboard = []
    has_fb_warning = False
    if not rows:
        text = "📭 <b>DANH SÁCH TRỐNG</b>\nBạn chưa theo dõi trang nào. Hãy bấm <b>➕ Thêm trang mới</b>."
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
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(items) + warning_box
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
        text = "📌 <b>TỦ ĐRL ĐÃ LƯU</b>\nBạn chưa lưu sự kiện nào."
    else:
        items = []
        for idx, (saved_id, page, summary, link) in enumerate(rows, 1):
            safe_page = html.escape(page)
            first_line = html.escape(summary.split("\n")[0]) if summary else "Sự kiện ĐRL"
            safe_link = html.escape(link, quote=True)
            items.append(f"<b>{idx}. [{safe_page}]</b> {first_line}\n🔗 <a href=\"{safe_link}\">Mở bài viết gốc</a>")
            keyboard.append([InlineKeyboardButton(f"🗑️ Xóa mục #{idx}", callback_data=f"del_saved_{saved_id}")])
        text = "📌 <b>SỰ KIỆN ĐÃ LƯU (Tối đa 10 mục gần nhất)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(items)

    keyboard.append([InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")])
    return text, InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    chat_id = update.effective_chat.id
    msg = get_welcome_text()
    kb = get_main_menu_keyboard(chat_id)
    if update.message:
        await update.message.reply_text(msg, reply_markup=kb, parse_mode=ParseMode.HTML)
    elif update.callback_query:
        await update.callback_query.answer()
        await safe_edit_message(update.callback_query, msg, kb)

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
            await safe_edit_message(
                query,
                "➕ <b>THÊM TRANG THEO DÕI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Gõ theo định dạng (hỗ trợ gửi nhiều dòng cùng lúc):\n"
                "<code>Tên_Trang Link_RSS</code>\n\n"
                "Ví dụ:\n<code>Đoàn Trường HUIT https://fetchrss.com/rss/...</code>\n\n"
                "👉 Gửi vào khung chat:",
                cancel_kb
            )

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
                text="✅ Đã hoàn tất quét bài mới! Nếu có bài chưa đọc, bot đã gửi ở trên.",
                parse_mode=ParseMode.HTML
            )

        elif data == "menu_help":
            await query.answer()
            help_text = (
                "📖 <b>HƯỚNG DẪN LẤY LINK FETCHRSS / RSS.APP</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "1. Truy cập <a href='https://fetchrss.com'>fetchrss.com</a> hoặc <a href='https://rss.app'>rss.app</a> và đăng nhập.\n"
                "2. Copy link Fanpage/Khoa/CLB của trường HUIT trên Facebook dán vào ô tạo RSS.\n"
                "3. Copy đường link có đuôi <code>.xml</code> hoặc <code>/rss/...</code> dán vào mục <b>Thêm trang mới</b> của Bot."
            )
            await safe_edit_message(query, help_text, back_kb)

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
                    # Tải feed bất đồng bộ để không làm đơ bot
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
# 6. TIẾN TRÌNH QUÉT BÀI ĐỊNH KỲ (GOM NHÓM THEO FEED ĐỂ TIẾT KIỆM QUOTA AI)
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

    # Gom nhóm các người dùng cùng theo dõi 1 đường link RSS
    feed_subscribers = {}
    for chat_id, feed_url, page_name in subs:
        if is_direct_facebook_url(feed_url):
            continue
        feed_subscribers.setdefault(feed_url, []).append((chat_id, page_name))

    for feed_url, subscribers in feed_subscribers.items():
        feed = await asyncio.to_thread(fetch_feed_sync, feed_url)
        if not feed or not getattr(feed, "entries", None):
            continue

        # Duyệt tối đa 3 bài mới nhất (từ cũ đến mới)
        for entry in reversed(feed.entries[:3]):
            post_id = entry.get('id') or entry.get('link')
            if not post_id:
                continue

            # Kiểm tra xem có người dùng nào chưa nhận bài này không
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

            # Chỉ bóc tách và gọi AI đúng 1 lần cho mỗi bài đăng mới
            raw_html = entry.get('description', '') or entry.get('summary', '') or entry.get('title', '')
            clean_text, extracted_links, image_urls = parse_entry_content(raw_html)
            post_link = clean_extracted_url(entry.get('link', ''))

            comment_links = await asyncio.to_thread(scrape_links_sync, post_link)
            all_links = list(dict.fromkeys(extracted_links + comment_links))

            is_drl, summary_html = await asyncio.to_thread(analyze_post_sync, clean_text, all_links, image_urls)

            # Phân phối kết quả cho từng người đăng ký
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

                for idx, link in enumerate(all_links[:5], 1):
                    if link != post_link and link.startswith("http"):
                        l_lower = link.lower()
                        is_form = any(f in l_lower for f in ["forms.gle", "docs.google.com/forms", "office.com", "bit.ly", "tinyurl", "shorturl"])
                        is_zalo = "zalo.me" in l_lower
                        label = f"📝 ĐIỀN FORM / ĐĂNG KÝ ({idx})" if is_form else (f"💬 NHÓM ZALO ({idx})" if is_zalo else f"🔗 Link đính kèm ({idx})")
                        keyboard.append([InlineKeyboardButton(label, url=link)])

                keyboard.append([InlineKeyboardButton("🌟 Lưu sự kiện này", callback_data="save_this_post")])
                reply_markup = InlineKeyboardMarkup(keyboard)

                badge = "🔥 <b>[SĂN ĐIỂM RÈN LUYỆN]</b>" if is_drl else "📢 <b>[BÀI ĐĂNG MỚI]</b>"
                safe_page_name = html.escape(page_name)
                message = f"{badge} từ <b>{safe_page_name}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n{summary_html}"

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
# 7. KHỞI CHẠY ỨNG DỤNG
# ==============================================================================
if __name__ == '__main__':
    init_db()
    start_keep_alive_server()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_text_input))

    app.job_queue.run_repeating(check_feeds_job, interval=CHECK_INTERVAL, first=5)

    print("🚀 Bot HUIT PRO v2.0 (Tối ưu Crawl + UptimeRobot Server + Lọc ĐRL Kép) đang chạy 24/7...")
    app.run_polling(drop_pending_updates=True)