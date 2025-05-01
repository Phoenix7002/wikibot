import discord
from discord.ext import commands, tasks
from discord import app_commands
import requests
import logging
import asyncio
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", handlers=[
    logging.StreamHandler(),
    logging.FileHandler("bot_log.txt", mode="a")
])

config = {
    "guild_id": 1040938900039929917,
    "channel_id": 1060574644240912495,
    "allowed_role_ids": [1060574682971119627, 1254852832130236466, 1052215941938827295, 1043226064517865563],
    "yougile_api_token": os.getenv("YOUGILE_API_TOKEN"),
    "board_id": "45c9040a-8323-4d02-906d-a30035513ea3",
    "column_ids": {
        "Свободные": "a7dcfed3-8a7a-409c-b440-368e36e10cba",
        "В процессе выполнения": "ab166056-f1c7-4975-a05f-040069caf027",
        "Проверяются и дорабатываются": "20f3b5bb-9828-40ab-8f9f-ee700b40a62b"
    }
}

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
is_updating = False

def has_allowed_role(user: discord.Member) -> bool:
    return any(role.id in config["allowed_role_ids"] for role in user.roles)

def get_tasks_from_yougile(column_id):
    url = "https://ru.yougile.com/api-v2/task-list"
    headers = {
        'Authorization': f"Bearer {config['yougile_api_token']}",
        'Content-Type': 'application/json'
    }
    params = {
        "columnId": column_id
    }

    try:
        logging.info(f"Запрос задач из колонки: {column_id}")
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()

        if not response.text.strip():
            logging.warning(f"Пустой ответ от YouGile для колонки {column_id}")
            return []

        data = response.json()
        tasks = data.get("content", [])
        logging.info(f"Получено {len(tasks)} задач из колонки {column_id}")
        return tasks

    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка при запросе: {e}")
    except ValueError as e:
        logging.error(f"Ошибка при разборе JSON: {e}, текст ответа: {response.text}")

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
    channel = bot.get_channel(config['channel_id'])
    if not channel:
        logging.error("Канал не найден")
        return

    tasks_text = []
    for column_name, column_id in config['column_ids'].items():
        column_tasks = get_tasks_from_yougile(column_id)
        formatted = format_tasks_for_message(column_tasks, column_name)
        tasks_text.append(f"## {column_name}\n{formatted}")
    
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    message = "\n".join(tasks_text) + f"\n\n-# {now}"

    async for msg in channel.history(limit=10):
        if msg.author == bot.user and msg.content.startswith("##"):
            logging.info("Редактирование предыдущего сообщения с задачами")
            await msg.edit(content=message)
            return

    logging.info("Отправка нового сообщения с задачами")
    sent_message = await channel.send(message)
    try:
        await sent_message.pin()
        logging.info("Сообщение закреплено.")
    except discord.Forbidden:
        logging.warning("Не удалось закрепить сообщение — недостаточно прав.")

@bot.tree.command(name="send_list", description="Отправить список задач", guild=discord.Object(id=config['guild_id']))
async def send_list(interaction: discord.Interaction):
    if interaction.channel_id != config["channel_id"]:
        await interaction.response.send_message("Эта команда доступна только в специальном канале.", ephemeral=True)
        return
    if not has_allowed_role(interaction.user):
        logging.warning(f"Нет прав у {interaction.user}")
        await interaction.response.send_message("У вас нет прав для выполнения команды.", ephemeral=True)
        return

    await interaction.response.send_message("Отправка задач...", ephemeral=True)
    await send_task_message()
    logging.info(f"Задачи отправлены пользователем {interaction.user}")

@bot.tree.command(name="start_update", description="Запустить автоматическое обновление списка", guild=discord.Object(id=config['guild_id']))
async def start_update(interaction: discord.Interaction):
    global is_updating
    if interaction.channel_id != config["channel_id"]:
        await interaction.response.send_message("Эта команда доступна только в специальном канале.", ephemeral=True)
        return
    if not has_allowed_role(interaction.user):
        await interaction.response.send_message("У вас нет прав для выполнения этой команды.", ephemeral=True)
        return
    if is_updating:
        logging.info("Попытка повторного запуска уже активного цикла обновления.")
        await interaction.response.send_message("Цикл уже запущен.", ephemeral=True)
        return
    await interaction.response.send_message("Цикл обновления запускается...", ephemeral=True)
    update_task_message.start()
    is_updating = True
    logging.info("Автоматическое обновление задач запущено.")

@bot.tree.command(name="stop_update", description="Остановить автоматическое обновление списка", guild=discord.Object(id=config['guild_id']))
async def stop_update(interaction: discord.Interaction):
    global is_updating
    if interaction.channel_id != config["channel_id"]:
        await interaction.response.send_message("Эта команда доступна только в специальном канале.", ephemeral=True)
        return
    if not has_allowed_role(interaction.user):
        await interaction.response.send_message("У вас нет прав для выполнения этой команды.", ephemeral=True)
        return
    update_task_message.stop()
    is_updating = False
    await interaction.response.send_message("Цикл обновления остановлен.", ephemeral=True)
    logging.info("Автоматическое обновление задач остановлено.")

@bot.tree.command(name="update_list", description="Обновить задачи вручную", guild=discord.Object(id=config['guild_id']))
async def update_list(interaction: discord.Interaction):
    if interaction.channel_id != config["channel_id"]:
        await interaction.response.send_message("Эта команда доступна только в специальном канале.", ephemeral=True)
        return
    if not has_allowed_role(interaction.user):
        await interaction.response.send_message("У вас нет прав для выполнения этой команды.", ephemeral=True)
        return
    if not update_task_message.is_running():
        await interaction.response.send_message("Цикл не запущен. Используйте /ystart.", ephemeral=True)
        return
    await interaction.response.send_message("Обновление задач...", ephemeral=True)
    await send_task_message()
    logging.info(f"Задачи обновлены пользователем {interaction.user}")

@tasks.loop(minutes=60)
async def update_task_message():
    logging.info("Автоматическое обновление задач")
    await send_task_message()

@bot.event
async def on_ready():
    logging.info(f"Бот подключён как {bot.user}")
    synced = await bot.tree.sync(guild=discord.Object(id=config['guild_id']))
    logging.info(f"Синхронизировано {len(synced)} команд.")

bot.run(os.getenv("BOT_TOKEN"))
