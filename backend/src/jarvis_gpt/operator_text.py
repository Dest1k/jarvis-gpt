"""Operator text normalization: confusables, keyboard layout, typos, garbage.

Used before intent detection so phrasing quirks (ё/е, NBSP, wrong layout,
light typos, and "face on keyboard" noise) do not defeat command recognition.
"""

from __future__ import annotations

import re
import unicodedata

# Same-meaning character variants operators commonly type.
_OPERATOR_CONFUSABLE_TRANSLATION: dict[int, str | None] = {
    0x00A0: " ",
    0x2000: " ",
    0x2001: " ",
    0x2002: " ",
    0x2003: " ",
    0x2004: " ",
    0x2005: " ",
    0x2006: " ",
    0x2007: " ",
    0x2008: " ",
    0x2009: " ",
    0x200A: " ",
    0x202F: " ",
    0x205F: " ",
    0x3000: " ",
    0x200B: None,
    0x200C: None,
    0x200D: None,
    0x2060: None,
    0xFEFF: None,
    0x0451: "е",
    0x0401: "Е",
}

# QWERTY physical keys → ЙЦУКЕН (and reverse) for common "wrong layout" paste/type.
_EN_TO_RU = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
    "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё",
)
_RU_TO_EN = str.maketrans(
    "йцукенгшщзхъфывапролджэячсмитьбю.ёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё",
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?~",
)

_CYR = re.compile(r"[а-яА-ЯёЁ]")
_LAT = re.compile(r"[a-zA-Z]")
_WORD = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]{2,}")
_GARBAGE_RUN = re.compile(r"[^\w\s\-.,!?/:@#%&+=а-яА-ЯёЁ]{3,}", re.UNICODE)
_REPEATED = re.compile(r"(.)\1{4,}")


def fold_operator_confusables(text: str) -> str:
    """Fold same-meaning character variants (NBSP, ZW*, ё/е)."""

    folded = unicodedata.normalize("NFC", str(text or ""))
    return folded.translate(_OPERATOR_CONFUSABLE_TRANSLATION)


def _layout_score(text: str) -> tuple[int, int]:
    return len(_CYR.findall(text)), len(_LAT.findall(text))


# Verb stems: confirm a wrong-layout flip produced a real command, and also
# protect intentional bilingual phrases ("открой Microsoft Edge") from being
# whole-message-translated. Nouns like "файл"/"file" alone must NOT block flip
# of "jnrhjq файл" → "открой файл".
_RU_VERB_STEMS = (
    "открой",
    "открыть",
    "закрой",
    "закрыть",
    "найди",
    "найти",
    "поищи",
    "поискать",
    "сделай",
    "сделать",
    "покажи",
    "показать",
    "запусти",
    "запустить",
    "создай",
    "создать",
    "удали",
    "удалить",
    "сохрани",
    "сохранить",
    "напиши",
    "написать",
    "прочитай",
    "прочитать",
    "скачай",
    "скачать",
    "отправь",
    "отправить",
    "выключи",
    "выключить",
    "включи",
    "включить",
    "перезапусти",
    "перезапустить",
    "распакуй",
    "распаковать",
    "упакуй",
    "упаковать",
    "напомн",  # напомни / напомнить
    "скажи",
    "сказать",
    "ответь",
    "ответить",
    "проверь",
    "проверить",
    "поставь",
    "поставить",
    "переимен",
    "переложи",
    "переложи",
    "скопируй",
    "скопировать",
    "перемести",
    "переместить",
    "вставь",
    "вставить",
    "скопир",
    "сгенерируй",
    "сгенерировать",
    "подготовь",
    "подготовить",
    "останови",
    "остановить",
    "продолжи",
    "продолжить",
    "отмени",
    "отменить",
    "подтверди",
    "подтвердить",
    "обнови",
    "обновить",
    "установи",
    "установить",
    "переведи",
    "перевести",
    "суммируй",
    "кратко",
    "запиши",
    "записать",
    "добавь",
    "добавить",
    "убери",
    "убрать",
    "очисти",
    "очистить",
    "перейди",
    "перейти",
    "зайди",
    "зайти",
    "посмотри",
    "посмотреть",
    "глянь",
    "глянуть",
    "проанализируй",
    "проанализировать",
    "разбери",
    "разобрать",
    "сравни",
    "сравнить",
    "посчитай",
    "посчитать",
    "вычисли",
    "вычислить",
    "отправ",
    "загрузи",
    "загрузить",
    "выгрузи",
    "выгрузить",
    "подключи",
    "подключить",
    "отключи",
    "отключить",
    "переключи",
    "переключить",
    "включи",
    "вырежи",
    "вырезать",
    "вставь",
    "вклеить",
)
_EN_VERB_STEMS = (
    "open",
    "close",
    "find",
    "search",
    "make",
    "show",
    "start",
    "launch",
    "create",
    "delete",
    "remove",
    "save",
    "write",
    "read",
    "download",
    "upload",
    "send",
    "extract",
    "pack",
    "unpack",
    "restart",
    "reboot",
    "stop",
    "kill",
    "run",
    "execute",
    "remind",
    "check",
    "verify",
    "set",
    "enable",
    "disable",
    "answer",
    "tell",
    "say",
    "copy",
    "move",
    "rename",
    "paste",
    "cut",
    "generate",
    "prepare",
    "cancel",
    "confirm",
    "update",
    "install",
    "translate",
    "summarize",
    "summarise",
    "add",
    "clear",
    "go",
    "look",
    "watch",
    "analyze",
    "analyse",
    "compare",
    "compute",
    "calculate",
    "connect",
    "disconnect",
    "switch",
    "focus",
    "type",
    "click",
    "press",
    "capture",
    "screenshot",
    "list",
    "print",
    "export",
    "import",
    "share",
    "forward",
    "reply",
    "snooze",
    "ack",
    "acknowledge",
)
# Full stem set used to validate that a flipped candidate looks like intent text.
_RU_COMMAND_STEMS = _RU_VERB_STEMS + (
    "память",
    "файл",
    "папку",
    "папка",
    "файлы",
    "папки",
    "статус",
    "помощь",
    "справка",
    "миссию",
    "миссия",
    "документ",
    "документы",
    "архив",
    "архивы",
    "через",
    "минут",
    "час",
    "секун",
    "недел",
    "дня",
    "день",
    "утром",
    "вечером",
    "ночью",
    "таблиц",
    "отчёт",
    "отчет",
    "сводк",
    "дайджест",
    "напоминан",
    "экран",
    "окно",
    "окна",
    "процесс",
    "процессы",
    "консоль",
    "терминал",
    "команд",
    "браузер",
    "вкладк",
    "страниц",
    "сайт",
    "ссылк",
    "картинк",
    "фото",
    "видео",
    "голос",
    "сообщен",
    "телеграм",
    "джарвис",
    "jarvis",
    "gpu",
    "cpu",
    "vram",
    "температур",
    "загрузк",
    "диспетчер",
    "профил",
    "модел",
)
_EN_COMMAND_STEMS = _EN_VERB_STEMS + (
    "memory",
    "file",
    "files",
    "folder",
    "folders",
    "directory",
    "status",
    "help",
    "mission",
    "document",
    "documents",
    "archive",
    "archives",
    "minute",
    "minutes",
    "hour",
    "hours",
    "second",
    "seconds",
    "week",
    "weeks",
    "day",
    "days",
    "morning",
    "evening",
    "night",
    "table",
    "report",
    "briefing",
    "digest",
    "reminder",
    "reminders",
    "screen",
    "window",
    "windows",
    "process",
    "processes",
    "console",
    "terminal",
    "command",
    "browser",
    "tab",
    "page",
    "site",
    "link",
    "image",
    "photo",
    "video",
    "voice",
    "message",
    "telegram",
    "jarvis",
    "gpu",
    "cpu",
    "vram",
    "temperature",
    "load",
    "dispatcher",
    "profile",
    "model",
    "calculator",
    "notepad",
    "edge",
    "chrome",
    "firefox",
    "excel",
    "word",
    "powerpoint",
    "pdf",
    "docx",
    "xlsx",
    "clipboard",
    "buffer",
)

# High-frequency + operator-domain words that signal "this token is real language
# after flip", including conversational prose (not only imperative commands).
# Also used as the fuzzy-typo correction dictionary.
_RU_WORDS = frozenset(
    {
        # discourse / function
        "если",
        "вот",
        "так",
        "что",
        "как",
        "мне",
        "меня",
        "тебе",
        "тебя",
        "нам",
        "вас",
        "вам",
        "его",
        "её",
        "ее",
        "их",
        "сейчас",
        "потом",
        "сразу",
        "быстро",
        "медленно",
        "можно",
        "нужно",
        "надо",
        "пожалуйста",
        "спасибо",
        "просто",
        "внезапно",
        "случайно",
        "нарочно",
        "писать",
        "пишу",
        "пишет",
        "начнет",
        "начнут",
        "начнёт",
        "начать",
        "начни",
        "начн",
        "закончи",
        "закончить",
        "через",
        "минут",
        "минуту",
        "минуты",
        "минутка",
        "часик",
        "час",
        "часа",
        "часов",
        "секунду",
        "секунды",
        "секунд",
        "неделю",
        "недели",
        "недель",
        "день",
        "дня",
        "дней",
        "сутки",
        "сегодня",
        "завтра",
        "послезавтра",
        "вчера",
        "утром",
        "днем",
        "днём",
        "вечером",
        "ночью",
        "и",
        "в",
        "на",
        "не",
        "да",
        "нет",
        "или",
        "для",
        "это",
        "эта",
        "этот",
        "эти",
        "всё",
        "все",
        "уже",
        "ещё",
        "еще",
        "только",
        "когда",
        "где",
        "кто",
        "почему",
        "зачем",
        "потому",
        "очень",
        "будет",
        "была",
        "было",
        "были",
        "есть",
        "был",
        "будут",
        "могу",
        "может",
        "можем",
        "хочу",
        "хочешь",
        "хочет",
        "давай",
        "давайте",
        "ладно",
        "хорошо",
        "плохо",
        "ок",
        "окей",
        "стоп",
        "хватит",
        "отмена",
        "отменить",
        "подтверди",
        "подтвердить",
        "без",
        "под",
        "над",
        "при",
        "про",
        "из",
        "от",
        "до",
        "по",
        "со",
        "об",
        "к",
        "у",
        "с",
        "о",
        "же",
        "ли",
        "бы",
        "то",
        "ни",
        "там",
        "тут",
        "здесь",
        "туда",
        "сюда",
        "снова",
        "опять",
        # core operator verbs (full forms)
        "напомни",
        "напомнить",
        "напоминание",
        "напоминания",
        "открой",
        "открыть",
        "закрой",
        "закрыть",
        "запусти",
        "запустить",
        "покажи",
        "показать",
        "сделай",
        "сделать",
        "скажи",
        "сказать",
        "ответь",
        "ответить",
        "проверь",
        "проверить",
        "найди",
        "найти",
        "поищи",
        "поискать",
        "сохрани",
        "сохранить",
        "удали",
        "удалить",
        "создай",
        "создать",
        "напиши",
        "написать",
        "прочитай",
        "прочитать",
        "скачай",
        "скачать",
        "отправь",
        "отправить",
        "выключи",
        "выключить",
        "включи",
        "включить",
        "перезапусти",
        "перезапустить",
        "перезагрузка",
        "перезагрузить",
        "распакуй",
        "распаковать",
        "упакуй",
        "упаковать",
        "поставь",
        "поставить",
        "переименуй",
        "переименовать",
        "переложи",
        "переложить",
        "скопируй",
        "скопировать",
        "перемести",
        "переместить",
        "вставь",
        "вставить",
        "вырежи",
        "вырезать",
        "сгенерируй",
        "сгенерировать",
        "подготовь",
        "подготовить",
        "останови",
        "остановить",
        "продолжи",
        "продолжить",
        "обнови",
        "обновить",
        "установи",
        "установить",
        "переведи",
        "перевести",
        "перевод",
        "суммируй",
        "суммировать",
        "кратко",
        "краткий",
        "запиши",
        "записать",
        "добавь",
        "добавить",
        "убери",
        "убрать",
        "очисти",
        "очистить",
        "перейди",
        "перейти",
        "зайди",
        "зайти",
        "посмотри",
        "посмотреть",
        "глянь",
        "погляди",
        "проанализируй",
        "проанализировать",
        "анализ",
        "разбери",
        "разобрать",
        "сравни",
        "сравнить",
        "посчитай",
        "посчитать",
        "вычисли",
        "вычислить",
        "загрузи",
        "загрузить",
        "выгрузи",
        "выгрузить",
        "подключи",
        "подключить",
        "отключи",
        "отключить",
        "переключи",
        "переключить",
        "набери",
        "набрать",
        "введи",
        "ввести",
        "кликни",
        "кликнуть",
        "нажми",
        "нажать",
        "сними",
        "снять",
        "скриншот",
        "скрин",
        "список",
        "перечисли",
        "перечислить",
        # nouns / domains
        "файл",
        "файла",
        "файлы",
        "файлов",
        "папка",
        "папку",
        "папки",
        "папок",
        "каталог",
        "каталога",
        "путь",
        "пути",
        "диск",
        "диска",
        "статус",
        "состояние",
        "здоровье",
        "помощь",
        "справка",
        "хелп",
        "документ",
        "документа",
        "документы",
        "документов",
        "архив",
        "архива",
        "архивы",
        "память",
        "памяти",
        "озу",
        "система",
        "системы",
        "сервер",
        "сервера",
        "модель",
        "модели",
        "профиль",
        "профиля",
        "профили",
        "чат",
        "чата",
        "сообщение",
        "сообщения",
        "сообщений",
        "задача",
        "задачи",
        "задач",
        "миссия",
        "миссии",
        "миссию",
        "план",
        "плана",
        "отчёт",
        "отчет",
        "отчёта",
        "отчета",
        "сводка",
        "сводку",
        "сводки",
        "дайджест",
        "таблица",
        "таблицу",
        "таблицы",
        "калькулятор",
        "блокнот",
        "браузер",
        "браузере",
        "вкладка",
        "вкладку",
        "вкладки",
        "страница",
        "страницу",
        "страницы",
        "сайт",
        "сайта",
        "ссылка",
        "ссылку",
        "ссылки",
        "картинка",
        "картинку",
        "изображение",
        "фото",
        "видео",
        "голос",
        "аудио",
        "телеграм",
        "телеграмма",
        "джарвис",
        "jarvis",
        "экран",
        "экрана",
        "экране",
        "окно",
        "окна",
        "окон",
        "процесс",
        "процесса",
        "процессы",
        "консоль",
        "терминал",
        "команда",
        "команды",
        "powershell",
        "cmd",
        "буфер",
        "клипборд",
        "обмен",
        "gpu",
        "cpu",
        "vram",
        "видеокарта",
        "видеокарты",
        "процессор",
        "процессора",
        "температура",
        "температуру",
        "загрузка",
        "загрузки",
        "нагрузка",
        "нагрузки",
        "диспетчер",
        "диспетчера",
        "квант",
        "токен",
        "токены",
        "промпт",
        "инструмент",
        "инструменты",
        "тул",
        "тулы",
        "агент",
        "рантайм",
        "runtime",
        "бэкап",
        "бэкапа",
        "резерв",
        "копия",
        "копию",
        "копии",
        "лог",
        "логи",
        "логов",
        "ошибка",
        "ошибки",
        "ошибок",
        "баг",
        "баги",
        "фикс",
        "починка",
        "почини",
        "починить",
        "почисть",
        "очистка",
        "очередь",
        "очереди",
        "фоне",
        "фоновый",
        "фоновая",
        "тихий",
        "тихие",
        "тишина",
        "quiet",
        "кнопка",
        "кнопки",
        "клавиатура",
        "мышь",
        "курсор",
        "фокус",
        "рабочий",
        "рабочий стол",
        "рабочийстол",
        "десктоп",
        "ноутбук",
        "машина",
        "машины",
        "пк",
        "комп",
        "компьютер",
        "компьютера",
        "винда",
        "windows",
        "linux",
        "docker",
        "контейнер",
        "контейнеры",
        "порт",
        "порты",
        "хост",
        "хоста",
        "мост",
        "bridge",
        "сеть",
        "сети",
        "интернет",
        "онлайн",
        "офлайн",
        "поиск",
        "найти",
        "гугл",
        "google",
        "яндекс",
        "yandex",
        "курс",
        "валюта",
        "доллар",
        "рубль",
        "евро",
        "погода",
        "новости",
        "новость",
        "цена",
        "цены",
        "купить",
        "магазин",
        "товар",
        "товары",
        "pdf",
        "docx",
        "xlsx",
        "csv",
        "json",
        "md",
        "markdown",
        "текст",
        "текста",
        "строка",
        "строки",
        "абзац",
        "заголовок",
        "список",
        "маркер",
        "формула",
        "формулы",
        "ячейка",
        "ячейки",
        "лист",
        "листы",
        "книга",
        "книги",
        "ворд",
        "word",
        "эксель",
        "excel",
        "powerpoint",
        "презентац",
        "слайд",
        "слайды",
        "zip",
        "rar",
        "7z",
        "пароль",
        "пароля",
        "шифр",
        "шифрование",
        "доступ",
        "права",
        "гость",
        "гости",
        "владелец",
        "админ",
        "администратор",
        "оператор",
        "пользователь",
        "юзер",
        "сессия",
        "сессии",
        "токен",
        "ключ",
        "секреты",
        "секрет",
        "настройк",
        "настройки",
        "конфиг",
        "конфигурация",
        "версия",
        "версии",
        "обновление",
        "обновления",
        "релиз",
        "билд",
        "сборка",
        "тест",
        "тесты",
        "прогон",
        "smoke",
        "аудит",
        "проверка",
        "проверки",
        "результат",
        "результаты",
        "итог",
        "итоги",
        "готово",
        "готов",
        "готова",
        "ошибся",
        "неверно",
        "верно",
        "правильно",
        "неправильно",
        "срочно",
        "важно",
        "важное",
        "позже",
        "немедленно",
        "коротко",
        "подробно",
        "детальнее",
        "детали",
        "контекст",
        "история",
        "диалог",
        "диалога",
        "разговор",
        "ответ",
        "ответа",
        "вопрос",
        "вопроса",
        "уточни",
        "уточнить",
        "поясни",
        "пояснить",
        "объясни",
        "объяснить",
        "расскажи",
        "рассказать",
        "опиши",
        "описать",
        "формат",
        "формате",
        "markdown",
        "одной",
        "одним",
        "слово",
        "словом",
        "фраза",
        "фразой",
        "предложением",
        "предложение",
        "раскладка",
        "раскладки",
        "опечатка",
        "опечатки",
        "очепятка",
        "очепятки",
        "ошибся",
        "перепутал",
        "перепутала",
    }
)
_EN_WORDS = frozenset(
    {
        # discourse / function
        "if",
        "then",
        "this",
        "that",
        "these",
        "those",
        "with",
        "from",
        "what",
        "when",
        "where",
        "why",
        "how",
        "who",
        "which",
        "please",
        "thanks",
        "thank",
        "just",
        "now",
        "later",
        "soon",
        "quickly",
        "slowly",
        "maybe",
        "perhaps",
        "really",
        "very",
        "also",
        "only",
        "again",
        "still",
        "already",
        "here",
        "there",
        "the",
        "and",
        "or",
        "not",
        "yes",
        "no",
        "ok",
        "okay",
        "for",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "can",
        "could",
        "will",
        "would",
        "should",
        "shall",
        "may",
        "might",
        "must",
        "about",
        "into",
        "over",
        "under",
        "after",
        "before",
        "between",
        "without",
        "within",
        "because",
        "while",
        "during",
        "until",
        "since",
        "than",
        "too",
        "more",
        "most",
        "less",
        "least",
        "all",
        "any",
        "some",
        "each",
        "every",
        "both",
        "few",
        "many",
        "much",
        "other",
        "another",
        "same",
        "such",
        "own",
        "my",
        "your",
        "his",
        "her",
        "its",
        "our",
        "their",
        "me",
        "you",
        "him",
        "us",
        "them",
        "i",
        "we",
        "they",
        "he",
        "she",
        "it",
        "do",
        "does",
        "did",
        "done",
        "doing",
        "have",
        "has",
        "had",
        "having",
        "get",
        "got",
        "give",
        "take",
        "put",
        "let",
        "keep",
        "need",
        "want",
        "try",
        "use",
        "using",
        "used",
        # core verbs
        "open",
        "close",
        "start",
        "launch",
        "stop",
        "kill",
        "run",
        "execute",
        "show",
        "make",
        "create",
        "delete",
        "remove",
        "save",
        "write",
        "read",
        "find",
        "search",
        "send",
        "download",
        "upload",
        "extract",
        "pack",
        "unpack",
        "restart",
        "reboot",
        "check",
        "verify",
        "remind",
        "set",
        "enable",
        "disable",
        "answer",
        "tell",
        "say",
        "copy",
        "move",
        "rename",
        "paste",
        "cut",
        "generate",
        "prepare",
        "cancel",
        "confirm",
        "update",
        "install",
        "translate",
        "summarize",
        "summarise",
        "add",
        "clear",
        "go",
        "look",
        "watch",
        "see",
        "analyze",
        "analyse",
        "compare",
        "compute",
        "calculate",
        "connect",
        "disconnect",
        "switch",
        "focus",
        "type",
        "click",
        "press",
        "capture",
        "screenshot",
        "list",
        "print",
        "export",
        "import",
        "share",
        "forward",
        "reply",
        "snooze",
        "ack",
        "acknowledge",
        "fix",
        "repair",
        "debug",
        "test",
        "build",
        "deploy",
        "commit",
        "push",
        "pull",
        "merge",
        "rebase",
        "branch",
        "clone",
        "fetch",
        "log",
        "logs",
        "trace",
        "profile",
        "profiles",
        # nouns / domains
        "status",
        "help",
        "file",
        "files",
        "folder",
        "folders",
        "directory",
        "directories",
        "path",
        "paths",
        "disk",
        "drive",
        "memory",
        "ram",
        "document",
        "documents",
        "archive",
        "archives",
        "mission",
        "missions",
        "task",
        "tasks",
        "plan",
        "plans",
        "report",
        "reports",
        "briefing",
        "digest",
        "reminder",
        "reminders",
        "table",
        "tables",
        "sheet",
        "sheets",
        "workbook",
        "calculator",
        "calc",
        "notepad",
        "browser",
        "tab",
        "tabs",
        "page",
        "pages",
        "site",
        "sites",
        "link",
        "links",
        "url",
        "image",
        "images",
        "photo",
        "photos",
        "video",
        "videos",
        "voice",
        "audio",
        "message",
        "messages",
        "chat",
        "chats",
        "telegram",
        "jarvis",
        "screen",
        "screens",
        "window",
        "windows",
        "process",
        "processes",
        "console",
        "terminal",
        "shell",
        "powershell",
        "cmd",
        "command",
        "commands",
        "clipboard",
        "buffer",
        "gpu",
        "cpu",
        "vram",
        "temperature",
        "temp",
        "load",
        "usage",
        "dispatcher",
        "model",
        "models",
        "token",
        "tokens",
        "prompt",
        "prompts",
        "tool",
        "tools",
        "agent",
        "agents",
        "runtime",
        "backup",
        "backups",
        "copy",
        "copies",
        "error",
        "errors",
        "bug",
        "bugs",
        "queue",
        "background",
        "quiet",
        "button",
        "buttons",
        "keyboard",
        "mouse",
        "cursor",
        "desktop",
        "laptop",
        "machine",
        "pc",
        "computer",
        "host",
        "hosts",
        "bridge",
        "network",
        "internet",
        "online",
        "offline",
        "search",
        "google",
        "yandex",
        "weather",
        "news",
        "price",
        "prices",
        "shop",
        "store",
        "buy",
        "pdf",
        "docx",
        "xlsx",
        "csv",
        "json",
        "markdown",
        "text",
        "line",
        "lines",
        "paragraph",
        "heading",
        "list",
        "formula",
        "cell",
        "cells",
        "word",
        "excel",
        "powerpoint",
        "slide",
        "slides",
        "presentation",
        "zip",
        "rar",
        "password",
        "passwords",
        "encrypt",
        "encryption",
        "access",
        "permission",
        "permissions",
        "guest",
        "owner",
        "admin",
        "administrator",
        "operator",
        "user",
        "users",
        "session",
        "sessions",
        "key",
        "keys",
        "secret",
        "secrets",
        "setting",
        "settings",
        "config",
        "configuration",
        "version",
        "versions",
        "update",
        "updates",
        "release",
        "build",
        "test",
        "tests",
        "smoke",
        "audit",
        "check",
        "result",
        "results",
        "summary",
        "done",
        "ready",
        "wrong",
        "right",
        "correct",
        "incorrect",
        "urgent",
        "important",
        "short",
        "brief",
        "detailed",
        "details",
        "context",
        "history",
        "dialog",
        "dialogue",
        "conversation",
        "answer",
        "question",
        "clarify",
        "explain",
        "describe",
        "format",
        "word",
        "phrase",
        "sentence",
        "minutes",
        "minute",
        "hours",
        "hour",
        "seconds",
        "second",
        "today",
        "tomorrow",
        "yesterday",
        "morning",
        "evening",
        "night",
        "week",
        "weeks",
        "day",
        "days",
        "edge",
        "chrome",
        "firefox",
        "docker",
        "container",
        "containers",
        "port",
        "ports",
        "linux",
        "system",
        "server",
        "servers",
        "health",
        "state",
        "ok",
        "fail",
        "failed",
        "success",
        "successful",
        "please",
        "asap",
        "typo",
        "typos",
        "layout",
        "keyboard",
    }
)

# Full-form lexicon used for fuzzy typo repair (same-layout mistypes like
# «отркой» → «открой», «напонми» → «напомни»). Stems alone are too short/noisy.
_TYPO_CANDIDATES_RU: tuple[str, ...] = tuple(
    sorted({w for w in _RU_WORDS if len(w) >= 4})
)
_TYPO_CANDIDATES_EN: tuple[str, ...] = tuple(
    sorted({w for w in _EN_WORDS if len(w) >= 4})
)

_LAT_WORD = re.compile(r"^[A-Za-z]+$")
_CYR_WORD = re.compile(r"^[А-Яа-яЁё]+$")
_TOKEN_PARTS = re.compile(r"[A-Za-zА-Яа-яЁё]+|[^A-Za-zА-Яа-яЁё]+")


def _contains_stem(text: str, stems: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(stem in folded for stem in stems)


def _fold_word(word: str) -> str:
    return word.casefold().replace("ё", "е")


def _looks_russian_word(word: str) -> bool:
    w = _fold_word(word)
    if len(w) < 2:
        return False
    if w in _RU_WORDS:
        return True
    for stem in _RU_COMMAND_STEMS:
        if w == stem or w.startswith(stem):
            return True
        # Accept short prefixes of longer stems ("мину" ≈ "минут") when typing cut off.
        if len(w) >= 4 and len(stem) >= 4 and stem.startswith(w):
            return True
    return False


def _looks_english_word(word: str) -> bool:
    w = word.casefold()
    if len(w) < 2:
        return False
    if w in _EN_WORDS:
        return True
    for stem in _EN_COMMAND_STEMS:
        if w == stem or w.startswith(stem):
            return True
        if len(w) >= 4 and len(stem) >= 4 and stem.startswith(w):
            return True
    return False


def _preserve_case(source: str, replacement: str) -> str:
    """Apply a dictionary hit while roughly keeping the operator's casing."""

    if not source or not replacement:
        return replacement
    if source.isupper():
        return replacement.upper()
    if source[0].isupper() and source[1:].islower():
        return replacement[:1].upper() + replacement[1:]
    if source.islower():
        return replacement.lower()
    return replacement


def correct_operator_typos(text: str) -> str:
    """Fuzzy-fix same-layout typos against the operator lexicon.

    Only rewrites a token when there is a **unique** dictionary winner within the
    length-scaled Levenshtein budget. Tokens that already match the lexicon (or
    look like paths/ids) are left alone.
    """

    raw = fold_operator_confusables(text)
    if not raw.strip():
        return raw
    if re.search(r"https?://|\\\\|[A-Za-z]:\\", raw):
        return raw

    parts = _TOKEN_PARTS.findall(raw)
    if not parts:
        return raw
    out: list[str] = []
    changed = False
    for part in parts:
        if _CYR_WORD.fullmatch(part) and len(part) >= 4:
            folded = _fold_word(part)
            # Exact lexicon hit — leave alone (no second-guessing real words).
            if folded in _RU_WORDS:
                out.append(part)
                continue
            hit = fuzzy_token_match(folded, list(_TYPO_CANDIDATES_RU))
            if hit and hit != folded:
                out.append(_preserve_case(part, hit))
                changed = True
                continue
        elif _LAT_WORD.fullmatch(part) and len(part) >= 4:
            folded = part.casefold()
            if folded in _EN_WORDS:
                out.append(part)
                continue
            hit = fuzzy_token_match(folded, list(_TYPO_CANDIDATES_EN))
            if hit and hit != folded:
                out.append(_preserve_case(part, hit))
                changed = True
                continue
        out.append(part)
    return "".join(out) if changed else raw


def _tokenwise_layout_flip(raw: str) -> str | None:
    """Flip pure-script tokens that become real words in the other layout.

    Keeps intentional mixed phrases intact: ``jnrhjq Microsoft Edge`` →
    ``открой Microsoft Edge`` (only the mistyped verb flips).
    """

    parts = _TOKEN_PARTS.findall(raw)
    if not parts:
        return None
    out: list[str] = []
    flipped_n = 0
    signal = False
    for part in parts:
        if _LAT_WORD.fullmatch(part):
            cand = part.translate(_EN_TO_RU)
            if _looks_russian_word(cand) and not _looks_english_word(part):
                out.append(cand)
                flipped_n += 1
                if _looks_russian_word(cand):
                    signal = True
                continue
        elif _CYR_WORD.fullmatch(part):
            cand = part.translate(_RU_TO_EN)
            if _looks_english_word(cand) and not _looks_russian_word(part):
                out.append(cand)
                flipped_n += 1
                signal = True
                continue
        out.append(part)
    if flipped_n == 0 or not signal:
        return None
    return "".join(out)


def try_layout_flip(text: str) -> str:
    """If the message looks typed in the wrong layout, return the flipped form.

    Handles both directions:
    - Russian typed on EN keys: ``tckb …`` / ``jnrhjq файл`` / ``yfgjvyb xthtp 5 vbye``
    - English typed on RU keys: ``щзут`` / ``ыефегы``

    Prefer **per-token** flip so intentional bilingual commands
    (``открой Microsoft Edge``, ``jnrhjq Microsoft Edge``) keep real English
    app names instead of whole-message gibberish.
    """

    raw = fold_operator_confusables(text)
    if not raw.strip():
        return raw
    # Do not flip paths / URLs / absolute Windows paths.
    if re.search(r"https?://|\\\\|[A-Za-z]:\\", raw):
        return raw

    tokenwise = _tokenwise_layout_flip(raw)
    if tokenwise is not None:
        return tokenwise

    # Whole-message fallback for pure wrong-layout runs that token lexicon missed.
    cyr, lat = _layout_score(raw)
    if lat >= 3 and lat > cyr * 2:
        if _contains_stem(raw, _RU_VERB_STEMS) or _contains_stem(raw, _EN_COMMAND_STEMS):
            return raw
        flipped = raw.translate(_EN_TO_RU)
        if _contains_stem(flipped, _RU_COMMAND_STEMS) and not _contains_stem(
            raw, _EN_COMMAND_STEMS
        ):
            return flipped
    if cyr >= 3 and cyr > lat * 2:
        if _contains_stem(raw, _EN_VERB_STEMS) or _contains_stem(raw, _RU_COMMAND_STEMS):
            return raw
        flipped = raw.translate(_RU_TO_EN)
        if _contains_stem(flipped, _EN_COMMAND_STEMS) and not _contains_stem(
            raw, _RU_COMMAND_STEMS
        ):
            return flipped
    return raw


def operator_message_candidates(text: str) -> list[str]:
    """Return unique normalized candidates for intent matching (original + layout)."""

    base = fold_operator_confusables(text)
    scrubbed = scrub_keyboard_smash(base)
    flipped = try_layout_flip(scrubbed)
    candidates: list[str] = []
    for item in (scrubbed, flipped, normalize_operator_message(text)):
        cleaned = re.sub(r"\s+", " ", item).strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    # Always include confusable-only fold for exact legacy matchers.
    folded_only = re.sub(r"\s+", " ", base).strip()
    if folded_only and folded_only not in candidates:
        candidates.insert(0, folded_only)
    return candidates


def scrub_keyboard_smash(text: str) -> str:
    """Reduce 'face on keyboard' noise while keeping word islands.

    Strips long non-word runs and collapses extreme character repetition, then
    keeps tokens that look like real words (length ≥ 2).
    """

    raw = fold_operator_confusables(text)
    raw = _GARBAGE_RUN.sub(" ", raw)
    raw = _REPEATED.sub(r"\1\1", raw)
    tokens = _WORD.findall(raw)
    if not tokens:
        return raw.strip()
    # If almost everything was garbage, reassemble only the word islands.
    if len("".join(tokens)) < max(4, int(len(re.sub(r"\s+", "", raw)) * 0.35)):
        return " ".join(tokens)
    return re.sub(r"\s+", " ", raw).strip()


def levenshtein(a: str, b: str, *, limit: int = 4) -> int:
    """Bounded Damerau–Levenshtein distance (insert/delete/sub/adjacent swap).

    Returns limit+1 when already worse so callers can prune early.
    """

    a = a.casefold()
    b = b.casefold()
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    if not a:
        return len(b)
    if not b:
        return len(a)
    # prev2/prev/cur rows for transposition (da[i-2][j-2]).
    prev2 = list(range(len(b) + 1))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, start=1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            val = min(ins, delete, sub)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                val = min(val, prev2[j - 2] + 1)
            cur.append(val)
            if val < row_min:
                row_min = val
        if row_min > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def fuzzy_token_match(
    token: str, candidates: list[str], *, max_dist: int | None = None
) -> str | None:
    """Return the unique closest candidate within a length-scaled edit budget."""

    token = (token or "").strip().casefold()
    if not token or len(token) < 3:
        return None
    if max_dist is not None:
        budget = max_dist
    elif len(token) <= 4:
        # Short tokens: allow one adjacent transposition ("opne"→"open") but not
        # multi-edit rewrites that collide with many dictionary neighbors.
        budget = 1
    elif len(token) <= 7:
        budget = 2
    else:
        budget = 3
    best: list[tuple[int, str]] = []
    for cand in candidates:
        d = levenshtein(token, cand, limit=budget)
        if d <= budget:
            best.append((d, cand))
    if not best:
        return None
    best.sort()
    # Require unique winner at the best distance.
    winners = [c for d, c in best if d == best[0][0]]
    if len(winners) != 1:
        return None
    return winners[0]


def normalize_operator_message(text: str) -> str:
    """Full normalization pipeline for operator-facing matching."""

    step = fold_operator_confusables(text)
    step = scrub_keyboard_smash(step)
    step = try_layout_flip(step)
    step = fold_operator_confusables(step)  # after layout flip, ё may reappear
    step = correct_operator_typos(step)
    return re.sub(r"\s+", " ", step).strip()
