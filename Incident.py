# Время на решение убрал
import datetime
import asyncio
import re
import os
import json
import logging
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo
from telegram.error import ChatMigrated, Forbidden

load_dotenv()

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

# Настройка логирования в файл (только наши логи, без токенов от библиотек)
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "incident.log")

logger = logging.getLogger("incident_bot")
logger.setLevel(logging.INFO)

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(_formatter)
logger.addHandler(_fh)

_sh = logging.StreamHandler()
_sh.setFormatter(_formatter)
logger.addHandler(_sh)

INCIDENTS_FILE = "incidents.json"

BROADCAST_GROUPS = [
    {"chat_id": -1002631818202},  # test1
    # {"chat_id": -1002054350266},  # КЦМГ
    # {"chat_id": -1002431919774, "thread_id": 2},  # ДКОиОК Инцидент менеджмент
    # {"chat_id": -1002923041724},  # Инциденты Операционный Отдел
]

ALLOWED_USERS = [
    771714551,  # Ruslan
    5895400196,  # cbk
    876688593,  # Emir
    190887814,  # Elturan
]

TZ = ZoneInfo("Asia/Bishkek")
BOT_START_TIME = datetime.datetime.now(tz=TZ)

# Режим тестирования: True — секунды вместо минут, False — боевой режим
TEST_MODE = False


def load_incidents():
    if os.path.exists(INCIDENTS_FILE):
        with open(INCIDENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for incident_id, incident in data.items():
                if isinstance(incident["time"], str):
                    # fromisoformat поддерживает смещение, получаем aware datetime
                    incident["time"] = datetime.datetime.fromisoformat(incident["time"].replace(" ", "T"))
            return data
    return {}


def save_incidents(incidents):
    incidents_to_save = {
        incident_id: {
            "text": incident["text"],
            "chat_id": incident["chat_id"],
            "time": incident["time"].isoformat() if isinstance(incident["time"], datetime.datetime) else incident[
                "time"],
            "jobs": incident["jobs"],
            "priority": incident.get("priority", "средний")
        }
        for incident_id, incident in incidents.items()
    }
    with open(INCIDENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(incidents_to_save, f, ensure_ascii=False, indent=2)


incidents = load_incidents()

# asyncio loop & scheduler & bot
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
scheduler = BackgroundScheduler(timezone=TZ)
scheduler.start()


def extract_key(text):
    match = re.search(r'Инцидент:\s*(.+?)(\n|$)', text)
    return match.group(1).strip() if match else None


def extract_jira_key(text):
    match = re.search(r'(ITSMJIRA-\d+)', text)
    return match.group(1) if match else None


def extract_resolution_time(text, message_time):
    match = re.search(r'\b(?:в\s*)?(\d{1,2}:\d{2})\b', text)
    if not match:
        return message_time
    time_str = match.group(1)
    try:
        hour, minute = map(int, time_str.split(':'))
        extracted_time = message_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if extracted_time > message_time:
            extracted_time -= datetime.timedelta(days=1)
        return extracted_time
    except (ValueError, IndexError):
        return message_time


def get_priority(text):
    """Определяет приоритет по ключевым словам или цифрам."""
    priority_words = {
        "высокий": "высокий",
        "критичный": "критичный",
        "средний": "средний",
        "низкий": "низкий"
    }

    for word, priority in priority_words.items():
        if word in text.lower():
            return priority

    # Ищем цифру приоритета: "Приоритет: 3", "поднят до 3", "понижен до 3 (Средний)" и т.д.
    match = re.search(r'(?:Приоритет[:\s]\s*|до\s+)(\d+)', text)
    if match:
        priority_number = match.group(1)
        if priority_number == '1':
            return "критичный"
        elif priority_number == '2':
            return "высокий"
        elif priority_number == '3':
            return "средний"
        elif priority_number in ['4', '5']:
            return "низкий"

    return None


async def safe_send(bot, chat_info, text):
    chat_id = chat_info["chat_id"]
    thread_id = chat_info.get("thread_id")
    try:
        await bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id)
    except ChatMigrated as e:
        new_id = e.new_chat_id
        await bot.send_message(chat_id=new_id, text=text)
    except Exception as e:
        logger.warning(f"Не удалось отправить сообщение в {chat_id}: {e}")


def schedule_reminders(app, chat_id, incident_id, start_time, priority):
    incident = incidents.get(incident_id)
    if incident:
        for job_id in incident.get("jobs", []):
            try:
                scheduler.remove_job(job_id)
            except Exception as e:
                logger.warning(f"Не удалось удалить задание {job_id}: {e}")
            incident["jobs"] = []

    if "средний" in priority.lower() or "низкий" in priority.lower():
        logger.info(f"Приоритет инцидента {incident_id} не требует напоминаний.")
        return []

    jobs = []

    if TEST_MODE:
        delay_50 = datetime.timedelta(seconds=50)
        delay_60 = datetime.timedelta(seconds=60)
        delay_3h = datetime.timedelta(seconds=180)
    else:
        delay_50 = datetime.timedelta(minutes=50)
        delay_60 = datetime.timedelta(minutes=60)
        delay_3h = datetime.timedelta(hours=3)

    job_50 = scheduler.add_job(
        notify_50_minutes, 'date',
        run_date=start_time + delay_50,
        args=[app, chat_id, incident_id, loop],
        id=f"{incident_id}_50",
        misfire_grace_time=3600
    )
    jobs.append(job_50.id)

    job_60 = scheduler.add_job(
        notify_60_minutes, 'date',
        run_date=start_time + delay_60,
        args=[app, chat_id, incident_id, loop],
        id=f"{incident_id}_60",
        misfire_grace_time=3600
    )
    jobs.append(job_60.id)

    job_3h = scheduler.add_job(
        notify_3_hours_later, 'date',
        run_date=start_time + delay_3h,
        args=[app, chat_id, incident_id, loop],
        id=f"{incident_id}_3h",
        misfire_grace_time=3600
    )
    jobs.append(job_3h.id)

    logger.info(f"Поставлены напоминания для {incident_id}: {jobs}")
    return jobs


def notify_50_minutes(app, chat_id, incident_id, loop):
    if incident_id not in incidents:
        return
    asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=chat_id,
                             text="Прошло 50 минут. Через 10 минут необходимо оповестить! @monitoring_cbk, @lord312, @WikiKarpenko"),
        loop
    )


def notify_60_minutes(app, chat_id, incident_id, loop):
    if incident_id not in incidents:
        return
    asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=chat_id, text="Прошло 60 минут! @monitoring_cbk, @lord312, @WikiKarpenko"), loop
    )


def notify_3_hours_later(app, chat_id, incident_id, loop):
    if incident_id not in incidents:
        return
    asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=chat_id,
                             text="Прошло 3 часа! Проверьте статус. @monitoring_cbk, @lord312, @WikiKarpenko"), loop
    )


def extract_time_from_text(text, message_time):
    """Извлекает время из текста сообщения."""
    match = re.search(r'\b(?:в\s*)?(\d{1,2}:\d{2})\b', text)
    if not match:
        return None  # Возвращаем None, если время не найдено
    time_str = match.group(1)
    try:
        hour, minute = map(int, time_str.split(':'))
        extracted_time = message_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if extracted_time > message_time:
            extracted_time -= datetime.timedelta(days=1)
        return extracted_time
    except (ValueError, IndexError):
        return None


def extract_detection_time(text, message_time):
    """Извлекает время выявления из текста инцидента."""
    # Ищем паттерн "Время выявления: ДД.ММ.ГГГГ ЧЧ:ММ"
    match = re.search(r'Время выявления:\s*(\d{2}\.\d{2}\.\d{4})\s+(\d{1,2}:\d{2})', text)
    if match:
        date_str = match.group(1)  # ДД.ММ.ГГГГ
        time_str = match.group(2)  # ЧЧ:ММ
        try:
            day, month, year = map(int, date_str.split('.'))
            hour, minute = map(int, time_str.split(':'))
            detection_time = datetime.datetime(year, month, day, hour, minute, 0, 0, tzinfo=TZ)
            return detection_time
        except (ValueError, IndexError):
            pass

    # Если не нашли полный формат, ищем просто время "Время выявления: ЧЧ:ММ"
    match = re.search(r'Время выявления:\s*(\d{1,2}:\d{2})', text)
    if match:
        time_str = match.group(1)
        try:
            hour, minute = map(int, time_str.split(':'))
            detection_time = message_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if detection_time > message_time:
                detection_time -= datetime.timedelta(days=1)
            return detection_time
        except (ValueError, IndexError):
            pass

    # Если не нашли время выявления, возвращаем время сообщения
    return message_time


def restore_jobs(incidents):
    now = datetime.datetime.now(tz=TZ)
    restored_count = 0
    for incident_id, incident in incidents.items():
        if incident.get("jobs"):
            continue

        # Проверяем приоритет - не восстанавливаем для средних/низких
        priority = incident.get("priority", "средний")
        if priority.lower() in ["средний", "низкий"]:
            logger.info(f"Пропускаем восстановление для {incident_id} - приоритет {priority}")
            continue

        time = incident["time"]
        elapsed = now - time

        remain_50 = max(datetime.timedelta(minutes=50) - elapsed, datetime.timedelta())
        remain_60 = max(datetime.timedelta(minutes=60) - elapsed, datetime.timedelta())
        remain_3h = max(datetime.timedelta(hours=3) - elapsed, datetime.timedelta())

        if remain_50.total_seconds() > 0 or remain_60.total_seconds() > 0 or remain_3h.total_seconds() > 0:
            # восстанавливаем только те, что еще не наступили
            jobs = []
            if remain_50.total_seconds() > 0:
                jobs.append(scheduler.add_job(notify_50_minutes, 'date', run_date=now + remain_50,
                                              args=[application, incident["chat_id"], incident_id, loop],
                                              id=f"{incident_id}_50",
                                              misfire_grace_time=3600).id)
            if remain_60.total_seconds() > 0:
                jobs.append(scheduler.add_job(notify_60_minutes, 'date', run_date=now + remain_60,
                                              args=[application, incident["chat_id"], incident_id, loop],
                                              id=f"{incident_id}_60",
                                              misfire_grace_time=3600).id)
            if remain_3h.total_seconds() > 0:
                jobs.append(scheduler.add_job(notify_3_hours_later, 'date', run_date=now + remain_3h,
                                              args=[application, incident["chat_id"], incident_id, loop],
                                              id=f"{incident_id}_3h",
                                              misfire_grace_time=3600).id)

            incident["jobs"] = jobs
            restored_count += 1
            logger.info(f"Восстановлены напоминания для {incident_id}")

    if restored_count > 0:
        save_incidents(incidents)
    logger.info(f"Восстановлено задач по {restored_count} инцидентам. Пересылок не делалось.")


JIRA_PATTERN = re.compile(r'ITSMJIRA-\d+')


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = msg.text
    chat_id = msg.chat_id
    if not update.message:
        return

    message_time = update.message.date.astimezone(TZ)
    if message_time < BOT_START_TIME:
        logger.info(f"Пропускаем старое сообщение ({message_time}) — бот стартовал в {BOT_START_TIME}")
        return

    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        logger.warning(f"Пользователь с ID {user_id} не авторизован.")
        return

    if not update.message.text:
        return

    text = update.message.text
    chat_id = update.effective_chat.id
    reply_to = update.message.reply_to_message

    resolution_words = ["заработал", "устранено", "устранен", "решено","решён", "локализован", "восстановлен", "включили в", "решен","устранен","устранён", "стабилизировалось"]
    rejection_words = ["отклонен", "отклонён", "инцидент отклонен", "инцидент отклонён"]
    priority_words = ["поднят до", "повышен до", "понижен до", "снижен до", "поднят на",
                      "Приоритет инцидента поднят до", "Приоритет инцидента повышен до",
                      "Приоритет инцидента понижен до", "Приоритет инцидента снижен до",
                      "Приоритет инцидент понижен до", "Приоритет:"]
    jira_pattern = re.compile(r'ITSMJIRA-\d+')

    is_new_incident = "Инцидент:" in text and "Приоритет:" in text and bool(JIRA_PATTERN.search(text))
    is_resolution_message = reply_to and any(word in text.lower() for word in resolution_words)
    is_rejection_message = reply_to and any(word in text.lower() for word in rejection_words)
    is_priority_update = any(word in text for word in priority_words)
    is_jira_update = bool(jira_pattern.search(text))

    if not (is_new_incident or is_resolution_message or is_rejection_message or is_jira_update or is_priority_update):
        return

    logger.info(f"Получено LIVE-сообщение: '{text}' в чате {chat_id} (в {message_time})")

    if is_new_incident:
        key = extract_key(text)
        jira_key = JIRA_PATTERN.search(text)
        if not jira_key:
            logger.warning("Не удалось извлечь JIRA ID из инцидента.")
            return

        incident_id = jira_key.group(0)
        if incident_id in incidents:
            logger.info(f"Инцидент '{incident_id}' уже существует. Вероятно, дублирующее сообщение — пропускаем.")
            return

        # Извлекаем время выявления из текста сообщения
        detection_time = extract_detection_time(text, message_time)
        priority = get_priority(text) or "средний"
        incidents[incident_id] = {"text": text, "chat_id": chat_id, "time": detection_time, "jobs": [], "priority": priority}
        logger.info(f"Обнаружен новый инцидент: {incident_id} с приоритетом {priority}, время выявления: {detection_time}")

        for group in BROADCAST_GROUPS:
            await safe_send(context.bot, group, text)

        if priority:
            incidents[incident_id]["jobs"] = schedule_reminders(application, chat_id, incident_id, detection_time, priority)
        save_incidents(incidents)
        return

    if is_resolution_message:
        replied_text = reply_to.text
        incident_id = extract_jira_key(replied_text)
        if not incident_id or incident_id not in incidents:
            logger.warning("Не удалось найти связанный инцидент.")
            return

        incident = incidents.get(incident_id)
        if incident:
            logger.info(f"Инцидент '{incident_id}' закрыт. Отменяю напоминания.")
            for job_id in incident.get("jobs", []):
                try:
                    scheduler.remove_job(job_id)
                except Exception as e:
                    logger.warning(f"Не удалось удалить задание {job_id}: {e}")

            resolution_time = extract_resolution_time(text, message_time)

            # Подсчитываем время на решение
            incident_start_time = incident["time"]
            time_to_resolve = resolution_time - incident_start_time
            total_seconds = max(time_to_resolve.total_seconds(), 0)
            hours = int(total_seconds // 3600)
            minutes = int((total_seconds % 3600) // 60)
            time_str = f"{hours} ч {minutes} мин" if hours > 0 else f"{minutes} мин"

            jira_link = f"\nJIRA: https://jiraportal.cbk.kg/projects/ITSMJIRA/queues/issue/{extract_jira_key(replied_text)}" if extract_jira_key(
                replied_text) else ""

            msg = (f"Инцидент '{incident_id}' решён.{jira_link}\n"
                   f"Время решения: {resolution_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   f"Время на устранение: {time_str}")

            await safe_send(context.bot, {"chat_id": chat_id}, msg)
            for group in BROADCAST_GROUPS:
                await safe_send(context.bot, group, msg)

            del incidents[incident_id]
            save_incidents(incidents)
        return

    if is_rejection_message:
        replied_text = reply_to.text
        incident_id = extract_jira_key(replied_text)
        if not incident_id or incident_id not in incidents:
            logger.warning("Не удалось найти связанный инцидент.")
            return

        incident = incidents.get(incident_id)
        if incident:
            logger.info(f"Инцидент '{incident_id}' отклонен. Отменяю напоминания.")
            for job_id in incident.get("jobs", []):
                try:
                    scheduler.remove_job(job_id)
                except Exception as e:
                    logger.warning(f"Не удалось удалить задание {job_id}: {e}")

            incident_name = extract_key(incident["text"])
            jira_link = f"\nJIRA: https://jiraportal.cbk.kg/projects/ITSMJIRA/queues/issue/{incident_id}"

            msg = f"Инцидент '{incident_name}' отклонен.{jira_link}\n{incident_id}"

            await safe_send(context.bot, {"chat_id": chat_id}, msg)
            for group in BROADCAST_GROUPS:
                await safe_send(context.bot, group, msg)

            del incidents[incident_id]
            save_incidents(incidents)
        return

    if is_priority_update:
        if not reply_to:
            logger.warning("Сообщение не является ответом на инцидент.")
            return

        incident_id = extract_jira_key(reply_to.text)
        if not incident_id or incident_id not in incidents:
            logger.warning("Не удалось найти связанный инцидент.")
            return

        incident = incidents[incident_id]
        priority = get_priority(text)

        if not priority:
            logger.warning(f"Не удалось определить приоритет из сообщения.")
            return

        incident_name = extract_key(incident["text"])
        old_priority = incident.get("priority", "средний")

        # Определяем тип изменения: повышение или понижение
        if "поднят" in text or "повышен" in text:
            action = "повышен"
        elif "понижен" in text or "снижен" in text:
            action = "понижен"
        else:
            action = "изменён"

        logger.info(f"Приоритет инцидента '{incident_id}' {action} с {old_priority} до {priority}.")

        # Рассылка по группам — при любом изменении приоритета
        msg = f"Приоритет инцидента '{incident_name}' {action} до {priority}.\n{incident_id}"
        await safe_send(context.bot, {"chat_id": chat_id}, msg)
        for group in BROADCAST_GROUPS:
            await safe_send(context.bot, group, msg)

        # Удаляем старые напоминания в любом случае
        for job_id in incident.get("jobs", []):
            try:
                scheduler.remove_job(job_id)
            except Exception as e:
                logger.warning(f"Не удалось удалить задание {job_id}: {e}")
        incident["jobs"] = []

        # Напоминания — только для высокий/критичный
        if priority in ["высокий", "критичный"]:
            start_time_text = extract_time_from_text(text, message_time)

            if start_time_text:
                start_time = start_time_text
            elif incident.get("time"):
                start_time = incident["time"]
            else:
                start_time = datetime.datetime.now(tz=TZ)
                incident["time"] = start_time

            incident["jobs"] = schedule_reminders(application, chat_id, incident_id, start_time, priority)

        incident["priority"] = priority
        save_incidents(incidents)

        return

if __name__ == '__main__':
    application = ApplicationBuilder().token(os.getenv("tg")).proxy(None).build()
    restore_jobs(incidents)
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    logger.info(f"Запускаем бота... START={BOT_START_TIME}")
    application.run_polling()