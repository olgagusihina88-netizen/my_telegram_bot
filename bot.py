import os
import logging
import base64
import re
import json
from datetime import datetime, timedelta
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

# ⚠️ ВАЖНО: Укажите свой ID администратора (узнать можно у @userinfobot)
ADMIN_ID = 1346576296  # ✅ ваш ID

if not TELEGRAM_TOKEN:
    raise ValueError("Не найден TELEGRAM_TOKEN")
if not OPENAI_API_KEY:
    raise ValueError("Не найден OPENAI_API_KEY")

GPT_MODEL = "gpt-4o-mini"

# ================== ХРАНЕНИЕ ИСТОРИИ ДИАЛОГА ==================
user_history = {}
MAX_HISTORY = 20

def get_history(user_id: int):
    return user_history.get(user_id, [])

def add_to_history(user_id: int, role: str, content: str):
    if user_id not in user_history:
        user_history[user_id] = []
    user_history[user_id].append({"role": role, "content": content})
    if len(user_history[user_id]) > MAX_HISTORY:
        user_history[user_id] = user_history[user_id][-MAX_HISTORY:]

# ================== СТАТИСТИКА ==================
STATS_FILE = "bot_stats.json"

def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        "users": {},
        "total_messages": 0,
        "total_users": 0,
    }

def save_stats(stats):
    with open(STATS_FILE, 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

def update_user_stats(user_id, username, first_name):
    stats = load_stats()
    user_id_str = str(user_id)
    now = datetime.now().isoformat()

    if user_id_str not in stats["users"]:
        stats["users"][user_id_str] = {
            "username": username,
            "first_name": first_name,
            "first_seen": now,
            "messages_count": 0,
            "last_seen": now
        }
        stats["total_users"] = len(stats["users"])
    else:
        stats["users"][user_id_str]["last_seen"] = now
        stats["users"][user_id_str]["messages_count"] += 1

    stats["total_messages"] += 1
    save_stats(stats)

def get_user_stats():
    stats = load_stats()
    users = stats["users"]
    now = datetime.now()
    today = now.date().isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()

    active_today = sum(1 for u in users.values() if u["last_seen"].startswith(today))
    active_week = sum(1 for u in users.values() if u["last_seen"] > week_ago)

    return {
        "total_users": stats["total_users"],
        "total_messages": stats["total_messages"],
        "active_today": active_today,
        "active_week": active_week,
        "users": users
    }

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


# ================== START ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    update_user_stats(user.id, user.username, user.first_name)

    if user.id in user_history:
        del user_history[user.id]

    await update.message.reply_text(
        "Привет! 👋 Я помогу тебе разобраться с задачами. Просто отправь мне пример или фото задания, и мы вместе его решим."
    )


# ================== КОМАНДА СТАТИСТИКИ (ТОЛЬКО ДЛЯ АДМИНА) ==================

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет доступа к этой команде.")
        return

    stats = get_user_stats()

    msg = (
        f"📊 **Статистика бота**\n\n"
        f"👥 Всего пользователей: {stats['total_users']}\n"
        f"💬 Всего сообщений: {stats['total_messages']}\n"
        f"📅 Активных сегодня: {stats['active_today']}\n"
        f"📆 Активных за неделю: {stats['active_week']}\n\n"
        f"**Топ-5 активных пользователей:**\n"
    )

    top_users = sorted(
        stats["users"].items(),
        key=lambda x: x[1]["messages_count"],
        reverse=True
    )[:5]

    for user_id_str, data in top_users:
        name = data["first_name"] or data["username"] or "Unknown"
        msg += f"• {name}: {data['messages_count']} сообщ.\n"

    await update.message.reply_text(msg, parse_mode="Markdown")


# ================== ОБРАБОТКА ТЕКСТА ==================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    update_user_stats(user.id, user.username, user.first_name)

    user_id = user.id
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
    update_user_stats(user.id, user.username, user.first_name)

    user_id = user.id

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


# ================== ЗАПУСК ==================

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
