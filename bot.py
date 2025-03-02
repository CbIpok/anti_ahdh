import telebot
from telebot import types
import sqlite3
import time
import os
import threading
import logging
import matplotlib.pyplot as plt
import config

# Настройка логирования: все события записываются в log.txt
logging.basicConfig(
    level=logging.INFO,
    filename='log.txt',
    filemode='a',
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация бота
bot = telebot.TeleBot(config.TOKEN)

# Глобальные словари для хранения состояния
main_messages = {}        # chat_id -> message_id основного сообщения
timer_threads = {}        # chat_id -> запущенный поток таймера
timer_stop_flags = {}     # chat_id -> threading.Event для остановки потока
user_states = {}          # chat_id -> dict с состоянием ввода (например, "adding_category", "adding_task")

# ==========================
# Работа с базой данных (SQLite)
# ==========================
DB_PATH = "tasks.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER,
            name TEXT NOT NULL,
            total_time INTEGER DEFAULT 0,
            FOREIGN KEY(category_id) REFERENCES categories(id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS current_task (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            task_id INTEGER,
            start_time INTEGER,
            saved_time INTEGER DEFAULT 0,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def get_db_connection():
    return sqlite3.connect(DB_PATH)

# ==========================
# Функции формирования клавиатур
# ==========================

def get_main_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn1 = types.InlineKeyboardButton("Категории", callback_data="menu_categories")
    btn2 = types.InlineKeyboardButton("Текущая задача", callback_data="menu_current_task")
    btn3 = types.InlineKeyboardButton("Статистика", callback_data="menu_statistics")
    markup.row(btn1, btn2)
    markup.row(btn3)
    return markup

def get_back_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn = types.InlineKeyboardButton("Назад", callback_data="back_main")
    markup.add(btn)
    return markup

# ==========================
# Функции отправки/редактирования сообщений
# ==========================

def send_main_menu(chat_id):
    text = "Главное меню"
    keyboard = get_main_keyboard()
    if chat_id in main_messages:
        try:
            bot.edit_message_text(text, chat_id, main_messages[chat_id], reply_markup=keyboard)
        except Exception as e:
            logger.exception("Error editing main menu: %s", e)
    else:
        msg = bot.send_message(chat_id, text, reply_markup=keyboard)
        main_messages[chat_id] = msg.message_id
    logger.info("Sent main menu to chat_id %s", chat_id)

def send_text(chat_id, text, reply_markup=None):
    if chat_id in main_messages:
        try:
            bot.edit_message_text(text, chat_id, main_messages[chat_id], reply_markup=reply_markup)
        except Exception as e:
            logger.exception("Error editing message: %s", e)
    else:
        msg = bot.send_message(chat_id, text, reply_markup=reply_markup)
        main_messages[chat_id] = msg.message_id

def generate_chart(data, filename="chart.png"):
    try:
        labels = list(data.keys())
        values = list(data.values())
        plt.figure()
        plt.bar(labels, values)
        plt.title("Статистика по времени")
        plt.savefig(filename)
        plt.close()
        logger.info("Chart generated: %s", filename)
        return filename
    except Exception as e:
        logger.exception("Error generating chart: %s", e)
        return None

# ==========================
# Таймер текущей задачи (работает в отдельном потоке)
# ==========================

def timer_thread(chat_id, task_id, stop_event):
    while not stop_event.is_set():
        time.sleep(10)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT start_time, saved_time, task_id FROM current_task WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        if row:
            start_time, saved_time, current_task_id = row
            # Если задача изменилась, завершаем поток
            if current_task_id != task_id:
                conn.close()
                break
            elapsed = int(time.time()) - start_time
            total = saved_time + elapsed
            text = f"Текущая задача (ID: {task_id})\nВремя выполнения: {total} сек."
            try:
                bot.edit_message_text(text, chat_id, main_messages.get(chat_id, 0))
            except Exception as e:
                logger.exception("Error editing timer message: %s", e)
        conn.close()
    logger.info("Timer thread for chat_id %s ended", chat_id)

def start_timer(chat_id, task_id):
    stop_timer(chat_id)  # остановим предыдущий таймер, если он есть
    conn = get_db_connection()
    cursor = conn.cursor()
    now = int(time.time())
    cursor.execute("SELECT id FROM current_task WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row:
        cursor.execute("UPDATE current_task SET task_id = ?, start_time = ?, saved_time = 0 WHERE chat_id = ?",
                       (task_id, now, chat_id))
    else:
        cursor.execute("INSERT INTO current_task (chat_id, task_id, start_time, saved_time) VALUES (?, ?, ?, 0)",
                       (chat_id, task_id, now))
    conn.commit()
    conn.close()
    stop_event = threading.Event()
    thread = threading.Thread(target=timer_thread, args=(chat_id, task_id, stop_event))
    thread.daemon = True
    timer_threads[chat_id] = thread
    timer_stop_flags[chat_id] = stop_event
    thread.start()
    logger.info("Started timer for chat_id %s, task_id %s", chat_id, task_id)

def stop_timer(chat_id):
    if chat_id in timer_stop_flags:
        timer_stop_flags[chat_id].set()
        del timer_stop_flags[chat_id]
    if chat_id in timer_threads:
        timer_threads[chat_id].join(timeout=1)
        del timer_threads[chat_id]
    # Завершаем запись текущей задачи: сохраняем время в tasks и удаляем запись из current_task
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT start_time, saved_time, task_id FROM current_task WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row:
        start_time, saved_time, task_id = row
        elapsed = int(time.time()) - start_time
        total = saved_time + elapsed
        cursor.execute("UPDATE tasks SET total_time = total_time + ? WHERE id = ?", (total, task_id))
        cursor.execute("DELETE FROM current_task WHERE chat_id = ?", (chat_id,))
        conn.commit()
    conn.close()
    logger.info("Stopped timer for chat_id %s", chat_id)

# ==========================
# Обработчики команд и callback'ов
# ==========================

# Обработчик команды /start
@bot.message_handler(commands=['start'])
def handle_start(message):
    chat_id = message.chat.id
    send_main_menu(chat_id)
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception as e:
        logger.exception("Error deleting /start message: %s", e)

# Обработчик callback для главного меню (начинается с "menu_")
@bot.callback_query_handler(func=lambda call: call.data.startswith("menu_"))
def handle_menu(call):
    chat_id = call.message.chat.id
    data = call.data
    if data == "menu_categories":
        show_categories(chat_id)
    elif data == "menu_current_task":
        show_current_task(chat_id)
    elif data == "menu_statistics":
        show_statistics(chat_id)
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.exception("Error answering callback: %s", e)

def show_categories(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM categories")
    rows = cursor.fetchall()
    conn.close()
    text = "Категории:\n"
    # Формируем клавиатуру напрямую через markup.add()
    markup = types.InlineKeyboardMarkup()
    for row in rows:
        cat_id, name = row
        text += f"{cat_id}. {name}\n"
        markup.add(types.InlineKeyboardButton(text=name, callback_data=f"view_tasks_{cat_id}"))
    markup.add(types.InlineKeyboardButton(text="Добавить категорию", callback_data="add_category"))
    markup.add(types.InlineKeyboardButton(text="Назад", callback_data="back_main"))
    send_text(chat_id, text, reply_markup=markup)
    logger.info("Displayed categories to chat_id %s", chat_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_tasks_"))
def handle_view_tasks(call):
    chat_id = call.message.chat.id
    cat_id = int(call.data.split("_")[-1])
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, total_time FROM tasks WHERE category_id = ?", (cat_id,))
    rows = cursor.fetchall()
    conn.close()
    text = f"Задачи в категории {cat_id}:\n"
    markup = types.InlineKeyboardMarkup()
    for row in rows:
        task_id, name, total_time = row
        text += f"{task_id}. {name} (Время: {total_time} сек.)\n"
        markup.add(types.InlineKeyboardButton(text=f"Выбрать {name}", callback_data=f"select_task_{task_id}"))
    markup.add(types.InlineKeyboardButton(text="Добавить задачу", callback_data=f"add_task_{cat_id}"))
    markup.add(types.InlineKeyboardButton(text="Назад", callback_data="menu_categories"))
    send_text(chat_id, text, reply_markup=markup)
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.exception("Error answering callback: %s", e)
    logger.info("Displayed tasks for category %s to chat_id %s", cat_id, chat_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("select_task_"))
def handle_select_task(call):
    chat_id = call.message.chat.id
    task_id = int(call.data.split("_")[-1])
    stop_timer(chat_id)
    start_timer(chat_id, task_id)
    text = f"Выбрана задача с ID {task_id}. Таймер запущен."
    markup = get_back_keyboard()
    send_text(chat_id, text, reply_markup=markup)
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.exception("Error answering callback: %s", e)
    logger.info("Selected task %s for chat_id %s", task_id, chat_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("add_category"))
def handle_add_category(call):
    chat_id = call.message.chat.id
    send_text(chat_id, "Введите название категории:")
    user_states[chat_id] = {"state": "adding_category"}
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.exception("Error answering callback: %s", e)
    logger.info("Prompted user %s to add category", chat_id)

@bot.message_handler(func=lambda message: message.chat.id in user_states and user_states[message.chat.id].get("state") == "adding_category")
def process_add_category(message):
    chat_id = message.chat.id
    cat_name = message.text.strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO categories (name) VALUES (?)", (cat_name,))
    conn.commit()
    conn.close()
    send_main_menu(chat_id)
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception as e:
        logger.exception("Error deleting user message: %s", e)
    user_states.pop(chat_id, None)
    logger.info("Added new category '%s' for chat_id %s", cat_name, chat_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("add_task_"))
def handle_add_task(call):
    chat_id = call.message.chat.id
    cat_id = int(call.data.split("_")[-1])
    send_text(chat_id, "Введите название задачи:")
    user_states[chat_id] = {"state": "adding_task", "category_id": cat_id}
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.exception("Error answering callback: %s", e)
    logger.info("Prompted user %s to add task in category %s", chat_id, cat_id)

@bot.message_handler(func=lambda message: message.chat.id in user_states and user_states[message.chat.id].get("state") == "adding_task")
def process_add_task(message):
    chat_id = message.chat.id
    task_name = message.text.strip()
    cat_id = user_states[chat_id].get("category_id")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tasks (category_id, name) VALUES (?, ?)", (cat_id, task_name))
    conn.commit()
    conn.close()
    send_main_menu(chat_id)
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception as e:
        logger.exception("Error deleting user message: %s", e)
    user_states.pop(chat_id, None)
    logger.info("Added new task '%s' in category %s for chat_id %s", task_name, cat_id, chat_id)

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def handle_back(call):
    chat_id = call.message.chat.id
    send_main_menu(chat_id)
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.exception("Error answering callback: %s", e)
    logger.info("User returned to main menu chat_id %s", chat_id)

# Глобальный обработчик callback для логирования
@bot.callback_query_handler(func=lambda call: True)
def log_all_callbacks(call):
    logger.info("Global callback received: %s", call.data)
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.exception("Error answering callback: %s", e)

# Функция для отображения текущей задачи
def show_current_task(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT task_id, start_time, saved_time FROM current_task WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        task_id, start_time, saved_time = row
        elapsed = int(time.time()) - start_time
        total = saved_time + elapsed
        text = f"Текущая задача (ID: {task_id})\nВремя выполнения: {total} сек."
    else:
        text = "Нет активной задачи."
    markup = get_back_keyboard()
    send_text(chat_id, text, reply_markup=markup)
    logger.info("Displayed current task for chat_id %s", chat_id)

# Функция для отображения статистики
def show_statistics(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT c.name, SUM(t.total_time) as total_time
        FROM categories c
        LEFT JOIN tasks t ON c.id = t.category_id
        GROUP BY c.id
    ''')
    rows = cursor.fetchall()
    conn.close()
    text = "Статистика по категориям:\n"
    data = {}
    for row in rows:
        name, total_time = row
        total_time = total_time if total_time else 0
        text += f"{name}: {total_time} сек.\n"
        data[name] = total_time
    chart_file = generate_chart(data)
    markup = get_back_keyboard()
    send_text(chat_id, text, reply_markup=markup)
    if chart_file and os.path.exists(chart_file):
        bot.send_photo(chat_id, photo=open(chart_file, 'rb'))
        os.remove(chart_file)
    logger.info("Displayed statistics for chat_id %s", chat_id)

if __name__ == '__main__':
    init_db()
    logger.info("Starting bot polling...")
    bot.polling(none_stop=True)
