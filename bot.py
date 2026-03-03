import os
import logging
import base64
import re
from datetime import datetime, timedelta
from typing import Dict, Optional

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
from openai import AsyncOpenAI

# Google Sheets
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ================== ПЕРЕМЕННЫЕ ==================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ⚠️ ВАЖНО: Укажите свой ID администратора (узнать можно у @userinfobot)
ADMIN_ID = 1346576296  # ✅ ваш ID

# Путь к JSON-ключу сервисного аккаунта (можно положить в корень проекта)
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "credentials.json")
# ID вашей Google Sheets (можно взять из URL)
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "your_spreadsheet_id")
# Имя листа (по умолчанию "users")
SHEET_NAME = os.getenv("SHEET_NAME", "users")

if not TELEGRAM_TOKEN:
    raise ValueError("Не найден TELEGRAM_TOKEN")
if not OPENAI_API_KEY:
    raise ValueError("Не найден OPENAI_API_KEY")

GPT_MODEL = "gpt-4o-mini"

# ================== ХРАНЕНИЕ ИСТОРИИ ДИАЛОГА ==================
user_history: Dict[int, list] = {}
MAX_HISTORY = 20

def get_history(user_id: int):
    return user_history.get(user_id, [])

def add_to_history(user_id: int, role: str, content: str):
    if user_id not in user_history:
        user_history[user_id] = []
    user_history[user_id].append({"role": role, "content": content})
    if len(user_history[user_id]) > MAX_HISTORY:
        user_history[user_id] = user_history[user_id][-MAX_HISTORY:]

# ================== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ==================

def init_google_sheets():
    """Подключается к Google Sheets и возвращает объект листа."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SHEETS_CREDENTIALS, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    return sheet

def load_users_from_sheet():
    """
    Загружает всех пользователей из таблицы.
    Ожидаемая структура таблицы (первая строка — заголовки):
    | username | phone       | first_seen              | trial_expires           | paid |
    """
    sheet = init_google_sheets()
    records = sheet.get_all_records()
    users_by_username = {}
    users_by_phone = {}
    for row in records:
        username = row.get("username", "").strip().lower()
        phone = row.get("phone", "").strip()
        if username:
            users_by_username[username] = {
                "phone": phone,
                "first_seen": row.get("first_seen"),
                "trial_expires": row.get("trial_expires"),
                "paid": str(row.get("paid", "")).upper() == "TRUE"
            }
        if phone:
            users_by_phone[phone] = {
                "username": username,
                "first_seen": row.get("first_seen"),
                "trial_expires": row.get("trial_expires"),
                "paid": str(row.get("paid", "")).upper() == "TRUE"
            }
    return users_by_username, users_by_phone

def add_user_to_sheet(username: str = None, phone: str = None):
    """Добавляет нового пользователя в таблицу с текущей датой первого входа и временем истечения пробного периода (+12 часов)."""
    now = datetime.now()
    expires = now + timedelta(hours=12)
    sheet = init_google_sheets()
    next_row = len(sheet.get_all_values()) + 1
    sheet.update(f"A{next_row}:E{next_row}", [[
        username or "",
        phone or "",
        now.strftime("%Y-%m-%d %H:%M:%S"),
        expires.strftime("%Y-%m-%d %H:%M:%S"),
        "FALSE"
    ]])

def update_user_paid(identifier: str, by_username: bool = True):
    """Отмечает пользователя как оплатившего (ставит TRUE в колонке paid). Идентификатор может быть username или телефон."""
    sheet = init_google_sheets()
    col = 1 if by_username else 2  # колонка A для username, B для телефона
    cells = sheet.findall(identifier)
    for cell in cells:
        if cell.col == col:
            sheet.update_cell(cell.row, 5, "TRUE")  # колонка E — paid
            return

def check_user_access(identifier: str, by_username: bool = True) -> bool:
    """
    Проверяет, есть ли у пользователя доступ.
    identifier: username (by_username=True) или номер телефона (by_username=False).
    Возвращает True, если:
    - пользователь оплатил (paid = TRUE), ИЛИ
    - пробный период ещё не истёк (trial_expires > now)
    """
    users_by_username, users_by_phone = load_users_from_sheet()
    user = None
    if by_username:
        user = users_by_username.get(identifier.lower())
    else:
        user = users_by_phone.get(identifier)
    if not user:
        # Новый пользователь — добавляем в таблицу и даём доступ
        if by_username:
            add_user_to_sheet(username=identifier)
        else:
            add_user_to_sheet(phone=identifier)
        return True
    if user.get("paid"):
        return True
    expires_str = user.get("trial_expires")
    if expires_str:
        try:
            expires = datetime.strptime(expires_str, "%Y-%m-%d %H:%M:%S")
            if datetime.now() < expires:
                return True
        except:
            pass
    return False

# ================== ПРОМПТ ==================

SYSTEM_PROMPT = """
Ты школьный помощник 1–9 классов. Твоя цель — научить ученика решать задачи, а не просто дать ответ.

Твой формат общения:
1. Сначала кратко поддержи ученика.
2. Объясни тему и покажи пример решения.
3. Затем дай ученику **похожий пример для самостоятельного решения**. Чётко сформулируй задание, например: «А теперь попробуй сам: реши пример 7 × 8».
4. Дождись ответа ученика.
5. Проверь ответ:
   - Если ответ правильный, похвали и переходи к следующей теме или предложи ещё один пример для закрепления.
   - Если ответ неправильный, объясни ошибку и дай **новый аналогичный пример** для повторной попытки.
6. Продолжай так, пока ученик не усвоит материал.

Всегда отвечай строго в формате:
Короткая поддержка (1 строка).
Заголовок с темой.
Пример.
Главное правило с 👉
Разбор по шагам (Шаг 1, Шаг 2, Шаг 3).
В конце вопрос или задание.

Пиши короткими абзацами.
Не используй сложные слова.
Не давай готовый ответ, пока ученик сам не решит.

**Важно:** Не используй LaTeX-разметку. Вместо \( \cdot \) пиши «×» или «*». Все математические выражения записывай в обычном тексте, например: 2 × 4 = 8.
"""

# ================== ЛОГИ ==================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# ================== ФУНКЦИЯ ДЛЯ ОЧИСТКИ ОТ LaTeX ==================

def clean_latex(text: str) -> str:
    text = re.sub(r'\\\(|\\\)', '', text)
    text = text.replace(r'\cdot', '×')
    return text


# ================== СОСТОЯНИЯ ДЛЯ ДИАЛОГА ==================
ASK_PHONE = 1

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    if username:
        # Есть username — проверяем доступ сразу
        if check_user_access(username, by_username=True):
            await update.message.reply_text(
                "Привет! 👋 У вас есть доступ. Просто отправьте мне пример или фото задания, и мы вместе его решим."
            )
        else:
            await update.message.reply_text(
                "🔒 Ваш пробный период (12 часов) истёк.\n"
                "Для получения полного доступа обратитесь к администратору: @ваш_контакт"
            )
        return ConversationHandler.END

    # Нет username — просим поделиться номером телефона
    contact_keyboard = KeyboardButton("📱 Отправить номер телефона", request_contact=True)
    reply_markup = ReplyKeyboardMarkup([[contact_keyboard]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        "Чтобы я мог вас идентифицировать, пожалуйста, поделитесь своим номером телефона.\n"
        "Это займёт всего секунду.",
        reply_markup=reply_markup
    )
    return ASK_PHONE

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    if not contact:
        # Если пользователь нажал отмену или отправил что-то другое
        await update.message.reply_text("Вы не отправили номер. Для доступа к боту необходимо поделиться номером.")
        return ConversationHandler.END

    phone = contact.phone_number
    # Проверяем доступ по номеру телефона
    if check_user_access(phone, by_username=False):
        await update.message.reply_text(
            "✅ Спасибо! Ваш номер сохранён. Теперь у вас есть доступ. Отправляйте задания.",
            reply_markup=None
        )
    else:
        await update.message.reply_text(
            "🔒 Ваш пробный период (12 часов) истёк.\n"
            "Для получения полного доступа обратитесь к администратору: @ваш_контакт",
            reply_markup=None
        )
    return ConversationHandler.END

# ================== КОМАНДА ДЛЯ АДМИНА: ПОМЕТИТЬ ПОЛЬЗОВАТЕЛЯ КАК ОПЛАТИВШЕГО ==================

async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Использование: /pay @username или /pay +71234567890"""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет доступа к этой команде.")
        return

    if not context.args:
        await update.message.reply_text("Укажите username или номер телефона пользователя, например: /pay @ivanov  или /pay +71234567890")
        return

    target = context.args[0].strip()
    if target.startswith('@'):
        # это username
        target = target[1:].lower()
        update_user_paid(target, by_username=True)
        await update.message.reply_text(f"✅ Пользователь @{target} отмечен как оплативший. Доступ открыт.")
    elif target.startswith('+'):
        # это номер телефона
        update_user_paid(target, by_username=False)
        await update.message.reply_text(f"✅ Пользователь с номером {target} отмечен как оплативший. Доступ открыт.")
    else:
        await update.message.reply_text("Неверный формат. Укажите username (с @) или номер телефона (с +).")


# ================== СТАТИСТИКА (ТОЛЬКО ДЛЯ АДМИНА) ==================

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет доступа к этой команде.")
        return

    users_by_username, users_by_phone = load_users_from_sheet()
    total = len(users_by_username) + len(users_by_phone)  # могут быть пересечения, но для простоты
    paid = sum(1 for u in list(users_by_username.values()) + list(users_by_phone.values()) if u.get("paid"))
    trial = total - paid

    msg = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Всего пользователей в таблице: {total}\n"
        f"💳 Оплатили: {paid}\n"
        f"⏳ На пробном периоде: {trial}\n\n"
    )

    await update.message.reply_text(msg, parse_mode="Markdown")


# ================== ОБРАБОТКА ТЕКСТА ==================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    # Пытаемся идентифицировать пользователя: сначала по username, потом по номеру (если сохранили ранее в context)
    # Для простоты будем проверять оба варианта
    users_by_username, users_by_phone = load_users_from_sheet()
    user_id = user.id

    # Проверяем доступ по username, если есть
    if username:
        if check_user_access(username, by_username=True):
            # доступ есть
            pass
        else:
            await update.message.reply_text(
                "🔒 Ваш пробный период (12 часов) истёк.\n"
                "Для получения полного доступа обратитесь к администратору: @ваш_контакт"
            )
            return
    else:
        # нет username — нужно искать по телефону, который мог быть сохранён ранее
        # В context мы не сохраняем телефон, поэтому придётся искать по всем записям
        # Можно хранить в context.user_data phone после того как пользователь поделился
        phone = context.user_data.get('phone')
        if phone:
            if check_user_access(phone, by_username=False):
                # доступ есть
                pass
            else:
                await update.message.reply_text(
                    "🔒 Ваш пробный период (12 часов) истёк.\n"
                    "Для получения полного доступа обратитесь к администратору: @ваш_контакт"
                )
                return
        else:
            # Пользователь без username и без сохранённого номера — просим поделиться номером
            contact_keyboard = KeyboardButton("📱 Отправить номер телефона", request_contact=True)
            reply_markup = ReplyKeyboardMarkup([[contact_keyboard]], one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(
                "Чтобы я мог вас идентифицировать, пожалуйста, поделитесь своим номером телефона.",
                reply_markup=reply_markup
            )
            # Здесь нужно перейти в состояние ASK_PHONE, но мы не в диалоге, поэтому просто выходим
            return

    # Если доступ есть, обрабатываем сообщение
    user_message = update.message.text
    history = get_history(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "user", "content": user_message}]

    try:
        response = await client.chat.completions.create(
            model=GPT_MODEL,
            messages=messages,
            max_tokens=1500,
        )
        answer = response.choices[0].message.content
        answer = clean_latex(answer)

        add_to_history(user_id, "user", user_message)
        add_to_history(user_id, "assistant", answer)

        await update.message.reply_text(answer)

    except Exception as e:
        logger.error(f"Ошибка OpenAI: {e}")
        await update.message.reply_text("❌ Ошибка при обработке запроса.")


# ================== ОБРАБОТКА ФОТО ==================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    user_id = user.id

    # Аналогичная проверка доступа, как в handle_text
    if username:
        if not check_user_access(username, by_username=True):
            await update.message.reply_text(
                "🔒 Ваш пробный период (12 часов) истёк.\n"
                "Для получения полного доступа обратитесь к администратору: @ваш_контакт"
            )
            return
    else:
        phone = context.user_data.get('phone')
        if phone:
            if not check_user_access(phone, by_username=False):
                await update.message.reply_text(
                    "🔒 Ваш пробный период (12 часов) истёк.\n"
                    "Для получения полного доступа обратитесь к администратору: @ваш_контакт"
                )
                return
        else:
            contact_keyboard = KeyboardButton("📱 Отправить номер телефона", request_contact=True)
            reply_markup = ReplyKeyboardMarkup([[contact_keyboard]], one_time_keyboard=True, resize_keyboard=True)
            await update.message.reply_text(
                "Чтобы я мог вас идентифицировать, пожалуйста, поделитесь своим номером телефона.",
                reply_markup=reply_markup
            )
            return

    try:
        await update.message.chat.send_action("typing")
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        user_message = update.message.caption or "Объясни задачу на фото."

        history = get_history(user_id)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_message},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                    },
                ],
            }
        ]

        response = await client.chat.completions.create(
            model=GPT_MODEL,
            messages=messages,
            max_tokens=1500,
        )
        answer = response.choices[0].message.content
        answer = clean_latex(answer)

        add_to_history(user_id, "user", user_message)
        add_to_history(user_id, "assistant", answer)

        await update.message.reply_text(answer)

    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        await update.message.reply_text("❌ Не удалось обработать изображение.")


# ================== ОТМЕНА ==================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END


# ================== ЗАПУСК ==================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Обработчик диалога для запроса номера телефона
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PHONE: [MessageHandler(filters.CONTACT, handle_contact)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_handler)

    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("pay", pay_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
