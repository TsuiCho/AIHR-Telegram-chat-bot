import os
import hashlib
import logging
import asyncio
import io
import json
import re
from typing import List, Dict, Optional
from pathlib import Path

import pdfplumber
from docx import Document
import asyncpg
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
import httpx

# Конфигурация
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"  # Исправлено
DB_URL = os.getenv("SUPABASE_DB_URL")
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_RESUMES = 50

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Временное хранилище состояния пользователей (в памяти)
user_state = {}

# Подключение к БД
async def get_db_conn():
    """Устанавливает и возвращает подключение к БД"""
    try:
        conn = await asyncpg.connect(DB_URL)
        logging.info("Успешное подключение к БД")
        return conn
    except Exception as e:
        logging.error(f"Ошибка подключения к БД: {e}")
        raise

# Логика обработки файлов
async def parse_resume(file_path: str) -> Optional[str]:
    """Извлечение текста из PDF/DOCX."""
    try:
        logging.info(f"Попытка парсинга файла: {file_path}")
        if not os.path.exists(file_path):
            logging.error(f"Файл не найден на диске: {file_path}")
            return None

        if file_path.endswith('.pdf'):
            with pdfplumber.open(file_path) as pdf:
                pages_text = []
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text()
                    if text:
                        pages_text.append(text)
                    else:
                        logging.warning(f"Страница {i+1} в PDF '{file_path}' не содержит текста")
                return "\n".join(pages_text)
        elif file_path.endswith('.docx'):
            doc = Document(file_path)
            full_text = []
            for i, para in enumerate(doc.paragraphs):
                if para.text.strip():
                    full_text.append(para.text)
                else:
                    logging.debug(f"Пустой параграф в DOCX '{file_path}', номер {i}")
            return "\n".join(full_text)
    except Exception as e:
        logging.error(f"Ошибка парсинга {file_path}: {e}", exc_info=True)
        return None

async def save_resume(file: types.Document, user_id: int) -> Optional[int]:
    """Сохранение резюме в БД."""
    try:
        if file.file_size > MAX_FILE_SIZE:
            logging.warning(f"Файл слишком большой: {file.file_size} байт")
            return None

        file_buffer = io.BytesIO()
        await bot.download(file, destination=file_buffer)
        file_bytes = file_buffer.getvalue()

        temp_path = Path(f"temp_{user_id}_{file.file_name}")
        try:
            with open(temp_path, 'wb') as f:
                f.write(file_bytes)

            text = await parse_resume(str(temp_path))
            if not text:
                logging.error(f"Не удалось извлечь текст из файла: {file.file_name}")
                return None

            content_hash = hashlib.md5(text.encode('utf-8')).hexdigest()

            file_path = f"resumes/{user_id}/{content_hash}_{file.file_name}"
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'wb') as f:
                f.write(file_bytes)

            conn = await get_db_conn()
            try:
                existing_resume = await conn.fetchval(
                    "SELECT resume_id FROM resumes WHERE file_hash = $1",
                    content_hash
                )
                if existing_resume:
                    logging.info(f"Резюме с hash {content_hash} уже существует, возвращаем существующий ID: {existing_resume}")
                    return existing_resume

                resume_id = await conn.fetchval(
                    """
                    INSERT INTO resumes (file_path, file_hash, original_name, file_size, uploaded_by_id)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING resume_id
                    """,
                    file_path, content_hash, file.file_name, file.file_size, user_id
                )
                logging.info(f"Резюме сохранено с ID: {resume_id}")
                return resume_id
            finally:
                await conn.close()

        finally:
            if temp_path.exists():
                temp_path.unlink()

    except Exception as e:
        logging.error(f"Ошибка сохранения резюме: {str(e)}", exc_info=True)
        return None

# Интеграция с DeepSeek
async def analyze_with_deepseek(vacancy_text: str, resumes: List[Dict]) -> List[Dict]:
    """Запрос к DeepSeek API для оценки резюме."""
    try:
        logging.info(f"Запуск анализа DeepSeek для вакансии длиной {len(vacancy_text)} символов и {len(resumes)} резюме")
        resumes_text = "\n\n---\n\n".join(
            f"ID: {r['resume_id']}\nТекст: {r['text'][:500]}..." for r in resumes  # обрезаем для логов
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "Ты HR-эксперт. Проанализируй резюме против вакансии. "
                    "Верни список JSON объектов с полями: resume_id (число), full_name (строка), "
                    "score (целое от 0 до 100), details (строка). Без пояснений."
                )
            },
            {
                "role": "user",
                "content": f"Вакансия: {vacancy_text}\n\nРезюме:\n{resumes_text}"
            }
        ]

        async with httpx.AsyncClient(timeout=30.0) as client:
            logging.info("Отправка запроса к DeepSeek API...")
            response = await client.post(
                DEEPSEEK_URL,
                json={
                    "model": "deepseek-chat",
                    "messages": messages,
                    "temperature": 0.1
                },
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json"
                }
            )
            logging.info(f"Получен статус ответа от DeepSeek: {response.status_code}")
            response.raise_for_status()
            result = response.json()

            logging.info(f"DeepSeek API ответ: {result}")

            if "choices" in result and result["choices"]:
                content = result["choices"][0]["message"]["content"]
                logging.info(f"Ответ от модели: {content[:500]}...")

                json_match = re.search(r'\[\s*\{.*\}\s*\]', content, re.DOTALL)
                if json_match:
                    try:
                        matches = json.loads(json_match.group())
                        logging.info(f"Успешно распаршен JSON из ответа DeepSeek: {len(matches)} matches")
                        return matches
                    except json.JSONDecodeError as je:
                        logging.error(f"Не удалось распарсить JSON: {je}")
                        logging.debug(f"Содержимое, которое не удалось распарсить: {json_match.group()}")
                else:
                    logging.error("JSON массив не найден в ответе")
                    logging.debug(f"Весь ответ: {content}")
            return []

    except Exception as e:
        logging.error(f"DeepSeek API ошибка: {e}", exc_info=True)
        return []

# Обработчики команд
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "🤖 Привет! Я HR-ассистент для отбора резюме.\n"
        "Отправьте мне описание вакансии текстом, а затем загрузите резюме (PDF/DOCX)."
    )

@dp.message(Command("process"))
async def process_resumes(message: Message):
    """Запуск анализа резюме."""
    try:
        user_id = message.from_user.id
        if user_id not in user_state or not user_state[user_id].get("resume_ids"):
            await message.answer("⚠ Нет данных для обработки! Сначала отправьте вакансию и резюме.")
            return

        logging.info(f"Пользователь {user_id} запустил обработку. Резюме: {len(user_state[user_id]['resume_ids'])}")
        logging.info(f"Сохранённые resume_ids: {user_state[user_id]['resume_ids']}")

        conn = await get_db_conn()
        try:
            logging.info(f"Запрос к БД: получение резюме с ID {user_state[user_id]['resume_ids']}")
            resumes = await conn.fetch(
                """
                SELECT resume_id, file_path 
                FROM resumes 
                WHERE resume_id = ANY($1::int[])
                """,
                user_state[user_id]["resume_ids"]
            )
            logging.info(f"Найдено резюме в БД: {len(resumes)}")

            for r in resumes:
                logging.debug(f"Резюме ID: {r['resume_id']}, Путь: {r['file_path']}")

        finally:
            await conn.close()

        if not resumes:
            await message.answer("❌ Нет резюме для анализа (не найдены в БД).")
            return

        parsed_resumes = []
        for resume in resumes:
            logging.info(f"Парсинг резюме ID {resume['resume_id']} по пути: {resume['file_path']}")
            text = await parse_resume(resume['file_path'])
            if text:
                logging.info(f"Успешно извлечён текст из резюме {resume['resume_id']} (длина: {len(text)})")
                parsed_resumes.append({
                    "resume_id": resume['resume_id'],
                    "text": text
                })
            else:
                logging.warning(f"Не удалось извлечь текст из резюме {resume['resume_id']}")

        logging.info(f"Успешно распаршено резюме: {len(parsed_resumes)} из {len(resumes)}")

        if not parsed_resumes:
            await message.answer("❌ Не удалось извлечь текст из резюме.")
            return

        matches = await analyze_with_deepseek(
            user_state[user_id]['vacancy_text'],
            parsed_resumes
        )
        logging.info(f"DeepSeek вернул matches: {len(matches)}")

        if not matches:
            await message.answer("❌ Не удалось проанализировать резюме.")
            return

        # Приведём типы и отфильтруем по resume_id
        valid_matches = []
        for m in matches:
            try:
                m['resume_id'] = int(m['resume_id'])
                m['score'] = int(m['score'])
                if 0 <= m['score'] <= 100:
                    valid_matches.append(m)
                else:
                    logging.warning(f"Оценка вне диапазона [0-100]: {m['score']}")
            except (ValueError, KeyError) as e:
                logging.error(f"Ошибка приведения данных в match: {m}, ошибка: {e}")
                continue

        top_matches = sorted(valid_matches, key=lambda x: x['score'], reverse=True)[:5]
        logging.info(f"Топ-5 matches: {[(m['score'], m.get('full_name', 'No name')) for m in top_matches]}")

        conn = await get_db_conn()
        try:
            logging.info("Сохранение вакансии в БД...")
            vacancy_id = await conn.fetchval(
                """
                INSERT INTO vacancies (hr_user_id, title, description) 
                VALUES ($1, $2, $3) 
                RETURNING vacancy_id
                """,
                user_id, "Auto-generated", user_state[user_id]['vacancy_text']
            )
            logging.info(f"Вакансия сохранена с ID: {vacancy_id}")

            for match in top_matches:
                logging.info(f"Сохранение match: resume_id={match['resume_id']}, score={match['score']}")
                await conn.execute(
                    """
                    INSERT INTO matches (vacancy_id, resume_id, score, details)
                    VALUES ($1, $2, $3, $4)
                    """,
                    vacancy_id, match['resume_id'], match['score'], match['details']
                )
            logging.info(f"Сохранено matches в БД: {len(top_matches)}")

        finally:
            await conn.close()

        response = ["🏆 Топ-5 кандидатов:"]
        for i, match in enumerate(top_matches, 1):
            name = match.get('full_name', 'Имя не указано')
            response.append(f"{i}. {name} - {match['score']}/100")

        await message.answer("\n".join(response))

        del user_state[user_id]
        logging.info(f"Сессия пользователя {user_id} очищена")

    except Exception as e:
        logging.error(f"Ошибка обработки резюме: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при обработке.")

@dp.message(Command("status"))
async def show_status(message: Message):
    """Показать текущее состояние"""
    user_id = message.from_user.id
    if user_id in user_state:
        vacancy_len = len(user_state[user_id]["vacancy_text"])
        resume_count = len(user_state[user_id]["resume_ids"])
        await message.answer(
            f"📊 Текущее состояние:\n"
            f"Вакансия: {vacancy_len} символов\n"
            f"Резюме: {resume_count} файлов\n"
            f"Используйте /process для анализа"
        )
    else:
        await message.answer("ℹ Нет активной сессии. Отправьте описание вакансии чтобы начать.")

@dp.message(Command("status"))
async def show_status(message: Message):
    """Показать текущее состояние"""
    user_id = message.from_user.id
    if user_id in user_state:
        vacancy_len = len(user_state[user_id]["vacancy_text"])
        resume_count = len(user_state[user_id]["resume_ids"])
        await message.answer(
            f"📊 Текущее состояние:\n"
            f"Вакансия: {vacancy_len} символов\n"
            f"Резюме: {resume_count} файлов\n"
            f"Используйте /process для анализа"
        )
    else:
        await message.answer("ℹ Нет активной сессии. Отправьте описание вакансии чтобы начать.")

@dp.message(Command("help"))
async def help_command(message: Message):
    """Отправляет пользователю справку по использованию бота."""
    help_text = (
        "📚 <b>Справка по использованию HR-бота</b>\n\n"
        "Я помогу вам отобрать лучшие резюме под вашу вакансию.\n\n"
        "<b>Доступные команды:</b>\n\n"
        "🔸 <code>/start</code> — начать работу с ботом\n"
        "🔸 <code>/help</code> — показать это сообщение\n"
        "🔸 <code>/status</code> — посмотреть текущее состояние (вакансия и загруженные резюме)\n"
        "🔸 <code>/process</code> — запустить анализ резюме и получить топ-кандидатов\n\n"
        "<b>Как это работает:</b>\n"
        "1. Отправьте <b>описание вакансии</b> текстом\n"
        "2. Загрузите <b>резюме</b> в формате PDF или DOCX (до 50 файлов)\n"
        "3. Нажмите <code>/process</code>, чтобы проанализировать кандидатов\n\n"
        "Я использую ИИ для оценки соответствия резюме вакансии и верну вам топ-5 кандидатов с оценкой от 0 до 100.\n\n"
        "💡 Если что-то пошло не так — используйте <code>/start</code>, чтобы начать заново."
    )
    await message.answer(help_text, parse_mode=ParseMode.HTML)
# Основной workflow
@dp.message(F.text)
async def handle_vacancy_description(message: Message):
    """Шаг 1: Получение описания вакансии."""
    try:
        user_id = message.from_user.id
        if user_id in user_state:
            if user_state[user_id]["resume_ids"]:
                await message.answer(
                    "⚠ Вы уже загрузили резюме. Чтобы начать заново, используйте /start."
                )
                return
        if len(message.text) > 5000:
            await message.answer("❌ Описание слишком длинное (макс. 5000 символов).")
            return

        user_state[user_id] = {
            "vacancy_text": message.text,
            "resume_ids": []
        }

        logging.info(f"Пользователь {user_id} сохранил вакансию. Длина текста: {len(message.text)}")

        await message.answer(
            "✅ Описание вакансии сохранено. Теперь загрузите резюме (PDF/DOCX).\n"
            f"Лимит: {MAX_RESUMES} файлов, каждый до {MAX_FILE_SIZE // 1024 // 1024} MB."
        )
    except Exception as e:
        logging.error(f"Ошибка обработки вакансии: {e}", exc_info=True)
        await message.answer("❌ Произошла ошибка при обработке вакансии.")

@dp.message(F.document)
async def handle_resumes(message: Message):
    """Шаг 2: Обработка загруженных резюме."""
    try:
        user_id = message.from_user.id
        if user_id not in user_state:
            await message.answer("⚠ Сначала отправьте описание вакансии!")
            return
        file = message.document
        if not file.file_name.lower().endswith(('.pdf', '.docx')):
            await message.answer("❌ Поддерживаются только PDF/DOCX.")
            return

        logging.info(f"Пользователь {user_id} загружает файл: {file.file_name}")

        resume_id = await save_resume(file, user_id)
        if not resume_id:
            await message.answer("❌ Файл слишком большой или поврежден.")
            return

        user_state[user_id]["resume_ids"].append(resume_id)
        count = len(user_state[user_id]["resume_ids"])

        logging.info(f"Резюме добавлено к сессии пользователя {user_id}. Всего резюме: {count}")

        await message.answer(
            f"✅ Резюме '{file.file_name}' загружено. "
            f"Загружено резюме: {count}. Можно добавить еще или нажать /process."
        )
    except Exception as e:
        logging.error(f"Ошибка обработки резюме: {e}")
        await message.answer("❌ Произошла ошибка при обработке резюме.")

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())