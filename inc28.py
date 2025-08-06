import datetime
import asyncio
import re
import os
import json
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
from zoneinfo import ZoneInfo
from telegram.error import ChatMigrated

load_dotenv()

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)

INCIDENTS_FILE = "../incbotOld/incidents.json"


def load_incidents():
    if os.path.exists(INCIDENTS_FILE):
        with open(INCIDENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for incident_id, incident in data.items():
                if isinstance(incident["time"], str):
                    incident["time"] = datetime.datetime.fromisoformat(incident["time"].replace(" ", "T"))
            return data
    return {}


def save_incidents(incidents):
    incidents_to_save = {
        incident_id: {
            "text": incident["text"],
            "chat_id": incident["chat_id"],
            "time": incident["time"].isoformat() if isinstance(incident["time"], datetime.datetime) else incident["time"],
            "jobs": incident["jobs"]
        }
        for incident_id, incident in incidents.items()
    }
    with open(INCIDENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(incidents_to_save, f, ensure_ascii=False, indent=2)


incidents = load_incidents()

# Setup scheduler and application
event_loop = asyncio.new_event_loop()
asyncio.set_event_loop(event_loop)
scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Bishkek"))
scheduler.start()
application = ApplicationBuilder().token(os.getenv("tg")).build()

BROADCAST_GROUPS = [
    -1002591060921,
    -1002631818202
]


def extract_key(text: str) -> str:
    match = re.search(r'Инцидент:\s*(.+?)(\n|$)', text)
    return match.group(1).strip() if match else None


def extract_jira_key(text: str) -> str | None:
    match = re.search(r'(ITSMJIRA-\d+)', text)
    return match.group(1) if match else None


def get_incident_key(text: str) -> str | None:
    key = extract_key(text)
    return key if key in incidents else None


async def safe_send(bot, chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except ChatMigrated as e:
        new_id = e.new_chat_id
        print(f"Чат {chat_id} → {new_id}")
        await bot.send_message(chat_id=new_id, text=text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id
    reply_to = update.message.reply_to_message

    resolution_words = ["заработал", "устранено", "решено", "локализован", "восстановлен"]
    priority_words = ["Приоритет инцидента поднят до", "понижен до", "повышен до"]
    jira_pattern = re.compile(r'ITSMJIRA-\d+')

    if not (
        ("Инцидент:" in text and "Время выявления инцидента:" in text)
        or (reply_to and any(word in text.lower() for word in resolution_words))
        or jira_pattern.search(text)
        or any(word in text for word in priority_words)
    ):
        return

    print(f"Получено сообщение: {text} в чате {chat_id}")

    if reply_to and any(word in text.lower() for word in resolution_words):
        replied_text = reply_to.text
        jira_key = extract_jira_key(replied_text) or extract_key(replied_text)
        key = extract_key(replied_text) or jira_key
        if not key:
            print("Не удалось извлечь ключ инцидента.")
            return

        incident_id = key
        incident = incidents.get(incident_id)
        if incident:
            print(f"Инцидент '{key}' закрыт. Отменяю напоминания.")
            for job_id in incident.get("jobs", []):
                try:
                    scheduler.remove_job(job_id)
                except Exception:
                    pass

            now = datetime.datetime.now(tz=ZoneInfo("Asia/Bishkek"))
            duration = now - incident["time"]
            jira_link = f"\nJIRA: https://jiraportal.cbk.kg/projects/ITSMJIRA/queues/issue/{jira_key}" if jira_key else ""

            msg = (
                f"Инцидент '{key}' решён.{jira_link}\n"
                f"Время решения: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Время на решение: {str(duration).split('.')[0]}"
            )

            await safe_send(context.bot, chat_id, msg)
            for group_id in BROADCAST_GROUPS:
                await safe_send(context.bot, group_id, msg)
            del incidents[incident_id]
            save_incidents(incidents)
        return

    if "Инцидент:" in text and "Время выявления инцидента:" in text:
        key = extract_key(text)
        if not key:
            print("Не удалось извлечь ключ из инцидента.")
            return

        now = datetime.datetime.now(tz=ZoneInfo("Asia/Bishkek"))
        incident_id = key

        if incident_id in incidents:
            print(f"Инцидент '{incident_id}' уже существует. Повтор не добавлен.")
            await safe_send(context.bot, chat_id, f"Инцидент '{incident_id}' уже существует. Повтор не добавлен.")
            return

        incidents[incident_id] = {"text": text, "chat_id": chat_id, "time": now, "jobs": []}
        print(f"Обнаружен инцидент: {incident_id}")

        for group_id in BROADCAST_GROUPS:
            await safe_send(context.bot, group_id, text)

        if ("Средний" not in text) and ("Низкий" not in text):
            job_50 = scheduler.add_job(notify_50_minutes, 'date', run_date=now + datetime.timedelta(minutes=50),
                                       args=[application, chat_id, incident_id, event_loop], id=f"{incident_id}_50")
            job_60 = scheduler.add_job(notify_60_minutes, 'date', run_date=now + datetime.timedelta(minutes=60),
                                       args=[application, chat_id, incident_id, event_loop], id=f"{incident_id}_60")
            incidents[incident_id]["jobs"].extend([job_50.id, job_60.id])
        else:
            print("Уровень инцидента не высокий — напоминания не ставим.")
        save_incidents(incidents)
        return

    if any(word in text for word in priority_words):
        if not reply_to:
            print("Сообщение не является ответом на инцидент.")
            return

        key = extract_key(reply_to.text)
        if not key or key not in incidents:
            print("Не удалось найти связанный инцидент.")
            return

        incident_id = key
        incident = incidents[incident_id]

        if ("поднят до" in text or "повышен до" in text) and ("высокий" , "Высокий" in text or "Критичный" in text):
            print(f"Приоритет инцидента повышен.")
            await safe_send(context.bot, chat_id, f"Приоритет инцидента '{key}' повышен.")
            for job_id in incident["jobs"]:
                try:
                    scheduler.remove_job(job_id)
                except:
                    pass
            incident["jobs"] = []

            now = incident["time"]
            elapsed = datetime.datetime.now(tz=ZoneInfo("Asia/Bishkek")) - now
            remain_50 = max(datetime.timedelta(minutes=50) - elapsed, datetime.timedelta())
            remain_60 = max(datetime.timedelta(minutes=60) - elapsed, datetime.timedelta())

            job_50 = scheduler.add_job(notify_50_minutes, 'date',
                                       run_date=datetime.datetime.now(tz=ZoneInfo("Asia/Bishkek")) + remain_50,
                                       args=[application, chat_id, incident_id, event_loop],
                                       id=f"{incident_id}_50")
            job_60 = scheduler.add_job(notify_60_minutes, 'date',
                                       run_date=datetime.datetime.now(tz=ZoneInfo("Asia/Bishkek")) + remain_60,
                                       args=[application, chat_id, incident_id, event_loop],
                                       id=f"{incident_id}_60")
            incident["jobs"].extend([job_50.id, job_60.id])
            print("Напоминания установлены после повышения приоритета.")
        elif "понижен до" in text and ("Средний" in text or "Низкий" in text):
            print(f"Приоритет понижен. Удаляем напоминания.")
            await safe_send(context.bot, chat_id, f"Приоритет инцидента '{key}' понижен.")
            for job_id in incident["jobs"]:
                try:
                    scheduler.remove_job(job_id)
                except:
                    pass
            incident["jobs"] = []
        save_incidents(incidents)


def notify_50_minutes(app, chat_id, incident_id, loop):
    if incident_id not in incidents:
        return
    asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=chat_id, text="Прошло 50 минут. Через 10 минут необходимо оповестить!"), loop
    )

def notify_60_minutes(app, chat_id, incident_id, loop):
    if incident_id not in incidents:
        return
    asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=chat_id, text="Прошло 60 минут! @PR, @Elturan, @Ruslank1111"), loop
    )
    job_3h = scheduler.add_job(notify_3_hours_later, 'date',
                               run_date=datetime.datetime.now(tz=ZoneInfo("Asia/Bishkek")) + datetime.timedelta(hours=3),
                               args=[app, chat_id, incident_id, loop], id=f"{incident_id}_3h")
    incidents[incident_id]["jobs"].append(job_3h.id)
    save_incidents(incidents)

def notify_3_hours_later(app, chat_id, incident_id, loop):
    if incident_id not in incidents:
        return
    asyncio.run_coroutine_threadsafe(
        app.bot.send_message(chat_id=chat_id, text="Прошло 3 часа! Проверьте статус. @PR, @Elturan, @Ruslank1111"), loop
    )

def restore_jobs(incidents):
    now = datetime.datetime.now(tz=ZoneInfo("Asia/Bishkek"))
    for incident_id, incident in incidents.items():
        if incident.get("jobs"):
            continue
        time = incident["time"]
        elapsed = now - time
        remain_50 = max(datetime.timedelta(minutes=50) - elapsed, datetime.timedelta())
        remain_60 = max(datetime.timedelta(minutes=60) - elapsed, datetime.timedelta())
        if remain_60.total_seconds() <= 0:
            continue
        job_50 = scheduler.add_job(notify_50_minutes, 'date', run_date=now + remain_50,
                                   args=[application, incident["chat_id"], incident_id, event_loop], id=f"{incident_id}_50")
        job_60 = scheduler.add_job(notify_60_minutes, 'date', run_date=now + remain_60,
                                   args=[application, incident["chat_id"], incident_id, event_loop], id=f"{incident_id}_60")
        incident["jobs"] = [job_50.id, job_60.id]
    save_incidents(incidents)


if __name__ == '__main__':
    restore_jobs(incidents)
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    print("Запускаем бота...")
    application.run_polling()
