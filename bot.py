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

# ================== ТВОЙ ПОЛНЫЙ ПРОМПТ (исправлен) ==================

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

❗️ **ВАЖНО:** Не используй LaTeX-разметку. Вместо \( \cdot \) пиши «×» или «*». Все математические выражения записывай в обычном тексте, например: 2 × 4 = 8. Не ставь символы \( и \) в тексте.
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
    # Удаляем \( и \)
    text = re.sub(r'\\\(|\\\)', '', text)
    # Заменяем \cdot на ×
    text = text.replace(r'\cdot', '×')
    return text


# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋 Отправь задачу текстом или фото — разберём её вместе."
    )


# ================== ОБРАБОТКА ТЕКСТА ==================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text

    try:
        response = await client.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            max_tokens=1500,
        )

        answer = response.choices[0].message.content
        # Очищаем ответ от возможных LaTeX-символов
        answer = clean_latex(answer)
        await update.message.reply_text(answer)

    except Exception as e:
        logger.error(f"Ошибка OpenAI: {e}")
        await update.message.reply_text("❌ Ошибка при обработке запроса.")


# ================== ОБРАБОТКА ФОТО ==================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.chat.send_action("typing")

        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()

        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        user_message = update.message.caption or "Объясни задачу на фото."

        response = await client.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
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
                },
            ],
            max_tokens=1500,
        )

        answer = response.choices[0].message.content
        answer = clean_latex(answer)
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
