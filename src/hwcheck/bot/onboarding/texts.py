"""Тексты и клавиатуры онбординга (спецификация §4, §4.7, §10.3). Логика — в шагах пакета.

Payload кнопок начинается с `ob:`: по нему маршрутизатор забирает нажатие у сценария проверки.
Payload недоверенный — разбирает и проверяет его router.py.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from hwcheck.bot.invites import InviteKind
from hwcheck.bot.max_api import Buttons, callback_button
from hwcheck.bot.onboarding.policy import POLICY_VERSION
from hwcheck.bot.subjects import subjects_for
from hwcheck.db.repo import InviteResult, StudentProfile

# развилка: коротко, что такое Домашка; что бот сделает для ученика и для родителя — во вводных
HELLO = (
    "Привет! Я Домашка — бот, который проверяет домашку по фото и помогает разобраться "
    "с ошибками. Кто ты?"
)
STUDENT_GRADE = "В каком ты классе?"
PARENT_GRADE = "В каком классе ваш ребёнок?"

# Вводные веток (§4): куда человек попал, что бот для него сделает и что дальше. Обещаем только
# то, что работает: проверяется математика, готовых ответов нет, призов и бонусов нет.
_STUDENT_PITCH = (
    "Фотографируешь тетрадь по математике — и за минуту узнаёшь, что решено верно, а где ошибка. "
    "Ответ я не подсказываю — помогаю найти ошибку самому."
)
STUDENT_INTRO = f"Ты в Домашке 👋 {_STUDENT_PITCH} Дальше — пара вопросов и разрешение родителя."
# кто фотографирует, вводное не говорит: класс ещё неизвестен, а за 1–4 класс фото присылает
# родитель; итог после проверки здесь не обещаем — его обещает ветка уведомлений (§9.1)
PARENT_INTRO = (
    "Вы в Домашке. Вам не нужно каждый вечер проверять домашку и вспоминать школьную программу: "
    "по фото тетради бот за минуту находит ошибки. Готовых ответов бот не даёт — он подсказками "
    "подводит ребёнка к тому, чтобы тот сам нашёл и исправил ошибку. Вычисления пересчитывает "
    "программа, а не нейросеть, поэтому проверке можно доверять.\n\n"
    "Сейчас бот проверяет математику. Дальше — класс ребёнка и согласие на обработку данных."
)
# вводное и вопрос о классе — одним сообщением: кнопки классов остаются под объяснением
STUDENT_WELCOME = f"{STUDENT_INTRO}\n\n{STUDENT_GRADE}"
PARENT_WELCOME = f"{PARENT_INTRO}\n\n{PARENT_GRADE}"

YOUNG_STUDENT = (
    "В 1–4 классе домашку мне присылает мама или папа из своего MAX. Перешли им ссылку на меня 👇"
)
BOT_LINK_FOR_PARENT = (
    "Домашка — бот, который проверяет домашку по фото и помогает ребёнку разобраться с ошибками "
    "подсказками: {link}"
)
SUBJECT_STUDENT = "Какой предмет сейчас?"
SUBJECT_PARENT = "Какой предмет проверяем?"
SUBJECT_SOON = (
    "Проверку по предмету «{title}» пока учу — сообщу, когда появится. "
    "А пока могу проверить математику."
)

NEED_PARENT = "Чтобы начать, нужно разрешение родителя. Перешли маме или папе сообщение ниже 👇"
PARENT_INVITE_MESSAGE = (
    "Здравствуйте! Ваш ребёнок хочет проверять домашку в Домашке — это бот, который находит ошибки "
    "и помогает разобраться с ними подсказками, без готовых ответов. Чтобы разрешить, откройте "
    "ссылку: {link}\nЕсли ссылка не открылась — отправьте боту код {code}"
)
WAITING_PARENT = (
    "Проверка откроется, когда родитель разрешит 🙏 Если ссылка потерялась — пришлю новую."
)
PHOTO_BLOCKED = "Сначала нужно разрешение родителя 🙏 До него я не смотрю фото."
EXAMPLE = (
    "Вот как проходит проверка 👇\n\n"
    "1. Ты присылаешь фото страницы тетради. Например, там решено: «№17: 803 + 169 = 753».\n"
    "2. Через минуту я отвечаю: «Проверил! 0 из 1 верно. №17 — есть ошибка (строка 1) ❌» "
    "и показываю кнопку «Разобрать №17».\n"
    "3. Нажимаешь — и я не называю ответ, а подсказываю: «Сложи сначала единицы: 3 + 9. "
    "Сколько получилось и что нужно запомнить?»\n"
    "4. Ошибку находишь ты сам — так и учимся 💪"
)
PARENT_ALLOWED = "Родитель разрешил! 🎉"
PARENT_NOT_ALLOWED = "Родитель пока не разрешил. Можно отправить ссылку ещё раз."
INSTRUCTION_STUDENT = (
    "Пришли фото страницы тетради с решением — целиком, при хорошем свете. Можно вместе с фото "
    "страницы учебника, до 4 фото за раз. Проверка занимает около минуты. Ошибки разберём вместе "
    "подсказками — готовых ответов я не даю 😉"
)
INSTRUCTION_PARENT = (
    "Готово! 🎉 Присылайте фото страницы тетради с решением — целиком, при хорошем свете. Можно "
    "вместе с фото страницы учебника, до 4 фото за раз. Проверка занимает около минуты. Ошибки бот "
    "разбирает подсказками, без готовых ответов — удобно пройти вместе с ребёнком."
)

CONSENT_SUMMARY = (
    "Что обрабатываем: класс, выбранный предмет, фото домашних заданий (хранятся 30 дней), "
    "результаты проверок, идентификатор MAX (в зашифрованном виде). Имя и школу не спрашиваем.\n"
    "Зачем: проверка домашки, разбор ошибок, уведомления родителю.\n"
    "Кому передаём: фото и текст заданий — в GigaChat API (ПАО Сбербанк) для распознавания и "
    "проверки.\n"
    "Где: серверы в России.\n"
    "Как отозвать: написать оператору — контакт в полном тексте.\n"
    "Нажимая «Согласен», вы подтверждаете, что вы родитель или законный представитель ребёнка.\n"
    f"Полный текст — кнопка «Полный текст» (политика {POLICY_VERSION})."
)
CHILD_ASKS_CONSENT = (
    "Ваш ребёнок ({grade} класс) хочет пользоваться Домашкой. Это бот, который проверяет домашку "
    "по математике: по фото тетради за минуту находит ошибки и подсказками помогает ребёнку "
    "исправить их самому, без готовых ответов. Вычисления пересчитывает программа, а не "
    "нейросеть. Чтобы ребёнок начал, нужно ваше согласие на обработку данных."
)
PARENT_FIRST_CONSENT = (
    "Ребёнок {grade} класса пользуется Домашкой из своего MAX, а вы даёте разрешение."
)
PARENT_SENDS_CONSENT = (
    "В 1–4 классе фото домашки присылаете вы — из своего MAX, можно вместе с ребёнком. "
    "Результат проверки вы увидите сразу, в этом же чате."
)
# итог родителю уходит только за ребёнка 5–9 класса со своим MAX (bot/notifier.py); за 1–4 класс
# фото присылает сам родитель — обещать ему итог было бы неправдой
NOTIFY_PROMISE = (
    "После каждой проверки пришлю вам короткий итог; отключить можно кнопкой под сообщением."
)
CONSENT_THANKS = "Спасибо! Ребёнок получил доступ и может присылать домашку ✅"
DECLINED = "Хорошо, доступ не открыт. Если передумаете — попросите ребёнка прислать новую ссылку."
FORWARD_TO_CHILD = "Спасибо! Перешлите ребёнку сообщение ниже 👇"
# ссылка ребёнку идёт следующим сообщением — «👇» остаётся последней строкой
FORWARD_TO_CHILD_NOTIFY = f"Спасибо! {NOTIFY_PROMISE}\n\nПерешлите ребёнку сообщение ниже 👇"
CHILD_INVITE_MESSAGE = (
    "Открой ссылку, чтобы подключиться к Домашке: {link}\n"
    "Если ссылка не открылась — отправь боту код {code}"
)
CHILD_LINKED = (
    "Привет! Родитель подключил тебя к Домашке 🎉 Это бот, который проверяет домашку по фото. "
    f"{_STUDENT_PITCH} Разрешение родителя уже есть."
)
CHILD_JOINED = "Ребёнок ({grade} класс) подключился к Домашке ✅"
LINK_GONE = "Эта ссылка уже не ждёт ответа. Откройте её ещё раз или попросите новую."

PARENT_STATUS_HEADER = "Ваши дети в Домашке:"
PARENT_PHOTO_NO_YOUNG = (
    "Фото домашки 5–9 класса присылает ребёнок в своём MAX. Если ребёнок в 1–4 классе — "
    "добавьте его и присылайте фото сами."
)
WHOSE_HOMEWORK = "Чья это домашка?"
PHOTOS_EXPIRED = "Не нашёл фото для проверки — пришлите их ещё раз 📸"

# ссылку ученика открывает родитель — на «вы», ссылку родителя открывает ребёнок — на «ты»
_PARENT_REFUSALS: dict[InviteResult, str] = {
    "expired": "Ссылка устарела — попросите ребёнка прислать новую.",
    "used": "Эта ссылка уже использована — попросите ребёнка прислать новую.",
    "invalid": "Ссылка не сработала — попросите ребёнка прислать новую.",
    "role_mismatch": "Эту ссылку нужно открыть в MAX родителя.",
    "has_parent": "У ребёнка уже подключён родитель.",
}
_CHILD_REFUSALS: dict[InviteResult, str] = {
    "expired": "Ссылка устарела — попроси родителя прислать новую.",
    "used": "Эта ссылка уже использована — попроси родителя прислать новую.",
    "invalid": "Ссылка не сработала — попроси родителя прислать новую.",
    "role_mismatch": "Эту ссылку нужно открыть в MAX ребёнка.",
    "has_parent": "У тебя уже подключён родитель 👍",
}


def bot_link(username: str) -> str:
    return f"https://max.ru/{username}"


def consent_text(intro: str) -> str:
    return f"{intro}\n\n{CONSENT_SUMMARY}"


def consent_thanks(*, notify: bool) -> str:
    """notify — итоги у родителя включены: иначе обещание итога было бы неправдой."""
    return f"{CONSENT_THANKS}\n\n{NOTIFY_PROMISE}" if notify else CONSENT_THANKS


def forward_to_child(*, notify: bool) -> str:
    return FORWARD_TO_CHILD_NOTIFY if notify else FORWARD_TO_CHILD


def invite_refusal(kind: InviteKind, result: InviteResult) -> str:
    refusals = _PARENT_REFUSALS if kind == "student_invites_parent" else _CHILD_REFUSALS
    return refusals.get(result, "Ссылка не подошла.")


def code_invalid(formal: bool) -> str:
    """formal — родитель или новый пользователь (код от ребёнка получает родитель), иначе ученик."""
    if formal:
        return "Код не подошёл 🤔 Проверьте его или попросите новую ссылку."
    return "Код не подошёл 🤔 Проверь его или попроси новую ссылку."


def code_rate_limited(formal: bool) -> str:
    if formal:
        return "Слишком много попыток ввода кода. Попробуйте через час."
    return "Слишком много попыток ввода кода. Попробуй через час."


def parent_status(children: Sequence[StudentProfile], waiting_grades: Sequence[int | None]) -> str:
    lines = [PARENT_STATUS_HEADER]
    for child in children:
        how = "фото присылаете вы" if child.sent_by_parent else "свой MAX"
        lines.append(f"• {child.grade} класс — {how}")
    lines += [f"• {grade} класс — ждём, когда ребёнок откроет ссылку" for grade in waiting_grades]
    if any(child.sent_by_parent for child in children):
        lines.append("\nЧтобы проверить домашку 1–4 класса, пришлите фото страницы тетради.")
    return "\n".join(lines)


def role_keyboard() -> Buttons:
    return [
        [
            callback_button("Я ученик", "ob:role:student"),
            callback_button("Я родитель", "ob:role:parent"),
        ]
    ]


def grade_keyboard(action: str) -> Buttons:
    """Классы 3×3; action `grade` — ученик о себе, `pgrade` — родитель о ребёнке."""
    return [
        [callback_button(str(grade), f"ob:{action}:{grade}") for grade in range(row, row + 3)]
        for row in (1, 4, 7)
    ]


def subject_keyboard(grade: int) -> Buttons:
    buttons = [
        callback_button(s.title if s.available else f"🔜 {s.title}", f"ob:subject:{s.code}")
        for s in subjects_for(grade)
    ]
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def consent_keyboard(accept: str, decline: str | None = None) -> Buttons:
    answer = [callback_button("Согласен", accept)]
    if decline is not None:
        answer.append(callback_button("Отказать", decline))
    return [[callback_button("Полный текст", "ob:policy")], answer]


def example_keyboard() -> Buttons:
    return [[callback_button("Посмотреть пример проверки", "ob:example")]]


def waiting_parent_keyboard() -> Buttons:
    return [
        [callback_button("Отправить ссылку ещё раз", "ob:resend")],
        [callback_button("Посмотреть пример проверки", "ob:example")],
    ]


def add_child_keyboard() -> Buttons:
    return [[callback_button("Добавить ребёнка", "ob:addchild")]]


def whose_keyboard(children: Sequence[StudentProfile]) -> Buttons:
    """Дети 1–4 класса по классу; одинаковый класс — «ребёнок N» по порядку добавления."""
    total = Counter(child.grade for child in children)
    seen: Counter[int] = Counter()
    rows: Buttons = []
    for child in children:
        seen[child.grade] += 1
        label = f"{child.grade} класс"
        if total[child.grade] > 1:
            label += f", ребёнок {seen[child.grade]}"
        rows.append([callback_button(label, f"ob:whose:{child.id}")])
    return rows
