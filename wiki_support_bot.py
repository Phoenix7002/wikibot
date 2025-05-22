import discord
from discord.ext import commands, tasks
from discord import app_commands
import requests
import logging
import asyncio
from datetime import datetime
import os
import json
from dotenv import load_dotenv
import re

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
            stickers = task.get("stickers", {})
            if stickers:
                first_sticker_name = next(iter(stickers.values()), None)
                if first_sticker_name:
                    line += f" — **{first_sticker_name}**"
        lines.append(line)

    return "\n".join(lines)

async def send_task_message():
    global free_column_tasks
    channel = bot.get_channel(config['channel_id'])
    if not channel:
        logging.error("Канал не найден")
        return

    tasks_text = []
    for column_name, column_id in config['column_ids'].items():
        column_tasks = get_tasks_from_yougile(column_id)
        if column_name == "Свободные":
            free_column_tasks = column_tasks
        formatted = format_tasks_for_message(column_tasks, column_name)
        tasks_text.append(f"## {column_name}\n{formatted}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    message = "\n".join(tasks_text) + f"\n\n-# {now}"

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
    if config["is_updating"]:
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
    if not update_task_message.is_running():
        await interaction.response.send_message("Цикл не запущен. Используйте /start-update.", ephemeral=True)
        return
    await interaction.response.send_message("Обновление задач...", ephemeral=True)
    await send_task_message()
    logging.info(f"Задачи обновлены пользователем {interaction.user}")

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

    all_tasks = []
    for column_id in config['column_ids'].values():
        column_tasks = get_tasks_from_yougile(column_id)
        all_tasks.extend(column_tasks)

    matched_task = next((task for task in all_tasks if task["title"].lower() == task_name.lower()), None)

    if not matched_task:
        await interaction.followup.send("Задача не найдена во всех колонках.", ephemeral=True)
        return

    raw_desc = matched_task.get("description", "Нет описания.")
    formatted_desc = html_to_discord(raw_desc)

    embed = discord.Embed(title=matched_task['title'], description=formatted_desc, color=0xffc86e)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="auto-pin", description="Включить/выключить автозакреп сообщений", guild=discord.Object(id=config['guild_id']))
async def auto_pin(interaction: discord.Interaction):
    config["auto_pin"] = not config.get("auto_pin", False)
    save_config(config)
    status = "включен" if config["auto_pin"] else "выключен"
    await interaction.response.send_message(f"Автозакреп теперь {status}.", ephemeral=True)

@tasks.loop(minutes=60)
async def update_task_message():
    logging.info("Автообновление задач")
    await send_task_message()

@bot.event
async def on_ready():
    logging.info(f"Бот подключён как {bot.user}")
    synced = await bot.tree.sync(guild=discord.Object(id=config['guild_id']))
    logging.info(f"Синхронизировано {len(synced)} команд.")

bot.run(os.getenv("BOT_TOKEN"))
