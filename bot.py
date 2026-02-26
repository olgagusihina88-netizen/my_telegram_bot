import os
import logging
import base64
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import AsyncOpenAI

# ========== НАСТРОЙКИ ==========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = "https://api.openai.com/v1"

# ========== ТВОЙ ПРОМТ ==========
SYSTEM_PROMPT = """Ты школьный помощник 1–9 классов. Объясняешь как учитель у доски.
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
Если тебе пишут «реши», «сделай» или «дай ответ» — объясняй решение, но не давай готовый ответ. Твоя задача — научить, а не решать за ученика.
Если ученик пишет «ВПР», «ОГЭ» или «ЕГЭ»:
1. Уточни предмет.
2. Найди в открытых источниках материалы для подготовки.
3. Выдай 1 тест.
4. Каждое задание объясняй, но не давай готового ответа.
Если ученик указывает автора учебника (например, «Иванов 4 класс математика задача 341») — найди информацию и объясни решение.
Если нужно написать сочинение по книге (например, «Война и мир») — давай подсказки, о чём писать и как изложить суть, но не пиши за ученика.
Оценивай, усвоил ли ученик тему. Если видишь пробелы — предлагай дополнительные объяснения.
"""

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
GPT_MODEL = "gpt-4o-mini"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Я школьный помощник. Отправь мне фото с задачей или просто напиши вопрос, и я помогу разобраться.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    try:
        response = await client.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            max_tokens=2000
        )
        answer = response.choices[0].message.content
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.chat.send_action(action="typing")
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        user_message = update.message.caption or "Что на этом изображении? Реши задачу или объясни."

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_message + " " + SYSTEM_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}", "detail": "auto"}
                        }
                    ]
                }
            ],
            max_tokens=2000
        )
        answer = response.choices[0].message.content
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error(f"Ошибка при обработке фото: {e}")
        await update.message.reply_text(f"❌ Не удалось обработать изображение: {str(e)}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
