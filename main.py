import os
import time
import imaplib
import email
import logging
from email.header import decode_header

import telebot
from dotenv import load_dotenv
import html2text

# --- 1. Загрузка конфигурации и настройка ---

# Загружаем переменные из .env файла
load_dotenv()

# Настройка логирования для отладки и мониторинга
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),  # Запись логов в файл
        logging.StreamHandler()         # Вывод логов в консоль
    ]
)

# Загрузка учетных данных из переменных окружения
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
# Загружаем ID чатов и преобразуем в список
TELEGRAM_CHAT_IDS = os.environ.get('TELEGRAM_CHAT_IDS', '').split(',')
GMAIL_ADDRESS = os.environ.get('GMAIL_ADDRESS')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD')

# Настройки IMAP сервера и интервала проверки
IMAP_SERVER = 'imap.gmail.com'
IMAP_PORT = 993
CHECK_INTERVAL_SECONDS = int(os.environ.get('CHECK_INTERVAL_SECONDS', 60))

# --- 2. Валидация конфигурации ---

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, GMAIL_ADDRESS, GMAIL_APP_PASSWORD]):
    logging.critical("КРИТИЧЕСКАЯ ОШИБКА: Не все переменные окружения заданы. Проверьте ваш .env файл.")
    exit(1)

# --- 3. Инициализация Telegram бота ---

try:
    bot = telebot.TeleBot(token=TELEGRAM_BOT_TOKEN)
    bot_info = bot.get_me()
    logging.info(f"Бот Telegram '{bot_info.username}' успешно подключен.")
except Exception as e:
    logging.critical(f"Не удалось подключиться к Telegram: {e}", exc_info=True)
    exit(1)

# --- 4. Функции для работы с почтой и Telegram ---

def connect_to_gmail():
    """Подключается к Gmail IMAP серверу и возвращает объект соединения."""
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select('inbox')
        logging.info("Успешное подключение к Gmail IMAP.")
        return mail
    except Exception as e:
        logging.error(f"Ошибка подключения или входа в Gmail: {e}", exc_info=True)
        return None

def parse_email(msg_data):
    """Извлекает тему, отправителя и тело письма. Преобразует HTML в Markdown."""
    raw_email = msg_data[0][1]
    msg = email.message_from_bytes(raw_email)

    # Декодирование темы письма
    subject, encoding = decode_header(msg["Subject"])[0]
    if isinstance(subject, bytes):
        subject = subject.decode(encoding if encoding else 'utf-8')

    # Декодирование отправителя
    sender, encoding = decode_header(msg.get("From"))[0]
    if isinstance(sender, bytes):
        sender = sender.decode(encoding if encoding else 'utf-8')

    # Поиск тела письма
    body_plain = ""
    body_html = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain" and not body_plain:
                try:
                    charset = part.get_content_charset() or 'utf-8'
                    body_plain = part.get_payload(decode=True).decode(charset, errors='ignore')
                except Exception as e:
                    logging.warning(f"Не удалось декодировать plain-text часть: {e}")
            elif content_type == "text/html" and not body_html:
                try:
                    charset = part.get_content_charset() or 'utf-8'
                    body_html = part.get_payload(decode=True).decode(charset, errors='ignore')
                except Exception as e:
                    logging.warning(f"Не удалось декодировать HTML часть: {e}")
    else:
        # Для писем без частей
        try:
            charset = msg.get_content_charset() or 'utf-8'
            body_plain = msg.get_payload(decode=True).decode(charset, errors='ignore')
        except Exception as e:
            logging.warning(f"Не удалось декодировать тело письма: {e}")

    # Выбор наилучшего контента и преобразование HTML в Markdown
    final_body = ""
    if body_plain:
        final_body = body_plain
    elif body_html:
        h = html2text.HTML2Text()
        h.ignore_links = False
        final_body = h.handle(body_html)

    # Формирование и обрезка сообщения для Telegram
    message_text = f"**Новое письмо**\n\n"
    message_text += f"**От:** {sender}\n"
    message_text += f"**Тема:** {subject}\n\n"
    message_text += f"```{final_body.strip()}```"

    if len(message_text) > 4096:
        return message_text[:4090] + "\n...`" # Обрезаем, сохраняя форматирование

    return message_text


def send_to_telegram(message_text):
    """Отправляет отформатированное сообщение во все чаты из списка."""
    for chat_id in TELEGRAM_CHAT_IDS:
        if not chat_id: continue # Пропускаем пустые ID, если в .env файле лишняя запятая
        try:
            bot.send_message(chat_id.strip(), message_text, parse_mode='Markdown')
            logging.info(f"Сообщение успешно отправлено в чат {chat_id.strip()}")
        except Exception as e:
            logging.error(f"Ошибка отправки в чат {chat_id.strip()}: {e}")

# --- 5. Основной цикл работы бота ---

def run_bot():
    """Главная функция: подключается к почте, проверяет и отправляет сообщения."""
    logging.info("Бот запущен. Начинаю проверку почты...")
    while True:
        mail = None
        try:
            mail = connect_to_gmail()
            if not mail:
                logging.warning("Не удалось подключиться к Gmail, повторная попытка через интервал.")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            # Поиск всех непрочитанных писем
            status, email_ids_raw = mail.search(None, 'UNSEEN')
            if status != 'OK':
                logging.error(f"Ошибка поиска писем: {status}")
                continue
                
            email_id_list = email_ids_raw[0].split()
            if email_id_list:
                logging.info(f"Найдено {len(email_id_list)} новых писем.")

                for email_id in reversed(email_id_list): # Начинаем с самых новых
                    try:
                        status, msg_data = mail.fetch(email_id, '(RFC822)')
                        if status == 'OK':
                            message_to_send = parse_email(msg_data)
                            send_to_telegram(message_to_send)
                            # Отмечаем письмо как прочитанное
                            mail.store(email_id, '+FLAGS', '\\Seen')
                        else:
                            logging.error(f"Не удалось получить письмо {email_id.decode()}: {status}")
                    except Exception as e:
                        logging.error(f"Ошибка при обработке письма {email_id.decode()}: {e}", exc_info=True)
            else:
                logging.info("Новых писем нет.")

        except imaplib.IMAP4.abort as e:
            logging.warning(f"Соединение с IMAP разорвано: {e}. Попытка переподключения...")
        except Exception as e:
            logging.error(f"Произошла непредвиденная ошибка в главном цикле: {e}", exc_info=True)

        finally:
            if mail:
                try:
                    mail.logout()
                    logging.info("Отключился от Gmail IMAP.")
                except Exception as e:
                    logging.error(f"Ошибка при выходе из IMAP: {e}")
        
        logging.info(f"Следующая проверка через {CHECK_INTERVAL_SECONDS} секунд.")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_bot()