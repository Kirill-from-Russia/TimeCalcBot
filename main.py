import os
from pathlib import Path
from dotenv import load_dotenv
import asyncio
import logging
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, F, BaseMiddleware, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / "config" / ".env"

load_dotenv(dotenv_path=env_path)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")

REMINDERS_DB = {}
reminder_id_counter = 1

scheduler = AsyncIOScheduler()


# --- Middleware для логирования username и команд/текста ---
class LoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, types.Message) and event.text:
            username = f"@{event.from_user.username}" if event.from_user.username else f"ID: {event.from_user.id}"
            
            if event.text.startswith('/'):
                logging.info(f"Пользователь {username} вызвал команду: {event.text}")
            else:
                logging.info(f"Пользователь {username} отправил текст: {event.text}")
                
        return await handler(event, data)


# --- Вспомогательный класс для расчета дат ---
class SimpleDate:
    def __init__(self, year: int, month: int, day: int):
        self.year = year
        self.month = month
        self.day = day

    def is_leap(self) -> bool:
        y = self.year if self.year > 0 else self.year + 1
        return (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)

    def days_in_month(self) -> int:
        days = [0, 31, 29 if self.is_leap() else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        return days[self.month]

    def to_tuple(self):
        y = self.year if self.year > 0 else self.year + 1
        return (y, self.month, self.day)


# --- Состояния FSM ---
class DateCalc(StatesGroup):
    waiting_for_first_date = State()
    waiting_for_second_date = State()


class ReminderState(StatesGroup):
    waiting_for_datetime = State()
    waiting_for_comment = State()


# --- Клавиатуры ---
def get_today_keyboard() -> types.ReplyKeyboardMarkup:
    kb = [[types.KeyboardButton(text="Сегодня")]]
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)


def get_skip_comment_keyboard() -> types.ReplyKeyboardMarkup:
    kb = [[types.KeyboardButton(text="Пропустить")]]
    return types.ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)


def get_change_first_date_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="✏️ Изменить первую дату", callback_data="calc_change_first")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def get_reminder_main_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="➕ Создать напоминание", callback_data="rem_create")],
        [InlineKeyboardButton(text="📋 Мои напоминания", callback_data="rem_list")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def get_calc_result_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="🔄 Еще раз", callback_data="calc_restart")],
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="calc_back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# --- Вспомогательные функции ---
def pluralize(n: int, form1: str, form2: str, form5: str) -> str:
    n_abs = abs(n)
    if 11 <= n_abs % 100 <= 14:
        return f"{n_abs} {form5}"

    last_digit = n_abs % 10
    if last_digit == 1:
        return f"{n_abs} {form1}"
    elif 2 <= last_digit <= 4:
        return f"{n_abs} {form2}"
    else:
        return f"{n_abs} {form5}"


def parse_user_date(text: str | None) -> dict | None:
    if not text:
        return None

    text_clean = text.strip()

    if text_clean.lower() == "сегодня":
        now = datetime.now()
        return {"year": now.year, "month": now.month, "day": now.day, "hour": 0, "minute": 0}

    pattern = r"^(\d{1,2})\.(\d{1,2})\.(-?\d{1,6})$"
    match = re.match(pattern, text_clean)

    if not match:
        return None

    day_str, month_str, year_str = match.groups()
    day, month, year = int(day_str), int(month_str), int(year_str)

    if year == 0 or not (1 <= month <= 12):
        return None

    max_days = [0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if not (1 <= day <= max_days[month]):
        return None

    return {"year": year, "month": month, "day": day, "hour": 0, "minute": 0}


def calculate_difference(d1: dict, d2: dict) -> str:
    start = SimpleDate(d1["year"], d1["month"], d1["day"])
    end = SimpleDate(d2["year"], d2["month"], d2["day"])

    if start.to_tuple() > end.to_tuple():
        start, end = end, start

    y1 = start.year if start.year > 0 else start.year + 1
    y2 = end.year if end.year > 0 else end.year + 1

    years = y2 - y1
    months = end.month - start.month
    days = end.day - start.day

    if days < 0:
        months -= 1
        prev_month = end.month - 1 if end.month > 1 else 12
        prev_year = end.year if end.month > 1 else (end.year - 1 if end.year != 1 else -1)
        temp_date = SimpleDate(prev_year, prev_month, 1)
        days += temp_date.days_in_month()

    if months < 0:
        years -= 1
        months += 12

    units = [
        (years, "год", "года", "лет"),
        (months, "месяц", "месяца", "месяцев"),
        (days, "день", "дня", "дней"),
    ]

    result_parts = [pluralize(value, f1, f2, f5) for value, f1, f2, f5 in units if value > 0]
    return "\n".join(result_parts) if result_parts else "0 дней"


def format_date_with_era(d: dict) -> str:
    year = d["year"]
    day_str = f"{d['day']:02d}"
    month_str = f"{d['month']:02d}"

    if year < 0:
        return f"{day_str}.{month_str}.{abs(year)} г. до н.э."
    else:
        return f"{day_str}.{month_str}.{year} г."


async def start_calculator_flow(message: types.Message, state: FSMContext):
    await state.clear()
    await state.set_state(DateCalc.waiting_for_first_date)
    await message.answer(
        "🧮 **Калькулятор дат**\n\n"
        "Вы можете вводить годы от 1 до 6 цифр, а также использовать знак минус (-) для дат **до нашей эры**.\n"
        "Примеры: `15.05.2024`, `01.01.1`, `12.04.-47`.\n\n"
        "Введите первую дату:",
        reply_markup=get_today_keyboard(),
        parse_mode="Markdown",
    )


async def send_help_menu(message: types.Message):
    help_text = (
        "📖 **Доступные разделы и команды:**\n\n"
        "🧮 /calculator — Калькулятор разницы между двумя датами\n"
        "⏰ /reminder — Менеджер напоминаний (создание, просмотр и удаление)\n"
        "❌ /cancel — Сбросить текущее действие"
    )
    await message.answer(help_text, parse_mode="Markdown")


async def send_reminder_notification(bot: Bot, user_id: int, reminder_id: int, comment: str):
    text = "🔔 **Напоминание!**"
    if comment:
        text += f"\n\n💬 Комментарий: {comment}"

    try:
        await bot.send_message(user_id, text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Не удалось отправить напоминание {user_id}: {e}")
    finally:
        REMINDERS_DB.pop(reminder_id, None)


async def main():
    session = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.outer_middleware(LoggingMiddleware())

    scheduler.start()

    # --- Команда /start ---
    @dp.message(Command("start"))
    async def cmd_start(message: types.Message, state: FSMContext):
        await state.clear()
        welcome_text = (
            "👋 **Приветствую!**\n\n"
            "Я ваш персональный многофункциональный помощник по работе со временем.\n\n"
            "📌 **Мои возможности:**\n"
            "• Расчёт точной разницы между датами.\n"
            "• Создание и управление напоминаниями.\n\n"
            "Для просмотра всех доступных разделов нажмите /help."
        )
        await message.answer(welcome_text, parse_mode="Markdown")

    # --- Команда /help ---
    @dp.message(Command("help"))
    async def cmd_help(message: types.Message):
        await send_help_menu(message)

    # --- Отмена любых действий ---
    @dp.message(Command("cancel"))
    async def cmd_cancel(message: types.Message, state: FSMContext):
        await state.clear()
        await message.answer("Действие отменено. Используйте /help для просмотра меню.")

    # --- Команда /calculator ---
    @dp.message(Command("calculator"))
    async def cmd_calculator(message: types.Message, state: FSMContext):
        await start_calculator_flow(message, state)

    @dp.message(DateCalc.waiting_for_first_date, F.text)
    async def process_first_date(message: types.Message, state: FSMContext):
        date1 = parse_user_date(message.text)

        if not date1:
            await message.answer(
                "❌ Неверный формат даты!\n"
                "Введите дату в формате ДД.ММ.ГГГГ (знак `-` для до н.э.):",
                reply_markup=get_today_keyboard(),
            )
            return

        await state.update_data(first_date=date1)
        await state.set_state(DateCalc.waiting_for_second_date)

        await message.answer(
            f"Принято: {format_date_with_era(date1)}\n\n"
            "Теперь введите вторую дату (или нажмите кнопку ниже, чтобы изменить первую):",
            reply_markup=get_change_first_date_keyboard(),
        )

    # Обработка кнопки "Изменить первую дату"
    @dp.callback_query(F.data == "calc_change_first")
    async def process_change_first_date(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.set_state(DateCalc.waiting_for_first_date)
        await callback.message.answer(
            "✏️ Введите новую первую дату:",
            reply_markup=get_today_keyboard(),
        )

    @dp.message(DateCalc.waiting_for_second_date, F.text)
    async def process_second_date(message: types.Message, state: FSMContext):
        date2 = parse_user_date(message.text)

        if not date2:
            await message.answer(
                "❌ Неверный формат даты! Введите корректную дату:",
                reply_markup=get_change_first_date_keyboard(),
            )
            return

        user_data = await state.get_data()
        date1 = user_data["first_date"]

        if date1 == date2:
            await message.answer(
                "⚠️ Ошибка: Вы ввели две одинаковые даты!\nПожалуйста, введите вторую дату:",
                reply_markup=get_change_first_date_keyboard(),
            )
            return

        formatted_diff = calculate_difference(date1, date2)

        s1 = SimpleDate(date1["year"], date1["month"], date1["day"])
        s2 = SimpleDate(date2["year"], date2["month"], date2["day"])

        earlier_date = date1 if s1.to_tuple() < s2.to_tuple() else date2
        later_date = date2 if s1.to_tuple() < s2.to_tuple() else date1

        result_text = (
            f"📅 Результат:\n\n"
            f"От: {format_date_with_era(earlier_date)}\n"
            f"До: {format_date_with_era(later_date)}\n\n"
            f"⏳ Разница:\n{formatted_diff}"
        )

        await message.answer(
            result_text,
            reply_markup=get_calc_result_keyboard(),
        )
        await state.clear()

    # --- Кнопки калькулятора ---
    @dp.callback_query(F.data == "calc_restart")
    async def process_calc_restart(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await start_calculator_flow(callback.message, state)

    @dp.callback_query(F.data == "calc_back")
    async def process_calc_back(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.clear()
        await send_help_menu(callback.message)

    # --- Команда /reminder ---
    @dp.message(Command("reminder"))
    async def cmd_reminder(message: types.Message, state: FSMContext):
        await state.clear()
        await message.answer(
            "⏰ **Управление напоминаниями**\n\nВыберите действие ниже:",
            reply_markup=get_reminder_main_keyboard(),
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data == "rem_create")
    async def process_rem_create(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.set_state(ReminderState.waiting_for_datetime)
        await callback.message.answer(
            "📅 Введите дату и время напоминания в формате **ДД.ММ.ГГГГ ЧЧ:ММ**\nПример: `25.12.2026 18:30`",
            parse_mode="Markdown",
        )

    @dp.message(ReminderState.waiting_for_datetime, F.text)
    async def process_reminder_datetime(message: types.Message, state: FSMContext):
        try:
            rem_datetime = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
        except ValueError:
            await message.answer(
                "❌ Неверный формат! Введите дату и время строго в формате **ДД.ММ.ГГГГ ЧЧ:ММ**",
                parse_mode="Markdown",
            )
            return

        if rem_datetime <= datetime.now():
            await message.answer("⚠️ Дата и время должны быть в будущем! Попробуйте еще раз:")
            return

        await state.update_data(rem_datetime=rem_datetime.isoformat())
        await state.set_state(ReminderState.waiting_for_comment)
        await message.answer(
            "📝 Введите комментарий к напоминанию (или нажмите **Пропустить**):",
            reply_markup=get_skip_comment_keyboard(),
            parse_mode="Markdown",
        )

    @dp.message(ReminderState.waiting_for_comment, F.text)
    async def process_reminder_comment(message: types.Message, state: FSMContext):
        global reminder_id_counter

        user_data = await state.get_data()
        rem_datetime = datetime.fromisoformat(user_data["rem_datetime"])

        comment = "" if message.text.strip().lower() == "пропустить" else message.text.strip()

        rem_id = reminder_id_counter
        reminder_id_counter += 1

        REMINDERS_DB[rem_id] = {
            "user_id": message.from_user.id,
            "time": rem_datetime,
            "text": comment,
        }

        scheduler.add_job(
            send_reminder_notification,
            "date",
            run_date=rem_datetime,
            args=[bot, message.from_user.id, rem_id, comment],
            id=str(rem_id),
        )

        time_str = rem_datetime.strftime("%d.%m.%Y в %H:%M")
        res_text = f"✅ **Напоминание успешно установлено!**\n\n📅 Дата: {time_str}"
        if comment:
            res_text += f"\n💬 Комментарий: {comment}"

        await message.answer(
            res_text,
            reply_markup=types.ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        await state.clear()

    @dp.callback_query(F.data == "rem_list")
    async def process_rem_list(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id

        user_reminders = {rid: rem for rid, rem in REMINDERS_DB.items() if rem["user_id"] == user_id}

        if not user_reminders:
            await callback.message.answer("📭 У вас нет активных напоминаний.")
            return

        kb = []
        text_lines = ["📋 **Ваши активные напоминания:**\n"]

        for rid, rem in user_reminders.items():
            time_str = rem["time"].strftime("%d.%m.%Y %H:%M")
            comment_str = f' - "{rem["text"]}"' if rem["text"] else ""
            text_lines.append(f"• **{time_str}**{comment_str}")

            kb.append([InlineKeyboardButton(text=f"❌ Удалить ({time_str})", callback_data=f"rem_del_{rid}")])

        kb_markup = InlineKeyboardMarkup(inline_keyboard=kb)
        await callback.message.answer("\n".join(text_lines), reply_markup=kb_markup, parse_mode="Markdown")

    @dp.callback_query(F.data.startswith("rem_del_"))
    async def process_rem_delete(callback: types.CallbackQuery):
        rem_id = int(callback.data.split("_")[2])

        if rem_id in REMINDERS_DB:
            REMINDERS_DB.pop(rem_id)
            try:
                scheduler.remove_job(str(rem_id))
            except Exception:
                pass
            await callback.answer("✅ Напоминание удалено!")
            await callback.message.edit_text("🗑️ Напоминание успешно удалено.")
        else:
            await callback.answer("⚠️ Напоминание не найдено или уже сработало.")

    # Нетекстовый ввод во время FSM
    @dp.message(DateCalc.waiting_for_first_date)
    @dp.message(DateCalc.waiting_for_second_date)
    @dp.message(ReminderState.waiting_for_datetime)
    @dp.message(ReminderState.waiting_for_comment)
    async def process_invalid_input_type(message: types.Message):
        await message.answer("Пожалуйста, отправьте текстовое сообщение.")

    # --- Универсальный обработчик неизвестных команд и случайных сообщений ---
    @dp.message()
    async def process_unknown_message(message: types.Message):
        await message.answer(
            "🤖 Я не понял ваше сообщение.\n\n"
            "📖 **Доступные команды:**\n"
            "🧮 /calculator — Калькулятор разницы между двумя датами\n"
            "⏰ /reminder — Менеджер напоминаний\n"
            "❌ /cancel — Сбросить текущее действие\n"
            "❓ /help — Справка по командам",
            parse_mode="Markdown"
        )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())