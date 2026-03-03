import os
import logging
import base64
import re
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
MAX_HISTORY = 20            # хранить последние 20 сообщений (примерно 10 пар)

def get_history(user_id: int):
    """Возвращает историю пользователя (список словарей с ролью и содержимым)"""
    return user_history.get(user_id, [])

def add_to_history(user_id: int, role: str, content: str):
    """Добавляет сообщение в историю и обрезает её до MAX_HISTORY"""
    if user_id not in user_history:
        user_history[user_id] = []
    user_history[user_id].append({"role": role, "content": content})
    if len(user_history[user_id]) > MAX_HISTORY:
        user_history[user_id] = user_history[user_id][-MAX_HISTORY:]

# ================== ПРОМПТ С ИНСТРУКЦИЕЙ ОБУЧЕНИЯ ==================

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
    """Удаляет LaTeX-скобки \(...\) и заменяет \cdot на ×."""
    text = re.sub(r'\\\(|\\\)', '', text)
    text = text.replace(r'\cdot', '×')
    return text


# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Сбрасываем историю при новом старте
    if user_id in user_history:
        del user_history[user_id]
    await update.message.reply_text(
        "Привет! 👋 Я помогу тебе разобраться с задачами. Просто отправь мне пример или фото задания, и мы вместе его решим."
    )


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
        answer = clean_latex(answer)

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

        # Для фото историю учитываем, но само изображение не храним в истории
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
        answer = clean_latex(answer)

        # Сохраняем в историю текстовую часть (вопрос и ответ)
        add_to_history(user_id, "user", user_message)
        add_to_history(user_id, "assistant", answer)

        await update.message.reply_text(answer)

    except Exception as e:
        logger.error(f"Ошибка фото: {e}")
        await update.message.reply_text("❌ Не удалось обработать изображение.")


# ================== ЗАПУСК ==================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
