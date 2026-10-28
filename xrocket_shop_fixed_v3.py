import os
import sqlite3
import time
import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==========================================
# CONFIGURATION
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ТОКЕН_БОТА_ОТ_BOTFATHER")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8934512194"))

# Paste your xRocket Pay API token here, or set via environment variables.
XROCKET_TOKEN = os.environ.get("XROCKET_TOKEN", "")
CRYPTOBOT_TOKEN = os.environ.get("CRYPTOBOT_TOKEN", "")

# xRocket Current API endpoints
XROCKET_INVOICES_URL = "https://pay.api.xrocket.exchange/api/v1/invoices"
XROCKET_INVOICE_URL = "https://pay.api.xrocket.exchange/api/v1/invoice"

# ==========================================
# DATABASE
# ==========================================
# On Railway: mount a Volume at /data and set DATA_DIR=/data in env vars.
# Locally the DB is created in the current directory.
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "shop_stock.db")
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    description TEXT,
    price REAL,
    content_type TEXT,
    content TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS product_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER,
    item_data TEXT,
    reserved_until INTEGER DEFAULT 0
)
""")

# Migration for databases created by older versions of the bot.
# CREATE TABLE IF NOT EXISTS does not add columns to an existing table.
cursor.execute("PRAGMA table_info(product_items)")
product_item_columns = {row[1] for row in cursor.fetchall()}
if "reserved_until" not in product_item_columns:
    cursor.execute(
        "ALTER TABLE product_items ADD COLUMN reserved_until INTEGER DEFAULT 0"
    )
if "reservation_order" not in product_item_columns:
    cursor.execute(
        "ALTER TABLE product_items ADD COLUMN reservation_order TEXT DEFAULT ''"
    )
conn.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    product_name TEXT,
    items_given TEXT,
    total_price REAL,
    pay_method TEXT,
    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

# Migrate older purchases tables without deleting purchase history.
cursor.execute("PRAGMA table_info(purchases)")
purchase_columns = {row[1] for row in cursor.fetchall()}
purchase_migrations = {
    "user_id": "INTEGER",
    "username": "TEXT",
    "product_name": "TEXT",
    "items_given": "TEXT DEFAULT ''",
    "total_price": "REAL DEFAULT 0",
    "pay_method": "TEXT DEFAULT ''",
    "date": "TIMESTAMP",
    "quantity": "INTEGER DEFAULT 1",
    "invoice_id": "TEXT DEFAULT ''",
}
for column_name, column_type in purchase_migrations.items():
    if column_name not in purchase_columns:
        cursor.execute(
            f"ALTER TABLE purchases ADD COLUMN {column_name} {column_type}"
        )

# Older rows can have NULL date after migration; fill those for display.
cursor.execute(
    "UPDATE purchases SET date = CURRENT_TIMESTAMP WHERE date IS NULL"
)
cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    invoice_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    username TEXT,
    product_id INTEGER NOT NULL,
    product_name TEXT,
    quantity INTEGER NOT NULL,
    total_price REAL NOT NULL,
    pay_method TEXT NOT NULL,
    item_ids TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
admin_temp = {}
user_quantity_temp = {}


def get_free_stock_count(prod_id: int, c_type: str):
    if c_type == "file":
        return "∞"
    current_time = int(time.time())
    # Expired reservations are automatically released.
    cursor.execute(
        """UPDATE product_items
           SET reserved_until = 0, reservation_order = ''
           WHERE product_id = ? AND reserved_until > 0 AND reserved_until <= ?""",
        (prod_id, current_time),
    )
    conn.commit()
    cursor.execute(
        """SELECT COUNT(*) FROM product_items
           WHERE product_id = ? AND (reserved_until = 0 OR reserved_until < ?)""",
        (prod_id, current_time),
    )
    return cursor.fetchone()[0]


def extract_lines_from_message(message):
    raw_text = ""
    if message.document:
        file_info = bot.get_file(message.document.file_id)
        downloaded = bot.download_file(file_info.file_path)
        raw_text = downloaded.decode("utf-8", errors="ignore")
    elif message.text:
        raw_text = message.text
    return [line.strip() for line in raw_text.splitlines() if line.strip()]


# ==========================================
# PAYMENT SYSTEMS
# ==========================================
def create_xrocket_invoice(amount: float):
    if not XROCKET_TOKEN:
        return None, None, "xRocket не настроен: отсутствует API-токен"

    headers = {
        "Authorization": f"Bearer {XROCKET_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "priceAmount": f"{amount:.2f}",
        "priceCurrency": "USDT",
        "payoutCurrency": "USDT",
        "description": "Оплата цифрового товара",
        "expiresIn": 600000,
    }

    try:
        resp = requests.post(
            XROCKET_INVOICES_URL,
            headers=headers,
            json=payload,
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            return None, None, f"HTTP {resp.status_code}: {resp.text}"

        data = resp.json()
        # Current API normally returns the invoice fields at the top level.
        if isinstance(data.get("data"), dict):
            data = data["data"]

        invoice_id = data.get("id") or data.get("invoiceId")
        links = data.get("links") or {}
        pay_url = (
            links.get("telegramBotLink")
            or links.get("webLink")
            or data.get("link")
            or data.get("payUrl")
        )

        if not invoice_id or not pay_url:
            return None, None, (
                "API ответил успешно, но в ответе нет ID счёта или ссылки. "
                f"Ответ: {resp.text[:1000]}"
            )
        return pay_url, str(invoice_id), None

    except requests.RequestException as exc:
        return None, None, f"Ошибка соединения с xRocket: {exc}"
    except ValueError:
        return None, None, "xRocket вернул ответ не в формате JSON"


def check_xrocket_invoice(invoice_id: str):
    """Returns True only when the API confirms the invoice is paid."""
    if not XROCKET_TOKEN:
        return False

    headers = {
        "Authorization": f"Bearer {XROCKET_TOKEN}",
        "Accept": "application/json",
    }

    # The invoice lookup route may differ by API version. Do not release
    # goods unless a successful response explicitly confirms payment.
    try:
        resp = requests.get(
            XROCKET_INVOICE_URL,
            headers=headers,
            params={"invoiceId": invoice_id},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"xRocket check error: HTTP {resp.status_code}: {resp.text[:500]}")
            return False

        data = resp.json()
        if isinstance(data.get("data"), dict):
            data = data["data"]
        status = str(data.get("status", "")).lower()
        return status in ("paid", "completed", "success", "active_paid")
    except (requests.RequestException, ValueError) as exc:
        print(f"xRocket payment check error: {exc}")
        return False


def create_cryptobot_invoice(amount: float):
    if not CRYPTOBOT_TOKEN:
        return None, None, "CryptoBot не настроен"
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    data = {"asset": "USDT", "amount": str(amount)}
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=10)
        res = resp.json()
        if res.get("ok"):
            return res["result"]["pay_url"], str(res["result"]["invoice_id"]), None
        return None, None, str(res)
    except (requests.RequestException, ValueError) as exc:
        return None, None, str(exc)


def check_cryptobot_invoice(invoice_id: str):
    if not CRYPTOBOT_TOKEN:
        return False
    url = f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    try:
        res = requests.get(url, headers=headers, timeout=10).json()
        if res.get("ok") and res["result"]["items"]:
            return res["result"]["items"][0]["status"] == "paid"
    except (requests.RequestException, ValueError, KeyError):
        pass
    return False


def release_reserved_items(item_ids):
    """Release item reservations so stock becomes available again."""
    ids = []
    for item_id in item_ids:
        try:
            ids.append(int(item_id))
        except (TypeError, ValueError):
            continue
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    cursor.execute(
        f"UPDATE product_items SET reserved_until = 0 WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()


# ==========================================
# USER AREA
# ==========================================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🛍 Каталог товаров", callback_data="catalog"))
    if message.from_user.id == ADMIN_ID:
        kb.add(InlineKeyboardButton("👑 Админ-панель", callback_data="admin_panel"))
    bot.send_message(
        message.chat.id,
        f"Привет, <b>{message.from_user.first_name}</b>! 👋\n"
        "Добро пожаловать в наш магазин цифровых товаров.",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda call: call.data == "catalog")
def show_catalog(call):
    cursor.execute("SELECT id, name, price, content_type FROM products")
    products = cursor.fetchall()
    if not products:
        bot.answer_callback_query(call.id, "😔 Товаров пока нет в наличии.", show_alert=True)
        return

    kb = InlineKeyboardMarkup()
    visible_count = 0
    for prod_id, name, price, c_type in products:
        stock = get_free_stock_count(prod_id, c_type)
        if c_type != "file" and stock == 0:
            continue
        visible_count += 1
        stock_label = "без ограничений" if c_type == "file" else f"{stock} шт."
        kb.add(InlineKeyboardButton(
            f"{name} | {price}$ | В наличии: {stock_label}",
            callback_data=f"prod_{prod_id}",
        ))
    if visible_count == 0:
        bot.answer_callback_query(
            call.id, "😔 Сейчас товаров в наличии нет.", show_alert=True
        )
        return
    if call.from_user.id == ADMIN_ID:
        kb.add(InlineKeyboardButton("👑 В админ-панель", callback_data="admin_panel"))

    try:
        bot.edit_message_text(
            "📂 <b>Выберите нужный товар:</b>",
            call.message.chat.id, call.message.message_id, reply_markup=kb,
        )
    except Exception:
        bot.send_message(call.message.chat.id, "📂 <b>Выберите нужный товар:</b>", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("prod_"))
def show_product(call):
    prod_id = int(call.data.split("_")[1])
    cursor.execute(
        "SELECT name, description, price, content_type FROM products WHERE id = ?",
        (prod_id,),
    )
    prod = cursor.fetchone()
    if not prod:
        bot.answer_callback_query(call.id, "Товар не найден!")
        return

    name, desc, price, c_type = prod
    stock = get_free_stock_count(prod_id, c_type)
    text = (
        f"📦 <b>Товар:</b> {name}\n\n"
        f"📝 <b>Описание:</b>\n{desc}\n\n"
        f"📊 <b>В наличии:</b> {stock} шт.\n"
        f"💵 <b>Цена за 1 шт.:</b> {price}$"
    )
    kb = InlineKeyboardMarkup()
    if stock == 0:
        kb.add(InlineKeyboardButton("❌ Нет в наличии", callback_data="none"))
    else:
        for start in (1, 4, 7):
            kb.row(*[
                InlineKeyboardButton(f"{i} шт", callback_data=f"qty_{prod_id}_{i}")
                for i in range(start, start + 3)
            ])
        kb.add(InlineKeyboardButton("✏️ Свое количество", callback_data=f"customqty_{prod_id}"))
    kb.add(InlineKeyboardButton("🔙 Назад к каталогу", callback_data="catalog"))
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id, reply_markup=kb
        )
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "none")
def cb_none(call):
    bot.answer_callback_query(call.id, "😔 Этот товар закончился!", show_alert=True)


def show_payment_options(chat_id, user_id, prod_id, qty, name, price):
    user_quantity_temp[user_id] = {"prod_id": prod_id, "qty": qty}
    kb = InlineKeyboardMarkup()
    if XROCKET_TOKEN:
        kb.add(InlineKeyboardButton("🚀 Оплатить через xRocket", callback_data="pay_xrocket"))
    if CRYPTOBOT_TOKEN:
        kb.add(InlineKeyboardButton("💎 Оплатить через CryptoBot", callback_data="pay_crypto"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"prod_{prod_id}"))
    total_price = round(price * qty, 2)
    bot.send_message(
        chat_id,
        f"🛒 Вы выбрали: <b>{name}</b>\n"
        f"📦 Количество: <b>{qty} шт.</b>\n"
        f"💰 К оплате: <b>{total_price}$</b>\n\n"
        "<i>⏳ При нажатии на оплату товар забронируется на 10 минут.</i>",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("qty_"))
def select_quantity(call):
    _, prod_id, qty = call.data.split("_")
    prod_id, qty = int(prod_id), int(qty)
    cursor.execute(
        "SELECT name, price, content_type FROM products WHERE id = ?", (prod_id,)
    )
    prod = cursor.fetchone()
    if not prod:
        return bot.answer_callback_query(call.id, "Товар не найден!")

    name, price, c_type = prod
    stock = get_free_stock_count(prod_id, c_type)
    if c_type == "lines" and qty > stock:
        return bot.answer_callback_query(
            call.id, f"❌ В наличии только {stock} шт.!", show_alert=True
        )
    user_quantity_temp[call.from_user.id] = {"prod_id": prod_id, "qty": qty}
    total_price = round(price * qty, 2)
    kb = InlineKeyboardMarkup()
    if XROCKET_TOKEN:
        kb.add(InlineKeyboardButton("🚀 Оплатить через xRocket", callback_data="pay_xrocket"))
    if CRYPTOBOT_TOKEN:
        kb.add(InlineKeyboardButton("💎 Оплатить через CryptoBot", callback_data="pay_crypto"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data=f"prod_{prod_id}"))
    bot.edit_message_text(
        f"🛒 Вы выбрали: <b>{name}</b>\n"
        f"📦 Количество: <b>{qty} шт.</b>\n"
        f"💰 К оплате: <b>{total_price}$</b>\n\n"
        "<i>⏳ При нажатии на оплату товар забронируется на 10 минут.</i>",
        call.message.chat.id, call.message.message_id, reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("customqty_"))
def custom_qty_start(call):
    prod_id = int(call.data.split("_")[1])
    msg = bot.send_message(
        call.message.chat.id, "✏️ Введите нужное количество цифрой (например: <code>15</code>):"
    )
    bot.register_next_step_handler(msg, process_custom_qty, prod_id)


def process_custom_qty(message, prod_id):
    try:
        qty = int((message.text or "").strip())
        if qty <= 0:
            raise ValueError
    except ValueError:
        msg = bot.send_message(message.chat.id, "❌ Введите корректное число больше 0:")
        bot.register_next_step_handler(msg, process_custom_qty, prod_id)
        return

    cursor.execute(
        "SELECT name, price, content_type FROM products WHERE id = ?", (prod_id,)
    )
    prod = cursor.fetchone()
    if not prod:
        return bot.send_message(message.chat.id, "Товар не найден.")

    name, price, c_type = prod
    stock = get_free_stock_count(prod_id, c_type)
    if c_type == "lines" and qty > stock:
        msg = bot.send_message(
            message.chat.id,
            f"❌ Недостаточно товара! В наличии только {stock} шт. Введите меньше:",
        )
        bot.register_next_step_handler(msg, process_custom_qty, prod_id)
        return

    user_quantity_temp[message.from_user.id] = {"prod_id": prod_id, "qty": qty}
    total_price = round(price * qty, 2)
    kb = InlineKeyboardMarkup()
    if XROCKET_TOKEN:
        kb.add(InlineKeyboardButton("🚀 Оплатить через xRocket", callback_data="pay_xrocket"))
    if CRYPTOBOT_TOKEN:
        kb.add(InlineKeyboardButton("💎 Оплатить через CryptoBot", callback_data="pay_crypto"))
    kb.add(InlineKeyboardButton("🔙 В каталог", callback_data="catalog"))
    bot.send_message(
        message.chat.id,
        f"🛒 Вы выбрали: <b>{name}</b>\n"
        f"📦 Количество: <b>{qty} шт.</b>\n"
        f"💰 К оплате: <b>{total_price}$</b>\n\n"
        "<i>⏳ При нажатии на оплату товар забронируется на 10 минут.</i>",
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda call: call.data in ("pay_xrocket", "pay_crypto"))
def process_payment_creation(call):
    user_data = user_quantity_temp.get(call.from_user.id)
    if not user_data:
        return bot.answer_callback_query(call.id, "Сессия устарела. Откройте товар заново.", show_alert=True)

    prod_id = user_data["prod_id"]
    qty = int(user_data["qty"])
    method = "xrocket" if call.data == "pay_xrocket" else "crypto"

    cursor.execute(
        "SELECT name, price, content_type FROM products WHERE id = ?", (prod_id,)
    )
    prod = cursor.fetchone()
    if not prod:
        return bot.answer_callback_query(call.id, "Товар не найден.", show_alert=True)
    name, price, c_type = prod
    total_price = round(float(price) * qty, 2)
    current_time = int(time.time())

    reserved_item_ids = []
    reservation_tag = f"pending_{call.from_user.id}_{current_time}"
    if c_type == "lines":
        cursor.execute(
            """SELECT id FROM product_items
               WHERE product_id = ? AND (reserved_until = 0 OR reserved_until <= ?)
               ORDER BY id ASC LIMIT ?""",
            (prod_id, current_time, qty),
        )
        rows = cursor.fetchall()
        if len(rows) < qty:
            return bot.answer_callback_query(
                call.id, "❌ Этого количества уже нет в наличии.", show_alert=True
            )
        reserved_item_ids = [row[0] for row in rows]
        expire_time = current_time + 600
        for item_id in reserved_item_ids:
            cursor.execute(
                """UPDATE product_items
                   SET reserved_until = ?, reservation_order = ?
                   WHERE id = ? AND (reserved_until = 0 OR reserved_until <= ?)""",
                (expire_time, reservation_tag, item_id, current_time),
            )
        conn.commit()
        # Verify the reservation succeeded for every requested item.
        placeholders = ",".join("?" for _ in reserved_item_ids)
        cursor.execute(
            f"SELECT COUNT(*) FROM product_items WHERE id IN ({placeholders}) AND reservation_order = ?",
            (*reserved_item_ids, reservation_tag),
        )
        if cursor.fetchone()[0] != len(reserved_item_ids):
            release_reserved_items(reserved_item_ids)
            return bot.answer_callback_query(
                call.id, "❌ Не удалось забронировать товар. Попробуйте ещё раз.", show_alert=True
            )

    bot.answer_callback_query(call.id, "⏳ Создаём счёт...")
    if method == "xrocket":
        pay_url, inv_id, err = create_xrocket_invoice(total_price)
        method_label = "xRocket"
    else:
        pay_url, inv_id, err = create_cryptobot_invoice(total_price)
        method_label = "CryptoBot"

    if not pay_url:
        release_reserved_items(reserved_item_ids)
        return bot.send_message(
            call.message.chat.id,
            f"❌ <b>Не удалось создать счёт.</b>\n<code>{err}</code>\n\nБронь товара снята.",
        )

    item_ids_str = ",".join(map(str, reserved_item_ids))
    if c_type == "lines":
        for item_id in reserved_item_ids:
            cursor.execute(
                "UPDATE product_items SET reservation_order = ? WHERE id = ?",
                (str(inv_id), item_id),
            )
    username = f"@{call.from_user.username}" if call.from_user.username else "Без юзернейма"
    cursor.execute(
        """INSERT OR REPLACE INTO orders
           (invoice_id, user_id, username, product_id, product_name, quantity,
            total_price, pay_method, item_ids, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (str(inv_id), call.from_user.id, username, prod_id, name, qty,
         total_price, method, item_ids_str),
    )
    conn.commit()

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔗 Перейти к оплате", url=pay_url))
    kb.add(InlineKeyboardButton("🔄 Проверить оплату", callback_data=f"check_{inv_id}"))
    kb.add(InlineKeyboardButton("❌ Отменить покупку", callback_data=f"cancelbuy_{inv_id}"))
    try:
        bot.edit_message_text(
            f"🧾 <b>Счёт создан</b>\n\n"
            f"📦 Товар: <b>{name}</b>\n"
            f"🔢 Количество: <b>{qty} шт.</b>\n"
            f"💳 Способ: <b>{method_label}</b>\n"
            f"💰 Сумма: <b>{total_price:.2f}$</b>\n"
            f"⏳ Бронь действует 10 минут.\n\n"
            "Если не оплатить за это время, товар автоматически вернётся в продажу.",
            call.message.chat.id, call.message.message_id, reply_markup=kb,
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            f"🧾 Счёт создан. Товар: {name}, количество: {qty}, сумма: {total_price:.2f}$",
            reply_markup=kb,
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("cancelbuy_"))
def cancel_purchase(call):
    invoice_id = call.data.split("_", 1)[1]
    cursor.execute(
        "SELECT user_id, item_ids, status FROM orders WHERE invoice_id = ?",
        (invoice_id,),
    )
    order = cursor.fetchone()
    if not order:
        return bot.answer_callback_query(call.id, "Заказ не найден.", show_alert=True)
    owner_id, item_ids_str, status = order
    if owner_id != call.from_user.id:
        return bot.answer_callback_query(call.id, "Это не ваш заказ.", show_alert=True)
    if status != "pending":
        return bot.answer_callback_query(call.id, "Этот заказ уже обработан.", show_alert=True)

    item_ids = [value for value in (item_ids_str or "").split(",") if value.isdigit()]
    release_reserved_items(item_ids)
    cursor.execute(
        "UPDATE orders SET status = 'cancelled' WHERE invoice_id = ? AND status = 'pending'",
        (invoice_id,),
    )
    conn.commit()
    user_quantity_temp.pop(call.from_user.id, None)

    try:
        bot.edit_message_text(
            "❌ <b>Покупка отменена.</b>\n"
            "Бронь снята, товар снова доступен в каталоге.\n"
            "⚠️ Не оплачивайте старый счёт.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🛍 В каталог", callback_data="catalog")
            ),
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Покупка отменена, бронь снята.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("check_"))
def check_payment(call):
    invoice_id = call.data.split("_", 1)[1]
    cursor.execute(
        """SELECT user_id, username, product_id, product_name, quantity,
                  total_price, pay_method, item_ids, status
           FROM orders WHERE invoice_id = ?""",
        (invoice_id,),
    )
    order = cursor.fetchone()
    if not order:
        return bot.answer_callback_query(call.id, "Заказ не найден. Создайте новый.", show_alert=True)

    owner_id, username, prod_id, name, qty, total_price, method, item_ids_str, status = order
    if owner_id != call.from_user.id:
        return bot.answer_callback_query(call.id, "Это не ваш заказ.", show_alert=True)
    if status == "paid":
        return bot.answer_callback_query(call.id, "Этот заказ уже выдан.", show_alert=True)
    if status != "pending":
        return bot.answer_callback_query(call.id, "Заказ отменён или уже закрыт.", show_alert=True)

    is_paid = check_xrocket_invoice(invoice_id) if method == "xrocket" else check_cryptobot_invoice(invoice_id)
    if not is_paid:
        return bot.answer_callback_query(
            call.id, "❌ Оплата пока не подтверждена. Подождите немного и проверьте снова.",
            show_alert=True,
        )

    cursor.execute(
        "SELECT name, price, content_type, content FROM products WHERE id = ?", (prod_id,)
    )
    prod = cursor.fetchone()
    if not prod:
        return bot.answer_callback_query(call.id, "Товар удалён. Напишите администратору.", show_alert=True)
    name, price, c_type, content = prod
    item_ids = [int(i) for i in (item_ids_str or "").split(",") if i.isdigit()]
    items_given_list = []

    if c_type == "lines":
        if len(item_ids) != qty:
            return bot.answer_callback_query(
                call.id, "Количество зарезервированных товаров не совпало. Напишите администратору.",
                show_alert=True,
            )
        current_time = int(time.time())
        placeholders = ",".join("?" for _ in item_ids)
        cursor.execute(
            f"""SELECT id, item_data FROM product_items
                WHERE id IN ({placeholders}) AND reservation_order = ?
                  AND reserved_until > ?""",
            (*item_ids, str(invoice_id), current_time),
        )
        reserved_rows = cursor.fetchall()
        if len(reserved_rows) != qty:
            # Never deliver stock that may have been released and sold to someone else.
            cursor.execute(
                "UPDATE orders SET status = 'payment_issue' WHERE invoice_id = ?",
                (invoice_id,),
            )
            conn.commit()
            bot.send_message(
                ADMIN_ID,
                f"⚠️ Оплата получена, но бронь истекла.\nЗаказ: <code>{invoice_id}</code>\n"
                f"Покупатель: <code>{owner_id}</code>, товар: {name}, сумма: {total_price}$",
            )
            return bot.answer_callback_query(
                call.id, "Оплата найдена, но бронь истекла. Администратор свяжется с вами.",
                show_alert=True,
            )

        items_given_list = [row[1] for row in reserved_rows]
        # Mark paid before delivery to prevent duplicate fulfilment on repeated taps.
        cursor.execute(
            "UPDATE orders SET status = 'paid' WHERE invoice_id = ? AND status = 'pending'",
            (invoice_id,),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return bot.answer_callback_query(call.id, "Заказ уже обрабатывается.", show_alert=True)
        cursor.executemany("DELETE FROM product_items WHERE id = ?", [(i,) for i in item_ids])
        items_given_text = "\n".join(items_given_list)
    else:
        cursor.execute(
            "UPDATE orders SET status = 'paid' WHERE invoice_id = ? AND status = 'pending'",
            (invoice_id,),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return bot.answer_callback_query(call.id, "Заказ уже обрабатывается.", show_alert=True)
        items_given_text = "Файл (документ)"

    cursor.execute(
        """INSERT INTO purchases
           (user_id, username, product_name, items_given, total_price, pay_method,
            quantity, invoice_id, date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (owner_id, username, name, items_given_text, total_price, method, qty, str(invoice_id)),
    )
    conn.commit()
    user_quantity_temp.pop(owner_id, None)

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    if c_type == "lines":
        bot.send_message(
            call.message.chat.id,
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"📦 Товар: <b>{name}</b>\n"
            f"🔢 Количество: <b>{qty} шт.</b>\n"
            f"🎁 <b>Ваш товар:</b>\n<code>{items_given_text}</code>",
        )
    else:
        try:
            bot.send_document(
                call.message.chat.id, content,
                caption=f"✅ Оплата подтверждена!\n📦 {name}\n🔢 Количество: {qty} шт.",
            )
        except Exception as exc:
            bot.send_message(
                call.message.chat.id,
                f"Оплата подтверждена, но файл не удалось отправить. "
                f"Напишите администратору и укажите заказ <code>{invoice_id}</code>.\n"
                f"Ошибка: {exc}",
            )

    try:
        bot.send_message(
            ADMIN_ID,
            f"💰 <b>Новая продажа!</b>\n"
            f"👤 Покупатель: {username} (<code>{owner_id}</code>)\n"
            f"📦 Товар: <b>{name}</b>\n"
            f"🔢 Количество: {qty} шт.\n"
            f"💵 Сумма: {float(total_price):.2f}$ ({method.upper()})\n"
            f"🧾 Заказ: <code>{invoice_id}</code>",
        )
    except Exception:
        pass


# ==========================================
# ADMIN PANEL
# ==========================================
@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if message.from_user.id != ADMIN_ID:
        return
    show_admin_menu(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_panel")
def cb_admin(call):
    if call.from_user.id != ADMIN_ID:
        return
    bot.clear_step_handler_by_chat_id(call.message.chat.id)
    show_admin_menu(call.message.chat.id, call.message.message_id)


def show_admin_menu(chat_id, message_id=None):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Добавить новый товар", callback_data="admin_add"))
    kb.add(InlineKeyboardButton("✏️ Управление товарами", callback_data="admin_manage"))
    kb.add(InlineKeyboardButton("📊 История покупок", callback_data="admin_buyers"))
    kb.add(InlineKeyboardButton("🏠 В каталог", callback_data="catalog"))
    text = "👑 <b>Панель администратора:</b>"
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "admin_add")
def admin_add_start(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            "Введите <b>название</b> нового товара:",
            call.message.chat.id, call.message.message_id,
            reply_markup=InlineKeyboardMarkup(),
        )
        msg = call.message
    except Exception:
        msg = bot.send_message(call.message.chat.id, "Введите <b>название</b> нового товара:")
    bot.register_next_step_handler(msg, step_add_name)


def step_add_name(message):
    name = (message.text or "").strip()
    if not name:
        msg = bot.send_message(message.chat.id, "❌ Название не должно быть пустым. Введите название:")
        bot.register_next_step_handler(msg, step_add_name)
        return
    admin_temp[message.chat.id] = {"name": name}
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⏭ Пропустить описание", callback_data="skip_product_description"))
    msg = bot.send_message(
        message.chat.id,
        "Введите <b>описание</b> товара или нажмите «Пропустить описание»:",
        reply_markup=kb,
    )
    bot.register_next_step_handler(msg, step_add_desc)


@bot.callback_query_handler(func=lambda call: call.data == "skip_product_description")
def skip_product_description(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    if chat_id not in admin_temp:
        return bot.answer_callback_query(call.id, "Сессия добавления товара устарела.", show_alert=True)
    bot.clear_step_handler_by_chat_id(chat_id)
    admin_temp[chat_id]["desc"] = ""
    bot.answer_callback_query(call.id, "Описание пропущено.")
    msg = bot.send_message(chat_id, "Введите <b>цену за 1 шт. в долларах ($)</b>:")
    bot.register_next_step_handler(msg, step_add_price)


def step_add_desc(message):
    if message.chat.id not in admin_temp:
        return bot.send_message(message.chat.id, "Сессия добавления товара устарела. Начните заново.")
    admin_temp[message.chat.id]["desc"] = (message.text or "").strip()
    msg = bot.send_message(message.chat.id, "Введите <b>цену за 1 шт. в долларах ($)</b>:")
    bot.register_next_step_handler(msg, step_add_price)


def step_add_price(message):
    try:
        price = float(message.text.replace(",", "."))
        if price <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        msg = bot.send_message(message.chat.id, "❌ Введите положительное число:")
        bot.register_next_step_handler(msg, step_add_price)
        return

    admin_temp[message.chat.id]["price"] = price
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📋 Построчно (1 строка = 1 шт)", callback_data="addtype_lines"))
    kb.add(InlineKeyboardButton("📁 Целый файл", callback_data="addtype_file"))
    bot.send_message(message.chat.id, "Выберите формат продажи:", reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("addtype_"))
def step_choose_type(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id)
    c_type = call.data.split("_")[1]
    admin_temp[call.message.chat.id]["c_type"] = c_type
    if c_type == "lines":
        msg = bot.send_message(call.message.chat.id, "📥 Отправьте строки товара (текстом или .txt файлом):")
        bot.register_next_step_handler(msg, step_add_content_lines)
    else:
        msg = bot.send_message(call.message.chat.id, "📁 Отправьте файл документом:")
        bot.register_next_step_handler(msg, step_add_content_file)


def step_add_content_lines(message):
    data = admin_temp.get(message.chat.id)
    if not data:
        return bot.send_message(message.chat.id, "Сессия добавления товара устарела. Начните заново.")
    lines = extract_lines_from_message(message)
    if not lines:
        msg = bot.send_message(message.chat.id, "❌ Строки не найдены! Повторите отправку:")
        bot.register_next_step_handler(msg, step_add_content_lines)
        return

    cursor.execute(
        """INSERT INTO products (name, description, price, content_type, content)
           VALUES (?, ?, ?, 'lines', '')""",
        (data["name"], data["desc"], data["price"]),
    )
    prod_id = cursor.lastrowid
    for line in lines:
        cursor.execute(
            "INSERT INTO product_items (product_id, item_data) VALUES (?, ?)",
            (prod_id, line),
        )
    conn.commit()
    admin_temp.pop(message.chat.id, None)
    sent = bot.send_message(message.chat.id, f"✅ Товар создан! Загружено: <b>{len(lines)} шт.</b>")
    show_admin_menu(message.chat.id, sent.message_id)


def step_add_content_file(message):
    if not message.document:
        msg = bot.send_message(message.chat.id, "❌ Отправьте файл документом!")
        bot.register_next_step_handler(msg, step_add_content_file)
        return
    data = admin_temp.get(message.chat.id)
    if not data:
        return bot.send_message(message.chat.id, "Сессия добавления товара устарела. Начните заново.")
    cursor.execute(
        """INSERT INTO products (name, description, price, content_type, content)
           VALUES (?, ?, ?, 'file', ?)""",
        (data["name"], data["desc"], data["price"], message.document.file_id),
    )
    conn.commit()
    admin_temp.pop(message.chat.id, None)
    sent = bot.send_message(message.chat.id, "✅ Файловый товар добавлен!")
    show_admin_menu(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_manage")
def admin_manage_prods(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id)
    cursor.execute("SELECT id, name, price, content_type FROM products")
    prods = cursor.fetchall()
    if not prods:
        return bot.answer_callback_query(call.id, "Товаров нет", show_alert=True)

    kb = InlineKeyboardMarkup()
    for p_id, name, price, c_type in prods:
        stock = get_free_stock_count(p_id, c_type)
        kb.add(InlineKeyboardButton(
            f"{name} ({price}$) | Свободно: {stock} шт.",
            callback_data=f"admprod_{p_id}",
        ))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text(
        "Управление товарами:", call.message.chat.id,
        call.message.message_id, reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("admprod_"))
def admin_prod_actions(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id)
    prod_id = int(call.data.split("_")[1])
    cursor.execute(
        "SELECT name, price, content_type FROM products WHERE id = ?", (prod_id,)
    )
    prod = cursor.fetchone()
    if not prod:
        return
    name, price, c_type = prod
    stock = get_free_stock_count(prod_id, c_type)
    kb = InlineKeyboardMarkup()
    if c_type == "lines":
        kb.add(InlineKeyboardButton("📥 Пополнить товар (+ строки)", callback_data=f"restock_{prod_id}"))
    kb.add(InlineKeyboardButton("🗑 Удалить товар", callback_data=f"delprod_{prod_id}"))
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_manage"))
    bot.edit_message_text(
        f"Товар: <b>{name}</b>\nЦена: {price}$\nСвободно: {stock} шт.",
        call.message.chat.id, call.message.message_id, reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("restock_"))
def admin_restock_start(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id)
    prod_id = int(call.data.split("_")[1])
    msg = bot.send_message(call.message.chat.id, "📥 Отправьте новые строки для пополнения:")
    bot.register_next_step_handler(msg, step_restock_save, prod_id)


def step_restock_save(message, prod_id):
    lines = extract_lines_from_message(message)
    if not lines:
        msg = bot.send_message(message.chat.id, "❌ Строки не найдены:")
        bot.register_next_step_handler(msg, step_restock_save, prod_id)
        return
    for line in lines:
        cursor.execute(
            "INSERT INTO product_items (product_id, item_data) VALUES (?, ?)",
            (prod_id, line),
        )
    conn.commit()
    sent = bot.send_message(message.chat.id, f"✅ Добавлено: <b>+{len(lines)} шт.</b>")
    show_admin_menu(message.chat.id, sent.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("delprod_"))
def admin_del_prod(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id)
    prod_id = int(call.data.split("_")[1])
    cursor.execute("DELETE FROM products WHERE id = ?", (prod_id,))
    cursor.execute("DELETE FROM product_items WHERE product_id = ?", (prod_id,))
    conn.commit()
    bot.answer_callback_query(call.id, "✅ Товар удален!", show_alert=True)
    show_admin_menu(call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "admin_buyers")
def admin_show_buyers(call):
    if call.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(call.id)
    cursor.execute(
        """SELECT username, user_id, product_name, quantity, total_price, pay_method, date
           FROM purchases ORDER BY id DESC LIMIT 20"""
    )
    rows = cursor.fetchall()
    if not rows:
        return bot.answer_callback_query(call.id, "Покупок ещё не было.", show_alert=True)

    def safe(value):
        value = str(value if value is not None else "—")
        return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    lines = ["📊 <b>Последние покупки</b>", ""]
    for uname, uid, pname, quantity, price, method, date in rows:
        uname = safe(uname or "Без юзернейма")
        pname = safe(pname or "Товар")
        quantity = int(quantity or 1)
        method = safe((method or "unknown").upper())
        date = safe(date or "Дата неизвестна")
        lines.append(
            f"👤 {uname} (<code>{uid}</code>)\n"
            f"🛒 <b>{pname}</b>\n"
            f"🔢 Количество: {quantity} шт.\n"
            f"💰 Сумма: <b>{float(price or 0):.2f}$</b> · {method}\n"
            f"🕒 {date}\n"
            "──────────────"
        )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    try:
        bot.edit_message_text(
            "\n\n".join(lines), call.message.chat.id, call.message.message_id,
            reply_markup=kb,
        )
    except Exception:
        bot.send_message(call.message.chat.id, "\n\n".join(lines), reply_markup=kb)


if __name__ == "__main__":
    if not BOT_TOKEN or BOT_TOKEN == "ТОКЕН_БОТА_ОТ_BOTFATHER":
        raise SystemExit(
            "❌ Укажите BOT_TOKEN: задайте переменную окружения BOT_TOKEN "
            "или вставьте токен прямо в файл."
        )
    print(f"📁 База данных: {DB_PATH}")
    if not XROCKET_TOKEN and not CRYPTOBOT_TOKEN:
        print("⚠️ Не настроен ни один способ оплаты: добавьте XROCKET_TOKEN и/или CRYPTOBOT_TOKEN.")

    # Telegram may temporarily time out on mobile networks. Increase HTTP timeouts
    # and restart polling instead of terminating the bot on a transient connection error.
    try:
        from telebot import apihelper
        apihelper.CONNECT_TIMEOUT = 30
        apihelper.READ_TIMEOUT = 60
    except Exception:
        pass

    print("✅ Бот запущен. База данных и миграции проверены.")
    while True:
        try:
            # skip_pending=False avoids the separate startup getUpdates call
            # that can fail before infinity_polling begins its retry loop.
            bot.infinity_polling(
                skip_pending=False,
                timeout=30,
                long_polling_timeout=30,
                restart_on_change=False,
            )
        except KeyboardInterrupt:
            print("🛑 Бот остановлен.")
            break
        except Exception as exc:
            print(f"⚠️ Временная ошибка соединения с Telegram: {type(exc).__name__}: {exc}")
            print("🔁 Повторное подключение через 5 секунд...")
            time.sleep(5)
