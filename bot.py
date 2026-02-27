import os
import logging
import base64
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from openai import AsyncOpenAI

# ================== ПЕРЕМЕННЫЕ ==================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("Не найден TELEGRAM_TOKEN")

if not OPENAI_API_KEY:
    raise ValueError("Не найден OPENAI_API_KEY")

GPT_MODEL = "gpt-4o-mini"

# ================== ХРАНЕНИЕ ИСТОРИИ ДИАЛОГА ==================
user_history = {}          # user_id -> список сообщений (роль, текст)
MAX_HISTORY = 10            # хранить последние 10 сообщений (примерно 5 пар)

def get_history(user_id: int):
    """Возвращает историю пользователя (список словарей с ролью и содержимым)"""
    return user_history.get(user_id, [])

def add_to_history(user_id: int, role: str, content: str):
    """Добавляет сообщение в историю и обрезает её до MAX_HISTORY"""
    if user_id not in user_history:
        user_history[user_id] = []
    user_history[user_id].append({"role": role, "content": content})
    # Ограничиваем длину истории (удаляем самые старые сообщения)
    if len(user_history[user_id]) > MAX_HISTORY:
        user_history[user_id] = user_history[user_id][-MAX_HISTORY:]

# ================== ТВОЙ ПОЛНЫЙ ПРОМПТ ==================

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
"""

# ================== ЛОГИ ==================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Очищаем историю при новом старте (пользователь начинает с нуля)
    if user_id in user_history:
        del user_history[user_id]
    await update.message.reply_text(
        "Привет! 👋 Отправь задачу текстом или фото — разберём её вместе."
    )


# ================== ОЧИСТКА ИСТОРИИ (КОМАНДА /CLEAR) ==================

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_history:
        del user_history[user_id]
        await update.message.reply_text("🧹 История диалога очищена. Начинаем с чистого листа.")
    else:
        await update.message.reply_text("История и так пуста.")


# ================== ОБРАБОТКА ТЕКСТА ==================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text

    # Получаем историю пользователя
    history = get_history(user_id)

    # Формируем список сообщений для OpenAI: системный промпт + история + текущее сообщение
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history + [{"role": "user", "content": user_message}]

    try:
        response = await client.chat.completions.create(
            model=GPT_MODEL,
            messages=messages,
            max_tokens=1500,
        )

        answer = response.choices[0].message.content

        # Добавляем текущий вопрос и ответ в историю
        add_to_history(user_id, "user", user_message)
        add_to_history(user_id, "assistant", answer)

        await update.message.reply_text(answer)

    except Exception as e:
        logger.error(f"Ошибка OpenAI: {e}")
        await update.message.reply_text("❌ Ошибка при обработке запроса.")


# ================== ОБРАБОТКА ФОТО ==================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        await update.message.chat.send_action("typing")

        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        user_message = update.message.caption or "Объясни задачу на фото."

        # Получаем историю
        history = get_history(user_id)

        # Для фото историю учитываем, но само изображение не храним в истории.
        # Текущее сообщение с фото отправляем как есть, история добавляется текстом.
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

        # Сохраняем в историю текстовую часть (вопрос и ответ)
        add_to_history(user_id, "user", user_message)      # сохраняем только текст, не фото
        add_to_history(user_id, "assistant", answer)

        await update.message.reply_text(answer)

    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        await update.message.reply_text("❌ Не удалось обработать изображение.")


# ================== ЗАПУСК ==================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_history))   # новая команда
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
