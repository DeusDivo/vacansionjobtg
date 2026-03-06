import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Укажите BOT_TOKEN в .env")

HH_API_URL = "https://api.hh.ru/vacancies"
SPAM_COOLDOWN_SECONDS = 5
DB_PATH = "bot_data.db"
DEFAULT_SALARY_MIN = 70000
DEFAULT_SALARY_MAX = 150000

COUNTRY_OPTIONS = {
    "all": (None, "Любая страна"),
    "ru": (113, "Россия"),
    "kz": (40, "Казахстан"),
    "by": (16, "Беларусь"),
}

CITY_OPTIONS_BY_COUNTRY = {
    "ru": {
        "any": (None, "Любой город"),
        "1": (1, "Москва"),
        "2": (2, "Санкт-Петербург"),
        "4": (4, "Новосибирск"),
    },
    "kz": {
        "any": (None, "Любой город"),
        "160": (160, "Алматы"),
        "159": (159, "Астана"),
        "205": (205, "Шымкент"),
    },
    "by": {
        "any": (None, "Любой город"),
        "1002": (1002, "Минск"),
        "2237": (2237, "Гомель"),
        "2238": (2238, "Витебск"),
    },
}

SALARY_MIN_OPTIONS = {
    "70000": (70000, "От 70 000"),
    "100000": (100000, "От 100 000"),
    "120000": (120000, "От 120 000"),
}

SALARY_MAX_OPTIONS = {
    "150000": (150000, "До 150 000"),
    "180000": (180000, "До 180 000"),
    "220000": (220000, "До 220 000"),
}

WORK_TYPE_OPTIONS = {
    "any": "Любой формат",
    "remote": "Удалённо",
    "office": "Только офис",
    "hybrid": "Гибрид",
}


@dataclass
class UserFilters:
    country_key: Optional[str] = None
    country_area: Optional[int] = None
    city_area: Optional[int] = None
    salary_min: int = DEFAULT_SALARY_MIN
    salary_max: int = DEFAULT_SALARY_MAX
    work_type: str = "any"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_add_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError:
        pass


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_filters (
                user_id INTEGER PRIMARY KEY,
                country_key TEXT,
                country_area INTEGER,
                city_area INTEGER,
                salary_min INTEGER,
                salary_max INTEGER,
                work_type TEXT
            )
            """
        )
        _safe_add_column(conn, "user_filters", "country_key", "TEXT")
        _safe_add_column(conn, "user_filters", "country_area", "INTEGER")
        _safe_add_column(conn, "user_filters", "city_area", "INTEGER")
        _safe_add_column(conn, "user_filters", "salary_min", "INTEGER")
        _safe_add_column(conn, "user_filters", "salary_max", "INTEGER")
        _safe_add_column(conn, "user_filters", "work_type", "TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_vacancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                vacancy_id TEXT NOT NULL,
                title TEXT NOT NULL,
                company TEXT,
                url TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, vacancy_id)
            )
            """
        )


def get_user_filters(user_id: int) -> UserFilters:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT country_key, country_area, city_area, salary_min, salary_max, work_type
            FROM user_filters
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    if not row:
        return UserFilters()

    return UserFilters(
        country_key=row["country_key"],
        country_area=row["country_area"],
        city_area=row["city_area"],
        salary_min=row["salary_min"] or DEFAULT_SALARY_MIN,
        salary_max=row["salary_max"] or DEFAULT_SALARY_MAX,
        work_type=row["work_type"] or "any",
    )


def upsert_user_filters(user_id: int, filters: UserFilters) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_filters (user_id, country_key, country_area, city_area, salary_min, salary_max, work_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                country_key = excluded.country_key,
                country_area = excluded.country_area,
                city_area = excluded.city_area,
                salary_min = excluded.salary_min,
                salary_max = excluded.salary_max,
                work_type = excluded.work_type
            """,
            (
                user_id,
                filters.country_key,
                filters.country_area,
                filters.city_area,
                filters.salary_min,
                filters.salary_max,
                filters.work_type,
            ),
        )


def save_vacancy(user_id: int, vacancy: dict) -> bool:
    with get_connection() as conn:
        try:
            conn.execute(
                """
                INSERT INTO saved_vacancies (user_id, vacancy_id, title, company, url)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    str(vacancy.get("id")),
                    vacancy.get("name", "Без названия"),
                    vacancy.get("employer", {}).get("name", "Не указана"),
                    vacancy.get("alternate_url", ""),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_saved_vacancies(user_id: int) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT title, company, url, created_at
            FROM saved_vacancies
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (user_id,),
        ).fetchall()


def in_salary_range(salary: Optional[dict], min_salary: int, max_salary: int) -> bool:
    if not salary:
        return False

    values = [v for v in (salary.get("from"), salary.get("to")) if isinstance(v, (int, float))]
    if not values:
        return False

    lower = min(values)
    upper = max(values)
    return not (upper < min_salary or lower > max_salary)


def matches_work_type(vacancy: dict, work_type: str) -> bool:
    if work_type == "any":
        return True

    schedule_name = ((vacancy.get("schedule") or {}).get("name") or "").lower()
    work_format_names = [item.get("name", "").lower() for item in vacancy.get("work_format", [])]
    combined = " ".join([schedule_name, *work_format_names])

    if work_type == "remote":
        return "удален" in combined or "remote" in combined
    if work_type == "office":
        return "офис" in combined and "гибрид" not in combined and "удален" not in combined
    if work_type == "hybrid":
        return "гибрид" in combined or "hybrid" in combined

    return True


def fetch_vacancies(query: str, filters: UserFilters, limit: int = 20) -> list[dict]:
    area = filters.city_area if filters.city_area is not None else filters.country_area
    params = {
        "text": query,
        "per_page": limit,
        "salary": filters.salary_min,
    }
    if area is not None:
        params["area"] = area

    response = requests.get(HH_API_URL, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    items = data.get("items", [])
    by_salary = [item for item in items if in_salary_range(item.get("salary"), filters.salary_min, filters.salary_max)]
    return [item for item in by_salary if matches_work_type(item, filters.work_type)]


def salary_to_text(salary: Optional[dict]) -> str:
    if not salary:
        return "Не указана"
    frm = salary.get("from")
    to = salary.get("to")
    cur = salary.get("currency", "")
    return f"{frm or '?'} - {to or '?'} {cur}"


def format_vacancy(vacancy: dict) -> str:
    schedule = (vacancy.get("schedule") or {}).get("name") or "Не указан"
    return (
        f"💼 <b>{vacancy.get('name', 'Без названия')}</b>\n"
        f"🏢 {vacancy.get('employer', {}).get('name', 'Не указана')}\n"
        f"🕒 Формат: {schedule}\n"
        f"💰 {salary_to_text(vacancy.get('salary'))}\n"
        f"🔗 {vacancy.get('alternate_url', '')}"
    )


def filters_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Страна", callback_data="filter:country")],
            [InlineKeyboardButton(text="Город", callback_data="filter:city")],
            [InlineKeyboardButton(text="Мин. зарплата", callback_data="filter:salary_min")],
            [InlineKeyboardButton(text="Макс. зарплата", callback_data="filter:salary_max")],
            [InlineKeyboardButton(text="Вид работы", callback_data="filter:work_type")],
        ]
    )


def post_filter_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Настроить ещё", callback_data="menu:filters")],
            [InlineKeyboardButton(text="🔎 Начать поиск", callback_data="menu:start_search")],
        ]
    )


def refresh_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить список вакансий", callback_data="refresh:list")]]
    )


def country_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, (_, title) in COUNTRY_OPTIONS.items():
        rows.append([InlineKeyboardButton(text=title, callback_data=f"set_country:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def city_keyboard(country_key: Optional[str]) -> InlineKeyboardMarkup:
    rows = []
    options = CITY_OPTIONS_BY_COUNTRY.get(country_key or "", {"any": (None, "Любой город")})
    for key, (_, title) in options.items():
        rows.append([InlineKeyboardButton(text=title, callback_data=f"set_city:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def salary_min_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, (_, title) in SALARY_MIN_OPTIONS.items():
        rows.append([InlineKeyboardButton(text=title, callback_data=f"set_salary_min:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def salary_max_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, (_, title) in SALARY_MAX_OPTIONS.items():
        rows.append([InlineKeyboardButton(text=title, callback_data=f"set_salary_max:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def work_type_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, title in WORK_TYPE_OPTIONS.items():
        rows.append([InlineKeyboardButton(text=title, callback_data=f"set_work_type:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def save_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💾 Сохранить", callback_data=f"save:{vacancy_id}")]]
    )


def merge_keyboards(primary: InlineKeyboardMarkup, secondary: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=primary.inline_keyboard + secondary.inline_keyboard)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
last_request_time: dict[int, float] = {}
last_results: dict[int, dict[str, dict]] = {}
last_query: dict[int, str] = {}


async def send_search_results(message: Message, user_id: int, query: str, bypass_cooldown: bool = False) -> None:
    now = time.time()
    previous = last_request_time.get(user_id, 0.0)
    if not bypass_cooldown and now - previous < SPAM_COOLDOWN_SECONDS:
        wait_seconds = int(SPAM_COOLDOWN_SECONDS - (now - previous)) + 1
        await message.answer(f"Слишком часто. Подожди {wait_seconds} сек.")
        return
    last_request_time[user_id] = now

    if len(query) < 2:
        await message.answer("Слишком короткий запрос.")
        return

    filters = get_user_filters(user_id)
    await message.answer("Ищу вакансии...")

    try:
        vacancies = fetch_vacancies(query, filters)
    except Exception as exc:
        await message.answer(f"Ошибка запроса: {exc}")
        return

    if not vacancies:
        await message.answer("Ничего не найдено в выбранных фильтрах 😔", reply_markup=refresh_keyboard())
        return

    last_query[user_id] = query
    last_results[user_id] = {str(v.get("id")): v for v in vacancies if v.get("id")}

    for vacancy in vacancies[:5]:
        vacancy_id = str(vacancy.get("id", ""))
        markup = save_keyboard(vacancy_id) if vacancy_id else None
        await message.answer(format_vacancy(vacancy), parse_mode="HTML", reply_markup=markup)

    await message.answer("Хочешь обновить список тем же запросом?", reply_markup=refresh_keyboard())


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    text = (
        "Привет! Я ищу вакансии Python на hh.ru.\n\n"
        "Команды:\n"
        "/filters — настроить страну, город, зарплату и формат работы\n"
        "/saved — показать сохранённые вакансии\n\n"
        "Просто отправь текст запроса, например: python django"
    )
    await message.answer(text)


@dp.message(Command("filters"))
async def filters_handler(message: Message) -> None:
    await message.answer("Выбери, какой фильтр изменить:", reply_markup=filters_keyboard())


@dp.callback_query(F.data == "menu:filters")
async def menu_filters(callback: CallbackQuery) -> None:
    await callback.message.answer("Выбери, какой фильтр изменить:", reply_markup=filters_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "menu:start_search")
async def menu_start_search(callback: CallbackQuery) -> None:
    await callback.message.answer("Отправь текст запроса для поиска вакансий.")
    await callback.answer()


@dp.callback_query(F.data == "refresh:list")
async def refresh_list(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    query = last_query.get(user_id)
    if not query:
        await callback.message.answer("Сначала отправь текстовый запрос, чтобы я знал что обновлять.")
        await callback.answer()
        return

    await send_search_results(callback.message, user_id, query, bypass_cooldown=True)
    await callback.answer("Список обновлён")


@dp.callback_query(F.data == "filter:country")
async def show_country_filter(callback: CallbackQuery) -> None:
    await callback.message.answer("Выбери страну:", reply_markup=country_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "filter:city")
async def show_city_filter(callback: CallbackQuery) -> None:
    user_filters = get_user_filters(callback.from_user.id)
    if not user_filters.country_key or user_filters.country_key not in CITY_OPTIONS_BY_COUNTRY:
        await callback.message.answer("Сначала выбери страну в фильтре 'Страна'.")
    else:
        await callback.message.answer("Выбери город:", reply_markup=city_keyboard(user_filters.country_key))
    await callback.answer()


@dp.callback_query(F.data == "filter:salary_min")
async def show_salary_min_filter(callback: CallbackQuery) -> None:
    await callback.message.answer("Выбери минимальную зарплату:", reply_markup=salary_min_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "filter:salary_max")
async def show_salary_max_filter(callback: CallbackQuery) -> None:
    await callback.message.answer("Выбери максимальную зарплату:", reply_markup=salary_max_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "filter:work_type")
async def show_work_type_filter(callback: CallbackQuery) -> None:
    await callback.message.answer("Выбери вид работы:", reply_markup=work_type_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("set_country:"))
async def set_country(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    country_key = callback.data.split(":", 1)[1]
    country_area, title = COUNTRY_OPTIONS.get(country_key, (None, "Любая страна"))

    filters = get_user_filters(user_id)
    filters.country_key = country_key if country_key in COUNTRY_OPTIONS else None
    filters.country_area = country_area
    filters.city_area = None
    upsert_user_filters(user_id, filters)

    await callback.message.answer(f"Фильтр страны обновлён: {title}", reply_markup=post_filter_keyboard())
    await callback.answer("Готово")


@dp.callback_query(F.data.startswith("set_city:"))
async def set_city(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    city_key = callback.data.split(":", 1)[1]

    filters = get_user_filters(user_id)
    options = CITY_OPTIONS_BY_COUNTRY.get(filters.country_key or "", {"any": (None, "Любой город")})
    city_area, title = options.get(city_key, (None, "Любой город"))

    filters.city_area = city_area
    upsert_user_filters(user_id, filters)

    await callback.message.answer(f"Фильтр города обновлён: {title}", reply_markup=post_filter_keyboard())
    await callback.answer("Готово")


@dp.callback_query(F.data.startswith("set_salary_min:"))
async def set_salary_min(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    salary_key = callback.data.split(":", 1)[1]
    salary_min, title = SALARY_MIN_OPTIONS.get(salary_key, (DEFAULT_SALARY_MIN, "От 70 000"))

    filters = get_user_filters(user_id)
    filters.salary_min = salary_min
    if filters.salary_min > filters.salary_max:
        filters.salary_max = filters.salary_min
    upsert_user_filters(user_id, filters)

    await callback.message.answer(f"Минимальная зарплата обновлена: {title}", reply_markup=post_filter_keyboard())
    await callback.answer("Готово")


@dp.callback_query(F.data.startswith("set_salary_max:"))
async def set_salary_max(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    salary_key = callback.data.split(":", 1)[1]
    salary_max, title = SALARY_MAX_OPTIONS.get(salary_key, (DEFAULT_SALARY_MAX, "До 150 000"))

    filters = get_user_filters(user_id)
    filters.salary_max = salary_max
    if filters.salary_max < filters.salary_min:
        filters.salary_min = filters.salary_max
    upsert_user_filters(user_id, filters)

    await callback.message.answer(f"Максимальная зарплата обновлена: {title}", reply_markup=post_filter_keyboard())
    await callback.answer("Готово")


@dp.callback_query(F.data.startswith("set_work_type:"))
async def set_work_type(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    work_type = callback.data.split(":", 1)[1]

    filters = get_user_filters(user_id)
    filters.work_type = work_type if work_type in WORK_TYPE_OPTIONS else "any"
    upsert_user_filters(user_id, filters)

    await callback.message.answer(
        f"Фильтр вида работы обновлён: {WORK_TYPE_OPTIONS.get(filters.work_type, 'Любой формат')}",
        reply_markup=post_filter_keyboard(),
    )
    await callback.answer("Готово")


@dp.callback_query(F.data.startswith("save:"))
async def save_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    vacancy_id = callback.data.split(":", 1)[1]
    vacancy = last_results.get(user_id, {}).get(vacancy_id)

    if not vacancy:
        await callback.answer("Вакансия устарела, сделай новый поиск", show_alert=True)
        return

    created = save_vacancy(user_id, vacancy)
    await callback.answer("Вакансия сохранена" if created else "Уже в сохранённых")


@dp.message(Command("saved"))
async def saved_handler(message: Message) -> None:
    user_id = message.from_user.id
    items = get_saved_vacancies(user_id)
    if not items:
        await message.answer("Список сохранённых вакансий пуст.")
        return

    text = ["<b>Сохранённые вакансии:</b>"]
    for row in items:
        text.append(f"\n• <b>{row['title']}</b> — {row['company']}\n{row['url']}")
    await message.answer("\n".join(text), parse_mode="HTML")


@dp.message(F.text)
async def search_handler(message: Message) -> None:
    user_id = message.from_user.id
    query = message.text.strip()
    await send_search_results(message, user_id, query)


async def main() -> None:
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())