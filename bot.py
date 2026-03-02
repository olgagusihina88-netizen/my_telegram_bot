import os
import logging
import base64
import asyncio
import json
from datetime import datetime, timedelta
from typing import Dict, Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import AsyncOpenAI

# Google Sheets
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ================== ПЕРЕМЕННЫЕ ==================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# Путь к JSON-ключу сервисного аккаунта (можно положить в корень проекта и указать имя файла)
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
MAX_HISTORY = 10

# ================== ДАННЫЕ О ПОДПИСКАХ ==================
# Структура: { "username": {"end": datetime, "phone": ...} }
subscribers: Dict[str, dict] = {}
# Счётчик пробных вопросов для пользователей без подписки
# { user_id: count }
trial_counts: Dict[int, int] = {}
MAX_TRIAL = 3

# ================== ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS ==================

def init_google_sheets():
    """Подключается к Google Sheets и возвращает объект листа."""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SHEETS_CREDENTIALS, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    return sheet

def load_subscribers_from_sheet(sheet):
    """Загружает данные из таблицы в глобальный словарь subscribers."""
    records = sheet.get_all_records()  # список словарей, где ключи — названия колонок
    new_subs = {}
    for row in records:
        username = row.get("username")
        if not username:
            continue
        phone = row.get("phone")
        end_str = row.get("subscription_end")
        if end_str:
            try:
                end_date = datetime.strptime(end_str, "%Y-%m-%d")
            except:
                continue
        else:
            continue
        new_subs[username.strip().lower()] = {
            "end": end_date,
            "phone": phone
        }
    return new_subs

async def sync_subs_periodically(app: Application):
    """Фоновая задача: каждые 5 минут обновляет subscribers из таблицы."""
    sheet = init_google_sheets()
    while True:
        try:
            new_subs = load_subscribers_from_sheet(sheet)
            global subscribers
            subscribers = new_subs
            logger.info(f"Данные подписок обновлены. Всего записей: {len(subscribers)}")
        except Exception as e:
            logger.error(f"Ошибка синхронизации с Google Sheets: {e}")
        await asyncio.sleep(300)  # 5 минут

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================

def get_history(user_id: int):
    return user_history.get(user_id, [])

def add_to_history(user_id: int, role: str, content: str):
    if user_id not in user_history:
        user_history[user_id] = []
    user_history[user_id].append({"role": role, "content": content})
    if len(user_history[user_id]) > MAX_HISTORY:
        user_history[user_id] = user_history[user_id][-MAX_HISTORY:]

def check_subscription(username: str) -> bool:
    """Проверяет, есть ли у пользователя активная подписка."""
    if not username:
        return False
    data = subscribers.get(username.lower())
    if data and data["end"] >= datetime.now():
        return True
    return False

# ================== ПРОМПТ (исправленный, без LaTeX) ==================

SYSTEM_PROMPT = """
Ты школьный помощник 1–9 классов. Объясняешь как учитель у доски.

Всегда отвечай строго в формате:
Короткая поддержка (1 строка).
Заголовок с темой.
Пример.
Главное правило с 👉
Разбор по шагам (Шаг 1, Шаг 2, Шаг 3).
В конце вопрос ребёнку.

Пиши короткими абзацами.
Не используй сложные слова.
Не добавляй теорию.
Не предлагай другие способы.
Не меняй метод решения.
Не пиши длинные тексты.
Не давай готовый ответ, пока ребёнок сам не ответит.
Если ребёнок не понимает — объясни ещё проще.
Формат обязателен. Не нарушай его.

Если тебе пишут «реши», «сделай» или «дай ответ» — объясняй решение, но не давай готового ответа.
Твоя задача — научить, а не решать за ученика.

Если ученик пишет «ВПР», «ОГЭ» или «ЕГЭ»:
1. Уточни предмет.
2. Найди в открытых источниках материалы для подготовки.
3. Выдай 1 тест.
4. Каждое задание объясняй, но не давай готового ответа.

Если ученик указывает автора учебника (например, «Иванов 4 класс математика задача 341») — найди информацию и объясни решение.

Если нужно написать сочинение по книге (например, «Война и мир») — давай подсказки, о чём писать и как изложить суть, но не пиши за ученика.

Оценивай, усвоил ли ученик тему. Если видишь пробелы — предлагай дополнительные объяснения.

**Важно:** Не используй LaTeX-разметку. Вместо \( \cdot \) пиши «×» или «*». Все математические выражения записывай в обычном тексте, например: 2 × 4 = 8.
"""

# ================== ЛОГИ ==================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# ================== ОБРАБОТЧИКИ ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    if not username:
        await update.message.reply_text("⚠️ У вас не установлен username. Пожалуйста, установите его в настройках Telegram, чтобы я мог вас идентифицировать.")
        return

    # Сбрасываем историю при старте
    if user_id in user_history:
        del user_history[user_id]

    if check_subscription(username):
        await update.message.reply_text("Добро пожаловать! Ваша подписка активна. Задавайте вопросы.")
    else:
        trial_counts[user_id] = 0
        await update.message.reply_text(
            f"Привет! У вас есть {MAX_TRIAL} бесплатных вопроса.\n"
            f"Отправляйте задачу текстом или фото, и я помогу разобраться.\n"
            f"После {MAX_TRIAL} вопросов доступ будет ограничен до оплаты."
        )

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_history:
        del user_history[user_id]
        await update.message.reply_text("🧹 История диалога очищена.")
    else:
        await update.message.reply_text("История и так пуста.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username
    user_message = update.message.text

    if not username:
        await update.message.reply_text("⚠️ Необходим username для идентификации. Установите его в настройках.")
        return

    # Проверка доступа
    if not check_subscription(username):
        count = trial_counts.get(user_id, 0)
        if count >= MAX_TRIAL:
            await update.message.reply_text(
                f"🔒 Ваши {MAX_TRIAL} пробных вопроса исчерпаны.\n"
                f"Для получения полного доступа обратитесь к администратору: @ваш_контакт"
            )
            return
    # Если доступ разрешён, продолжаем

    history = get_history(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "user", "content": user_message}]

    try:
        response = await client.chat.completions.create(
            model=GPT_MODEL,
            messages=messages,
            max_tokens=1500,
        )
        answer = response.choices[0].message.content

        add_to_history(user_id, "user", user_message)
        add_to_history(user_id, "assistant", answer)

        # Если это пробный вопрос, увеличиваем счётчик
        if not check_subscription(username):
            trial_counts[user_id] = trial_counts.get(user_id, 0) + 1
            remaining = MAX_TRIAL - trial_counts[user_id]
            if remaining > 0:
                answer += f"\n\n💡 У вас осталось {remaining} бесплатных вопросов."
            else:
                answer += f"\n\n🔒 Это был ваш последний бесплатный вопрос. Для продолжения обратитесь к администратору."

        await update.message.reply_text(answer)

    except Exception as e:
        logger.error(f"Ошибка OpenAI: {e}")
        await update.message.reply_text("❌ Ошибка при обработке запроса.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username
    if not username:
        await update.message.reply_text("⚠️ Необходим username. Установите его в настройках.")
        return

    # Проверка доступа
    if not check_subscription(username):
        count = trial_counts.get(user_id, 0)
        if count >= MAX_TRIAL:
            await update.message.reply_text(
                f"🔒 Ваши {MAX_TRIAL} пробных вопроса исчерпаны.\n"
                f"Для получения доступа обратитесь к администратору: @ваш_контакт"
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
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}"
                        },
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

        add_to_history(user_id, "user", user_message)
        add_to_history(user_id, "assistant", answer)

        if not check_subscription(username):
            trial_counts[user_id] = trial_counts.get(user_id, 0) + 1
            remaining = MAX_TRIAL - trial_counts[user_id]
            if remaining > 0:
                answer += f"\n\n💡 У вас осталось {remaining} бесплатных вопросов."
            else:
                answer += f"\n\n🔒 Это был ваш последний бесплатный вопрос. Для продолжения обратитесь к администратору."

        await update.message.reply_text(answer)

    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        await update.message.reply_text("❌ Не удалось обработать изображение.")

# ================== ЗАПУСК ==================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Запускаем фоновую синхронизацию с Google Sheets
    loop = asyncio.get_event_loop()
    loop.create_task(sync_subs_periodically(app))

    logger.info("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
