import discord
from discord.ext import commands, tasks
from discord import app_commands
import requests
import logging
import asyncio
from datetime import datetime, timedelta, UTC
import os
import json
from dotenv import load_dotenv
import re
import random

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", handlers=[
    logging.StreamHandler(),
    logging.FileHandler("bot_log.txt", mode="a")
])

CONFIG_FILE = "bot_config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    raise FileNotFoundError("Файл конфигурации не найден.")

def save_config(new_data: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)

config = load_config()
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="/", intents=intents)

LOG_FILE = "bot_log.txt"
MAX_LINES = 5000

# free_column_tasks = []
cached_tasks = []

mention_times = []
ignore_until = datetime.min.replace(tzinfo=UTC)

def clear_log_if_too_big():
    try:
        line_count = 0
        with open(LOG_FILE, "rb") as f:
            for _ in f:
                line_count += 1
        if line_count > MAX_LINES:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                pass
            logging.info(f"Лог-файл очищен, т.к. достиг {line_count} строк")
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.error(f"Ошибка при проверке размера лога: {e}")

def get_tasks_from_yougile(column_id):
    url = "https://ru.yougile.com/api-v2/task-list"
    headers = {
        'Authorization': f"Bearer {os.getenv('YOUGILE_API_TOKEN')}",
        'Content-Type': 'application/json'
    }
    params = {"columnId": column_id}

    try:
        logging.info(f"Запрос задач из колонки: {column_id}")
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        if not response.text.strip():
            logging.warning(f"Пустой ответ от YouGile для колонки {column_id}")
            return []
        data = response.json()
        return data.get("content", [])
    except Exception as e:
        logging.error(f"Ошибка при запросе: {e}")
        return []

def format_tasks_for_message(tasks, column_name):
    if not tasks:
        return "Задач нет.\n"

    lines = []
    for task in tasks:
        line = f"- {task['title']}"
        if column_name in ["В процессе выполнения", "Проверяются и дорабатываются"]:
            stickers = task.get("stickers")
            if isinstance(stickers, dict) and stickers:
                first_sticker_name = next(iter(stickers.values()), None)
                if first_sticker_name:
                    line += f" — **{first_sticker_name}**"
        lines.append(line)

    return "\n".join(lines)

async def send_task_message():
#    global free_column_tasks, cached_tasks
    global cached_tasks

#    free_column_tasks = []
    cached_tasks = []

    try:
        channel = await bot.fetch_channel(config['channel_id'])
        if not channel:
            logging.error("Канал с указанным ID не найден.")
            return
    except Exception as e:
        logging.error(f"Ошибка при получении канала: {e}")
        return

    tasks_text = []
    all_tasks = []

    for column_name, column_id in config['column_ids'].items():
        try:
            column_tasks = get_tasks_from_yougile(column_id)
        except Exception as e:
            logging.error(f"Ошибка при получении задач из колонки '{column_name}': {e}")
            column_tasks = []
#       if column_name == "Свободные":
#            free_column_tasks = column_tasks
        all_tasks.extend(column_tasks)
        formatted = format_tasks_for_message(column_tasks, column_name)
        tasks_text.append(f"## {column_name}\n{formatted}")

    cached_tasks = all_tasks

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    message = (
        "\n".join(tasks_text) +
        f"\n-# Дата изменения: {now}"
        f"\n-# (Время из Германии, Москва ≈ +2 часа)"
    )

    try:
        if config.get("message_id"):
            old_msg = await channel.fetch_message(config["message_id"])
            if old_msg and old_msg.author == bot.user:
                await old_msg.edit(content=message)
                logging.info("Сообщение с задачами обновлено.")
                return
    except Exception as e:
        logging.warning(f"Не удалось редактировать старое сообщение: {e}")

    sent_message = await channel.send(message)
    config["message_id"] = sent_message.id
    if config.get("auto_pin"):
        try:
            await sent_message.pin()
            logging.info("Сообщение закреплено.")
        except discord.Forbidden:
            logging.warning("Не удалось закрепить сообщение — недостаточно прав.")
    save_config(config)

@bot.tree.command(name="send-list", description="Отправить список задач", guild=discord.Object(id=config['guild_id']))
async def send_list(interaction: discord.Interaction):
    await interaction.response.send_message("Отправка задач...", ephemeral=True)
    await send_task_message()
    logging.info(f"Задачи отправлены пользователем {interaction.user}")

@bot.tree.command(name="start-update", description="Запустить автоматическое обновление списка", guild=discord.Object(id=config['guild_id']))
async def start_update(interaction: discord.Interaction):
    if update_task_message.is_running():
        await interaction.response.send_message("Цикл уже запущен.", ephemeral=True)
        return
    update_task_message.start()
    config["is_updating"] = True
    save_config(config)
    await interaction.response.send_message("Цикл обновления запускается...", ephemeral=True)
    logging.info("Автообновление запущено")

@bot.tree.command(name="stop-update", description="Остановить автоматическое обновление списка", guild=discord.Object(id=config['guild_id']))
async def stop_update(interaction: discord.Interaction):
    update_task_message.stop()
    config["is_updating"] = False
    save_config(config)
    await interaction.response.send_message("Цикл обновления остановлен.", ephemeral=True)
    logging.info("Автообновление остановлено")

@bot.tree.command(name="update-list", description="Обновить задачи вручную", guild=discord.Object(id=config['guild_id']))
async def update_list(interaction: discord.Interaction):
    await interaction.response.send_message("Обновление задач...", ephemeral=True)
    await send_task_message()
    logging.info(f"Задачи обновлены пользователем {interaction.user}")

@bot.tree.command(name="text-train", description="Отправить обучающий инструктаж", guild=discord.Object(id=config['guild_id']))
async def text_train(interaction: discord.Interaction):
    await interaction.response.send_message("Отправка инструктажа...", ephemeral=True)
    try:
        channel = await bot.fetch_channel(config['channel_id'])
        if not channel:
            logging.error("Канал с указанным ID не найден.")
            return
    except Exception as e:
        logging.error(f"Ошибка при получении канала: {e}")
        return

    training_texts = config.get("training_texts", [])
    if not training_texts:
        logging.error("В конфиге нет текстов инструктажа")
        return

    for i, part in enumerate(training_texts):
        try:
            await channel.send(part)
            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Ошибка при отправке части {i + 1}: {e}")

    logging.info(f"Инструктаж отправлен пользователем {interaction.user}")

def html_to_discord(text):
    replacements = [
        ("<strong>", "**"), ("</strong>", "**"),
        ("<em>", "*"), ("</em>", "*"),
        ("<p>", ""), ("</p>", "\n\n"),
        ("<br>", "\n"),
        ("&nbsp;", " "),
    ]
    for old, new in replacements:
        text = text.replace(old, new)

    text = re.sub(r"<ul>|<ol>", "", text)
    text = re.sub(r"</ul>|</ol>", "", text)
    text = re.sub(r"<li>", "- ", text)
    text = re.sub(r"</li>", "\n", text)
    text = re.sub(r"<.*?>", "", text)
    return text.strip()

@bot.tree.command(name="task-desc", description="Показать описание задачи по имени", guild=discord.Object(id=config['guild_id']))
@app_commands.describe(task_name="Имя задачи")
async def task_desc(interaction: discord.Interaction, task_name: str):
    await interaction.response.defer(thinking=True)

    all_tasks = cached_tasks.copy() if 'cached_tasks' in globals() else []

    matched_task = next((task for task in all_tasks if task["title"].lower() == task_name.lower()), None)

    if not matched_task:
        await interaction.followup.send("Задача не найдена во всех колонках.", ephemeral=True)
        return

    raw_desc = matched_task.get("description", "Нет описания.")
    formatted_desc = html_to_discord(raw_desc)

    stickers_text = ""
    stickers_data = matched_task.get("stickers", {})
    if stickers_data:
        sticker_lines = []
        for sticker_id, state_id in stickers_data.items():
            if not state_id:
                continue
            sticker_info = config.get("stickers", {}).get(sticker_id)
            if not sticker_info:
                continue
            state_name = sticker_info["states"].get(state_id)
            if not state_name:
                continue
            sticker_lines.append(f"• {sticker_info['name']}: {state_name}")
        if sticker_lines:
            stickers_text = "\n".join(sticker_lines)

    full_description = []
    if stickers_text:
        full_description.append(stickers_text)
    full_description.append(formatted_desc)

    embed = discord.Embed(
        title=matched_task['title'],
        description="\n\n".join(full_description),
        color=0xffc86e
    )
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="auto-pin", description="Включить/выключить автозакреп сообщений", guild=discord.Object(id=config['guild_id']))
async def auto_pin(interaction: discord.Interaction):
    config["auto_pin"] = not config.get("auto_pin", False)
    save_config(config)
    status = "включен" if config["auto_pin"] else "выключен"
    await interaction.response.send_message(f"Автозакреп теперь {status}.", ephemeral=True)

@bot.tree.command(name="translate", description="Перевод между русским и тугосеринским", guild=discord.Object(id=config['guild_id']))
@app_commands.describe(
    direction="Направление перевода",
    text="Текст для перевода"
)
@app_commands.choices(direction=[
    app_commands.Choice(name="С русского", value="ru_to_tuga"),
    app_commands.Choice(name="С тугосеринского", value="tuga_to_ru")
])
async def translate(interaction: discord.Interaction, direction: app_commands.Choice[str], text: str):
    translations = config.get("translations", {})
    result = []

    if direction.value == "tuga_to_ru":
        reversed_translations = {v.lower(): k for k, v in translations.items()}
        used_dict = reversed_translations
    else:
        used_dict = {k.lower(): v for k, v in translations.items()}

    words = re.findall(r'\w+|\W+', text)

    for word in words:
        lower_word = word.lower()
        replacement = used_dict.get(lower_word)
        if replacement:
            if word.istitle():
                result.append(replacement.capitalize())
            elif word.isupper():
                result.append(replacement.upper())
            else:
                result.append(replacement)
        else:
            result.append(word)

    translated = ''.join(result)
    await interaction.response.send_message(f"**Перевод:** {translated}", ephemeral=True)

# @bot.tree.command(name="task-select", description="Выбрать задачу из свободных и закрепить за собой", guild=discord.Object(id=config['guild_id']))
# @app_commands.describe(task_name="Точное название задачи из колонки 'Свободные'")
# async def task_select(interaction: discord.Interaction, task_name: str):
#    await interaction.response.defer(thinking=True)
#
#    task = next((t for t in free_column_tasks if t["title"].strip().lower() == task_name.strip().lower()), None)
#    if not task:
#        await interaction.followup.send(f"Задача с названием '{task_name}' не найдена в колонке 'Свободные'.", ephemeral=True)
#        return

@tasks.loop(minutes=60)
async def update_task_message():
    logging.info("Автообновление задач")
    await send_task_message()

@tasks.loop(minutes=60)
async def log_file_maintenance():
    clear_log_if_too_big()

@bot.event
async def on_message(message):
    global mention_times, ignore_until
    
    if message.author == bot.user or not bot.user.mentioned_in(message):
        return

    if str(message.channel.id) != str(config.get("channel_id")):
        return

    now = datetime.now(UTC)

    mention_times = [t for t in mention_times if (now - t).total_seconds() <= 10]
    mention_times.append(now)

    if len(mention_times) > 10:
        ignore_until = now + timedelta(seconds=60)
        mention_times.clear()
        logging.warning(f"[SPAM] Включён игнор упоминаний до {ignore_until.isoformat()}")
        return

    if ignore_until and now < ignore_until:
        return

    query = re.sub(rf"<@!?{bot.user.id}>", "", message.content).strip().lower()

    if not config or "flags" not in config or "responses" not in config:
        await message.reply("Извините, я не могу общаться по техническим причинам.")
        return

    if not query:
        await message.reply(get_random_unknown_reply(message))
        return

    matched_flags = set()
    words = re.findall(r"\b\w+\b", query)

    has_L = any(kw.lower() in words for kw in config.get("flags", {}).get("L", []))
    has_M = any(kw.lower() in words for kw in config.get("flags", {}).get("M", []))

    if has_L and has_M:
        await message.reply("Ошибка: Обнаружены несовместимые флаги.")
        return
    elif has_L:
        parts = [part.strip() for part in query.split("или")]
        if len(parts) >= 2:
            choice = random.choice(["Первый вариант", "Второй вариант"])
            await message.reply(choice)
        else:
            await message.reply("Не удалось распознать два варианта выбора.")
        return
    elif has_M:
        cleaned = re.sub(rf"<@!?{bot.user.id}>", "", message.content, flags=re.IGNORECASE).strip()

        trigger_words = config["flags"].get("M", [])
        trigger_regex = r"\b(" + "|".join(re.escape(w) for w in trigger_words) + r")\b"
        match = re.search(trigger_regex, cleaned, flags=re.IGNORECASE)

        if match:
            trigger_start = match.start()
            trigger_end = match.end()

            before = cleaned[:trigger_start].strip()
            after = cleaned[trigger_end:].strip()

            if not before and after:
                await message.reply(after)
            elif before and not after:
                await message.reply(before)
            elif before and after:
                await message.reply(after)
            else:
                await message.reply("Мне нечего сказать.")
        else:
            await message.reply("Не найдено ключевое слово.")
        return

    for flag, keywords in config["flags"].items():
        for kw in keywords:
            if kw.lower() in words:
                matched_flags.add(flag)
                break

    if not matched_flags:
        await message.reply(get_random_unknown_reply(message))
        return

    response_key = None
    possible_keys = config["responses"]
    sorted_keys = sorted(possible_keys.keys(), key=lambda k: -len(k.split("+")))

    for key in sorted_keys:
        parts = set(key.split("+"))
        if parts.issubset(matched_flags):
            response_key = key
            break

    if response_key:
        await message.reply(config["responses"][response_key])
    else:
        await message.reply(get_random_unknown_reply(message))

def get_random_unknown_reply(message):
    seed = (message.id + datetime.now(UTC).microsecond) % 100
    if seed == 99:
        return "<:please:1382690563622572135>"
    elif seed >= 90:
        return "Я вас не понимаю... Простите... <:AllCool:1382690549194031124>"
    elif seed >= 70:
        return "Я вас не понимаю... <:AllCool:1382690549194031124>"
    else:
        return "Я вас не понимаю."

@bot.event
async def on_ready():
    logging.info(f"Бот подключён как {bot.user}")
    synced = await bot.tree.sync(guild=discord.Object(id=config['guild_id']))
    logging.info(f"Синхронизировано {len(synced)} команд.")

    if config.get("is_updating") and not update_task_message.is_running():
        update_task_message.start()
        logging.info("Автообновление запущено при старте бота.")

    if not log_file_maintenance.is_running():
        log_file_maintenance.start()
        logging.info("Запущено периодическое обслуживание лога")

try:
    bot.run(os.getenv("BOT_TOKEN"))
except Exception as e:
    logging.critical(f"Не удалось запустить бота: {e}")