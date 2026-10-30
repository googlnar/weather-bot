import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_TIMEOUT_SECONDS = 35
POLLING_TIMEOUT_SECONDS = 30
RETRY_DELAY_SECONDS = 3
ADMIN_ID = 1967475632
STATS_FILE = "stats.json"

logger = logging.getLogger("telegram_bot")


class TelegramAPIError(Exception):
    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


def telegram_api(token: str, method: str, payload: dict | None = None) -> dict | list:
    url = f"https://api.telegram.org/bot{token}/{method}"
    request = Request(
        url,
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            result = json.loads(error.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramAPIError(f"Telegram returned HTTP {error.code}.", error.code) from None
    except (TimeoutError, URLError, OSError) as error:
        raise TelegramAPIError(f"Network request failed ({type(error).__name__}).") from None
    except json.JSONDecodeError:
        raise TelegramAPIError("Telegram returned an unreadable response.") from None

    if not result.get("ok"):
        raise TelegramAPIError(result.get("description", "Telegram API request failed."), result.get("error_code"))
    return result["result"]


def send_message(token: str, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram_api(token, "sendMessage", payload)


def send_photo(token: str, chat_id: int, photo_url: str, caption: str = "") -> None:
    telegram_api(token, "sendPhoto", {"chat_id": chat_id, "photo": photo_url, "caption": caption})


def get_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "TelegramBot/1.0"})
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def load_stats() -> dict:
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"users": {}, "weather_requests": 0, "total_messages": 0}


def save_stats(stats: dict) -> None:
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Не удалось сохранить статистику: %s", e)


def add_user(user_id: int, username: str | None, first_name: str | None) -> None:
    stats = load_stats()
    uid = str(user_id)
    if uid not in stats["users"]:
        stats["users"][uid] = {
            "username": username,
            "first_name": first_name,
            "first_seen": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "requests": 0
        }
    stats["users"][uid]["requests"] = stats["users"][uid].get("requests", 0) + 1
    stats["total_messages"] = stats.get("total_messages", 0) + 1
    save_stats(stats)


def add_weather_request() -> None:
    stats = load_stats()
    stats["weather_requests"] = stats.get("weather_requests", 0) + 1
    save_stats(stats)


def weather_code_to_text(code: int) -> str:
    codes = {
        0: "Ясно ☀️", 1: "Преим. ясно 🌤", 2: "Перем. облачность ⛅", 3: "Пасмурно ☁️",
        45: "Туман 🌫", 48: "Изморозь 🌫",
        51: "Морось 🌦", 53: "Морось 🌦", 55: "Сильная морось 🌦",
        61: "Дождь 🌧", 63: "Дождь 🌧", 65: "Сильный дождь 🌧",
        71: "Снег ❄️", 73: "Снег ❄️", 75: "Сильный снег ❄️",
        80: "Ливень 🌧", 81: "Ливень 🌧", 82: "Сильный ливень 🌧",
        95: "Гроза ⛈", 96: "Гроза с градом ⛈", 99: "Гроза с градом ⛈",
    }
    return codes.get(code, "Неизвестно")


def get_weather(city: str) -> str | None:
    try:
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={quote(city)}&count=1&language=ru&format=json"
        geo = get_json(geo_url)
        if not geo.get("results"):
            return None

        place = geo["results"][0]
        lat = place["latitude"]
        lon = place["longitude"]
        name = place.get("name", city)
        country = place.get("country", "")

        weather_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,surface_pressure,wind_speed_10m"
            f"&hourly=temperature_2m,weather_code,precipitation_probability"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max"
            f"&timezone=auto&forecast_days=3"
        )
        data = get_json(weather_url)

        current = data["current"]
        daily = data["daily"]
        hourly = data["hourly"]

        temp = current["temperature_2m"]
        feels = current["apparent_temperature"]
        humidity = current["relative_humidity_2m"]
        pressure = round(current["surface_pressure"] * 0.750062)
        wind = current["wind_speed_10m"]
        condition = weather_code_to_text(current["weather_code"])

        lines = [
            f"📍 {name}, {country}",
            "",
            f"Сейчас: {condition}",
            f"🌡 {temp}°C  (ощущается как {feels}°C)",
            f"💧 Влажность: {humidity}%",
            f"📊 Давление: {pressure} мм рт.ст.",
            f"💨 Ветер: {wind} км/ч",
        ]

        try:
            sunrise = datetime.fromisoformat(daily["sunrise"][0]).strftime("%H:%M")
            sunset = datetime.fromisoformat(daily["sunset"][0]).strftime("%H:%M")
            lines.append(f"🌅 Восход: {sunrise}   🌇 Закат: {sunset}")
        except Exception:
            pass

        lines.append("")
        lines.append("📅 Прогноз на 3 дня:")
        day_names = ["Сегодня", "Завтра", "Послезавтра"]

        for i in range(3):
            d_cond = weather_code_to_text(daily["weather_code"][i])
            d_max = daily["temperature_2m_max"][i]
            d_min = daily["temperature_2m_min"][i]
            d_pop = daily["precipitation_probability_max"][i]
            pop_text = f"  💧{d_pop}%" if d_pop and d_pop > 0 else ""
            lines.append(f"{day_names[i]}: {d_cond}  {d_min}…{d_max}°C{pop_text}")

        lines.append("")
        lines.append("🕐 Ближайшие часы:")

        now = datetime.fromisoformat(current["time"])
        count = 0
        for i, t in enumerate(hourly["time"]):
            hour_time = datetime.fromisoformat(t)
            if hour_time <= now:
                continue
            hour_str = hour_time.strftime("%H:%M")
            temp_h = hourly["temperature_2m"][i]
            cond_h = weather_code_to_text(hourly["weather_code"][i])
            pop = hourly["precipitation_probability"][i]
            if pop and pop > 0:
                lines.append(f"{hour_str}  {temp_h}°C  {cond_h}  💧{pop}%")
            else:
                lines.append(f"{hour_str}  {temp_h}°C  {cond_h}")
            count += 1
            if count >= 8:
                break

        add_weather_request()
        return "\n".join(lines)

    except Exception as e:
        logger.warning("Ошибка погоды: %s", e)
        return "Не удалось получить погоду. Попробуй позже."


def get_main_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "Москва"}, {"text": "Санкт-Петербург"}],
            [{"text": "Новосибирск"}, {"text": "Екатеринбург"}],
            [{"text": "Казань"}, {"text": "Нижний Новгород"}],
            [{"text": "Челябинск"}, {"text": "Самара"}],
            [{"text": "Ростов-на-Дону"}, {"text": "Уфа"}],
            [{"text": "Красноярск"}, {"text": "Воронеж"}],
            [{"text": "Пермь"}, {"text": "Волгоград"}],
            [{"text": "Краснодар"}, {"text": "Сочи"}],
            [{"text": "Владивосток"}, {"text": "Калининград"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }


def handle_update(token: str, update: dict) -> None:
    message = update.get("message")
    if not message or not message.get("text"):
        return

    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = user.get("id")
    username = user.get("username")
    first_name = user.get("first_name")

    text = message["text"].strip()
    parts = text.split(maxsplit=1)
    command = parts[0].split("@", maxsplit=1)[0].lower()
    argument = parts[1] if len(parts) > 1 else ""

    if user_id:
        add_user(user_id, username, first_name)

    keyboard = get_main_keyboard()

    if command == "/stats":
        if user_id != ADMIN_ID:
            send_message(token, chat_id, "Эта команда доступна только администратору.")
            return

        stats = load_stats()
        users_count = len(stats.get("users", {}))
        weather_count = stats.get("weather_requests", 0)
        total_msgs = stats.get("total_messages", 0)

        reply = (
            f"📊 Статистика бота\n\n"
            f"👥 Пользователей: {users_count}\n"
            f"🌤 Запросов погоды: {weather_count}\n"
            f"💬 Всего сообщений: {total_msgs}\n\n"
            f"Последние пользователи:\n"
        )

        users = list(stats.get("users", {}).items())[-10:]
        for uid, info in reversed(users):
            name = info.get("first_name") or "Без имени"
            uname = f"@{info['username']}" if info.get("username") else ""
            reqs = info.get("requests", 0)
            reply += f"• {name} {uname} — {reqs} запр.\n"

        send_message(token, chat_id, reply)
        return

    if command == "/start":
        reply = (
            "Привет! Я подробный погодный бот.\n\n"
            "Нажми на кнопку с городом или просто напиши название.\n\n"
            "Команды:\n"
            "/cities — список городов\n"
            "/advice — совет\n"
            "/dog — собака\n"
            "/help — помощь"
        )
        send_message(token, chat_id, reply, reply_markup=keyboard)
        return

    if command == "/help":
        reply = (
            "Как пользоваться:\n\n"
            "• Нажми на кнопку с городом\n"
            "• Или просто напиши название города\n\n"
            "Бот покажет подробную погоду на 3 дня + по часам."
        )
        send_message(token, chat_id, reply, reply_markup=keyboard)
        return

    if command == "/cities":
        reply = (
            "🏙 Крупные города:\n\n"
            "Москва, Санкт-Петербург, Новосибирск, Екатеринбург,\n"
            "Казань, Нижний Новгород, Челябинск, Самара,\n"
            "Ростов-на-Дону, Уфа, Красноярск, Воронеж,\n"
            "Пермь, Волгоград, Краснодар, Сочи,\n"
            "Владивосток, Калининград и другие.\n\n"
            "Просто нажми кнопку или напиши название."
        )
        send_message(token, chat_id, reply, reply_markup=keyboard)
        return

    if command == "/weather":
        city = argument if argument else "Москва"
        result = get_weather(city)
        if result is None:
            send_message(token, chat_id, f"Город «{city}» не найден.", reply_markup=keyboard)
        else:
            send_message(token, chat_id, result, reply_markup=keyboard)
        return

    if command == "/advice":
        try:
            data = get_json("https://api.adviceslip.com/advice")
            reply = f"💡 Совет: {data['slip']['advice']}"
        except Exception:
            reply = "Не удалось получить совет."
        send_message(token, chat_id, reply, reply_markup=keyboard)
        return

    if command == "/dog":
        try:
            data = get_json("https://dog.ceo/api/breeds/image/random")
            send_photo(token, chat_id, data["message"], "Вот тебе собака 🐶")
        except Exception:
            send_message(token, chat_id, "Не удалось получить фото собаки.", reply_markup=keyboard)
        return

    if not text.startswith("/"):
        result = get_weather(text)
        if result is None:
            send_message(token, chat_id, f"Город «{text}» не найден.", reply_markup=keyboard)
        else:
            send_message(token, chat_id, result, reply_markup=keyboard)
        return

    send_message(token, chat_id, "Неизвестная команда. Напиши /help", reply_markup=keyboard)


def run_bot(token: str) -> None:
    try:
        bot = telegram_api(token, "getMe")
    except TelegramAPIError as error:
        logger.error("Не удалось проверить токен бота: %s", error)
        return

    logger.info("Бот @%s запущен и ожидает сообщения.", bot["username"])
    offset = 0

    while True:
        try:
            updates = telegram_api(token, "getUpdates", {
                "offset": offset,
                "timeout": POLLING_TIMEOUT_SECONDS,
                "allowed_updates": ["message"],
            })
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(token, update)
                except (KeyError, TelegramAPIError) as error:
                    logger.warning("Не удалось обработать сообщение: %s", error)
        except TelegramAPIError as error:
            if error.error_code in (401, 403):
                logger.error("Telegram отклонил токен.")
                return
            logger.warning("%s Повтор через %s сек.", error, RETRY_DELAY_SECONDS)
            time.sleep(RETRY_DELAY_SECONDS)


# ===== Веб-сервер для поддержания жизни =====
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive")

    def log_message(self, format, *args):
        return  # отключаем логи


def start_keepalive_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    logger.info("Keep-alive сервер запущен на порту %s", port)
    server.serve_forever()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.error("Не задан TELEGRAM_BOT_TOKEN.")
        return 1

    # Запускаем веб-сервер в отдельном потоке
    threading.Thread(target=start_keepalive_server, daemon=True).start()

    # Запускаем бота
    run_bot(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
