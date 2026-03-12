from __future__ import annotations

"""
Конфиг-каталог анонимных обращений.

НАСТРОЙКА: редактируйте блоки, email адресатов и темы в этом файле.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AppealBlock:
    block_id: str
    block_name: str
    recipient_email: str


@dataclass(frozen=True)
class AppealTopic:
    topic_id: str
    topic_name: str
    block_id: str


# НАСТРОЙКА: email начальников блоков ЦА меняйте здесь.
APPEAL_BLOCKS: tuple[AppealBlock, ...] = (
    AppealBlock(
        block_id="development",
        block_name="Блок развития",
        recipient_email="nachalnikblokaRazvitie@mail.ru",
    ),
    AppealBlock(
        block_id="pbotos",
        block_name="Блок ПБОТОС",
        recipient_email="nachalnikblokaPBOTOS@mail.ru",
    ),
    AppealBlock(
        block_id="gochs",
        block_name="Блок ГОиЧС",
        recipient_email="nachalnikblokaGOiCHS@mail.ru",
    ),
    AppealBlock(
        block_id="mto",
        block_name="Блок МТО",
        recipient_email="nachalnikblokaMTO@mail.ru",
    ),
    AppealBlock(
        block_id="aho",
        block_name="Блок АХО",
        recipient_email="nachalnikblokaAHO@mail.ru",
    ),
    AppealBlock(
        block_id="hr",
        block_name="Блок кадров",
        recipient_email="nachalnikblokaKadry@mail.ru",
    ),
)


# НАСТРОЙКА: темы, доступные сотрудникам, меняйте здесь.
# Для добавления темы: добавьте новый AppealTopic с нужным block_id.
APPEAL_TOPICS: tuple[AppealTopic, ...] = (
    AppealTopic("dev_system_failures", "Сбои ИТ-систем и сервисов", "development"),
    AppealTopic("dev_workplace_it_equipment", "Оснащение рабочего места ИТ-оборудованием", "development"),
    AppealTopic("dev_process_changes", "Предложение по изменению/улучшению процесса", "development"),
    AppealTopic("dev_access_issues", "Проблемы с доступом и учетной записью", "development"),
    AppealTopic("pbotos_safety_violation", "Нарушение требований охраны труда/безопасности", "pbotos"),
    AppealTopic("pbotos_ppe_issue", "Проблемы с СИЗ и защитным оборудованием", "pbotos"),
    AppealTopic("pbotos_hazardous_conditions", "Опасные условия на рабочем месте", "pbotos"),
    AppealTopic("pbotos_safety_improvement", "Предложение по повышению безопасности", "pbotos"),
    AppealTopic("gochs_training_request", "Запрос на обучение по ГОиЧС", "gochs"),
    AppealTopic("gochs_emergency_actions", "Вопрос по действиям при ЧС", "gochs"),
    AppealTopic("gochs_alerting_issue", "Неисправность средств оповещения/эвакуации", "gochs"),
    AppealTopic("gochs_drill_proposal", "Предложение по тренировкам и учениям", "gochs"),
    AppealTopic("mto_resource_shortage", "Нехватка материалов/инструментов", "mto"),
    AppealTopic("mto_supply_request", "Запрос на обеспечение ресурсами", "mto"),
    AppealTopic("mto_consumables", "Проблемы с расходными материалами", "mto"),
    AppealTopic("mto_equipment_delivery", "Задержка поставки оборудования/инвентаря", "mto"),
    AppealTopic("aho_workplace_equipment", "Оснащение рабочего места (мебель/условия)", "aho"),
    AppealTopic("aho_facility_issue", "Проблемы с помещением (свет, климат, ремонт)", "aho"),
    AppealTopic("aho_sanitary_conditions", "Санитарное состояние и хозобслуживание", "aho"),
    AppealTopic("aho_office_resources", "Обеспечение офисных и бытовых зон", "aho"),
    AppealTopic("hr_schedule_vacation", "Вопрос по графику работы/отпуску", "hr"),
    AppealTopic("hr_documents_requests", "Справки и кадровые документы", "hr"),
    AppealTopic("hr_personnel_data", "Некорректные кадровые данные", "hr"),
    AppealTopic("hr_labor_procedures", "Вопрос по кадровым процедурам", "hr"),
)


_BLOCK_BY_ID = {item.block_id: item for item in APPEAL_BLOCKS}
_TOPIC_BY_ID = {item.topic_id: item for item in APPEAL_TOPICS}


def list_topics_for_ui() -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for topic in APPEAL_TOPICS:
        block = _BLOCK_BY_ID.get(topic.block_id)
        if not block:
            continue
        payload.append(
            {
                "topic_id": topic.topic_id,
                "topic_name": topic.topic_name,
                "block_id": block.block_id,
                "block_name": block.block_name,
            }
        )
    return payload


def resolve_topic(topic_id: str) -> dict[str, str] | None:
    topic = _TOPIC_BY_ID.get(topic_id)
    if not topic:
        return None

    block = _BLOCK_BY_ID.get(topic.block_id)
    if not block:
        return None

    return {
        "topic_id": topic.topic_id,
        "topic_name": topic.topic_name,
        "block_id": block.block_id,
        "block_name": block.block_name,
        "recipient_email": block.recipient_email,
    }
