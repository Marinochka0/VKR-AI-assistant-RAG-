# EduAnalytics — Django сайт для анализа курсов

## Быстрый старт

### 1. Установка зависимостей
```bash
pip install -r requirements.txt
```

### 2. Запуск
```bash
python manage.py runserver
```

Открой браузер: http://127.0.0.1:8000

---

## Структура проекта

```
edu_analytics/
├── manage.py
├── requirements.txt
├── edu_analytics/          # Конфиг Django
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── analyzer/               # Приложение
    ├── analytics.py        # Логика анализа (из 2-version.py)
    ├── views.py            # Django views
    ├── urls.py
    └── templates/
        └── analyzer/
            └── index.html  # Интерфейс
```

## Как пользоваться

1. Введи поисковый запрос / уточнение (необязательно)
2. Загрузи Excel файл с результатами тестов (`.xlsx`)
3. Загрузи `.txt` файлы с текстами тестов (необязательно)
4. Нажми **«Сгенерировать аналитический отчёт»**

## API ключ

Ключ OpenRouter уже вшит в `settings.py`. Чтобы изменить — задай переменную окружения:
```bash
export OPENROUTER_API_KEY=sk-or-v1-...
python manage.py runserver
```

## Требования к Excel

Обязательные столбцы:
- `ID пользователя`
- `Баллы за попытку`

Опциональные:
- `Номер попытки`
- `Максимальный балл за вопрос` или `Максимальный балл`
- `Баллы за попытку (%)`
