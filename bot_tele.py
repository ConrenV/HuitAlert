# -*- coding: utf-8 -*-
import os
import sys
import sqlite3
import re
import logging
import asyncio
from pathlib import Path
import requests
import feedparser
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# Ép cửa sổ Console/Terminal trên Windows hiển thị đúng tiếng Việt UTF-8
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ================= CẤU HÌNH BẢO MẬT (.env) =================
env_path = Path(__file__).resolve().parent / '.env'
load_dotenv(dotenv_path=env_path)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHECK_INTERVAL = 30

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError(
        "❌ LỖI: Không tìm thấy TELEGRAM_TOKEN hoặc GEMINI_API_KEY!\n"
        "Hãy kiểm tra lại file '.env' cùng thư mục với bot_tele.py."
    )
# ===========================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

ai_client = genai.Client(api_key=GEMINI_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def init_db():
    conn = sqlite3.connect('huit_bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                 (chat_id INTEGER, feed_url TEXT, page_name TEXT, PRIMARY KEY (chat_id, feed_url))''')
    c.execute('''CREATE TABLE IF NOT EXISTS seen_posts
                 (post_id TEXT PRIMARY KEY, page_name TEXT, summary TEXT, post_link TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_settings
                 (chat_id INTEGER PRIMARY KEY, only_drl INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS saved_posts
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, page_name TEXT, summary TEXT, post_link TEXT)''')
    conn.commit()
    conn.close()

def is_direct_facebook_url(url: str) -> bool:
    u = url.lower()
    return ("facebook.com" in u or "fb.com" in u) and not any(r in u for r in ["rss", "xml", "feed"])

def parse_entry_content(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    links = [a['href'] for a in soup.find_all('a', href=True)]
    images = [img['src'] for img in soup.find_all('img', src=True)]
    text = soup.get_text(separator="\n")
    raw_urls = re.findall(r'(https?://[^\s<>"]+|www\.[^\s<>"]+)', text)
    all_links = list(dict.fromkeys(links + raw_urls))
    return text.strip(), all_links, images

def scrape_links_sync(post_url):
    if not post_url:
        return []
    found_links = []
    try:
        resp = requests.get(post_url, headers=HEADERS, timeout=5)
        html = resp.text.replace("\\/", "/")
        patterns = [
            r'https?://forms\.gle/[a-zA-Z0-9_-]+',
            r'https?://docs\.google\.com/forms/[^\s"<>\\]+',
            r'https?://forms\.office\.com/[^\s"<>\\]+',
            r'https?://zalo\.me/g/[a-zA-Z0-9_-]+',
            r'https?://bit\.ly/[a-zA-Z0-9_-]+'
        ]
        for pat in patterns:
            matches = re.findall(pat, html)
            found_links.extend(matches)
    except Exception:
        pass
    return list(dict.fromkeys(found_links))

def analyze_post_sync(text, links, image_urls=None):
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
        "Bạn là trợ lý thông tin cho sinh viên trường HUIT.\n"
        f"Nội dung bài đăng gốc:\n\"{text}\"\nCác link tìm thấy: {links}\n\n"
        "QUY TẮC PHÂN LOẠI CỰC KỲ NGHIÊM NGẶT:\n"
        "1. Dòng đầu tiên chỉ ghi đúng 1 từ: [CO_DRL] CHỈ KHI bài viết thông báo rõ ràng về việc CỘNG ĐIỂM RÈN LUYỆN, tham gia hoạt động tình nguyện, hội thảo học thuật, khảo sát, hoặc có form đăng ký tham gia sự kiện. Nếu bài chỉ là vinh danh kết quả, chúc mừng, thông tin thời sự, hoặc không có quyền lợi điểm số cụ thể, BẮT BUỘC phải ghi [KHONG_DRL].\n"
        "2. Tóm tắt ngắn gọn theo thực tế bài đăng, TUYỆT ĐỐI KHÔNG tự bịa đặt hay suy diễn quyền lợi điểm rèn luyện nếu bài gốc không nhắc đến:\n"
        "📌 Hoạt động: (Tên sự kiện hoặc tiêu đề chính)\n"
        "⏰ Thời gian / Địa điểm: (Nếu có trong bài)\n"
        "🎁 Quyền lợi / Điểm cộng: (Chỉ ghi số ĐRL hoặc quyền lợi thực tế, nếu không có hãy ghi 'Không có')\n"
        "⚠️ Lưu ý: (Yêu cầu chính nếu có)"
    )
    contents.append(prompt)

    try:
        response = ai_client.models.generate_content(model='gemini-2.5-flash', contents=contents)
        return response.text.replace("*", "")
    except Exception as e:
        logging.error(f"AI Error: {e}")
        text_lower = text.lower()
        strict_drl_kw = ["rèn luyện", "ren luyen", "đrl", "drl", "điểm cộng", "diem cong", "cộng điểm"]
        is_drl = any(k in text_lower for k in strict_drl_kw)
        tag = "[CO_DRL]" if is_drl else "[KHONG_DRL]"
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        short_text = "\n".join(lines[:4])
        reward_text = "Cộng Điểm Rèn Luyện" if is_drl else "Không có"
        return (
            f"{tag}\n"
            f"📌 Hoạt động: {lines[0][:80] if lines else 'Thông báo mới'}\n"
            f"📝 Nội dung chính:\n{short_text[:280]}...\n"
            f"🎁 Quyền lợi: {reward_text}"
        )

def fetch_feed_sync(feed_url):
    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=6)
        return feedparser.parse(resp.content)
    except Exception:
        return feedparser.parse(feed_url)

# --- GIAO DIỆN MENU CHÍNH & QUẢN LÝ ---
def get_main_menu_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("📋 Quản lý trang (Sửa/Xóa)", callback_data="menu_list"),
            InlineKeyboardButton("⚡ Quét bài ngay", callback_data="menu_checknow")
        ],
        [
            InlineKeyboardButton("📌 Tủ ĐRL đã lưu", callback_data="menu_saved"),
            InlineKeyboardButton("🔎 Tìm bài cũ", callback_data="menu_search_prompt")
        ],
        [
            InlineKeyboardButton("🎯 Chế độ lọc bài (ĐRL / Tất cả)", callback_data="menu_drl"),
            InlineKeyboardButton("🧪 Test bài mẫu (Demo)", callback_data="menu_demo")
        ],
        [
            InlineKeyboardButton("📖 Hướng dẫn & Cách lấy link RSS", callback_data="menu_help")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_welcome_text():
    return (
        "🎓 <b>BOT SĂN ĐIỂM RÈN LUYỆN HUIT 24/7</b> 🎓\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <i>Trợ lý AI tự động canh bài mới, phân loại chuẩn xác sự kiện có ĐRL và bóc tách link siêu tốc.</i>\n\n"
        "👇 <b>Hãy chọn một chức năng bên dưới để bắt đầu:</b>"
    )

def build_subscription_list_view(chat_id: int):
    conn = sqlite3.connect('huit_bot.db')
    c = conn.cursor()
    c.execute("SELECT rowid, page_name, feed_url FROM subscriptions WHERE chat_id=?", (chat_id,))
    rows = c.fetchall()
    conn.close()

    keyboard = []
    has_fb_warning = False
    if not rows:
        text = (
            "📭 <b>DANH SÁCH ĐANG TRỐNG</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Bạn chưa theo dõi Fanpage nào.\n"
            "💡 Hãy bấm nút <b>➕ Thêm trang mới nhanh</b> bên dưới."
        )
    else:
        items = []
        for idx, (rowid, name, url) in enumerate(rows, 1):
            if is_direct_facebook_url(url):
                has_fb_warning = True
                status_badge = "⚠️ <i>(Link FB trực tiếp - Cần đổi sang RSS)</i>"
            else:
                status_badge = "✅ <i>(Link RSS hợp lệ)</i>"

            items.append(f"{idx}. <b>{name}</b> {status_badge}\n   🔗 <code>{url}</code>")
            keyboard.append([
                InlineKeyboardButton(f"✏️ Tên #{idx}", callback_data=f"edit_name_{rowid}"),
                InlineKeyboardButton(f"🔗 Đổi Link #{idx}", callback_data=f"edit_url_{rowid}"),
                InlineKeyboardButton(f"🗑️ Xóa #{idx}", callback_data=f"del_sub_{rowid}")
            ])

        warning_box = (
            "\n\n🚨 <b>CHÚ Ý QUAN TRỌNG:</b>\n"
            "<i>Trang dùng link <code>facebook.com</code> trực tiếp sẽ bị chặn quét. Hãy bấm <b>🔗 Đổi Link</b> để dùng link <code>rss.app</code>!</i>"
            if has_fb_warning else ""
        )

        text = (
            "📋 <b>QUẢN LÝ CÁC TRANG ĐANG THEO DÕI</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n\n".join(items)
            + warning_box +
            "\n\n👇 <i>Chọn thao tác tương ứng bên dưới:</i>"
        )

    keyboard.append([InlineKeyboardButton("➕ Thêm trang mới nhanh", callback_data="add_sub_prompt")])
    keyboard.append([InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")])
    return text, InlineKeyboardMarkup(keyboard)

def build_saved_posts_view(chat_id: int):
    conn = sqlite3.connect('huit_bot.db')
    c = conn.cursor()
    c.execute("SELECT id, page_name, summary, post_link FROM saved_posts WHERE chat_id=? ORDER BY id DESC LIMIT 8", (chat_id,))
    rows = c.fetchall()
    conn.close()

    keyboard = []
    if not rows:
        text = (
            "📌 <b>TỦ LƯU TRỮ SỰ KIỆN ĐRL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "Bạn chưa lưu sự kiện nào.\n"
            "💡 <i>Bấm nút <b>[🌟 Lưu sự kiện này]</b> dưới các bài đăng để cất vào đây nhé!</i>"
        )
    else:
        items = []
        for idx, (saved_id, page, summary, link) in enumerate(rows, 1):
            first_line = summary.split("\n")[0] if summary else "Sự kiện ĐRL"
            items.append(
                f"<b>{idx}. [{page}]</b> {first_line}\n"
                f"🔗 <a href='{link}'>Mở bài viết / Form</a>"
            )
            keyboard.append([InlineKeyboardButton(f"🗑️ Bỏ lưu mục #{idx}", callback_data=f"del_saved_{saved_id}")])

        text = (
            "📌 <b>CÁC SỰ KIỆN ĐÃ LƯU</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n\n".join(items)
        )

    keyboard.append([InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")])
    return text, InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    msg = get_welcome_text()
    if update.message:
        await update.message.reply_text(msg, reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
    elif update.callback_query:
        query = update.callback_query
        await query.answer()
        try:
            await query.edit_message_text(msg, reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
        except Exception:
            pass

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")]])

    try:
        if data == "menu_list":
            context.user_data.clear()
            text, kb = build_subscription_list_view(chat_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

        elif data.startswith("del_sub_"):
            rowid = int(data.split("_")[2])
            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions WHERE rowid=? AND chat_id=?", (rowid, chat_id))
            conn.commit()
            conn.close()

            text, kb = build_subscription_list_view(chat_id)
            await query.edit_message_text(
                "✅ <i>Đã xóa trang thành công!</i>\n\n" + text,
                reply_markup=kb,
                parse_mode=ParseMode.HTML
            )

        elif data.startswith("edit_name_"):
            rowid = int(data.split("_")[2])
            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("SELECT page_name, feed_url FROM subscriptions WHERE rowid=? AND chat_id=?", (rowid, chat_id))
            row = c.fetchone()
            conn.close()

            if not row:
                await query.edit_message_text("⚠️ Trang này không còn tồn tại.", reply_markup=back_kb)
                return

            context.user_data["action"] = "rename_sub"
            context.user_data["target_rowid"] = rowid

            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy bỏ", callback_data="menu_list")]])
            await query.edit_message_text(
                f"✏️ <b>ĐỔI TÊN TRANG THEO DÕI</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔹 Tên hiện tại: <b>{row[0]}</b>\n\n"
                f"👉 <b>Gõ tên mới vào khung chat:</b>",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.HTML
            )

        elif data.startswith("edit_url_"):
            rowid = int(data.split("_")[2])
            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("SELECT page_name, feed_url FROM subscriptions WHERE rowid=? AND chat_id=?", (rowid, chat_id))
            row = c.fetchone()
            conn.close()

            if not row:
                await query.edit_message_text("⚠️ Trang này không còn tồn tại.", reply_markup=back_kb)
                return

            context.user_data["action"] = "change_url_sub"
            context.user_data["target_rowid"] = rowid

            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy bỏ", callback_data="menu_list")]])
            await query.edit_message_text(
                f"🔗 <b>ĐỔI LINK RSS CHO TRANG: {row[0]}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔹 Link cũ: <code>{row[1]}</code>\n\n"
                f"👉 <b>Dán Link RSS mới (bắt đầu bằng http...) vào khung chat:</b>",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.HTML
            )

        elif data == "add_sub_prompt":
            context.user_data["action"] = "add_sub"
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy bỏ", callback_data="menu_list")]])
            await query.edit_message_text(
                "➕ <b>THÊM TRANG THEO DÕI MỚI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "👉 Gõ theo định dạng:\n<code>Tên_Trang Link_RSS</code>\n\n"
                "📌 <b>Ví dụ:</b>\n<code>DoanHoi_HUIT https://rss.app/feeds/xxxx.xml</code>",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.HTML
            )

        elif data == "menu_saved":
            context.user_data.clear()
            text, kb = build_saved_posts_view(chat_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        elif data.startswith("del_saved_"):
            saved_id = int(data.split("_")[2])
            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("DELETE FROM saved_posts WHERE id=? AND chat_id=?", (saved_id, chat_id))
            conn.commit()
            conn.close()
            text, kb = build_saved_posts_view(chat_id)
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        elif data == "save_this_post":
            msg_text = query.message.text or ""
            lines = [l.strip() for l in msg_text.split("\n") if l.strip()]
            page_name = "HUIT"
            if lines and "từ " in lines[0]:
                page_name = lines[0].split("từ ")[-1].replace("(Bản Demo)", "").strip()
            summary = "\n".join(lines[2:6]) if len(lines) > 2 else msg_text[:200]

            post_link = "https://www.facebook.com/DhCongThuongHCM"
            if query.message.reply_markup:
                for row in query.message.reply_markup.inline_keyboard:
                    for btn in row:
                        if btn.url:
                            post_link = btn.url
                            break

            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("INSERT INTO saved_posts (chat_id, page_name, summary, post_link) VALUES (?, ?, ?, ?)",
                      (chat_id, page_name, summary, post_link))
            conn.commit()
            conn.close()
            await context.bot.send_message(
                chat_id=chat_id,
                text="🌟 <b>Đã lưu vào [📌 Tủ ĐRL đã lưu]!</b>",
                parse_mode=ParseMode.HTML
            )

        elif data == "menu_search_prompt":
            context.user_data["action"] = "search_post"
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy bỏ", callback_data="menu_back")]])
            await query.edit_message_text(
                "🔎 <b>TÌM KIẾM BÀI ĐĂNG CŨ</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "👉 <b>Gõ từ khóa vào khung chat (VD: hội thảo, tình nguyện, CNTT...):</b>",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.HTML
            )

        elif data == "menu_drl":
            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("SELECT only_drl FROM user_settings WHERE chat_id=?", (chat_id,))
            row = c.fetchone()
            current_status = row[0] if row else 0
            new_status = 1 if current_status == 0 else 0
            c.execute("INSERT OR REPLACE INTO user_settings (chat_id, only_drl) VALUES (?, ?)", (chat_id, new_status))
            conn.commit()
            conn.close()

            if new_status == 1:
                status_str = "🟢 <b>CHỈ NHẬN BÀI CÓ ĐRL</b>\n<i>(Bot lọc bỏ các bài vinh danh, thông báo chung, chỉ gửi sự kiện/khảo sát có điểm)</i>"
            else:
                status_str = "⚪ <b>NHẬN TẤT CẢ BÀI ĐĂNG MỚI</b>\n<i>(Nhận mọi bài đăng từ Fanpage, kể cả bài chúc mừng, vinh danh)</i>"

            mode_toggle_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Đổi chế độ lọc", callback_data="menu_drl")],
                [InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")]
            ])
            await query.edit_message_text(
                f"🎯 <b>CÀI ĐẶT CHẾ ĐỘ LỌC BÀI ĐĂNG</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Trạng thái hiện tại:\n{status_str}",
                reply_markup=mode_toggle_kb,
                parse_mode=ParseMode.HTML
            )

        elif data == "menu_help":
            help_text = (
                "📖 <b>HƯỚNG DẪN CÁCH LẤY LINK RSS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔹 <b>Bước 1:</b> Truy cập <a href='https://rss.app'>rss.app</a>\n"
                "🔹 <b>Bước 2:</b> Dán link Fanpage Facebook vào ➔ Bấm <b>Generate</b>.\n"
                "🔹 <b>Bước 3:</b> Copy link có đuôi <code>.xml</code>.\n"
                "🔹 <b>Bước 4:</b> Vào <b>📋 Quản lý trang</b> ➔ Bấm <b>🔗 Đổi Link</b> để dán vào."
            )
            await query.edit_message_text(help_text, reply_markup=back_kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        elif data == "menu_checknow":
            await query.edit_message_text(
                "⏳ <b>Đang quét ngầm toàn bộ các trang...</b>",
                reply_markup=back_kb,
                parse_mode=ParseMode.HTML
            )
            asyncio.create_task(check_feeds_job_manual(context, chat_id))

        elif data == "menu_demo":
            await query.edit_message_text(
                "🧪 <b>Đang tạo bài đăng mẫu HUIT...</b>",
                reply_markup=back_kb,
                parse_mode=ParseMode.HTML
            )
            asyncio.create_task(send_demo_post(context, chat_id))

        elif data == "menu_back":
            context.user_data.clear()
            await query.edit_message_text(get_welcome_text(), reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)

    except Exception as e:
        logging.error(f"Menu error: {e}")

async def handle_user_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("action")
    if not action:
        return

    chat_id = update.effective_chat.id
    text_input = update.message.text.strip()

    if action == "rename_sub":
        rowid = context.user_data.get("target_rowid")
        new_name = text_input.replace("\n", " ")[:40]

        conn = sqlite3.connect('huit_bot.db')
        c = conn.cursor()
        c.execute("UPDATE subscriptions SET page_name=? WHERE rowid=? AND chat_id=?", (new_name, rowid, chat_id))
        conn.commit()
        conn.close()

        context.user_data.clear()
        list_text, kb = build_subscription_list_view(chat_id)
        await update.message.reply_text(
            f"✅ Đã đổi tên thành: <b>{new_name}</b>!\n\n{list_text}",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    elif action == "change_url_sub":
        rowid = context.user_data.get("target_rowid")
        new_url = text_input.split()[0]

        if not new_url.startswith("http"):
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy bỏ", callback_data="menu_list")]])
            await update.message.reply_text(
                "⚠️ Link phải bắt đầu bằng <code>http://</code> hoặc <code>https://</code>. Gửi lại:",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.HTML
            )
            return

        conn = sqlite3.connect('huit_bot.db')
        c = conn.cursor()
        c.execute("SELECT page_name FROM subscriptions WHERE rowid=? AND chat_id=?", (rowid, chat_id))
        row = c.fetchone()
        page_name = row[0] if row else "Trang"
        c.execute("UPDATE subscriptions SET feed_url=? WHERE rowid=? AND chat_id=?", (new_url, rowid, chat_id))
        conn.commit()
        conn.close()

        context.user_data.clear()
        list_text, kb = build_subscription_list_view(chat_id)
        await update.message.reply_text(
            f"✅ Đã cập nhật Link mới cho <b>{page_name}</b>!\n\n{list_text}",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    elif action == "add_sub":
        parts = text_input.split()
        if len(parts) < 2 or not parts[-1].startswith("http"):
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy bỏ", callback_data="menu_list")]])
            await update.message.reply_text(
                "⚠️ Định dạng sai. Nhập: <code>Tên_Trang Link_RSS</code>",
                reply_markup=cancel_kb,
                parse_mode=ParseMode.HTML
            )
            return

        feed_url = parts[-1]
        page_name = " ".join(parts[:-1])
        context.user_data.clear()

        wait_msg = await update.message.reply_text(f"⏳ Đang thêm trang <b>{page_name}</b>...", parse_mode=ParseMode.HTML)
        feed = await asyncio.to_thread(fetch_feed_sync, feed_url)

        conn = sqlite3.connect('huit_bot.db')
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO subscriptions VALUES (?, ?, ?)", (chat_id, feed_url, page_name))
        for entry in feed.entries[:10]:
            post_id = entry.get('id', entry.get('link'))
            if post_id:
                c.execute("INSERT OR IGNORE INTO seen_posts VALUES (?, ?, ?, ?)",
                          (f"{chat_id}_{post_id}", page_name, entry.get('title', ''), entry.get('link', '')))
        conn.commit()
        conn.close()

        list_text, kb = build_subscription_list_view(chat_id)
        await wait_msg.edit_text(
            f"✅ Đã thêm <b>{page_name}</b> thành công!\n\n{list_text}",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    elif action == "search_post":
        kw = text_input.lower()
        context.user_data.clear()
        back_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔎 Tìm từ khóa khác", callback_data="menu_search_prompt")],
            [InlineKeyboardButton("🔙 Quay lại Menu chính", callback_data="menu_back")]
        ])

        conn = sqlite3.connect('huit_bot.db')
        c = conn.cursor()
        c.execute("SELECT page_name, summary, post_link FROM seen_posts WHERE lower(summary) LIKE ? LIMIT 5", (f"%{kw}%",))
        rows = c.fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text(
                f"📭 Không tìm thấy bài đăng nào khớp với từ khóa: <b>{kw}</b>",
                reply_markup=back_kb,
                parse_mode=ParseMode.HTML
            )
            return

        msg = f"🔎 <b>Kết quả tìm kiếm cho '{kw}':</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(
            [f"📌 <b>[{p}]</b>\n{s[:140]}...\n🔗 <a href='{l}'>Xem bài viết</a>" for p, s, l in rows]
        )
        await update.message.reply_text(msg, reply_markup=back_kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def send_demo_post(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    sample_post_text = (
        "[THÔNG BÁO ĐĂNG KÝ HỘI THẢO CHUYÊN ĐỀ AI & CHUYỂN ĐỔI SỐ 2026]\n"
        "Đoàn Thanh niên - Hội Sinh viên Trường ĐH Công Thương TP.HCM (HUIT) tổ chức buổi hội thảo kỹ năng ứng dụng AI.\n"
        "- Thời gian: 08h00 Chủ Nhật, ngày 15/11/2026\n"
        "- Quyền lợi: Cộng 5 Điểm Rèn Luyện (Mục 1) cho sinh viên tham gia đầy đủ.\n"
        "- Link form: https://forms.gle/HUITDemoForm2026"
    )
    sample_links = ["https://forms.gle/HUITDemoForm2026"]
    sample_fb_url = "https://www.facebook.com/DhCongThuongHCM"

    ai_result = await asyncio.to_thread(analyze_post_sync, sample_post_text, sample_links)
    is_drl = "[CO_DRL]" in ai_result
    summary_clean = ai_result.replace("[CO_DRL]", "").replace("[KHONG_DRL]", "").strip()

    keyboard = [
        [InlineKeyboardButton("🌐 Mở bài gốc", url=sample_fb_url)],
        [InlineKeyboardButton("📝 ĐIỀN FORM ĐĂNG KÝ NGAY!", url=sample_links[0])],
        [
            InlineKeyboardButton("🌟 Lưu sự kiện này", callback_data="save_this_post"),
            InlineKeyboardButton("🔙 Menu Chính", callback_data="menu_back")
        ]
    ]

    demo_message = (
        "🔥 <b>[SĂN ĐIỂM RÈN LUYỆN]</b> từ <b>DoanHoi_HUIT</b> <i>(Bản Demo)</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{summary_clean}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Bấm [🌟 Lưu sự kiện này] để cất vào Tủ lưu trữ!</i>"
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=demo_message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

async def check_feeds_job_manual(context: ContextTypes.DEFAULT_TYPE, target_chat_id=None):
    conn = sqlite3.connect('huit_bot.db')
    c = conn.cursor()
    if target_chat_id:
        c.execute("SELECT chat_id, feed_url, page_name FROM subscriptions WHERE chat_id=?", (target_chat_id,))
    else:
        c.execute("SELECT chat_id, feed_url, page_name FROM subscriptions")
    subs = c.fetchall()
    conn.close()

    if target_chat_id and not subs:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text="⚠️ Bạn chưa thêm trang nào để quét!",
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        )
        return

    new_post_count = 0
    for chat_id, feed_url, page_name in subs:
        if is_direct_facebook_url(feed_url):
            continue

        feed = await asyncio.to_thread(fetch_feed_sync, feed_url)

        conn = sqlite3.connect('huit_bot.db')
        c = conn.cursor()
        c.execute("SELECT only_drl FROM user_settings WHERE chat_id=?", (chat_id,))
        setting = c.fetchone()
        only_drl = setting[0] if setting else 0
        conn.close()

        for entry in reversed(feed.entries[:5]):
            post_id = entry.get('id', entry.get('link'))
            if not post_id:
                continue
            unique_key = f"{chat_id}_{post_id}"

            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("SELECT 1 FROM seen_posts WHERE post_id=?", (unique_key,))
            already_seen = c.fetchone()
            conn.close()

            if already_seen:
                continue

            raw_html = entry.get('description', '') or entry.get('summary', '') or entry.get('title', '')
            clean_text, extracted_links, image_urls = parse_entry_content(raw_html)
            post_link = entry.get('link', '')

            comment_links = await asyncio.to_thread(scrape_links_sync, post_link)
            all_links = list(dict.fromkeys(extracted_links + comment_links))

            ai_result = await asyncio.to_thread(analyze_post_sync, clean_text, all_links, image_urls)
            is_drl = "[CO_DRL]" in ai_result
            summary_clean = ai_result.replace("[CO_DRL]", "").replace("[KHONG_DRL]", "").strip()

            # --- SỬA LỖI QUAN TRỌNG: LỌC CHUẨN XÁC THEO CÀI ĐẶT CỦA NGƯỜI DÙNG ---
            if only_drl == 1 and not is_drl:
                continue

            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO seen_posts VALUES (?, ?, ?, ?)", (unique_key, page_name, summary_clean, post_link))
            conn.commit()
            conn.close()

            new_post_count += 1
            keyboard = []
            if post_link:
                keyboard.append([InlineKeyboardButton("🌐 Mở bài gốc", url=post_link)])

            for idx, link in enumerate(all_links[:5], 1):
                if link != post_link:
                    is_form = any(f in link for f in ["forms.gle", "docs.google.com/forms", "office.com", "bit.ly"])
                    is_zalo = "zalo.me" in link
                    if is_form:
                        label = f"📝 ĐIỀN FORM ĐĂNG KÝ ({idx})!"
                    elif is_zalo:
                        label = f"💬 VÀO NHÓM ZALO ({idx})"
                    else:
                        label = f"🔗 Link đính kèm ({idx})"
                    keyboard.append([InlineKeyboardButton(label, url=link)])

            if is_drl:
                keyboard.append([InlineKeyboardButton("🌟 Lưu sự kiện này", callback_data="save_this_post")])

            reply_markup = InlineKeyboardMarkup(keyboard)
            badge = "🔥 <b>[SĂN ĐIỂM RÈN LUYỆN]</b>" if is_drl else "📢 <b>[BÀI ĐĂNG MỚI]</b>"
            message = f"{badge} từ <b>{page_name}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n{summary_clean}"

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logging.error(f"Send Error: {e}")

    if target_chat_id and new_post_count == 0:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text="✅ <b>Đã quét xong!</b> Không có bài đăng mới nào thỏa mãn bộ lọc hiện tại.",
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        )

async def check_feeds_job(context: ContextTypes.DEFAULT_TYPE):
    await check_feeds_job_manual(context)

if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_text_input))

    app.job_queue.run_repeating(check_feeds_job, interval=CHECK_INTERVAL, first=5)

    print("🚀 Bot HUIT PRO v3.2 (Fix lọc ĐRL tuyệt đối + Chống ảo giác AI) đang chạy 24/7... Nhấn Ctrl+C để dừng.")
    app.run_polling()