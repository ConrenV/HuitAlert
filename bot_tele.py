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

if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

env_path = Path(__file__).resolve().parent / '.env'
load_dotenv(dotenv_path=env_path)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHECK_INTERVAL = 30

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("❌ LỖI: Thiếu TELEGRAM_TOKEN hoặc GEMINI_API_KEY trong file .env!")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
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
        "QUY TẮC PHÂN LOẠI:\n"
        "1. Dòng đầu tiên chỉ ghi đúng 1 từ: [CO_DRL] CHỈ KHI bài viết thông báo rõ ràng về việc CỘNG ĐIỂM RÈN LUYỆN, tham gia hoạt động tình nguyện, hội thảo học thuật, khảo sát, hoặc có form đăng ký tham gia sự kiện. Nếu là bài vinh danh, chúc mừng, thông báo chung không có điểm thì ghi [KHONG_DRL].\n"
        "2. Tóm tắt ngắn gọn theo thực tế:\n"
        "📌 Hoạt động: (Tên sự kiện)\n"
        "⏰ Thời gian / Địa điểm: (Nếu có)\n"
        "🎁 Quyền lợi: (Số ĐRL hoặc 'Không có')\n"
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

def get_main_menu_keyboard(chat_id: int):
    conn = sqlite3.connect('huit_bot.db')
    c = conn.cursor()
    c.execute("SELECT only_drl FROM user_settings WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    current_drl = row[0] if row else 0

    drl_label = "🟢 Chế độ: Chỉ nhận bài ĐRL" if current_drl == 1 else "⚪ Chế độ: Nhận tất cả bài"

    keyboard = [
        [InlineKeyboardButton("📋 Quản lý trang (Thêm/Sửa/Xóa)", callback_data="menu_list")],
        [InlineKeyboardButton("📌 Tủ ĐRL đã lưu", callback_data="menu_saved")],
        [InlineKeyboardButton(drl_label, callback_data="menu_toggle_drl")],
        [InlineKeyboardButton("📖 Hướng dẫn lấy link FetchRSS", callback_data="menu_help")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_welcome_text():
    return (
        "🎓 <b>BOT SĂN ĐIỂM RÈN LUYỆN HUIT v1.0</b> 🎓\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <i>Hệ thống tự động canh bài mới và lọc sự kiện ĐRL 24/7.</i>\n\n"
        "👇 <b>Chọn chức năng bên dưới:</b>"
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
        text = "📭 <b>DANH SÁCH TRỐNG</b>\nBạn chưa theo dõi trang nào. Hãy bấm <b>➕ Thêm trang mới</b>."
    else:
        items = []
        for idx, (rowid, name, url) in enumerate(rows, 1):
            if is_direct_facebook_url(url):
                has_fb_warning = True
                status_badge = "⚠️ <i>(Cần đổi sang FetchRSS)</i>"
            else:
                status_badge = "✅"

            items.append(f"{idx}. <b>{name}</b> {status_badge}\n   🔗 <code>{url}</code>")
            keyboard.append([
                InlineKeyboardButton(f"✏️ Sửa #{idx}", callback_data=f"edit_name_{rowid}"),
                InlineKeyboardButton(f"🔗 Link #{idx}", callback_data=f"edit_url_{rowid}"),
                InlineKeyboardButton(f"🗑️ Xóa #{idx}", callback_data=f"del_sub_{rowid}")
            ])

        warning_box = "\n\n🚨 <i>Lưu ý: Trang dùng link Facebook trực tiếp sẽ bị chặn. Hãy bấm nút tương ứng để đổi link FetchRSS!</i>" if has_fb_warning else ""
        text = "📋 <b>DANH SÁCH TRANG THEO DÕI</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(items) + warning_box

    keyboard.append([InlineKeyboardButton("➕ Thêm trang mới (Hỗ trợ nhiều dòng)", callback_data="add_sub_prompt")])
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
        text = "📌 <b>TỦ ĐÃ LƯU</b>\nBạn chưa lưu sự kiện nào."
    else:
        items = []
        for idx, (saved_id, page, summary, link) in enumerate(rows, 1):
            first_line = summary.split("\n")[0] if summary else "Sự kiện"
            items.append(f"<b>{idx}. [{page}]</b> {first_line}\n🔗 <a href='{link}'>Xem bài viết</a>")
            keyboard.append([InlineKeyboardButton(f"🗑️ Xóa mục #{idx}", callback_data=f"del_saved_{saved_id}")])
        text = "📌 <b>SỰ KIỆN ĐÃ LƯU</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(items)

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
        query = update.callback_query
        await query.answer()
        try:
            await query.edit_message_text(msg, reply_markup=kb, parse_mode=ParseMode.HTML)
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
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        elif data.startswith("del_sub_"):
            rowid = int(data.split("_")[2])
            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("DELETE FROM subscriptions WHERE rowid=? AND chat_id=?", (rowid, chat_id))
            conn.commit()
            conn.close()
            text, kb = build_subscription_list_view(chat_id)
            await query.edit_message_text("✅ Đã xóa thành công!\n\n" + text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        elif data.startswith("edit_name_"):
            rowid = int(data.split("_")[2])
            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("SELECT page_name FROM subscriptions WHERE rowid=? AND chat_id=?", (rowid, chat_id))
            row = c.fetchone()
            conn.close()
            if not row:
                return
            context.user_data["action"] = "rename_sub"
            context.user_data["target_rowid"] = rowid
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy", callback_data="menu_list")]])
            await query.edit_message_text(f"✏️ Đổi tên cho trang (Hiện tại: <b>{row[0]}</b>):\n\nGõ tên mới vào khung chat:", reply_markup=cancel_kb, parse_mode=ParseMode.HTML)

        elif data.startswith("edit_url_"):
            rowid = int(data.split("_")[2])
            context.user_data["action"] = "change_url_sub"
            context.user_data["target_rowid"] = rowid
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy", callback_data="menu_list")]])
            await query.edit_message_text("🔗 Dán Link FetchRSS mới vào khung chat:", reply_markup=cancel_kb, parse_mode=ParseMode.HTML)

        elif data == "add_sub_prompt":
            context.user_data["action"] = "add_sub"
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Hủy", callback_data="menu_list")]])
            await query.edit_message_text(
                "➕ <b>THÊM TRANG THEO DÕI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Gõ theo định dạng (hỗ trợ nhiều dòng cùng lúc):\n"
                "<code>Tên_Trang Link_RSS</code>\n\n"
                "👉 Gửi vào khung chat:",
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
            page_name = lines[0].split("từ ")[-1] if lines and "từ " in lines[0] else "HUIT"
            summary = "\n".join(lines[1:5]) if len(lines) > 1 else msg_text[:200]
            post_link = "https://www.facebook.com"
            if query.message.reply_markup:
                for row in query.message.reply_markup.inline_keyboard:
                    for btn in row:
                        if btn.url:
                            post_link = btn.url
                            break
            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("INSERT INTO saved_posts (chat_id, page_name, summary, post_link) VALUES (?, ?, ?, ?)", (chat_id, page_name, summary, post_link))
            conn.commit()
            conn.close()
            await context.bot.send_message(chat_id=chat_id, text="🌟 Đã lưu vào Tủ!", parse_mode=ParseMode.HTML)

        elif data == "menu_toggle_drl":
            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("SELECT only_drl FROM user_settings WHERE chat_id=?", (chat_id,))
            row = c.fetchone()
            current = row[0] if row else 0
            new_val = 0 if current == 1 else 1
            c.execute("INSERT OR REPLACE INTO user_settings (chat_id, only_drl) VALUES (?, ?)", (chat_id, new_val))
            conn.commit()
            conn.close()

            new_kb = get_main_menu_keyboard(chat_id)
            await query.edit_message_text(get_welcome_text(), reply_markup=new_kb, parse_mode=ParseMode.HTML)

        elif data == "menu_help":
            help_text = (
                "📖 <b>HƯỚNG DẪN LẤY LINK FETCHRSS</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "1. Vào <a href='https://fetchrss.com'>fetchrss.com</a> đăng nhập tài khoản.\n"
                "2. Dán link Fanpage Facebook vào và tạo RSS.\n"
                "3. Copy link RSS nhận được dán vào bot là xong."
            )
            await query.edit_message_text(help_text, reply_markup=back_kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        elif data == "menu_back":
            context.user_data.clear()
            msg = get_welcome_text()
            kb = get_main_menu_keyboard(chat_id)
            await query.edit_message_text(msg, reply_markup=kb, parse_mode=ParseMode.HTML)

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
        new_name = text_input[:40]
        conn = sqlite3.connect('huit_bot.db')
        c = conn.cursor()
        c.execute("UPDATE subscriptions SET page_name=? WHERE rowid=? AND chat_id=?", (new_name, rowid, chat_id))
        conn.commit()
        conn.close()
        context.user_data.clear()
        text, kb = build_subscription_list_view(chat_id)
        await update.message.reply_text(f"✅ Đã đổi tên thành: <b>{new_name}</b>\n\n{text}", reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    elif action == "change_url_sub":
        rowid = context.user_data.get("target_rowid")
        new_url = text_input.split()[0]
        conn = sqlite3.connect('huit_bot.db')
        c = conn.cursor()
        c.execute("UPDATE subscriptions SET feed_url=? WHERE rowid=? AND chat_id=?", (new_url, rowid, chat_id))
        conn.commit()
        conn.close()
        context.user_data.clear()
        text, kb = build_subscription_list_view(chat_id)
        await update.message.reply_text(f"✅ Đã cập nhật Link mới!\n\n{text}", reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    elif action == "add_sub":
        lines = text_input.split("\n")
        context.user_data.clear()
        wait_msg = await update.message.reply_text("⏳ Đang xử lý thêm trang...", parse_mode=ParseMode.HTML)

        added_count = 0
        conn = sqlite3.connect('huit_bot.db')
        c = conn.cursor()

        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[-1].startswith("http"):
                feed_url = parts[-1]
                page_name = " ".join(parts[:-1])
                try:
                    c.execute("INSERT INTO subscriptions VALUES (?, ?, ?)", (chat_id, feed_url, page_name))
                    feed = fetch_feed_sync(feed_url)
                    for entry in feed.entries[:5]:
                        post_id = entry.get('id', entry.get('link'))
                        if post_id:
                            c.execute("INSERT OR IGNORE INTO seen_posts VALUES (?, ?, ?, ?)", (f"{chat_id}_{post_id}", page_name, entry.get('title', ''), entry.get('link', '')))
                    added_count += 1
                except sqlite3.IntegrityError:
                    pass # Tránh lỗi trùng lặp link RSS đã tồn tại

        conn.commit()
        conn.close()

        text, kb = build_subscription_list_view(chat_id)
        await wait_msg.edit_text(f"✅ Đã thêm thành công <b>{added_count}</b> trang mới!\n\n{text}", reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def check_feeds_job(context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('huit_bot.db')
    c = conn.cursor()
    c.execute("SELECT chat_id, feed_url, page_name FROM subscriptions")
    subs = c.fetchall()
    conn.close()

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

        for entry in reversed(feed.entries[:3]):
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

            if only_drl == 1 and not is_drl:
                conn = sqlite3.connect('huit_bot.db')
                c = conn.cursor()
                c.execute("INSERT OR REPLACE INTO seen_posts VALUES (?, ?, ?, ?)", (unique_key, page_name, summary_clean, post_link))
                conn.commit()
                conn.close()
                continue

            conn = sqlite3.connect('huit_bot.db')
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO seen_posts VALUES (?, ?, ?, ?)", (unique_key, page_name, summary_clean, post_link))
            conn.commit()
            conn.close()

            keyboard = []
            if post_link:
                keyboard.append([InlineKeyboardButton("🌐 Mở bài gốc", url=post_link)])

            for idx, link in enumerate(all_links[:5], 1):
                if link != post_link:
                    is_form = any(f in link for f in ["forms.gle", "docs.google.com/forms", "office.com", "bit.ly"])
                    is_zalo = "zalo.me" in link
                    label = f"📝 ĐIỀN FORM ({idx})" if is_form else (f"💬 ZALO ({idx})" if is_zalo else f"🔗 Link ({idx})")
                    keyboard.append([InlineKeyboardButton(label, url=link)])

            keyboard.append([InlineKeyboardButton("🌟 Lưu sự kiện này", callback_data="save_this_post")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            badge = "🔥 <b>[SĂN ĐIỂM RÈN LUYỆN]</b>" if is_drl else "📢 <b>[BÀI ĐĂNG MỚI]</b>"
            message = f"{badge} từ <b>{page_name}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n{summary_clean}"

            try:
                await context.bot.send_message(chat_id=chat_id, text=message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            except Exception as e:
                logging.error(f"Send Error: {e}")

if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_text_input))

    app.job_queue.run_repeating(check_feeds_job, interval=CHECK_INTERVAL, first=5)

    print("🚀 Bot HUIT PRO v1.0 (Hoàn chỉnh tối ưu) đang chạy 24/7... Nhấn Ctrl+C để dừng.")
    app.run_polling()