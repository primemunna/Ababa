import os
import io
import json
import html
import time
import uuid
import threading
from datetime import datetime, timezone, timedelta

import qrcode
import telebot
from telebot import types

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "9249303434:AAFb1byPYPpmv49lEShyMgWV8LXS-XD8VX4")
ADMIN_PASSWORD = "7434"
DEVELOPER_USERNAME = "@ai_brain_box_2"
DEFAULT_UPI_ID = "8016922924@ibl"
MIN_ADD_RS = 10
MAX_ADD_RS = 1000

DATA_DIR = "data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
PRODUCTS_FILE = os.path.join(DATA_DIR, "products.json")
CHANNELS_FILE = os.path.join(DATA_DIR, "channels.json")
PAYMENTS_FILE = os.path.join(DATA_DIR, "payments.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

IST = timezone(timedelta(hours=5, minutes=30))
file_lock = threading.Lock()
state_lock = threading.Lock()

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# In-memory user state & logged-in admins
user_states = {}
logged_admins = set()


# ==================== FILE STORAGE HELPERS ====================
def ensure_data_files():
    os.makedirs(DATA_DIR, exist_ok=True)
    defaults = {
        USERS_FILE: {},
        PRODUCTS_FILE: {},
        CHANNELS_FILE: [],
        PAYMENTS_FILE: [],
        CONFIG_FILE: {"upi_id": DEFAULT_UPI_ID, "admin_ids": []},
    }
    for path, default_val in defaults.items():
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default_val, f, indent=2, ensure_ascii=False)


def load_json(path, default):
    with file_lock:
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default


def save_json(path, data):
    with file_lock:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)


def now_ist():
    dt = datetime.now(IST)
    return dt.strftime("%d-%m-%Y"), dt.strftime("%I:%M %p")


def format_size(num_bytes):
    if not num_bytes:
        return "Unknown"
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} TB"


def escape(text):
    return html.escape(str(text or ""))


def get_bot_username():
    return bot.get_me().username


# ==================== USER DATA HELPERS ====================
def get_user(uid, username=None, first_name=None):
    uid_str = str(uid)
    users = load_json(USERS_FILE, {})
    created = False
    if uid_str not in users:
        date_s, time_s = now_ist()
        users[uid_str] = {
            "id": uid,
            "username": username or "",
            "name": first_name or "User",
            "rs": 0.0,
            "coins": 0,
            "bonus_rs": 0.0,
            "bonus_coins": 0,
            "raffles": 0,
            "raffle_coins": 0,
            "raffle_history": [],
            "referred_by": None,
            "verified_once": False,
            "free_history": [],
            "paid_history": [],
            "join_date": date_s,
            "join_time": time_s,
        }
        created = True
    else:
        changed = False
        if username is not None and users[uid_str].get("username") != username:
            users[uid_str]["username"] = username
            changed = True
        if first_name is not None and users[uid_str].get("name") != first_name:
            users[uid_str]["name"] = first_name
            changed = True
        if changed:
            save_json(USERS_FILE, users)
    if created:
        save_json(USERS_FILE, users)
    return users[uid_str]


def update_user_field(uid, callback):
    uid_str = str(uid)
    users = load_json(USERS_FILE, {})
    if uid_str in users:
        callback(users[uid_str])
        save_json(USERS_FILE, users)
        return users[uid_str]
    return None


def set_state(uid, action, **kwargs):
    with state_lock:
        user_states[str(uid)] = {"action": action, **kwargs}


def get_state(uid):
    with state_lock:
        return user_states.get(str(uid))


def clear_state(uid):
    with state_lock:
        user_states.pop(str(uid), None)


# ==================== KEYBOARDS ====================
USER_MENU_BUTTONS = [
    "Free Product", "Paid Product",
    "Raffle Leaderboard",
    "Raffle", "Buy History",
    "Wallet",
    "Developer", "Help Support",
    "Admin Login"
]

ADMIN_MENU_BUTTONS = [
    "Add Channel", "Manage Channel",
    "Add Product", "Manage Product",
    "Users", "Bonus Send",
    "Notification Send", "Payment Request",
    "Raffle Leaderboard (Admin)", "Coin Leaderboard",
    "Rs Leaderboard", "Add Money History",
    "UPI Settings", "User Panel"
]


def user_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("Free Product", "Paid Product")
    markup.row("Raffle Leaderboard")
    markup.row("Raffle", "Buy History")
    markup.row("Wallet")
    markup.row("Developer", "Help Support")
    markup.row("Admin Login")
    return markup


def admin_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.row("Add Channel", "Manage Channel")
    markup.row("Add Product", "Manage Product")
    markup.row("Users", "Bonus Send")
    markup.row("Notification Send", "Payment Request")
    markup.row("Raffle Leaderboard (Admin)", "Coin Leaderboard")
    markup.row("Rs Leaderboard", "Add Money History")
    markup.row("UPI Settings", "User Panel")
    return markup


# ==================== CHANNEL FORCE JOIN ====================
def check_user_channels(uid):
    channels = load_json(CHANNELS_FILE, [])
    not_joined = []
    for ch in channels:
        chat_id = ch["chat_id"]
        try:
            member = bot.get_chat_member(chat_id, uid)
            if member.status not in ("member", "administrator", "creator"):
                not_joined.append(ch)
        except Exception:
            # If bot is not admin or channel invalid, treat as not joined or skip safely
            not_joined.append(ch)
    return not_joined


def send_force_join_message(chat_id, channels):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in channels:
        link = ch.get("link", "")
        name = ch.get("name", "Join Channel")
        if link:
            markup.add(types.InlineKeyboardButton(f"📢 {name}", url=link))
    markup.add(types.InlineKeyboardButton("✅ Verified", callback_data="verify_join"))
    bot.send_message(
        chat_id,
        "🔒 <b>Channel Verification Required</b>\n\n"
        "বটটি ব্যবহার করতে নিচের সব চ্যানেলে জয়েন করুন এবং তারপর <b>Verified</b> বাটনে ক্লিক করুন:",
        reply_markup=markup,
    )


def finalize_verification(uid, chat_id):
    users = load_json(USERS_FILE, {})
    uid_str = str(uid)
    user = users.get(uid_str)
    if user and not user.get("verified_once"):
        user["verified_once"] = True
        ref_id = user.get("referred_by")
        if ref_id and str(ref_id) in users and str(ref_id) != uid_str:
            ref_user = users[str(ref_id)]
            ref_user["raffles"] = ref_user.get("raffles", 0) + 1
            ref_user["raffle_coins"] = ref_user.get("raffle_coins", 0) + 1
            ref_user["coins"] = ref_user.get("coins", 0) + 1
            date_s, time_s = now_ist()
            uname = f"@{user['username']}" if user.get("username") else user.get("name", str(uid))
            ref_user.setdefault("raffle_history", []).append({
                "user_id": uid,
                "username": uname,
                "date": date_s,
                "time": time_s,
            })
            try:
                bot.send_message(
                    int(ref_id),
                    f"🎉 <b>New Raffle Completed!</b>\n"
                    f"👤 User: {escape(uname)}\n"
                    f"🪙 Reward: <b>+1 Coin</b>",
                )
            except Exception:
                pass
        save_json(USERS_FILE, users)

    bot.send_message(
        chat_id,
        "✅ <b>Verified Successfully!</b>\nনিচের মেনু থেকে আপনার পছন্দের অপশন বেছে নিন:",
        reply_markup=user_main_keyboard(),
    )


# ==================== /START HANDLER ====================
@bot.message_handler(commands=["start"])
def handle_start(message):
    uid = message.from_user.id
    clear_state(uid)
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or "User"

    users = load_json(USERS_FILE, {})
    is_new = str(uid) not in users
    user = get_user(uid, username, first_name)

    # Check referral code if new user
    parts = message.text.split()
    if is_new and len(parts) > 1:
        ref_code = parts[1].strip()
        if ref_code.isdigit() and ref_code != str(uid) and ref_code in users:
            def set_ref(u):
                u["referred_by"] = int(ref_code)
            update_user_field(uid, set_ref)

    channels = load_json(CHANNELS_FILE, [])
    if channels:
        not_joined = check_user_channels(uid)
        if not_joined:
            send_force_join_message(message.chat.id, channels)
            return

    finalize_verification(uid, message.chat.id)


# ==================== DISPLAY HELPERS ====================
def send_raffle_info(chat_id, uid):
    user = get_user(uid)
    bot_uname = get_bot_username()
    link = f"https://t.me/{bot_uname}?start={uid}"
    text = (
        "🎟 <b>Raffle & Earn Coins</b>\n\n"
        f"🔗 <b>Your Raffle Link:</b>\n<code>{escape(link)}</code>\n\n"
        f"🪙 <b>Per Raffle:</b> 1 Coin\n"
        f"👥 <b>Total Raffle:</b> {user.get('raffles', 0)}\n"
        f"💰 <b>Total Raffle Coins:</b> {user.get('raffle_coins', 0)} Coins\n"
        f"💳 <b>Current Coin Balance:</b> {user.get('coins', 0)} Coins"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📜 Raffle History", callback_data="raffle_history"))
    bot.send_message(chat_id, text, reply_markup=markup)


def send_product_list(chat_id, ptype):
    products = load_json(PRODUCTS_FILE, {})
    filtered = [p for p in products.values() if p.get("type") == ptype]
    if not filtered:
        label = "Free" if ptype == "free" else "Paid"
        bot.send_message(chat_id, f"❌ বর্তমানে কোনো <b>{label} Product</b> নেই।")
        return

    markup = types.InlineKeyboardMarkup(row_width=1)
    for p in filtered:
        if ptype == "free":
            price_txt = f"{int(p['price'])} Coins"
        else:
            price_txt = f"₹{p['price']}"
        markup.add(
            types.InlineKeyboardButton(
                f"{p['title']} — {price_txt}",
                callback_data=f"prod_view:{p['id']}",
            )
        )
    header = "🎁 <b>Free Products</b> (Select to view):" if ptype == "free" else "💎 <b>Paid Products</b> (Select to view):"
    bot.send_message(chat_id, header, reply_markup=markup)


def send_product_detail(chat_id, prod_id):
    products = load_json(PRODUCTS_FILE, {})
    p = products.get(prod_id)
    if not p:
        bot.send_message(chat_id, "❌ Product পাওয়া যায়নি।")
        return

    images = p.get("images", [])
    if len(images) == 1:
        try:
            bot.send_photo(chat_id, images[0])
        except Exception:
            pass
    elif len(images) > 1:
        media = [types.InputMediaPhoto(img) for img in images[:10]]
        try:
            bot.send_media_group(chat_id, media)
        except Exception:
            pass

    if p["type"] == "free":
        price_str = f"{int(p['price'])} Coins"
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🛒 Buy", callback_data=f"prod_buy:{prod_id}"),
            types.InlineKeyboardButton("🪙 Earn Coin", callback_data="earn_coin"),
        )
    else:
        price_str = f"₹{p['price']}"
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🛒 Buy", callback_data=f"prod_buy:{prod_id}"),
            types.InlineKeyboardButton("➕ Add Rs", callback_data="add_rs"),
        )

    text = (
        f"📦 <b>Title:</b> {escape(p['title'])}\n"
        f"🏷 <b>Price:</b> {price_str}\n\n"
        f"📝 <b>Description:</b>\n{escape(p['description'])}"
    )
    bot.send_message(chat_id, text, reply_markup=markup)


def deliver_product_file(chat_id, product):
    title = escape(product["title"])
    delivery = product.get("delivery", {})
    dtype = delivery.get("kind")

    if dtype == "link":
        url = delivery.get("url", "")
        bot.send_message(
            chat_id,
            f"✅ <b>Purchase Successful!</b>\n\n"
            f"📦 <b>Title:</b> {title}\n"
            f"🔗 <b>Download Link:</b> {escape(url)}",
        )
    elif dtype == "document":
        fsize = format_size(delivery.get("file_size", 0))
        caption = f"✅ <b>Title:</b> {title}\n📁 <b>Size:</b> {fsize}"
        bot.send_document(chat_id, delivery["file_id"], caption=caption)
    elif dtype == "video":
        fsize = format_size(delivery.get("file_size", 0))
        caption = f"✅ <b>Title:</b> {title}\n📁 <b>Size:</b> {fsize}"
        bot.send_video(chat_id, delivery["file_id"], caption=caption)
    elif dtype == "audio":
        fsize = format_size(delivery.get("file_size", 0))
        caption = f"✅ <b>Title:</b> {title}\n📁 <b>Size:</b> {fsize}"
        bot.send_audio(chat_id, delivery["file_id"], caption=caption)
    elif dtype == "photo":
        fsize = format_size(delivery.get("file_size", 0))
        caption = f"✅ <b>Title:</b> {title}\n📁 <b>Size:</b> {fsize}"
        bot.send_photo(chat_id, delivery["file_id"], caption=caption)
    else:
        bot.send_message(chat_id, f"✅ <b>{title}</b> কেনা সম্পন্ন হয়েছে! (কোনো ফাইল সংযুক্ত নেই)")


def send_qr_and_ask_proof(chat_id, uid, amount):
    cfg = load_json(CONFIG_FILE, {"upi_id": DEFAULT_UPI_ID})
    upi_id = cfg.get("upi_id", DEFAULT_UPI_ID)
    upi_uri = f"upi://pay?pa={upi_id}&pn=TelegramStore&am={amount}&cu=INR"

    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(upi_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)

    bot.send_photo(
        chat_id,
        bio,
        caption=(
            f"💳 <b>Add Money Request: ₹{amount}</b>\n\n"
            f"🏦 <b>UPI ID:</b> <code>{escape(upi_id)}</code>\n"
            f"💰 <b>Amount:</b> ₹{amount}\n\n"
            "উপরের QR Code স্ক্যান করে অথবা UPI ID-তে পেমেন্ট করুন।"
        ),
    )
    bot.send_message(
        chat_id,
        "📤 <b>Upload Payment Image:</b>\nপেমেন্ট করার পর এখন পেমেন্টের স্ক্রিনশট (Image) এখানে পাঠান।",
    )
    set_state(uid, "user_wait_payment_img", amount=amount)


def send_raffle_leaderboard(chat_id, requester_uid=None):
    users = load_json(USERS_FILE, {})
    sorted_users = sorted(
        users.values(),
        key=lambda u: (u.get("raffles", 0), u.get("raffle_coins", 0)),
        reverse=True,
    )
    lines = ["🏆 <b>Raffle Leaderboard (Top 10)</b>\n"]
    for idx, u in enumerate(sorted_users[:10], start=1):
        uname = f"@{u['username']}" if u.get("username") else u.get("name", str(u["id"]))
        lines.append(f"{idx}. {escape(uname)} — 🎟 {u.get('raffles', 0)} | 🪙 {u.get('raffle_coins', 0)} Coins")

    if requester_uid is not None:
        my_rank = next((i for i, u in enumerate(sorted_users, 1) if u["id"] == requester_uid), None)
        me = users.get(str(requester_uid), {})
        lines.append("\n━━━━━━━━━━━━━━━")
        lines.append(
            f"📍 <b>Your Rank:</b> #{my_rank if my_rank else '-'}\n"
            f"🎟 <b>Raffles:</b> {me.get('raffles', 0)} | 🪙 <b>Coins:</b> {me.get('raffle_coins', 0)}"
        )

    bot.send_message(chat_id, "\n".join(lines))


# ==================== CALLBACK QUERY HANDLER ====================
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data

    # 1. Verify Channel Join
    if data == "verify_join":
        not_joined = check_user_channels(uid)
        if not_joined:
            bot.answer_callback_query(call.id, "❌ আপনি এখনো সব চ্যানেলে জয়েন করেননি!", show_alert=True)
            return
        bot.answer_callback_query(call.id, "✅ Verified!")
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        finalize_verification(uid, chat_id)
        return

    # 2. View Product
    if data.startswith("prod_view:"):
        prod_id = data.split(":", 1)[1]
        bot.answer_callback_query(call.id)
        send_product_detail(chat_id, prod_id)
        return

    # 3. Buy Product Preview
    if data.startswith("prod_buy:"):
        prod_id = data.split(":", 1)[1]
        products = load_json(PRODUCTS_FILE, {})
        p = products.get(prod_id)
        if not p:
            bot.answer_callback_query(call.id, "❌ Product পাওয়া যায়নি!", show_alert=True)
            return
        user = get_user(uid)
        bot.answer_callback_query(call.id)

        if p["type"] == "free":
            bal = int(user.get("coins", 0))
            price = int(p["price"])
            after = bal - price
            if bal >= price:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("✅ Confirm Buy", callback_data=f"prod_confirm:{prod_id}"))
                bot.send_message(
                    chat_id,
                    f"🛒 <b>Confirm Free Purchase</b>\n\n"
                    f"📦 <b>Product:</b> {escape(p['title'])}\n"
                    f"🪙 <b>Your Coin Balance:</b> {bal} Coins\n"
                    f"🏷 <b>Price:</b> {price} Coins\n"
                    f"💳 <b>Balance After Buy:</b> {after} Coins",
                    reply_markup=markup,
                )
            else:
                bot.send_message(
                    chat_id,
                    f"❌ <b>Insufficient Coin Balance!</b>\n\n"
                    f"🪙 <b>Your Balance:</b> {bal} Coins\n"
                    f"🏷 <b>Price:</b> {price} Coins\n"
                    f"আরও <b>{price - bal} Coins</b> প্রয়োজন। নিচের লিংক শেয়ার করে কয়েন আয় করুন:",
                )
                send_raffle_info(chat_id, uid)
        else:
            bal = float(user.get("rs", 0))
            price = float(p["price"])
            after = bal - price
            if bal >= price:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("✅ Confirm Buy", callback_data=f"prod_confirm:{prod_id}"))
                bot.send_message(
                    chat_id,
                    f"🛒 <b>Confirm Paid Purchase</b>\n\n"
                    f"📦 <b>Product:</b> {escape(p['title'])}\n"
                    f"🏷 <b>Price:</b> ₹{price}\n"
                    f"💰 <b>Your Balance:</b> ₹{bal}\n"
                    f"💳 <b>Balance After Buy:</b> ₹{after}",
                    reply_markup=markup,
                )
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("➕ Add Rs", callback_data="add_rs"))
                bot.send_message(
                    chat_id,
                    f"❌ <b>Insufficient Rs Balance!</b>\n\n"
                    f"💰 <b>Your Balance:</b> ₹{bal}\n"
                    f"🏷 <b>Price:</b> ₹{price}\n"
                    f"আরও <b>₹{price - bal}</b> প্রয়োজন।",
                    reply_markup=markup,
                )
        return

    # 4. Confirm Buy
    if data.startswith("prod_confirm:"):
        prod_id = data.split(":", 1)[1]
        products = load_json(PRODUCTS_FILE, {})
        p = products.get(prod_id)
        if not p:
            bot.answer_callback_query(call.id, "❌ Product পাওয়া যায়নি!", show_alert=True)
            return

        users = load_json(USERS_FILE, {})
        u = users.get(str(uid))
        if not u:
            bot.answer_callback_query(call.id, "User error!", show_alert=True)
            return

        date_s, time_s = now_ist()
        if p["type"] == "free":
            price = int(p["price"])
            if u.get("coins", 0) < price:
                bot.answer_callback_query(call.id, "❌ যথেষ্ট Coin নেই!", show_alert=True)
                send_raffle_info(chat_id, uid)
                return
            u["coins"] -= price
            u.setdefault("free_history", []).append({
                "prod_id": prod_id,
                "title": p["title"],
                "price": f"{price} Coins",
                "date": date_s,
                "time": time_s,
                "delivery": p.get("delivery", {}),
            })
        else:
            price = float(p["price"])
            if u.get("rs", 0) < price:
                bot.answer_callback_query(call.id, "❌ যথেষ্ট Rs নেই!", show_alert=True)
                return
            u["rs"] = round(u["rs"] - price, 2)
            u.setdefault("paid_history", []).append({
                "prod_id": prod_id,
                "title": p["title"],
                "price": f"₹{price}",
                "date": date_s,
                "time": time_s,
                "delivery": p.get("delivery", {}),
            })

        save_json(USERS_FILE, users)
        bot.answer_callback_query(call.id, "✅ Purchase Successful!")
        deliver_product_file(chat_id, p)
        return

    # 5. Earn Coin & Raffle History
    if data == "earn_coin":
        bot.answer_callback_query(call.id)
        send_raffle_info(chat_id, uid)
        return

    if data == "raffle_history":
        bot.answer_callback_query(call.id)
        user = get_user(uid)
        hist = user.get("raffle_history", [])
        if not hist:
            bot.send_message(chat_id, "📜 এখনো কেউ আপনার Raffle লিংক দিয়ে জয়েন করেনি।")
            return
        lines = ["📜 <b>Raffle History</b>\n"]
        for i, h in enumerate(reversed(hist[-30:]), 1):
            lines.append(f"{i}. {escape(h['username'])} — 📅 {h['date']} ⏰ {h['time']}")
        bot.send_message(chat_id, "\n".join(lines))
        return

    # 6. Add Rs
    if data == "add_rs":
        bot.answer_callback_query(call.id)
        set_state(uid, "user_wait_add_amount")
        bot.send_message(
            chat_id,
            f"💰 <b>Add Money (Rs)</b>\n\n"
            f"কত টাকা অ্যাড করতে চান তা লিখুন (Min <b>₹{MIN_ADD_RS}</b> - Max <b>₹{MAX_ADD_RS}</b>):",
        )
        return

    # 7. Buy History
    if data in ("history:free", "history:paid"):
        bot.answer_callback_query(call.id)
        htype = data.split(":")[1]
        user = get_user(uid)
        key = "free_history" if htype == "free" else "paid_history"
        items = user.get(key, [])
        if not items:
            bot.send_message(chat_id, f"📭 আপনার কোনো <b>{htype.title()} Buy History</b> নেই।")
            return

        markup = types.InlineKeyboardMarkup(row_width=1)
        lines = [f"🧾 <b>{htype.title()} Buy History</b>\n"]
        for idx, it in enumerate(items):
            lines.append(
                f"{idx + 1}. <b>{escape(it['title'])}</b> ({it['price']})\n"
                f"   📅 {it['date']} | ⏰ {it['time']}"
            )
            markup.add(
                types.InlineKeyboardButton(
                    f"📥 Download: {it['title']}",
                    callback_data=f"history_get:{htype}:{idx}",
                )
            )
        bot.send_message(chat_id, "\n".join(lines), reply_markup=markup)
        return

    if data.startswith("history_get:"):
        _, htype, idx_s = data.split(":")
        idx = int(idx_s)
        user = get_user(uid)
        items = user.get("free_history" if htype == "free" else "paid_history", [])
        if 0 <= idx < len(items):
            bot.answer_callback_query(call.id)
            deliver_product_file(chat_id, items[idx])
        else:
            bot.answer_callback_query(call.id, "Not found!", show_alert=True)
        return

    # ==================== ADMIN CALLBACKS ====================
    if uid not in logged_admins:
        bot.answer_callback_query(call.id, "⚠️ Admin session expired!", show_alert=True)
        return

    if data.startswith("adm_del_ch:"):
        idx = int(data.split(":")[1])
        channels = load_json(CHANNELS_FILE, [])
        if 0 <= idx < len(channels):
            removed = channels.pop(idx)
            save_json(CHANNELS_FILE, channels)
            bot.answer_callback_query(call.id, f"Deleted {removed['name']}")
            bot.edit_message_text(
                f"✅ Channel <b>{escape(removed['name'])}</b> ডিলিট করা হয়েছে।",
                chat_id,
                call.message.message_id,
            )
        return

    if data.startswith("adm_prod_type:"):
        ptype = data.split(":")[1]
        bot.answer_callback_query(call.id)
        set_state(uid, "adm_prod_title", ptype=ptype)
        bot.send_message(chat_id, "📝 Product-এর <b>Title / Name</b> লিখুন:")
        return

    if data == "adm_img_done":
        st = get_state(uid)
        if not st or st.get("action") != "adm_prod_images":
            bot.answer_callback_query(call.id, "Session expired.")
            return
        bot.answer_callback_query(call.id, "Images saved!")
        st["action"] = "adm_prod_file"
        with state_lock:
            user_states[str(uid)] = st
        bot.send_message(
            chat_id,
            "📁 এখন Product-এর <b>File</b> (Document/Video/Audio) আপলোড করুন অথবা সরাসরি <b>Download Link</b> (URL) পাঠান:",
        )
        return

    if data.startswith("adm_mng_prod:"):
        prod_id = data.split(":", 1)[1]
        products = load_json(PRODUCTS_FILE, {})
        p = products.get(prod_id)
        if not p:
            bot.answer_callback_query(call.id, "Not found!", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✏️ Edit Price", callback_data=f"adm_edit_price:{prod_id}"),
            types.InlineKeyboardButton("🗑 Delete", callback_data=f"adm_del_prod:{prod_id}"),
        )
        price_str = f"{int(p['price'])} Coins" if p["type"] == "free" else f"₹{p['price']}"
        bot.send_message(
            chat_id,
            f"📦 <b>{escape(p['title'])}</b>\n"
            f"📂 Type: {p['type'].upper()}\n"
            f"🏷 Price: {price_str}\n"
            f"🖼 Images: {len(p.get('images', []))}",
            reply_markup=markup,
        )
        return

    if data.startswith("adm_del_prod:"):
        prod_id = data.split(":", 1)[1]
        products = load_json(PRODUCTS_FILE, {})
        if prod_id in products:
            title = products[prod_id]["title"]
            del products[prod_id]
            save_json(PRODUCTS_FILE, products)
            bot.answer_callback_query(call.id, "Deleted!")
            bot.edit_message_text(f"🗑 Product <b>{escape(title)}</b> ডিলিট করা হয়েছে।", chat_id, call.message.message_id)
        return

    if data.startswith("adm_edit_price:"):
        prod_id = data.split(":", 1)[1]
        bot.answer_callback_query(call.id)
        set_state(uid, "adm_edit_price_val", prod_id=prod_id)
        bot.send_message(chat_id, "🏷 নতুন <b>Price</b> (0 বা তার বেশি সংখ্যা) লিখুন:")
        return

    if data.startswith("adm_users_page:"):
        page = int(data.split(":")[1])
        bot.answer_callback_query(call.id)
        send_admin_users_page(chat_id, page, edit_msg_id=call.message.message_id)
        return

    if data.startswith("adm_bonus_target:"):
        target = data.split(":")[1]
        bot.answer_callback_query(call.id)
        if target == "user":
            set_state(uid, "adm_bonus_uid")
            bot.send_message(chat_id, "👤 যে ইউজারকে বোনাস দেবেন তার <b>User ID</b> লিখুন:")
        else:
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("🪙 Coins", callback_data="adm_bonus_type:all:coins"),
                types.InlineKeyboardButton("💰 Rs", callback_data="adm_bonus_type:all:rs"),
            )
            bot.send_message(chat_id, "সবাইকে কোন ধরনের বোনাস পাঠাতে চান?", reply_markup=markup)
        return

    if data.startswith("adm_bonus_type:"):
        _, target_uid, btype = data.split(":")
        bot.answer_callback_query(call.id)
        set_state(uid, "adm_bonus_amount", target_uid=target_uid, btype=btype)
        bot.send_message(chat_id, f"💵 কত পরিমাণ <b>{btype.upper()}</b> বোনাস পাঠাতে চান তা লিখুন:")
        return

    if data.startswith("adm_pay_act:"):
        _, action, req_id = data.split(":")
        payments = load_json(PAYMENTS_FILE, [])
        req = next((r for r in payments if r["id"] == req_id), None)
        if not req:
            bot.answer_callback_query(call.id, "Request not found!", show_alert=True)
            return
        if req["status"] != "pending":
            bot.answer_callback_query(call.id, f"Already {req['status']}!", show_alert=True)
            return

        date_s, time_s = now_ist()
        req["action_date"] = date_s
        req["action_time"] = time_s

        if action == "approve":
            req["status"] = "approved"
            users = load_json(USERS_FILE, {})
            u = users.get(str(req["user_id"]))
            if u:
                u["rs"] = round(u.get("rs", 0) + req["amount"], 2)
                save_json(USERS_FILE, users)
            save_json(PAYMENTS_FILE, payments)
            bot.answer_callback_query(call.id, "✅ Approved!")
            try:
                bot.edit_message_caption(
                    f"✅ <b>APPROVED</b> | ₹{req['amount']}\n👤 User: {escape(req['username'])} (<code>{req['user_id']}</code>)",
                    chat_id,
                    call.message.message_id,
                )
            except Exception:
                pass
            try:
                bot.send_message(
                    req["user_id"],
                    f"🎉 <b>Payment Approved!</b>\n"
                    f"💰 আপনার Wallet-এ <b>₹{req['amount']}</b> যোগ করা হয়েছে।",
                )
            except Exception:
                pass
        else:
            req["status"] = "rejected"
            save_json(PAYMENTS_FILE, payments)
            bot.answer_callback_query(call.id, "❌ Rejected!")
            try:
                bot.edit_message_caption(
                    f"❌ <b>REJECTED</b> | ₹{req['amount']}\n👤 User: {escape(req['username'])} (<code>{req['user_id']}</code>)",
                    chat_id,
                    call.message.message_id,
                )
            except Exception:
                pass
            try:
                bot.send_message(
                    req["user_id"],
                    f"❌ <b>Payment Rejected!</b>\n"
                    f"₹{req['amount']}-এর পেমেন্ট রিকোয়েস্টটি বাতিল করা হয়েছে। সাহায্যের জন্য {DEVELOPER_USERNAME}-এ যোগাযোগ করুন।",
                )
            except Exception:
                pass
        return


def send_admin_users_page(chat_id, page=0, edit_msg_id=None):
    users = list(load_json(USERS_FILE, {}).values())
    per_page = 12
    total = len(users)
    start = page * per_page
    chunk = users[start : start + per_page]

    lines = [f"👥 <b>Total Users:</b> {total} (Page {page + 1})\n"]
    for u in chunk:
        uname = f"@{u['username']}" if u.get("username") else u.get("name", "User")
        lines.append(
            f"• <code>{u['id']}</code> | {escape(uname)} | 💰₹{u.get('rs', 0)} | 🪙{u.get('coins', 0)} | 🎟{u.get('raffles', 0)}"
        )

    markup = types.InlineKeyboardMarkup(row_width=2)
    btns = []
    if page > 0:
        btns.append(types.InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_users_page:{page - 1}"))
    if start + per_page < total:
        btns.append(types.InlineKeyboardButton("Next ➡️", callback_data=f"adm_users_page:{page + 1}"))
    if btns:
        markup.add(*btns)

    text = "\n".join(lines)
    if edit_msg_id:
        bot.edit_message_text(text, chat_id, edit_msg_id, reply_markup=markup)
    else:
        bot.send_message(chat_id, text, reply_markup=markup)


# ==================== NON-TEXT MESSAGE HANDLERS (PHOTOS / FILES) ====================
@bot.message_handler(content_types=["photo", "document", "video", "audio"])
def handle_media_messages(message):
    uid = message.from_user.id
    chat_id = message.chat.id
    st = get_state(uid)
    if not st:
        return

    action = st.get("action")

    # 1. User uploading payment screenshot
    if action == "user_wait_payment_img":
        if message.content_type != "photo":
            bot.send_message(chat_id, "⚠️ দয়া করে পেমেন্টের একটি <b>Image/Screenshot</b> পাঠান।")
            return
        amount = st["amount"]
        clear_state(uid)
        photo_id = message.photo[-1].file_id
        user = get_user(uid, message.from_user.username, message.from_user.first_name)
        uname = f"@{user['username']}" if user.get("username") else user.get("name", str(uid))
        date_s, time_s = now_ist()
        req_id = str(uuid.uuid4())[:8]

        payments = load_json(PAYMENTS_FILE, [])
        payments.append({
            "id": req_id,
            "user_id": uid,
            "username": uname,
            "amount": amount,
            "photo_id": photo_id,
            "status": "pending",
            "date": date_s,
            "time": time_s,
        })
        save_json(PAYMENTS_FILE, payments)

        bot.send_message(
            chat_id,
            f"✅ <b>Payment Proof Received!</b>\n"
            f"আপনার <b>₹{amount}</b> রিকোয়েস্টটি Admin / Support-এ পাঠানো হয়েছে। ভেরিফাই হলে ব্যালেন্স অ্যাড হয়ে যাবে।",
        )

        # Notify admins
        cfg = load_json(CONFIG_FILE, {})
        notify_ids = set(cfg.get("admin_ids", [])) | logged_admins
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ Approve", callback_data=f"adm_pay_act:approve:{req_id}"),
            types.InlineKeyboardButton("❌ Reject", callback_data=f"adm_pay_act:reject:{req_id}"),
        )
        for aid in notify_ids:
            try:
                bot.send_photo(
                    aid,
                    photo_id,
                    caption=(
                        f"🔔 <b>New Payment Request!</b>\n\n"
                        f"👤 User: {escape(uname)} (<code>{uid}</code>)\n"
                        f"💰 Amount: <b>₹{amount}</b>\n"
                        f"📅 {date_s} | ⏰ {time_s}"
                    ),
                    reply_markup=markup,
                )
            except Exception:
                pass
        return

    # 2. Admin uploading multi-images for product
    if action == "adm_prod_images" and uid in logged_admins:
        if message.content_type != "photo":
            bot.send_message(chat_id, "⚠️ শুধু Photo পাঠান অথবা Done বাটনে ক্লিক করুন।")
            return
        file_id = message.photo[-1].file_id
        with state_lock:
            st = user_states.get(str(uid))
            if not st:
                return
            st.setdefault("images", []).append(file_id)
            count = len(st["images"])
            mg_id = message.media_group_id
            should_reply = (mg_id is None) or (st.get("last_mg_id") != mg_id)
            st["last_mg_id"] = mg_id

        if should_reply:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Done Uploading Images", callback_data="adm_img_done"))
            bot.send_message(
                chat_id,
                f"🖼 Image added! (Total: {count})\nআরও ছবি পাঠাতে পারেন অথবা <b>Done</b> ক্লিক করুন:",
                reply_markup=markup,
            )
        return

    # 3. Admin uploading deliverable product file
    if action == "adm_prod_file" and uid in logged_admins:
        delivery = {}
        if message.content_type == "document":
            delivery = {
                "kind": "document",
                "file_id": message.document.file_id,
                "file_size": message.document.file_size,
            }
        elif message.content_type == "video":
            delivery = {
                "kind": "video",
                "file_id": message.video.file_id,
                "file_size": message.video.file_size,
            }
        elif message.content_type == "audio":
            delivery = {
                "kind": "audio",
                "file_id": message.audio.file_id,
                "file_size": message.audio.file_size,
            }
        elif message.content_type == "photo":
            delivery = {
                "kind": "photo",
                "file_id": message.photo[-1].file_id,
                "file_size": message.photo[-1].file_size,
            }

        save_new_product(uid, chat_id, st, delivery)
        return

    # 4. Admin broadcast media
    if action == "adm_broadcast" and uid in logged_admins:
        clear_state(uid)
        users = load_json(USERS_FILE, {})
        sent, failed = 0, 0
        for u_id in list(users.keys()):
            try:
                bot.copy_message(int(u_id), chat_id, message.message_id)
                sent += 1
            except Exception:
                failed += 1
        bot.send_message(chat_id, f"📢 <b>Notification Sent!</b>\n✅ Success: {sent}\n❌ Failed: {failed}")
        return


def save_new_product(uid, chat_id, st, delivery):
    prod_id = str(uuid.uuid4())[:8]
    products = load_json(PRODUCTS_FILE, {})
    products[prod_id] = {
        "id": prod_id,
        "type": st["ptype"],
        "title": st["title"],
        "description": st["description"],
        "price": st["price"],
        "images": st.get("images", []),
        "delivery": delivery,
    }
    save_json(PRODUCTS_FILE, products)
    clear_state(uid)
    bot.send_message(
        chat_id,
        f"✅ <b>Product Added Successfully!</b>\n"
        f"📦 Title: {escape(st['title'])}\n"
        f"📂 Category: {st['ptype'].upper()}\n"
        f"🖼 Images: {len(st.get('images', []))}",
        reply_markup=admin_main_keyboard(),
    )


# ==================== TEXT MESSAGE HANDLER ====================
@bot.message_handler(content_types=["text"])
def handle_text_messages(message):
    uid = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()
    get_user(uid, message.from_user.username, message.from_user.first_name)

    # Check if user clicked any menu button (resets active input state cleanly)
    if text in USER_MENU_BUTTONS or (uid in logged_admins and text in ADMIN_MENU_BUTTONS):
        clear_state(uid)

    # ---------- CHECK USER PANEL MENU BUTTONS ----------
    if text == "Free Product":
        if check_user_channels(uid):
            send_force_join_message(chat_id, load_json(CHANNELS_FILE, []))
            return
        send_product_list(chat_id, "free")
        return

    if text == "Paid Product":
        if check_user_channels(uid):
            send_force_join_message(chat_id, load_json(CHANNELS_FILE, []))
            return
        send_product_list(chat_id, "paid")
        return

    if text == "Raffle Leaderboard":
        send_raffle_leaderboard(chat_id, requester_uid=uid)
        return

    if text == "Raffle":
        send_raffle_info(chat_id, uid)
        return

    if text == "Buy History":
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🪙 Free", callback_data="history:free"),
            types.InlineKeyboardButton("💰 Paid", callback_data="history:paid"),
        )
        bot.send_message(chat_id, "🧾 <b>Select Buy History Type:</b>", reply_markup=markup)
        return

    if text == "Wallet":
        user = get_user(uid)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("➕ Add Rs", callback_data="add_rs"))
        bot.send_message(
            chat_id,
            f"👛 <b>Your Wallet</b>\n\n"
            f"💰 <b>Total Rs Balance:</b> ₹{user.get('rs', 0)}\n"
            f"🪙 <b>Coin Balance:</b> {user.get('coins', 0)} Coins\n"
            f"🎁 <b>Total Bonus Received:</b> ₹{user.get('bonus_rs', 0)} | {user.get('bonus_coins', 0)} Coins",
            reply_markup=markup,
        )
        return

    if text in ("Developer", "Help Support"):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("💬 Contact Support", url="https://t.me/ai_brain_box_2"))
        bot.send_message(
            chat_id,
            f"👨‍💻 <b>Developer & Help Support</b>\n\n"
            f"যেকোনো সাহায্য বা সাপোর্টের জন্য যোগাযোগ করুন: {DEVELOPER_USERNAME}",
            reply_markup=markup,
        )
        return

    if text == "Admin Login":
        set_state(uid, "wait_admin_pass")
        bot.send_message(chat_id, "🔐 <b>Enter Admin Password:</b>")
        return

    # ---------- CHECK ADMIN PANEL MENU BUTTONS ----------
    if uid in logged_admins:
        if text == "User Panel":
            bot.send_message(chat_id, "🔙 User Panel-এ ফিরে এসেছেন:", reply_markup=user_main_keyboard())
            return

        if text == "Add Channel":
            set_state(uid, "adm_add_ch_name")
            bot.send_message(chat_id, "📢 চ্যানেলের <b>Name</b> লিখুন (যেমন: Main Channel):")
            return

        if text == "Manage Channel":
            channels = load_json(CHANNELS_FILE, [])
            if not channels:
                bot.send_message(chat_id, "📭 কোনো চ্যানেল অ্যাড করা নেই।")
                return
            markup = types.InlineKeyboardMarkup(row_width=1)
            for idx, ch in enumerate(channels):
                markup.add(
                    types.InlineKeyboardButton(
                        f"🗑 Delete: {ch['name']} ({ch['chat_id']})",
                        callback_data=f"adm_del_ch:{idx}",
                    )
                )
            bot.send_message(chat_id, "⚙️ <b>Manage Channels:</b>", reply_markup=markup)
            return

        if text == "Add Product":
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("🪙 Free Product", callback_data="adm_prod_type:free"),
                types.InlineKeyboardButton("💰 Paid Product", callback_data="adm_prod_type:paid"),
            )
            bot.send_message(chat_id, "📦 কোন ধরনের Product অ্যাড করবেন?", reply_markup=markup)
            return

        if text == "Manage Product":
            products = load_json(PRODUCTS_FILE, {})
            if not products:
                bot.send_message(chat_id, "📭 কোনো Product নেই।")
                return
            markup = types.InlineKeyboardMarkup(row_width=1)
            for p in products.values():
                pr = f"{int(p['price'])} Coins" if p["type"] == "free" else f"₹{p['price']}"
                markup.add(
                    types.InlineKeyboardButton(
                        f"[{p['type'].upper()}] {p['title']} ({pr})",
                        callback_data=f"adm_mng_prod:{p['id']}",
                    )
                )
            bot.send_message(chat_id, "🛠 <b>Manage Products:</b>", reply_markup=markup)
            return

        if text == "Users":
            send_admin_users_page(chat_id, 0)
            return

        if text == "Bonus Send":
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("👤 Single User", callback_data="adm_bonus_target:user"),
                types.InlineKeyboardButton("👥 All Users", callback_data="adm_bonus_target:all"),
            )
            bot.send_message(chat_id, "🎁 কাদের বোনাস পাঠাতে চান?", reply_markup=markup)
            return

        if text == "Notification Send":
            set_state(uid, "adm_broadcast")
            bot.send_message(chat_id, "📢 সব ইউজারকে পাঠানোর জন্য <b>Text / Photo / File</b> পাঠান:")
            return

        if text == "Payment Request":
            payments = load_json(PAYMENTS_FILE, [])
            pending = [r for r in payments if r["status"] == "pending"]
            if not pending:
                bot.send_message(chat_id, "✅ কোনো Pending Payment Request নেই।")
                return
            for req in pending[:10]:
                markup = types.InlineKeyboardMarkup(row_width=2)
                markup.add(
                    types.InlineKeyboardButton("✅ Approve", callback_data=f"adm_pay_act:approve:{req['id']}"),
                    types.InlineKeyboardButton("❌ Reject", callback_data=f"adm_pay_act:reject:{req['id']}"),
                )
                bot.send_photo(
                    chat_id,
                    req["photo_id"],
                    caption=(
                        f"💳 <b>Pending Request #{req['id']}</b>\n"
                        f"👤 User: {escape(req['username'])} (<code>{req['user_id']}</code>)\n"
                        f"💰 Amount: <b>₹{req['amount']}</b>\n"
                        f"📅 {req['date']} | ⏰ {req['time']}"
                    ),
                    reply_markup=markup,
                )
            return

        if text == "Raffle Leaderboard (Admin)":
            send_raffle_leaderboard(chat_id)
            return

        if text == "Coin Leaderboard":
            users = sorted(load_json(USERS_FILE, {}).values(), key=lambda u: u.get("coins", 0), reverse=True)
            lines = ["🪙 <b>Coin Leaderboard (Top 15)</b>\n"]
            for i, u in enumerate(users[:15], 1):
                uname = f"@{u['username']}" if u.get("username") else u.get("name", str(u["id"]))
                lines.append(f"{i}. {escape(uname)} — 🪙 {u.get('coins', 0)} Coins")
            bot.send_message(chat_id, "\n".join(lines))
            return

        if text == "Rs Leaderboard":
            users = sorted(load_json(USERS_FILE, {}).values(), key=lambda u: u.get("rs", 0), reverse=True)
            lines = ["💰 <b>Rs Leaderboard (Top 15)</b>\n"]
            for i, u in enumerate(users[:15], 1):
                uname = f"@{u['username']}" if u.get("username") else u.get("name", str(u["id"]))
                lines.append(f"{i}. {escape(uname)} — ₹{u.get('rs', 0)}")
            bot.send_message(chat_id, "\n".join(lines))
            return

        if text == "Add Money History":
            payments = load_json(PAYMENTS_FILE, [])
            done = [r for r in payments if r["status"] != "pending"]
            if not done:
                bot.send_message(chat_id, "📭 এখনো কোনো Add Money History নেই।")
                return
            lines = ["📜 <b>Add Money History (Last 20)</b>\n"]
            for r in reversed(done[-20:]):
                icon = "✅" if r["status"] == "approved" else "❌"
                lines.append(
                    f"{icon} ₹{r['amount']} | {escape(r['username'])} (<code>{r['user_id']}</code>)\n"
                    f"   📅 {r['date']} ⏰ {r['time']} [{r['status'].upper()}]"
                )
            bot.send_message(chat_id, "\n".join(lines))
            return

        if text == "UPI Settings":
            cfg = load_json(CONFIG_FILE, {"upi_id": DEFAULT_UPI_ID})
            set_state(uid, "adm_set_upi")
            bot.send_message(
                chat_id,
                f"⚙️ বর্তমান UPI ID: <code>{escape(cfg.get('upi_id', DEFAULT_UPI_ID))}</code>\n\n"
                "নতুন <b>UPI ID</b> লিখুন:",
            )
            return

    # ---------- CHECK CONVERSATION STATES ----------
    st = get_state(uid)
    if not st:
        return

    action = st.get("action")

    # Admin password verification
    if action == "wait_admin_pass":
        clear_state(uid)
        if text == ADMIN_PASSWORD:
            logged_admins.add(uid)
            cfg = load_json(CONFIG_FILE, {"upi_id": DEFAULT_UPI_ID, "admin_ids": []})
            if uid not in cfg.get("admin_ids", []):
                cfg.setdefault("admin_ids", []).append(uid)
                save_json(CONFIG_FILE, cfg)
            bot.send_message(chat_id, "✅ <b>Admin Panel Unlocked!</b>", reply_markup=admin_main_keyboard())
        else:
            bot.send_message(chat_id, "❌ ভুল পাসওয়ার্ড!")
        return

    # User Add Rs amount input
    if action == "user_wait_add_amount":
        try:
            amt = float(text)
        except ValueError:
            bot.send_message(chat_id, "⚠️ সঠিক সংখ্যা লিখুন (যেমন: 50)।")
            return
        if amt < MIN_ADD_RS or amt > MAX_ADD_RS:
            bot.send_message(chat_id, f"⚠️ Amount অবশ্যই <b>₹{MIN_ADD_RS}</b> থেকে <b>₹{MAX_ADD_RS}</b>-এর মধ্যে হতে হবে।")
            return
        amt_val = int(amt) if amt.is_integer() else round(amt, 2)
        send_qr_and_ask_proof(chat_id, uid, amt_val)
        return

    # Admin States
    if uid not in logged_admins:
        clear_state(uid)
        return

    if action == "adm_add_ch_name":
        set_state(uid, "adm_add_ch_id", ch_name=text)
        bot.send_message(
            chat_id,
            "🆔 চ্যানেলের <b>Username বা Chat ID</b> দিন (যেমন: <code>@mychannel</code> বা <code>-1001234567890</code>)\n"
            "<i>(মনে রাখবেন: বটকে চ্যানেলে Admin বানাতে হবে)</i>:",
        )
        return

    if action == "adm_add_ch_id":
        set_state(uid, "adm_add_ch_link", ch_name=st["ch_name"], ch_id=text)
        bot.send_message(chat_id, "🔗 চ্যানেলের <b>Invite Link</b> দিন (যেমন: <code>https://t.me/mychannel</code>):")
        return

    if action == "adm_add_ch_link":
        channels = load_json(CHANNELS_FILE, [])
        channels.append({
            "name": st["ch_name"],
            "chat_id": st["ch_id"],
            "link": text,
        })
        save_json(CHANNELS_FILE, channels)
        clear_state(uid)
        bot.send_message(chat_id, "✅ চ্যানেল সফলভাবে অ্যাড হয়েছে!", reply_markup=admin_main_keyboard())
        return

    if action == "adm_prod_title":
        set_state(uid, "adm_prod_desc", ptype=st["ptype"], title=text)
        bot.send_message(chat_id, "📄 Product-এর <b>Description</b> লিখুন:")
        return

    if action == "adm_prod_desc":
        set_state(uid, "adm_prod_price", ptype=st["ptype"], title=st["title"], description=text)
        unit = "Coins" if st["ptype"] == "free" else "Rs (₹)"
        bot.send_message(chat_id, f"🏷 Product-এর <b>Price ({unit})</b> লিখুন (0 ও দেওয়া যাবে):")
        return

    if action == "adm_prod_price":
        try:
            price = float(text)
            if price < 0:
                raise ValueError
        except ValueError:
            bot.send_message(chat_id, "⚠️ সঠিক Price (0 বা তার বেশি) লিখুন:")
            return
        price_val = int(price) if st["ptype"] == "free" or price.is_integer() else round(price, 2)
        set_state(
            uid,
            "adm_prod_images",
            ptype=st["ptype"],
            title=st["title"],
            description=st["description"],
            price=price_val,
            images=[],
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⏭ Skip / Done Images", callback_data="adm_img_done"))
        bot.send_message(
            chat_id,
            "🖼 এখন Product-এর <b>Images</b> (১ বা একাধিক ছবি) পাঠান।\nছবি পাঠানো হয়ে গেলে <b>Done</b> ক্লিক করুন:",
            reply_markup=markup,
        )
        return

    if action == "adm_prod_file":
        if text.startswith("http://") or text.startswith("https://"):
            delivery = {"kind": "link", "url": text}
            save_new_product(uid, chat_id, st, delivery)
        else:
            bot.send_message(chat_id, "⚠️ অনুগ্রহ করে একটি ফাইল আপলোড করুন অথবা `https://` দিয়ে শুরু হওয়া Link পাঠান।")
        return

    if action == "adm_edit_price_val":
        try:
            price = float(text)
            if price < 0:
                raise ValueError
        except ValueError:
            bot.send_message(chat_id, "⚠️ সঠিক Price লিখুন:")
            return
        products = load_json(PRODUCTS_FILE, {})
        prod_id = st["prod_id"]
        if prod_id in products:
            p = products[prod_id]
            p["price"] = int(price) if p["type"] == "free" or price.is_integer() else round(price, 2)
            save_json(PRODUCTS_FILE, products)
            bot.send_message(chat_id, f"✅ Price আপডেট হয়েছে: {p['price']}")
        clear_state(uid)
        return

    if action == "adm_bonus_uid":
        if not text.isdigit():
            bot.send_message(chat_id, "⚠️ সঠিক numeric User ID লিখুন:")
            return
        users = load_json(USERS_FILE, {})
        if text not in users:
            bot.send_message(chat_id, "❌ এই User ID ডাটাবেজে নেই।")
            return
        clear_state(uid)
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("🪙 Coins", callback_data=f"adm_bonus_type:{text}:coins"),
            types.InlineKeyboardButton("💰 Rs", callback_data=f"adm_bonus_type:{text}:rs"),
        )
        bot.send_message(chat_id, f"👤 User <code>{text}</code>-কে কোন বোনাস পাঠাবেন?", reply_markup=markup)
        return

    if action == "adm_bonus_amount":
        try:
            amt = float(text)
            if amt <= 0:
                raise ValueError
        except ValueError:
            bot.send_message(chat_id, "⚠️ সঠিক Amount লিখুন:")
            return

        target_uid = st["target_uid"]
        btype = st["btype"]
        clear_state(uid)

        users = load_json(USERS_FILE, {})
        targets = list(users.keys()) if target_uid == "all" else [target_uid]
        count = 0
        for tid in targets:
            if tid in users:
                if btype == "coins":
                    val = int(amt)
                    users[tid]["coins"] = users[tid].get("coins", 0) + val
                    users[tid]["bonus_coins"] = users[tid].get("bonus_coins", 0) + val
                    msg = f"🎁 <b>Bonus Received!</b>\nAdmin আপনাকে <b>+{val} Coins</b> বোনাস দিয়েছেন।"
                else:
                    val = round(amt, 2)
                    users[tid]["rs"] = round(users[tid].get("rs", 0) + val, 2)
                    users[tid]["bonus_rs"] = round(users[tid].get("bonus_rs", 0) + val, 2)
                    msg = f"🎁 <b>Bonus Received!</b>\nAdmin আপনাকে <b>+₹{val}</b> বোনাস দিয়েছেন।"
                count += 1
                try:
                    bot.send_message(int(tid), msg)
                except Exception:
                    pass
        save_json(USERS_FILE, users)
        bot.send_message(chat_id, f"✅ {count} জন ইউজারকে সফলভাবে বোনাস পাঠানো হয়েছে!")
        return

    if action == "adm_broadcast":
        clear_state(uid)
        users = load_json(USERS_FILE, {})
        sent, failed = 0, 0
        for u_id in list(users.keys()):
            try:
                bot.send_message(int(u_id), text)
                sent += 1
            except Exception:
                failed += 1
        bot.send_message(chat_id, f"📢 <b>Notification Sent!</b>\n✅ Success: {sent}\n❌ Failed: {failed}")
        return

    if action == "adm_set_upi":
        cfg = load_json(CONFIG_FILE, {})
        cfg["upi_id"] = text
        save_json(CONFIG_FILE, cfg)
        clear_state(uid)
        bot.send_message(chat_id, f"✅ UPI ID আপডেট হয়েছে: <code>{escape(text)}</code>")
        return


# ==================== MAIN LOOP ====================
if __name__ == "__main__":
    ensure_data_files()
    print("Bot is running...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            print("Polling error:", e)
            time.sleep(5)
