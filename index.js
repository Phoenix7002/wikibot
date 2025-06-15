require('dotenv').config();

const fs = require('fs');
const winston = require('winston');

const logger = winston.createLogger({
    level: 'info',
    format: winston.format.combine(
        winston.format.timestamp({ format: 'YYYY-MM-DD HH:mm:ss' }),
        winston.format.printf(({ timestamp, level, message }) => {
            return `${timestamp} - ${level.toUpperCase()} - ${message}`;
        })
    ),
    transports: [
        new winston.transports.Console(),
        new winston.transports.File({ filename: 'bot_log.txt' })
    ]
});

const CONFIG_FILE = './bot_config.json';

function loadConfig() {
    if (fs.existsSync(CONFIG_FILE)) {
        return JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf-8'));
    } else {
        throw new Error('Файл конфигурации не найден.');
    }
}

function saveConfig(newData) {
    fs.writeFileSync(CONFIG_FILE, JSON.stringify(newData, null, 2), 'utf-8');
}

const config = loadConfig();

const { Client, GatewayIntentBits, Partials } = require('discord.js');

const client = new Client({
    intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent
    ],
    partials: [Partials.Channel]
});

let freeColumnTasks = [];

const axios = require('axios');

async function getTasksFromYougile(columnId) {
    const url = 'https://ru.yougile.com/api-v2/task-list';
    const headers = {
        Authorization: `Bearer ${process.env.YOUGILE_API_TOKEN}`,
        'Content-Type': 'application/json'
    };
    const params = { columnId };

    try {
        logger.info(`Запрос задач из колонки: ${columnId}`);
        const response = await axios.get(url, { headers, params });

        if (!response.data) {
            logger.warn(`Пустой ответ от YouGile для колонки ${columnId}`);
            return [];
        }

        if (response.status !== 200) {
            logger.warn(`Неожиданный статус ответа: ${response.status} для колонки ${columnId}`);
            return [];
        }

        return response.data.content || [];
    } catch (e) {
        if (e.response) {
            logger.error(`Ошибка API YouGile: ${e.response.status} - ${e.response.data}`);
        } else {
            logger.error(`Ошибка при запросе к YouGile: ${e.message}`);
        }
        return [];
    }
}

function formatTasksForMessage(tasks, columnName) {
    if (!tasks || tasks.length === 0) {
        return 'Задач нет.\n';
    }

    const lines = tasks.map(task => {
        let line = `- ${task.title}`;
        if (
            ['В процессе выполнения', 'Проверяются и дорабатываются'].includes(columnName)
        ) {
            const stickers = task.stickers;
            if (stickers && typeof stickers === 'object') {
                const firstStickerName = Object.values(stickers)[0];
                if (firstStickerName) {
                    line += ` — **${firstStickerName}**`;
                }
            }
        }
        return line;
    });

    return lines.join('\n');
}

const dayjs = require('dayjs');

async function sendTaskMessage() {
    if (!config.channel_id) {
        logger.error('ID канала не указан в конфиге');
        return;
    }

    let channel;
    try {
        channel = await client.channels.fetch(config.channel_id);
        if (!channel) {
            throw new Error('Канал не найден');
        }
    } catch (e) {
        logger.error(`Ошибка получения канала: ${e.message}`);
        return;
    }

    const tasksText = [];

    for (const [columnName, columnId] of Object.entries(config.column_ids)) {
        const columnTasks = await getTasksFromYougile(columnId);

        if (columnName === 'Свободные') {
            freeColumnTasks = columnTasks;
        }

        const formatted = formatTasksForMessage(columnTasks, columnName);
        tasksText.push(`## ${columnName}\n${formatted}`);
    }

    const now = dayjs().format('YYYY-MM-DD HH:mm');
    const message = tasksText.join('\n') + `\n\n-# ${now}`;

    try {
        if (config.message_id) {
            const oldMsg = await channel.messages.fetch(config.message_id);
            if (oldMsg && oldMsg.author.id === client.user.id) {
                await oldMsg.edit(message);
                logger.info('Сообщение с задачами обновлено.');
                return;
            }
        }
    } catch (e) {
        logger.warn(`Не удалось редактировать старое сообщение: ${e}`);
    }

    try {
        const sentMessage = await channel.send(message);
        config.message_id = sentMessage.id;

        if (config.auto_pin) {
            try {
                await sentMessage.pin();
                logger.info('Сообщение закреплено.');
            } catch (e) {
                logger.warn('Не удалось закрепить сообщение — недостаточно прав.');
            }
        }

        saveConfig(config);
    } catch (e) {
        logger.error(`Ошибка при отправке сообщения: ${e}`);
    }
}

const { REST, Routes, SlashCommandBuilder } = require('discord.js');

const commands = [
    new SlashCommandBuilder().setName('send-list').setDescription('Отправить список задач'),
    new SlashCommandBuilder().setName('start-update').setDescription('Запустить автоматическое обновление списка'),
    new SlashCommandBuilder().setName('stop-update').setDescription('Остановить автоматическое обновление списка'),
    new SlashCommandBuilder().setName('update-list').setDescription('Обновить задачи вручную'),
    new SlashCommandBuilder().setName('text-train').setDescription('Отправить обучающий инструктаж'),
    new SlashCommandBuilder()
        .setName('task-desc')
        .setDescription('Показать описание задачи по имени')
        .addStringOption(option =>
            option.setName('task_name')
                .setDescription('Имя задачи')
                .setRequired(true)
        ),
    new SlashCommandBuilder().setName('auto-pin').setDescription('Включить/выключить автозакреп сообщений')
].map(cmd => cmd.toJSON());

const rest = new REST({ version: '10' }).setToken(process.env.BOT_TOKEN);

let updateTaskMessageInterval;

const updateTaskMessageLoop = {
    start() {
        updateTaskMessageInterval = setInterval(async () => {
            try {
                logger.info('Автообновление задач');
                await sendTaskMessage();
            } catch (e) {
                logger.error('Ошибка при автообновлении задач:', e);
            }
        }, 60 * 60 * 1000);
    },
    stop() {
        clearInterval(updateTaskMessageInterval);
    }
};

client.on('ready', async () => {
    logger.info(`Бот подключён как ${client.user.tag}`);
    
    if (!config.guild_id) {
        logger.error('В конфиге отсутствует guild_id (ID сервера)');
        return;
    }

    const guild = client.guilds.cache.get(config.guild_id);
    if (!guild) {
        logger.error(`Бот не находится на сервере с ID ${config.guild_id}`);
        logger.error('Добавьте бота на сервер и проверьте правильность guild_id в конфиге');
        return;
    }

    try {
        const me = await guild.members.fetch(client.user.id);
        const { PermissionsBitField } = require('discord.js');

        if (!me.permissions.has(PermissionsBitField.Flags.UseApplicationCommands)) {
            logger.error('У бота нет прав на использование команд (USE_APPLICATION_COMMANDS)');
            logger.error('Дайте боту права администратора или включите "USE_APPLICATION_COMMANDS"');
        }

        const data = await rest.put(
            Routes.applicationGuildCommands(client.user.id, config.guild_id),
            { body: commands }
        );
        
        logger.info(`Успешно зарегистрировано ${data.length} команд на сервере ${guild.name}`);
        
        if (config.is_updating) {
            updateTaskMessageLoop.start();
            logger.info('Автообновление задач запущено');
        }
    } catch (error) {
        logger.error('Ошибка регистрации команд:', error);
    }
});

client.on('interactionCreate', async interaction => {
    if (!interaction.isChatInputCommand()) return;

    try {
        const { commandName } = interaction;

        if (commandName === 'send-list') {
            await interaction.reply({ content: 'Отправка задач...', ephemeral: true });
            await sendTaskMessage();
            logger.info(`Задачи отправлены пользователем ${interaction.user.tag}`);
        }

        else if (commandName === 'start-update') {
            if (config.is_updating) {
                return interaction.reply({ content: 'Цикл уже запущен.', ephemeral: true });
            }
            updateTaskMessageLoop.start();
            config.is_updating = true;
            saveConfig(config);
            await interaction.reply({ content: 'Цикл обновления запускается...', ephemeral: true });
            logger.info('Автообновление запущено');
        }

        else if (commandName === 'stop-update') {
            updateTaskMessageLoop.stop();
            config.is_updating = false;
            saveConfig(config);
            await interaction.reply({ content: 'Цикл обновления остановлен.', ephemeral: true });
            logger.info('Автообновление остановлено');
        }

        else if (commandName === 'update-list') {
            await interaction.reply({ content: 'Обновление задач...', ephemeral: true });
            await sendTaskMessage();
            logger.info(`Задачи обновлены пользователем ${interaction.user.tag}`);
        }

        else if (commandName === 'text-train') {
            await interaction.reply({ content: 'Отправка инструктажа...', ephemeral: true });
            const channel = await client.channels.fetch(config.channel_id);
            if (!channel) {
                logger.error("Канал для инструктажа не найден");
                return;
            }
            const trainingTexts = config.training_texts || [];
            if (!trainingTexts.length) {
                logger.error("В конфиге нет текстов инструктажа");
                return;
            }
            for (const part of trainingTexts) {
                try {
                    await channel.send(part);
                    await new Promise(resolve => setTimeout(resolve, 1000));
                } catch (e) {
                    logger.error(`Ошибка при отправке инструктажа: ${e}`);
                }
            }
            logger.info(`Инструктаж отправлен пользователем ${interaction.user.tag}`);
        }

        else if (commandName === 'task-desc') {
            await interaction.deferReply({ ephemeral: true });
            const taskName = interaction.options.getString('task_name');

            let allTasks = [];
            for (const columnId of Object.values(config.column_ids)) {
                const tasks = await getTasksFromYougile(columnId);
                allTasks = allTasks.concat(tasks);
            }

            const matchedTask = allTasks.find(task => task.title.toLowerCase() === taskName.toLowerCase());

            if (!matchedTask) {
                return interaction.editReply('Задача не найдена во всех колонках.');
            }

            const rawDesc = matchedTask.description || 'Нет описания.';
            const formattedDesc = htmlToDiscord(rawDesc);

            await interaction.editReply({
                embeds: [{
                    title: matchedTask.title,
                    description: formattedDesc,
                    color: 0xffc86e,
                }]
            });
        }

        else if (commandName === 'auto-pin') {
            config.auto_pin = !config.auto_pin;
            saveConfig(config);
            const status = config.auto_pin ? 'включен' : 'выключен';
            await interaction.reply({ content: `Автозакреп теперь ${status}.`, ephemeral: true });
        }
    } catch (error) {
        logger.error(`Ошибка обработки команды: ${error}`);
        if (interaction.deferred || interaction.replied) {
            await interaction.followUp({ content: 'Произошла ошибка при выполнении команды', ephemeral: true });
        } else {
            await interaction.reply({ content: 'Произошла ошибка при выполнении команды', ephemeral: true });
        }
    }
});

function htmlToDiscord(text) {
    const replacements = [
        [/<strong>/g, '**'], [/<\/strong>/g, '**'],
        [/<em>/g, '*'], [/<\/em>/g, '*'],
        [/<p>/g, ''], [/<\/p>/g, '\n\n'],
        [/<br>/g, '\n'],
        [/&nbsp;/g, ' '],
    ];
    replacements.forEach(([oldRegex, newStr]) => {
        text = text.replace(oldRegex, newStr);
    });

    text = text.replace(/<ul>|<ol>/g, '');
    text = text.replace(/<\/ul>|<\/ol>/g, '');
    text = text.replace(/<li>/g, '- ');
    text = text.replace(/<\/li>/g, '\n');
    text = text.replace(/<.*?>/g, '');

    return text.trim();
}

client.on('messageCreate', async message => {
    if (message.author.bot) return;
    if (!message.mentions.has(client.user)) return;

    let query = message.content.replace(new RegExp(`<@!?${client.user.id}>`, 'g'), '').trim().toLowerCase();

    if (!query) {
        await message.channel.send('Я вас не понимаю');
        return;
    }

    const matchedFlags = new Set();
    const words = query.match(/\b\w+\b/g) || [];

    for (const [flag, keywords] of Object.entries(config.flags || {})) {
        for (const kw of keywords) {
            if (words.includes(kw.toLowerCase())) {
                matchedFlags.add(flag);
                break;
            }
        }
    }

    if (matchedFlags.size === 0) {
        await message.channel.send('Я вас не понимаю');
        return;
    }

    let responseKey = null;
    const possibleKeys = config.responses || {};
    const sortedKeys = Object.keys(possibleKeys).sort((a, b) => b.split('+').length - a.split('+').length);

    for (const key of sortedKeys) {
        const parts = new Set(key.split('+'));
        if ([...parts].every(p => matchedFlags.has(p))) {
            responseKey = key;
            break;
        }
    }

    if (responseKey) {
        await message.channel.send(config.responses[responseKey]);
    } else {
        await message.channel.send('Я вас не понимаю');
    }
});

client.login(process.env.BOT_TOKEN);