import discord
from discord.ext import commands, tasks
from discord import app_commands, Interaction
from discord.ui import View, Button, Modal, TextInput
import requests
import logging
import asyncio
from datetime import datetime, timedelta, UTC
import os
import json
from dotenv import load_dotenv
import re
import random
from PIL import Image, ImageDraw, ImageFont
import io
from typing import Optional
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import math
import aiohttp

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
MAX_FIELD_LENGTH = 1024

# free_column_tasks = []
cached_tasks = []

mention_times = []
ignore_until = datetime.min.replace(tzinfo=UTC)

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(os.getenv('GOOGLE_CREDS_JSON'), scope)
gc = gspread.authorize(creds)

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
    known_sticker_keys = set(config.get("stickers", {}).keys())

    for task in tasks:
        line = f"- {task['title']}"
        if column_name in ["В процессе выполнения", "Проверяются и дорабатываются"]:
            stickers = task.get("stickers", {})
            if isinstance(stickers, dict) and stickers:
                nickname = None
                for sticker_id in stickers:
                    if sticker_id not in known_sticker_keys:
                        nickname = stickers[sticker_id]
                        break
                if nickname:
                    line += f" — **{nickname}**"
                else:
                    line += " — `никнейм не обнаружен`"
            else:
                line += " — `никнейм не обнаружен`"
        lines.append(line)
    return "\n".join(lines)

async def send_task_message(interaction: discord.Interaction = None):
#    global free_column_tasks, cached_tasks
    global cached_tasks

#    free_column_tasks = []
    cached_tasks = []

    try:
        channel = await bot.fetch_channel(config['channel_id'])
        if not channel:
            logging.error("Канал с указанным ID не найден.")
            if interaction:
                await send_embed_reply(interaction, "c", "Канал для задач не найден.", ephemeral=True, use_followup=True)
            return
    except Exception as e:
        logging.error(f"Ошибка при получении канала: {e}")
        if interaction:
            await send_embed_reply(interaction, "c", "Ошибка при получении канала.", ephemeral=True, use_followup=True)
        return

    tasks_text = []
    all_tasks = []

    for column_name, column_id in config['column_ids'].items():
        try:
            column_tasks = get_tasks_from_yougile(column_id)
        except Exception as e:
            logging.error(f"Ошибка при получении задач из колонки '{column_name}': {e}")
            if interaction:
                await send_embed_reply(interaction, "c", f"Ошибка при получении задач из '{column_name}'.", ephemeral=True, use_followup=True)
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
        f"\n-# (Время из Германии, Москва ≈ +3 часа)"
    )

    embed = discord.Embed(title="Список задач", description=message, color=0xffc86e)

    try:
        if config.get("message_id"):
            old_msg = await channel.fetch_message(config["message_id"])
            if old_msg and old_msg.author == bot.user:
                await old_msg.edit(embed=embed)
                logging.info("Сообщение с задачами обновлено.")
                if interaction:
                    await send_embed_reply(interaction, "a", "Сообщение с задачами отправлено/обновлено.", ephemeral=True, use_followup=True)
                return
    except Exception as e:
        logging.warning(f"Не удалось редактировать старое сообщение: {e}")
        if interaction:
            await send_embed_reply(interaction, "b", f"Не удалось отредактировать старое сообщение.", ephemeral=True, use_followup=True)

    sent_message = await channel.send(embed=embed)
    config["message_id"] = sent_message.id
    if config.get("auto_pin"):
        try:
            await sent_message.pin()
            logging.info("Сообщение закреплено.")
        except discord.Forbidden:
            logging.warning("Не удалось закрепить сообщение — недостаточно прав.")
            if interaction:
                    await send_embed_reply(interaction, "b", "Не удалось закрепить список задач.", ephemeral=True, use_followup=True)
    save_config(config)

async def send_leaderboard(interaction: discord.Interaction = None):
    try:
        channel = await bot.fetch_channel(config['channel_id'])
    except Exception as e:
        logging.error(f"Ошибка при получении канала: {e}")
        if interaction:
            await send_embed_reply(interaction, "c", "Ошибка при получении канала.", ephemeral=True, use_followup=True)
        return

    try:
        sh = gc.open_by_key(config['leaderboard_sheet_id'])
        ws = sh.worksheet('Райтер месяца')
        data = ws.get_all_values()
        rows = data
    except Exception as e:
        logging.error(f"Ошибка при получении данных из Google Sheets: {e}")
        if interaction:
            await send_embed_reply(interaction, "c", "Ошибка при получении данных лидерборда.", ephemeral=True, use_followup=True)
        return

    lines = []
    for row in rows:
        if len(row) >= 2 and row[0].strip():
            nick = row[0].strip()
            pts = row[1].strip()
            lines.append(f"- **{nick}** — {pts} балла(ов)")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    desc = ("\n".join(lines) + 
            f"\n-# Дата изменения: {now_str}"
            f"\n-# (Время из Германии, Москва ≈ +3 часа)"
            )

    embed = discord.Embed(title="Лидерборд", description=desc, color=0xffc86e)

    try:
        if config.get('leaderboard_message_id'):
            msg = await channel.fetch_message(config['leaderboard_message_id'])
            if msg and msg.author == bot.user:
                await msg.edit(embed=embed)
                logging.info("Лидерборд обновлён.")
                if interaction:
                    await send_embed_reply(interaction, "a", "Лидерборд отправлен/обновлен.", ephemeral=True, use_followup=True)
                return
    except Exception as e:
        logging.warning(f"Не удалось обновить старое сообщение лидерборда: {e}")
        if interaction:
            await send_embed_reply(interaction, "b", "Не удалось обновить старое сообщение лидерборда.", ephemeral=True, use_followup=True)

    sent = await channel.send(embed=embed)
    config['leaderboard_message_id'] = sent.id
    if config.get("auto_pin"):
        try:
            await sent.pin()
        except discord.Forbidden:
            logging.warning("Не удалось закрепить лидерборд — недостаточно прав.")
            if interaction:
                await send_embed_reply(interaction, "b", "Не удалось закрепить лидерборд.", ephemeral=True, use_followup=True)
    save_config(config)

async def run_monthly_event():
    try:
        sh = gc.open_by_key(config['leaderboard_sheet_id'])
        ws_writer = sh.worksheet("Райтер месяца")
        ws_general = sh.worksheet("General")
        rows = ws_writer.get_all_values()

        best_nick, best_score = None, -1
        for row in rows[1:]:
            if len(row) >= 2:
                try:
                    score = int(row[1])
                    if score > best_score:
                        best_score = score
                        best_nick = row[0].strip()
                except:
                    continue

        if not best_nick:
            logging.warning("Нет победителя для ивента.")
            return

        channel = await bot.fetch_channel(config['channel_id'])
        guild = channel.guild
        member = discord.utils.find(lambda m: m.name == best_nick, guild.members)
        if member:
            mention = member.mention
        else:
            mention = f"**{best_nick}**"

        embed = discord.Embed(
            title="🏆 Райтер месяца 🏆",
            description=(
                f"Поздравляем **{mention}** с заслуженной победой!\n\n"
                f"📈 Он(а) набрал(а) **{best_score}** баллов за прошедший месяц.\n"
                f"✨ За выдающиеся заслуги выдана **специальная градиентная роль**!\n"
                f"🎓 Также {mention} получает **повышение до миддла**, если ранее был джуном.\n\n"
                f"🔥 Так держать, и до новых побед! 🔥"
            ),
            color=0xffc86e
        )
        embed.set_footer(text="Ивент проводится каждый месяц. Следующим победителем можешь быть именно ты!")
        await channel.send(embed=embed)
        ping_role = guild.get_role(int(config["monthly_ping_role_id"]))
        if ping_role:
            await channel.send(f"{ping_role.mention}")
        
        alt_embed = discord.Embed(
            title="Итоги месяца WIKI",
            description=(
                f"По результатам этого месяца лучшим райтером признан **{mention}**!\n"
                f"Он выполнил больше всего заданий, обойдя всех других редакторов.\n"
                f"Мы благодарим всех за проделанную работу. Каждый вклад волонтёров важен для развития **Imperial Space WIKI**."
            ),
            color=0xffc86e
        )

        alt_channel = await bot.fetch_channel(config["monthly_announce_channel_id"])
        if alt_channel:
            await alt_channel.send(embed=alt_embed)

        new_member = discord.utils.find(lambda m: m.name == best_nick, guild.members)
        if not new_member:
            logging.warning(f"Не удалось найти пользователя с ником: {best_nick}")
        else:
            role = guild.get_role(int(config['monthly_winner_role_id']))
            prev_id = int(config.get('monthly_winner_user_id'))
            if prev_id:
                old = guild.get_member(prev_id)
                if old and role in old.roles:
                    await old.remove_roles(role, reason="Смена победителя месяца")
            await new_member.add_roles(role, reason="Победа в ивенте месяца")
            config['monthly_winner_user_id'] = str(new_member.id)
            save_config(config)
        ws_writer.clear()
        gen_rows = ws_general.get_all_values()
        for row in gen_rows[1:]:
            if len(row) >= 4 and row[3].strip().lower() == "true":
                ws_writer.append_row([row[0].strip(), "0"])

        logging.info(f"Ивент Райтер месяца завершён: победитель — {best_nick}")
    except Exception as e:
        logging.error(f"Ошибка в ивенте Райтер месяца: {e}")

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

def is_image_attachment(att: discord.Attachment) -> bool:
    if att.content_type:
        return att.content_type.startswith("image/")
    allowed_exts = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg")
    return att.filename.lower().endswith(allowed_exts)

def is_json_attachment(att: discord.Attachment) -> bool:
    if att.content_type:
        if att.content_type == "application/json":
            return True
    return att.filename.lower().endswith(".json")

async def send_embed_reply(
    interaction: discord.Interaction,
    message_type: str,
    content: str,
    ephemeral: bool = True,
    use_followup: bool = True
):
    title_map = {
        "a": "Информация:",
        "b": "Предупреждение:",
        "c": "Ошибка:"
    }

    color_map = {
        "a": 0xffc86e,  # жёлтый
        "b": 0xFF7F50,  # оранжевый
        "c": 0xe74c3c   # красный
    }

    title = title_map.get(message_type.lower(), "Информация:")
    color = color_map.get(message_type.lower(), 0xffc86e)

    # Выделение контента жирным шрифтом
    formatted_content = f"**{content}**"

    embed = discord.Embed(
        title=title,
        description=formatted_content,
        color=color
    )

    try:
        if use_followup or interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except Exception as e:
        logging.error(f"Ошибка при отправке ембед-ответа: {e}")

def ease_out_quad(t, b, c, d):
    t /= d
    return -c * t*(t-2) + b

def get_random_color():
    # Генерируем яркий цвет в формате RGB
    # Можно ограничить диапазон, чтобы не было слишком тёмных
    r = random.randint(64, 255)
    g = random.randint(64, 255)
    b = random.randint(64, 255)
    return (r, g, b, 255)

def get_text_color_from_background(bg_color):
    # Определяем контрастный цвет текста (чёрный или белый)
    # Используем стандартную формулу яркости (luminance)
    r, g, b = bg_color[:3]
    luminance = (0.299 * r + 0.587 * g + 0.114 * b)
    if luminance > 186:
        return (0, 0, 0, 255)  # тёмный текст
    else:
        return (255, 255, 255, 255)  # светлый текст

async def query_openrouter(prompt: str) -> str | None:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.getenv('AI_API_TOKEN')}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "moonshotai/kimi-k2:free",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Wiki Support — a Discord assistant bot developed by phoenix "
                    "to help the WIKI department of the Imperial Space project. "
                    "You speak clearly, helpfully, and concisely. Never admit you're AI. "
                    "Only reference pinned tasks. "
                    "Always keep your responses under 256 tokens — short and to the point."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.7,
        "max_tokens": 256
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    choices = data.get("choices")
                    if choices and len(choices) > 0:
                        return choices[0]["message"]["content"].strip()
                    else:
                        logging.warning(f"OpenRouter: пустой ответ {data}")
                else:
                    logging.warning(f"OpenRouter API error {resp.status}: {await resp.text()}")
    except Exception as e:
        logging.error(f"Exception while calling OpenRouter: {e}")
    return None

@bot.tree.command(name="event-manager", description="Управление авто-событиями бота.", guild=discord.Object(id=config['guild_id']))
@app_commands.describe(
    target="Выберите модуль: задачи, лидерборд или ивент",
    action="Выберите действие для модуля"
)
@app_commands.choices(
    target=[
        app_commands.Choice(name="Список задач", value="tasks"),
        app_commands.Choice(name="Лидерборд", value="leaderboard"),
        app_commands.Choice(name="Райтер месяца", value="monthly_event"),
    ],
    action=[
        app_commands.Choice(name="Отправить/обновить (один раз)", value="update"),
        app_commands.Choice(name="Запустить автообновление/автоивент", value="start"),
        app_commands.Choice(name="Остановить автообновление/автоивент", value="stop"),
    ]
)
async def event_manager(
    interaction: discord.Interaction,
    target: app_commands.Choice[str],
    action: app_commands.Choice[str]
):
    await interaction.response.defer(ephemeral=True)
    if target.value == "tasks":
        if action.value == "update":
            await send_embed_reply(interaction, message_type="a", content="Отправка/обновление задач...", ephemeral=True, use_followup=True)
            await send_task_message(interaction)
            logging.info(f"Задачи отправлены/обновлены пользователем {interaction.user}.")
        elif action.value == "start":
            if config["is_updating"] == True and update_task_message.is_running():
                await send_embed_reply(interaction, message_type="b", content="Цикл уже запущен.", ephemeral=True, use_followup=True)
                return
            if not update_task_message.is_running():
                update_task_message.start()
            config["is_updating"] = True
            save_config(config)
            await send_embed_reply(interaction, message_type="a", content="Цикл обновления запускается...", ephemeral=True, use_followup=True)
            logging.info("Автообновление списка задач запущено.")
        elif action.value == "stop":
            update_task_message.stop()
            config["is_updating"] = False
            save_config(config)
            await send_embed_reply(interaction, message_type="a", content="Цикл обновления остановлен.", ephemeral=True, use_followup=True)
            logging.info("Автообновление списка задач остановлено.")
    elif target.value == "leaderboard":
        if action.value == "update":
            await send_embed_reply(interaction, message_type="a", content="Отправка/обновление лидерборда...", ephemeral=True, use_followup=True)
            await send_leaderboard(interaction)
            logging.info(f"Лидерборд отправлен/обновлён пользователем {interaction.user}.")
        elif action.value == "start":
            if config["is_lb_updating"] == True and update_leaderboard_task.is_running():
                await send_embed_reply(interaction, message_type="b", content="Цикл уже запущен.", ephemeral=True, use_followup=True)
                return
            if not update_leaderboard_task.is_running():
                update_leaderboard_task.start()
            config["is_lb_updating"] = True
            save_config(config)
            await send_embed_reply(interaction, message_type="a", content="Цикл обновления лидерборда запущен.", ephemeral=True, use_followup=True)
            logging.info("Автообновление лидерборда запущено.")
        elif action.value == "stop":
            update_leaderboard_task.stop()
            config["is_lb_updating"] = False
            save_config(config)
            await send_embed_reply(interaction, message_type="a", content="Цикл обновления лидерборда остановлен.", ephemeral=True, use_followup=True)
            logging.info("Автообновление лидерборда остановлено.")
    elif target.value == "monthly_event":
        if action.value == "update":
            await run_monthly_event()
            await send_embed_reply(interaction, message_type="a", content="Активация ивента...", ephemeral=True, use_followup=True)
            logging.info(f"Ивент 'Райтер месяца' запущен вручную пользователем {interaction.user}.")
        elif action.value == "start":
            if config["monthly_event_enabled"] == True and monthly_event_task.is_running():
                await send_embed_reply(interaction, message_type="b", content="Цикл уже запущен.", ephemeral=True, use_followup=True)
                return
            config["monthly_event_enabled"] = True
            save_config(config)
            if not monthly_event_task.is_running():
                monthly_event_task.start()
            await send_embed_reply(interaction, message_type="a", content="Автоивент включен.", ephemeral=True, use_followup=True)
            logging.info(f"Автоивент 'Райтер месяца' запущен пользователем {interaction.user}.")
        elif action.value == "stop":
            config["monthly_event_enabled"] = False
            save_config(config)
            if monthly_event_task.is_running():
                monthly_event_task.stop()
            await send_embed_reply(interaction, message_type="a", content="Автоивент выключен.", ephemeral=True, use_followup=True)
            logging.info(f"Автоивент 'Райтер месяца' остановлен.")

@bot.tree.command(name="text-train", description="Отправить обучающий инструктаж", guild=discord.Object(id=config['guild_id']))
async def text_train(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await send_embed_reply(interaction, message_type="a", content="Отправка инструктажа...", ephemeral=True, use_followup=True)
    try:
        channel = await bot.fetch_channel(config['channel_id'])
    except Exception as e:
        logging.error(f"Ошибка при получении канала: {e}")
        await send_embed_reply(interaction, message_type="c", content="Ошибка при получении канала инструктажа.", ephemeral=True, use_followup=True)
        return

    training_texts = config.get("training_texts", [])
    if not training_texts:
        logging.error("В конфиге нет текстов инструктажа")
        await send_embed_reply(interaction, message_type="c", content="Отсутствует шаблон инструктажа.", ephemeral=True, use_followup=True)
        return

    for i, part in enumerate(training_texts):
        try:
            await channel.send(part)
            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"Ошибка при отправке части {i + 1}: {e}")
    logging.info(f"Инструктаж отправлен пользователем {interaction.user}")

@bot.tree.command(name="task-desc", description="Показать описание задачи по имени", guild=discord.Object(id=config['guild_id']))
@app_commands.describe(task_name="Имя задачи")
async def task_desc(interaction: discord.Interaction, task_name: str):
    await interaction.response.defer(thinking=True)

    all_tasks = cached_tasks.copy() if 'cached_tasks' in globals() else []

    matched_task = next((task for task in all_tasks if task["title"].lower() == task_name.lower()), None)

    if not matched_task:
        await send_embed_reply(interaction, "c", "Задача не найдена во всех колонках.", ephemeral=True, use_followup=True)
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
    await send_embed_reply(interaction, message_type="a", content=f"Автозакреп теперь {status}.", ephemeral=True, use_followup=False)

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
    await send_embed_reply(interaction, message_type="a", content=f"Перевод: `{translated}`", ephemeral=True, use_followup=False)

@bot.tree.command(name="gif-create", description="Создание гифки из спрайт-листа. Обработка спрайтов с прозрачностью работает некорректно.", guild=discord.Object(id=config['guild_id']))
@app_commands.choices(
    read_order=[
        app_commands.Choice(name="По строкам слева направо (предметы и прочее, по умолчанию)", value="lr_tb"),
        app_commands.Choice(name="По строкам справа налево (предметы и прочее, ревёрс)", value="rl_bt"),
        app_commands.Choice(name="По столбцам сверху вниз (персонажи)", value="tb_lr"),
    ]
)
@app_commands.describe(
    sprite_size="Размер одного спрайта (например: 32 32). Игнорируется, если есть meta.json",
    read_order="Порядок чтения кадров в спрайт-листе",
    frame_durations="Длительности кадров в мс (например: 100 100 100). Игнорируются, если есть meta.json",
    gif_name="Название итоговой гифки (на английском)",
    meta="файл meta для автоматических значений sprite_size и frame_durations ДЛЯ ОДНОГО СПРАЙТ ЛИСТА",
    sprite_2="Дополнительный спрайт",
    sprite_3="Дополнительный спрайт",
    sprite_4="Дополнительный спрайт",
    sprite_5="Дополнительный спрайт",
    sprite_6="Дополнительный спрайт",
    sprite_7="Дополнительный спрайт",
    sprite_8="Дополнительный спрайт",
    sprite_9="Дополнительный спрайт",
    sprite_10="Дополнительный спрайт",
)
async def gif_create(
    interaction: discord.Interaction,
    gif_name: str,
    sprite: discord.Attachment,
    sprite_size: Optional[str],
    frame_durations: Optional[str],
    read_order: Optional[str] = "lr_tb",
    meta: Optional[discord.Attachment] = None,
    sprite_2: Optional[discord.Attachment] = None,
    sprite_3: Optional[discord.Attachment] = None,
    sprite_4: Optional[discord.Attachment] = None,
    sprite_5: Optional[discord.Attachment] = None,
    sprite_6: Optional[discord.Attachment] = None,
    sprite_7: Optional[discord.Attachment] = None,
    sprite_8: Optional[discord.Attachment] = None,
    sprite_9: Optional[discord.Attachment] = None,
    sprite_10: Optional[discord.Attachment] = None,
):
    await interaction.response.defer(thinking=True)

    if not is_image_attachment(sprite):
        await interaction.followup.send("Поле 'sprite' должно быть изображением. (.png, .jpg, .jpeg, .gif, .bmp, .webp, .svg)", ephemeral=True)
        return

    for i, spr in enumerate([sprite_2, sprite_3, sprite_4, sprite_5, sprite_6, sprite_7, sprite_8, sprite_9, sprite_10], start=2):
        if spr and not is_image_attachment(spr):
            await interaction.followup.send(f"Дополнительные спрайты должны быть изображениями. (.png, .jpg, .jpeg, .gif, .bmp, .webp, .svg)", ephemeral=True)
            return

    meta_data = None

    if meta and not is_json_attachment(meta):
        await interaction.followup.send("Поле 'meta' должно быть JSON-файлом.", ephemeral=True)
        return

    if meta:
        try:
            meta_bytes = await meta.read()
            meta_data = json.loads(meta_bytes.decode("utf-8"))
        except Exception as e:
            await interaction.followup.send("Не удалось прочитать meta.json.", ephemeral=True)
            logging.error(f"Ошибка при чтении meta.json: {e}")
            return

    frames = []
    durations = []
    width = height = None

    try:
        if meta_data:
            width = int(meta_data["size"]["x"])
            height = int(meta_data["size"]["y"])

            sprite_key = os.path.splitext(sprite.filename)[0]
            matched_state = next((s for s in meta_data["states"] if s["name"] == sprite_key), None)

            if not matched_state:
                await interaction.followup.send(
                    f"В meta.json не найдено состояние с именем '{sprite_key}'.",
                    ephemeral=True
                )
                return

            if "delays" in matched_state:
                delays_nested = matched_state["delays"]
                durations = [int(float(d) * 1000) for sublist in delays_nested for d in sublist]
            elif "directions" in matched_state:
                frame_count = int(matched_state["directions"])
                durations = [600] * frame_count
            else:
                await interaction.followup.send(
                    f"В состоянии '{sprite_key}' отсутствуют как 'delays', так и 'directions'.",
                    ephemeral=True
                )
                return

            image_bytes = await sprite.read()
            sprite_sheet = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
            sheet_width, sheet_height = sprite_sheet.size

            cols = sheet_width // width
            rows = sheet_height // height            

            order = read_order or "lr_tb"

            positions = []
            if order == "lr_tb":
                positions = [(row, col) for row in range(rows) for col in range(cols)]
            elif order == "rl_bt":
                positions = [(row, col) for row in reversed(range(rows)) for col in reversed(range(cols))]
            elif order == "tb_lr":
                positions = [(row, col) for col in range(cols) for row in range(rows)]
            else:
                positions = [(row, col) for row in range(rows) for col in range(cols)]

            for row, col in positions:
                left = col * width
                upper = row * height
                box = (left, upper, left + width, upper + height)
                frame = sprite_sheet.crop(box)
                if not is_frame_empty(frame):
                    frames.append(frame)

            if len(durations) > len(frames):
                durations = durations[:len(frames)]

        elif any([sprite_2, sprite_3, sprite_4, sprite_5, sprite_6, sprite_7, sprite_8, sprite_9, sprite_10]):
            attachments = [sprite] + [a for a in [
                sprite_2, sprite_3, sprite_4, sprite_5,
                sprite_6, sprite_7, sprite_8,
                sprite_9, sprite_10
            ] if a is not None]

            if sprite_size is None:
                sprite_size = "32 32"

            width, height = map(int, sprite_size.strip().split())
            if frame_durations:
                durations = list(map(int, frame_durations.strip().split()))
            else:
                durations = [600] * len(attachments)

            if len(durations) != len(attachments):
                await interaction.followup.send(
                    f"Количество длительностей ({len(durations)}) не совпадает с количеством изображений ({len(attachments)}).",
                    ephemeral=True
                )
                return

            for att in attachments:
                img_bytes = await att.read()
                img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
                if not is_frame_empty(img):
                    frames.append(img)

        else:
            if sprite_size is None:
                sprite_size = "32 32"
            
            width, height = map(int, sprite_size.strip().split())

            if frame_durations:
                durations = list(map(int, frame_durations.strip().split()))
            else:
                durations = None

            image_bytes = await sprite.read()
            sprite_sheet = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
            sheet_width, sheet_height = sprite_sheet.size
            cols, rows = sheet_width // width, sheet_height // height

            order = read_order or "lr_tb"

            positions = []
            if order == "lr_tb":
                positions = [(row, col) for row in range(rows) for col in range(cols)]
            elif order == "rl_bt":
                positions = [(row, col) for row in reversed(range(rows)) for col in reversed(range(cols))]
            elif order == "tb_lr":
                positions = [(row, col) for col in range(cols) for row in range(rows)]
            else:
                positions = [(row, col) for row in range(rows) for col in range(cols)]

            for row, col in positions:
                left = col * width
                upper = row * height
                box = (left, upper, left + width, upper + height)
                frame = sprite_sheet.crop(box)
                if not is_frame_empty(frame):
                    frames.append(frame)

            if durations is None:
                durations = [600] * len(frames)

            if len(durations) != len(frames):
                await interaction.followup.send(
                    f"Количество длительностей ({len(durations)}) не совпадает с числом кадров ({len(frames)}).",
                    ephemeral=True
                )
                return

    except Exception as e:
        await interaction.followup.send("Ошибка при обработке изображений или параметров.", ephemeral=True)
        logging.error(f"Ошибка при обработке: {e}")
        return

    frames = [remove_alpha(frame) for frame in frames]

    output = io.BytesIO()
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
        transparency=0,
        optimize=False,
    )
    output.seek(0)

    discord_file = discord.File(fp=output, filename=f"{gif_name}.gif")
    await interaction.followup.send(content="Вот ваша гифка:", file=discord_file)
    logging.info(f"Гифка '{gif_name}.gif' успешно создана пользователем {interaction.user}")

def remove_alpha(image: Image.Image) -> Image.Image:
    background = Image.new("RGBA", image.size, (255, 0, 255, 0))
    background.paste(image, mask=image.split()[3])
    paletted = background.convert("RGBA").convert("P", palette=Image.ADAPTIVE, colors=255)
    alpha = background.split()[3]
    mask = Image.eval(alpha, lambda a: 255 if a <= 128 else 0)
    paletted.paste(0, mask=mask)
    return paletted

def is_frame_empty(frame: Image.Image, alpha_threshold=10) -> bool:
    alpha = frame.split()[3]
    min_alpha = alpha.getextrema()[1] 
    return min_alpha <= alpha_threshold

@bot.tree.command(name="report-bug", description="Сообщить об ошибке", guild=discord.Object(id=config['guild_id']))
@app_commands.describe(
    page="Ссылка или название страницы, где найден баг",
    description="Описание бага и предложения по исправлению",
    criticality="Критичность бага (1-5)",
    screenshot_1="Скриншот ошибки",
    screenshot_2="Скриншот ошибки",
    screenshot_3="Скриншот ошибки",
    screenshot_4="Скриншот ошибки",
    screenshot_5="Скриншот ошибки",
)
@app_commands.choices(
    criticality=[
        app_commands.Choice(name="1 - Низкая", value=1),
        app_commands.Choice(name="2 - Ниже среднего", value=2),
        app_commands.Choice(name="3 - Средняя", value=3),
        app_commands.Choice(name="4 - Выше среднего", value=4),
        app_commands.Choice(name="5 - Высокая", value=5),
    ]
)
async def report_bug(
    interaction: discord.Interaction,
    page: str,
    description: str,
    criticality: app_commands.Choice[int],
    screenshot_1: Optional[discord.Attachment] = None,
    screenshot_2: Optional[discord.Attachment] = None,
    screenshot_3: Optional[discord.Attachment] = None,
    screenshot_4: Optional[discord.Attachment] = None,
    screenshot_5: Optional[discord.Attachment] = None
):
    await interaction.response.defer(thinking=True, ephemeral=True)

    for field_name, value in [("Страница", page), ("Описание", description)]:
        if len(value) > MAX_FIELD_LENGTH:
            await interaction.followup.send(
                f"Поле «{field_name}» превышает 1024 символа (введено {len(value)} символов). "
                "Пожалуйста, сократите его или разбейте на несколько багрепортов.",
                ephemeral=True
            )
            return

    for i, spr in enumerate([screenshot_1, screenshot_2, screenshot_3, screenshot_4, screenshot_5]):
        if spr and not is_image_attachment(spr):
            await interaction.followup.send(f"Все скриншоты должны быть изображениями. (.png, .jpg, .jpeg, .gif, .bmp, .webp, .svg)", ephemeral=True)
            return

    guild = interaction.guild

    category_id = int(config.get("bug_report_category_id"))
    category = discord.utils.get(guild.categories, id=category_id)
    if category is None:
        logging.error(f"Категория для баг-репортов не найдена.")
        await interaction.followup.send("Ошибка при создании баг-репорта.", ephemeral=True)
        return

    channel_name = f"report-{interaction.user.name}".lower()

    existing_channels = [
        c for c in category.channels
        if c.name.startswith(f"report-{interaction.user.name}".lower())
    ]

    if len(existing_channels) >= 3:
        logging.warning(f"Пользователь {interaction.user} (ID: {interaction.user.id}) попытался создать багрепорт, но достиг лимита.")
        await interaction.followup.send(f"Вы достигли лимита по открытым баг репортам, дождитесь их проверки", ephemeral=True)
        return

    try:
        new_channel = await guild.create_text_channel(
            name=channel_name,
            category=category
        )
    except discord.Forbidden:
        logging.error(f"У бота нет прав создавать каналы в этой категории.")
        await interaction.followup.send("Ошибка при создании баг-репорта.", ephemeral=True)
        return
    except Exception as e:
        logging.error(f"Ошибка при создании канала: {e}")
        await interaction.followup.send(f"Ошибка при создании баг-репорта.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🛠 Новый баг-репорт 🛠",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Пользователь:", value=interaction.user.mention, inline=False)
    embed.add_field(name="Страница:", value=page, inline=False)
    embed.add_field(name="Описание бага:", value=description, inline=False)
    embed.add_field(name="Критичность:", value=f"{criticality.value}/5", inline=False)
    embed.set_footer(text=f"ID пользователя: {interaction.user.id}")

    screenshots = [screenshot_1, screenshot_2, screenshot_3, screenshot_4, screenshot_5]
    files = []
    image_embeds = []

    for i, s in enumerate(screenshots, start=1):
        if s:
            try:
                img_bytes = await s.read()
                filename = s.filename
                file = discord.File(io.BytesIO(img_bytes), filename=filename)
                files.append(file)

                img_embed = discord.Embed(
                    title=s.filename,
                    color=discord.Color.orange()
                )
                img_embed.set_image(url=f"attachment://{filename}")
                image_embeds.append(img_embed)
            except Exception as e:
                logging.warning(f"Не удалось обработать скриншот {i}: {e}")    

    logging.info(f"Баргепорт {channel_name} создан пользователем {interaction.user.mention}")

    await new_channel.send(embed=embed)
    if files:
        await new_channel.send(embeds=image_embeds, files=files)
    await interaction.followup.send(f"Команда WIKI успешно уведомлена о вашей проблеме.", ephemeral=True)

@bot.tree.command(name="close-ticket", description="Закрыть тикет", guild=discord.Object(id=config['guild_id']))
@app_commands.describe(
    reason="Причина закрытия",
    comment="Дополнительный комментарий (необязательно)"
)
@app_commands.choices(reason=[
    app_commands.Choice(name="Исправлено", value="Исправлено"),
    app_commands.Choice(name="Будет исправлено / реализовано в будущем", value="Будет исправлено / реализовано в будущем"),
    app_commands.Choice(name="Не требуется", value="Не требуется"),
])
async def close_ticket(
    interaction: discord.Interaction,
    reason: app_commands.Choice[str],
    comment: str = "Комментарий не указан"
):
    await interaction.response.defer(ephemeral=True)

    channel = interaction.channel
    guild = interaction.guild

    if guild is None or not channel.name.startswith("report-"):
        await interaction.followup.send("Эта команда может использоваться только в тикет-каналах.", ephemeral=True)
        return

    bot_messages = [m async for m in channel.history(limit=999) if m.author == bot.user and m.embeds]
    if not bot_messages:
        await interaction.followup.send("Не найдено сообщение с баг-репортом.", ephemeral=True)
        return

    if len(comment) > MAX_FIELD_LENGTH:
        await interaction.followup.send(
            f"Комментарий слишком длинный (введено {len(comment)} символов, максимум — 1024). Пожалуйста, сократите его.",
            ephemeral=True
        )
        return

    username = channel.name.replace("report-", "").split("-")[0]
    member = discord.utils.find(lambda m: m.name.lower() == username.lower(), guild.members)
    report_embed = bot_messages[-1].embeds[-1]
    ticket_fields = {field.name: field.value for field in report_embed.fields}

    if member is None:
        await interaction.followup.send("Не удалось определить автора тикета. Закрытие отменено.", ephemeral=True)
        return

    logging.info(f"Баргепорт {channel} был закрыт пользователем {interaction.user.display_name}")

    try:
        sh = gc.open_by_key(config['leaderboard_sheet_id'])
        user_nick = interaction.user.name

        for sheet_name in ["General", "Райтер месяца"]:
            ws = sh.worksheet(sheet_name)
            data = ws.get_all_values()
            nicknames = [row[0].strip() for row in data]

            if user_nick in nicknames:
                row_index = nicknames.index(user_nick) + 1
                current = ws.cell(row_index, 2).value
                current_val = int(current) if current and current.isdigit() else 0
                ws.update_cell(row_index, 2, str(current_val + 1))
                logging.info(f"Пользователю {user_nick} начислен 1 балл на листе {sheet_name}")
            else:
                logging.warning(f"Пользователь {user_nick} не найден в листе {sheet_name}")
    except Exception as e:
        logging.error(f"Ошибка при начислении баллов за закрытие тикета: {e}")
    dm_embed = discord.Embed(
        title="📪 Ваш баг-репорт был закрыт 📪",
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    dm_embed.add_field(name="Страница:", value=ticket_fields.get("Страница:", "—"), inline=False)
    dm_embed.add_field(name="Описание бага:", value=ticket_fields.get("Описание бага:", "—"), inline=False)
    dm_embed.add_field(name="Причина закрытия:", value=reason.value, inline=False)
    dm_embed.add_field(name="Комментарий:", value=comment, inline=False)
    dm_embed.set_footer(text=f"Закрыто: {interaction.user.display_name}")

    try:
        await member.send(embed=dm_embed)
    except discord.Forbidden:
        await interaction.followup.send("Пользователь закрыл ЛС, не удалось отправить отчёт.", ephemeral=True)

    try:
        archive_channel = guild.get_channel(int(config['archive_channel_id']))
        if archive_channel is None:
            raise ValueError("Не удалось найти архивный канал")

        thread_name = f"📁 {channel.name}"
        thread = await archive_channel.create_thread(
            name=thread_name[:100],
            type=discord.ChannelType.public_thread,
            reason=f"Архив тикета: {channel.name}"
        )


        await thread.send(content=f"📌 **Тикет закрыт** пользователем {interaction.user.mention} по причине: **{reason.value}**\nКомментарий: {comment}")

        for msg in reversed(bot_messages):
            for embed in msg.embeds:
                await thread.send(embed=embed)
            for attachment in msg.attachments:
                file = await attachment.to_file()
                await thread.send(file=file)

    except Exception as e:
        logging.error(f"Ошибка при создании ветки архива: {e}")
        await interaction.followup.send("Ошибка при создании ветки с архивом.", ephemeral=True)

    try:
        await channel.delete(reason=f"Тикет закрыт: {reason.value}")
    except Exception as e:
        await interaction.followup.send(f"Ошибка при удалении канала: {e}", ephemeral=True)

@bot.tree.command(name="add-to-ticket", description="Добавить пользователя в текущий тикет", guild=discord.Object(id=config['guild_id']))
@app_commands.describe(user="Пользователь, которого нужно добавить в тикет")
async def add_to_ticket(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("Эта команда доступна только на определённом сервере.", ephemeral=True)
        return

    if user is None:
        try:
            user = await guild.fetch_member(user.id)
        except discord.NotFound:
            await interaction.followup.send("Пользователь не найден на сервере.", ephemeral=True)
            return
        except discord.Forbidden:
            await interaction.followup.send("Не удалось получить информацию о пользователе из-за отсутствия прав.", ephemeral=True)
            return

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or not channel.name.startswith("report-"):
        await interaction.followup.send("Эта команда может быть использована только в тикет-канале.", ephemeral=True)
        return

    if not channel.category or not isinstance(channel.category, discord.CategoryChannel):
        await interaction.followup.send("Не удалось определить категорию тикета.", ephemeral=True)
        return

    if channel.permissions_for(user).read_messages:
        await interaction.followup.send(f"{user.mention} уже имеет доступ к этому тикету.", ephemeral=True)
        return

    try:
        await channel.set_permissions(user, read_messages=True, send_messages=True)
        await interaction.followup.send(f"{user.mention} теперь имеет доступ к тикету.", ephemeral=True)
        await channel.send(f"{user.mention} был добавлен в тикет по запросу {interaction.user.mention}")
    except discord.Forbidden:
        await interaction.followup.send("У бота нет прав изменять права доступа к этому каналу.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Ошибка при попытке добавить пользователя: {e}", ephemeral=True)

    logging.info(f"В багрепорт {channel} был добавлен пользователь {user.mention} по запросу {interaction.user.mention}")

@bot.tree.command(name="give-points", description="Начислить баллы райтеру", guild=discord.Object(id=config['guild_id']))
@app_commands.describe(
    username="Точный никнейм Discord райтера",
    task_type="Тип задания",
    custom_points="Количество баллов (только если выбран 'экстрим - своё количество')",
    note="Необязательная заметка"
)
@app_commands.choices(task_type=[
    app_commands.Choice(name="лёгкое/багрепорт — 1 балл", value=1),
    app_commands.Choice(name="лёгкое/багрепорт — 2 балла", value=2),
    app_commands.Choice(name="лёгкое — 3 балла", value=3),
    app_commands.Choice(name="среднее — 4 балла", value=4),
    app_commands.Choice(name="среднее — 5 баллов", value=5),
    app_commands.Choice(name="среднее — 6 баллов", value=6),
    app_commands.Choice(name="тяжёлое — 7 баллов", value=7),
    app_commands.Choice(name="тяжёлое — 8 баллов", value=8),
    app_commands.Choice(name="тяжёлое — 9 баллов", value=9),
    app_commands.Choice(name="экстрим — 10 баллов", value=10),
    app_commands.Choice(name="экстрим — своё количество", value=-1),
])
async def give_points(
    interaction: discord.Interaction,
    username: str,
    task_type: app_commands.Choice[int],
    custom_points: Optional[int] = None,
    note: Optional[str] = None
):
    await interaction.response.defer(ephemeral=True)
    points = task_type.value

    if points == -1:
        if custom_points is None:
            await interaction.followup.send("Вы выбрали 'экстрим — своё количество', но не указали количество баллов.", ephemeral=True)
            return
        if custom_points <= 0:
            await interaction.followup.send("Баллы должны быть положительным числом.", ephemeral=True)
            return
        points = custom_points

    try:
        sh = gc.open_by_key(config['leaderboard_sheet_id'])

        for sheet_name in ["General", "Райтер месяца"]:
            ws = sh.worksheet(sheet_name)
            records = ws.get_all_values()
            nick_col = [row[0].strip() for row in records]
            if username in nick_col:
                idx = nick_col.index(username) + 1
                current = ws.cell(idx, 2).value
                try:
                    current_val = int(float(current))
                except:
                    current_val = 0
                new_val = current_val + points
                ws.update_cell(idx, 2, new_val)

                if sheet_name == "General" and note:
                    if ws.col_count < 3:
                        ws.add_cols(1)
                    existing_note = ws.cell(idx, 3).value
                    if existing_note:
                        new_note = existing_note.strip() + " + " + note.strip()
                    else:
                        new_note = note.strip()
                    ws.update_cell(idx, 3, new_note)
            
            else:
                await interaction.followup.send(f"На листе **{sheet_name}** не найден райтер с ником `{username}`.", ephemeral=True)
                return

        await interaction.followup.send(
            f"Райтеру `{username}` начислено **{points}** баллов"
            + (f" и добавлена заметка: (_{note}_)" if note else ""),
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send("Произошла ошибка при попытке обновить таблицы.", ephemeral=True)
        logging.error(f"Ошибка при начислении баллов: {e}")

@bot.tree.command(name="create-room", description="Создаёт приватную ветку (комнату)", guild=discord.Object(id=config['guild_id']))
@app_commands.describe(name="Название комнаты", mode="Режим комнаты")
@app_commands.choices(mode=[app_commands.Choice(name="Рулетка", value="roulette")])
async def create_room(interaction: discord.Interaction, name: str, mode: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    current_room = config.get("game_room", {})
    if current_room.get("thread_id"):
        await interaction.followup.send("Игровая комната уже создана. Закройте её перед созданием новой.", ephemeral=True)
        return

    try:
        base_channel = await bot.fetch_channel(config['channel_id'])

        thread = await base_channel.create_thread(
            name=name,
            type=discord.ChannelType.private_thread,
            invitable=False
        )

        await thread.add_user(interaction.user)

        main_embed = discord.Embed(
            title=f"Рулетка (владелец: {interaction.user})",
            description=(
                "Игра скоро начнётся!\n\n"
                "• Чтобы пригласить других участников, используйте пинг с их юзернеймом.\n"
                "• Чтобы сделать ставку — используйте кнопку **Сделать ставку** ниже в списке участников.\n"
                "• Каждый участник может сделать ставку только один раз.\n\n"
            ),
            color=0xffc86e
        )

        participants_embed = discord.Embed(
            title="Участники",
            description="Пока нет участников.",
            color=0xffc86e
        )

        main_msg = await thread.send(embed=main_embed, view=MainView(thread, interaction.user.id))
        participants_msg = await thread.send(embed=participants_embed, view=BetView(thread))
        await main_msg.pin()
        await participants_msg.pin()

        config["game_room"] = {
            "thread_id": thread.id,
            "participants": {},
            "mode": mode.value,
            "participants_msg_id": participants_msg.id
        }
        save_config(config)

        await interaction.followup.send(f"Комната {thread.mention} создана.", ephemeral=True)

    except Exception as e:
        logging.error(f"Ошибка при создании комнаты: {e}")
        await interaction.followup.send("Произошла ошибка при создании комнаты.", ephemeral=True)

class BetModal(Modal, title="Сделать ставку"):
    bet_input = TextInput(label="Введите количество баллов", placeholder="Только число", required=True)

    def __init__(self, user, thread):
        super().__init__()
        self.user = user
        self.thread = thread

    async def on_submit(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            bet_value = int(self.bet_input.value)
            if bet_value <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send("Введите корректное положительное число.", ephemeral=True)
            return

        try:
            sh = gc.open_by_key(config['leaderboard_sheet_id'])
            ws = sh.worksheet('Gambling')
            rows = ws.get_all_values()

            user_nick = str(self.user.name)
            user_row_idx = None
            user_score = 0
            for i, row in enumerate(rows):
                if row and row[0].strip() == user_nick:
                    user_row_idx = i + 1
                    user_score = int(row[1]) if len(row) > 1 and row[1].isdigit() else 0
                    break

            if user_row_idx is None:
                await interaction.followup.send("Вас нет в таблице казино.", ephemeral=True)
                return

            game = config.get("game_room", {})
            participants = game.get("participants", {})

            user_id_str = str(self.user.id)
            current_bet = participants.get(user_id_str, {}).get("bet", 0)
            new_bet = current_bet + bet_value

            if bet_value > user_score:
                await interaction.followup.send(
                    f"Недостаточно баллов ({user_score}). Сейчас у вас поставлено {current_bet} баллов.",
                    ephemeral=True
                )
                return

            participants[user_id_str] = {
                "nick": user_nick,
                "bet": new_bet
            }

            game["participants"] = participants
            config["game_room"] = game
            save_config(config)

            try:
                ws.update_cell(user_row_idx, 2, str(user_score - bet_value))
                logging.info("Баллы обновлены в Google Sheets.")
            except Exception as e:
                logging.error(f"Ошибка при обновлении баллов: {e}")

            participants_msg_id = game.get("participants_msg_id")
            if participants_msg_id is None:
                await interaction.followup.send("Ошибка: сообщение со списком участников не найдено.", ephemeral=True)
                return

            participants_msg = await self.thread.fetch_message(participants_msg_id)

            if participants:
                desc = "\n".join(f"- **{data['nick']}** — {data['bet']} баллов" for data in participants.values())
            else:
                desc = "Пока нет участников."

            participants_embed = discord.Embed(
                title="Участники",
                description=desc,
                color=0xffc86e
            )

            await participants_msg.edit(embed=participants_embed, view=BetView(self.thread))

            await interaction.followup.send(f"Ставка {bet_value} баллов принята! Итоговая ставка: {new_bet}.", ephemeral=True)

        except Exception as e:
            logging.error(f"Ошибка ставки: {e}")
            await interaction.followup.send("Произошла ошибка. Попробуйте позже.", ephemeral=True)

class MainView(View):
    def __init__(self, thread, owner_id):
        super().__init__(timeout=None)
        self.thread = thread
        self.owner_id = owner_id

    def draw_wheel(self, sectors, rotation_deg, size=600):
        img = Image.new("RGBA", (size, size + 40), (255, 255, 255, 0))
        draw = ImageDraw.Draw(img)
        center = (size // 2, (size + 40) // 2 + 20)
        radius = size // 2 - 20  # увеличен радиус круга ограничения

        try:
            font = ImageFont.truetype("arial.ttf", 20)  # увеличен размер шрифта
        except:
            font = ImageFont.load_default()

        bbox = [center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius]

        for sector in sectors:
            start = sector['start'] + rotation_deg
            end = sector['end'] + rotation_deg
            draw.pieslice(bbox, start, end, fill=sector['color'], outline=None)

        draw.ellipse(bbox, outline="black", width=10)

        for sector in sectors:
            mid_angle_deg = (sector['start'] + sector['end']) / 2 + rotation_deg
            mid_angle_rad = math.radians(mid_angle_deg)
            text_radius = radius * 0.7
            text_x = center[0] + text_radius * math.cos(mid_angle_rad)
            text_y = center[1] + text_radius * math.sin(mid_angle_rad)

            nick = sector['nick']
            if len(nick) > 14:
                nick = nick[:12] + "…"

            text_img = Image.new("RGBA", (200, 40), (255, 255, 255, 0))
            text_draw = ImageDraw.Draw(text_img)
            text_draw.text((0, 0), nick, font=font, fill=sector['text_color'])

            rotated = text_img.rotate(-mid_angle_deg, resample=Image.BICUBIC, expand=1)
            tw, th = rotated.size
            img.paste(rotated, (int(text_x - tw / 2), int(text_y - th / 2)), rotated)

        arrow_w = 30
        arrow_h = 25
        arrow_tip = (center[0], center[1] - radius - 10)
        arrow_left = (center[0] - arrow_w // 2, arrow_tip[1] - arrow_h)
        arrow_right = (center[0] + arrow_w // 2, arrow_tip[1] - arrow_h)
        draw.polygon([arrow_tip, arrow_left, arrow_right], fill="red", outline="black")

        return img

    def generate_wheel_gif(self, sectors, stop_at_angle, duration_sec=6, fps=30, pause_frames=20):
        frames = []
        total_frames = duration_sec * fps
        full_rotations = 5
        offset = random.uniform(-5, 5)  # микро-смещение центра сектора
        final_angle = 360 * full_rotations + (270 - stop_at_angle + offset) % 360

        for i in range(total_frames):
            eased_rotation = ease_out_quad(i, 0, final_angle, total_frames)
            frame = self.draw_wheel(sectors, -eased_rotation % 360)
            frames.append(frame)

        final_frame = frames[-1]
        for _ in range(pause_frames):
            frames.append(final_frame.copy())

        buffer = io.BytesIO()
        frames[0].save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=1000 // fps,
            loop=0
        )
        buffer.seek(0)
        return buffer

    @discord.ui.button(label="Начать игру", style=discord.ButtonStyle.success)
    async def start_game_button(self, interaction: Interaction, button: Button):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != self.owner_id:
            await interaction.followup.send("Начать игру может только владелец комнаты.", ephemeral=True)
            return

        game = config.get("game_room", {})
        participants = game.get("participants", {})

        count = len(participants)
        if count < 2:
            await interaction.followup.send("Недостаточно участников для начала игры (минимум 2).", ephemeral=True)
            return
        if count > 10:
            await interaction.followup.send("Слишком много участников для этой игры (максимум 10).", ephemeral=True)
            return

        total_bet = sum(p['bet'] for p in participants.values())
        sectors = []
        start_angle = 0
        for data in participants.values():
            weight = data['bet'] / total_bet
            angle = weight * 360
            color = get_random_color()
            text_color = get_text_color_from_background(color)
            sectors.append({
                "nick": data["nick"],
                "start": start_angle,
                "end": start_angle + angle,
                "color": color,
                "text_color": text_color
            })
            start_angle += angle

        # Выбираем победителя с весом по углу сектора
        weights = [s['end'] - s['start'] for s in sectors]
        winner_sector = random.choices(sectors, weights=weights)[0]
        winner_nick = winner_sector['nick']
        sector_center = (winner_sector['start'] + winner_sector['end']) / 2  # Центр сектора

        # Генерируем gif с учетом, что стрелка смотрит вверх (270 градусов - центр сектора)
        gif_buffer = self.generate_wheel_gif(sectors, sector_center)
        gif_file = discord.File(fp=gif_buffer, filename="roulette.gif")
        await self.thread.send(file=gif_file)
        await asyncio.sleep(3)

        try:
            sh = gc.open_by_key(config['leaderboard_sheet_id'])
            ws = sh.worksheet('Gambling')
            rows = ws.get_all_values()

            winner_row_idx = None
            for i, row in enumerate(rows):
                if row and row[0].strip() == winner_nick:
                    winner_row_idx = i + 1
                    break

            if winner_row_idx is None:
                await interaction.followup.send(f"Победитель {winner_nick} не найден в таблице 'Gambling'.", ephemeral=True)
                return

            current_score = int(rows[winner_row_idx - 1][1]) if len(rows[winner_row_idx - 1]) > 1 and rows[winner_row_idx - 1][1].isdigit() else 0
            new_score = current_score + total_bet
            ws.update_cell(winner_row_idx, 2, str(new_score))

            await self.thread.send(
                f"🎉 Игра завершена! Победитель: **{winner_nick}**\n"
                f"Он получает сумму всех ставок — **{total_bet}** баллов!"
            )

            game["participants"] = {}
            config["game_room"] = game
            save_config(config)

            participants_msg_id = game.get("participants_msg_id")
            if participants_msg_id:
                participants_msg = await self.thread.fetch_message(participants_msg_id)
                empty_embed = discord.Embed(title="Участники", description="Пока нет участников.", color=0xffc86e)
                await participants_msg.edit(embed=empty_embed, view=BetView(self.thread))

            await interaction.followup.send("Игра успешно завершена.", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"Ошибка при завершении игры: {e}", ephemeral=True)
            logging.error(f"Ошибка при завершении игры: {e}")

    @discord.ui.button(label="Закрыть комнату", style=discord.ButtonStyle.danger)
    async def close_thread_button(self, interaction: Interaction, button: Button):
        if interaction.user.id != self.owner_id:
            await interaction.followup.send("Закрыть ветку может только владелец комнаты.", ephemeral=True)
            return

        try:
            await self.thread.delete()
        except Exception as e:
            await interaction.response.send_message(f"Не удалось удалить ветку: {e}", ephemeral=True)
            return

        config["game_room"] = {}
        save_config(config)

class BetView(View):
    def __init__(self, thread):
        super().__init__(timeout=None)
        self.thread = thread

    @discord.ui.button(label="Сделать ставку", style=discord.ButtonStyle.primary)
    async def bet_button(self, interaction: Interaction, button: Button):
        modal = BetModal(interaction.user, self.thread)
        await interaction.response.send_modal(modal)

@bot.tree.command(name="points-manager", description="Управление баллами пользователей", guild=discord.Object(id=config['guild_id']))
@app_commands.describe(mode="Режим работы", amount="Число баллов", recipient="Кому передаёте баллы (для передачи)")
@app_commands.choices(mode=[
    app_commands.Choice(name="Посмотреть баланс", value="balance"),
    app_commands.Choice(name="Конвентировать баллы", value="convert"),
    app_commands.Choice(name="Передать баллы", value="transfer")
])
async def points_manager(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    amount: Optional[int] = None,
    recipient: Optional[discord.User] = None
):
    await interaction.response.defer(ephemeral=True)

    try:
        user_nick = str(interaction.user.name)
        sh = gc.open_by_key(config['leaderboard_sheet_id'])

        if mode.value == "balance":
            ws = sh.worksheet("Gambling")
            rows = ws.get_all_values()
            for row in rows:
                if row and row[0].strip() == user_nick:
                    score = int(row[1]) if len(row) > 1 and row[1].isdigit() else 0
                    await interaction.followup.send(f"Ваш текущий баланс: **{score}** игровых баллов.", ephemeral=True)
                    return
            await interaction.followup.send("Вы не найдены в таблице 'Gambling'.", ephemeral=True)

        elif mode.value == "convert":
            if amount is None or amount <= 0:
                await interaction.followup.send("Укажите корректное количество баллов для конверсии.", ephemeral=True)
                return

            ws_writer = sh.worksheet("Райтер месяца")
            rows_writer = ws_writer.get_all_values()
            row_idx = None
            current_points = 0
            for i, row in enumerate(rows_writer):
                if row and row[0].strip() == user_nick:
                    row_idx = i + 1
                    current_points = int(row[1]) if len(row) > 1 and row[1].isdigit() else 0
                    break

            if row_idx is None:
                await interaction.followup.send("Вы не найдены в таблице 'Райтер месяца'.", ephemeral=True)
                return
            if current_points < amount:
                await interaction.followup.send(f"Недостаточно очков. У вас: {current_points}.", ephemeral=True)
                return

            new_writer_points = current_points - amount
            game_points = amount * 1000

            ws_writer.update_cell(row_idx, 2, str(new_writer_points))

            ws_gamble = sh.worksheet("Gambling")
            rows_gamble = ws_gamble.get_all_values()
            gamble_row_idx = None
            gamble_points = 0
            for i, row in enumerate(rows_gamble):
                if row and row[0].strip() == user_nick:
                    gamble_row_idx = i + 1
                    gamble_points = int(row[1]) if len(row) > 1 and row[1].isdigit() else 0
                    break

            if gamble_row_idx:
                ws_gamble.update_cell(gamble_row_idx, 2, str(gamble_points + game_points))
            else:
                ws_gamble.append_row([user_nick, str(game_points)])

            await interaction.followup.send(
                f"✅ Конвертировано {amount} баллов в **{game_points}** игровых баллов.",
                ephemeral=True
            )

        elif mode.value == "transfer":
            if amount is None or amount <= 0 or recipient is None:
                await interaction.followup.send("Укажите пользователя и корректное количество баллов.", ephemeral=True)
                return

            recipient_nick = str(recipient.name)

            ws = sh.worksheet("Gambling")
            rows = ws.get_all_values()
            sender_idx = None
            recipient_idx = None
            sender_score = 0
            recipient_score = 0

            for i, row in enumerate(rows):
                if row:
                    if row[0].strip() == user_nick:
                        sender_idx = i + 1
                        sender_score = int(row[1]) if len(row) > 1 and row[1].isdigit() else 0
                    elif row[0].strip() == recipient_nick:
                        recipient_idx = i + 1
                        recipient_score = int(row[1]) if len(row) > 1 and row[1].isdigit() else 0

            if sender_idx is None or sender_score < amount:
                await interaction.followup.send("Недостаточно баллов для перевода.", ephemeral=True)
                return

            ws.update_cell(sender_idx, 2, str(sender_score - amount))
            if recipient_idx:
                ws.update_cell(recipient_idx, 2, str(recipient_score + amount))
            else:
                await interaction.followup.send(f"Пользователь {recipient.mention} не найден. Передача не выполнена.", ephemeral=True)
                return

            await interaction.followup.send(f"💸 Переведено {amount} баллов пользователю {recipient.mention}.", ephemeral=True)

    except Exception as e:
        logging.error(f"Ошибка в points_manager: {e}")
        await interaction.followup.send("Произошла ошибка. Попробуйте позже.", ephemeral=True)

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

@tasks.loop(minutes=60)
async def update_leaderboard_task():
    logging.info("Автообновление лидерборда")
    await send_leaderboard()

@tasks.loop(hours=24)
async def monthly_event_task():
    now = datetime.now()
    if now.day == 1:
        await run_monthly_event()

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
        await message.reply(get_random_unknown_reply())
        return

    response = await query_openrouter(query)

    if response:
        await message.reply(sanitize_mentions(response, message.guild))
        return
    else:
        logging.info("AI не сработал, fallback на ключи.")

    matched_flags = set()
    words = re.findall(r"\b\w+\b", query)

    has_L = any(kw.lower() in words for kw in config.get("flags", {}).get("L", []))
    has_M = any(kw.lower() in words for kw in config.get("flags", {}).get("M", []))

    if has_L and has_M:
        cleaned = re.sub(rf"<@!?{bot.user.id}>", "", message.content, flags=re.IGNORECASE).strip()

        trigger_words = config["flags"].get("M", [])
        trigger_regex = r"\b(" + "|".join(re.escape(w) for w in trigger_words) + r")\b"
        match = re.search(trigger_regex, cleaned, flags=re.IGNORECASE)

        if match:
            trigger_end = match.end()
            trimmed = cleaned[trigger_end:].strip()
            parts = [part.strip() for part in trimmed.split("или")]

            if len(parts) >= 2:
                choice = random.choice(parts)
                await message.reply(sanitize_mentions(choice, message.guild))
            else:
                await message.reply("Не удалось распознать два варианта выбора.")
        else:
            await message.reply("Не найдено ключевое слово.")
        return
    elif has_L:
        parts = [part.strip() for part in query.split("или")]
        if len(parts) >= 2:
            choice = random.choice(["Первый вариант", "Второй вариант"])
            await message.reply(sanitize_mentions(choice, message.guild))
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
                await message.reply(sanitize_mentions(after, message.guild))
            elif before and not after:
                await message.reply(sanitize_mentions(before, message.guild))
            elif before and after:
                await message.reply(sanitize_mentions(after, message.guild))
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
        await message.reply(get_random_unknown_reply())
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
        await message.reply(get_random_unknown_reply())

def sanitize_mentions(text: str, guild: Optional[discord.Guild]) -> str:
    if guild:
        role_pattern = re.compile(r"<@&(\d+)>")
        for match in role_pattern.finditer(text):
            role_id = int(match.group(1))
            role = guild.get_role(role_id)
            if role:
                role_name = f"`@{role.name}`"
                text = text.replace(match.group(0), role_name)
            else:
                text = text.replace(match.group(0), "`@роль`")
    else:
        text = re.sub(r"<@&\d+>", "`@роль`", text)
    text = re.sub(r"@everyone", "@​everyone", text, flags=re.IGNORECASE)
    text = re.sub(r"@here", "@​here", text, flags=re.IGNORECASE)
    return text

def get_random_unknown_reply():
    replies = [
        # Очень частое — повышено с 600 → 700
        ("Я вас не понимаю.", 700),

        # Пореже — повышено с 200 → 250
        ("Я вас не понимаю... <:AllCool:1382668950545891410>", 250),

        # Редкие
        ("Я вас не понимаю... Простите... <:AllCool:1382668950545891410>", 80),  # повышено с 40
        ("Что", 30),
        ("Да", 30),
        ("Нет", 30),

        # Гипер редкие
        ("<:AllCool:1382668950545891410>", 7),
        ("<:please:1382668723638243360>", 7),
        (":clown:", 6),
    ]

    population, weights = zip(*replies)
    return random.choices(population, weights=weights, k=1)[0]

@bot.event
async def on_ready():
    logging.info(f"Бот подключён как {bot.user}")
    synced = await bot.tree.sync(guild=discord.Object(id=config['guild_id']))
    logging.info(f"Синхронизировано {len(synced)} команд.")

    if config.get("is_updating") and not update_task_message.is_running():
        update_task_message.start()
        logging.info("Автообновление задач запущено при старте бота.")

    if config.get('is_lb_updating') and not update_leaderboard_task.is_running():
        update_leaderboard_task.start()
        logging.info("Автообновление лидерборда запущено при старте.")

    if config.get("monthly_event_enabled") and not monthly_event_task.is_running():
        monthly_event_task.start()
        logging.info("Запущено автообновление Райтера месяца.")

    if not log_file_maintenance.is_running():
        log_file_maintenance.start()
        logging.info("Запущено периодическое обслуживание лога.")

try:
    bot.run(os.getenv("BOT_TOKEN"))
except Exception as e:
    logging.critical(f"Не удалось запустить бота: {e}")
