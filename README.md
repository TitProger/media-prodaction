# Content Factory 🎬

Автоматизированный конвейер создания вертикальных видео для YouTube Shorts и TikTok Reels.

## Что делает

1. **Split-screen** — верхнее и нижнее видео склеиваются в формат 1080×1920 (9:16)
2. **Субтитры** — авто-транскрипция через OpenAI Whisper, сгорают по центру экрана
3. **Рекламный баннер** — PNG/JPG накладывается с fade-in / fade-out по заданному таймкоду

## Требования

- Python 3.10+
- FFmpeg установлен и доступен в PATH
- (опционально) CUDA для ускорения Whisper на GPU

## Установка

```bash
cd content-factory
pip install -e ".[dev]"
```

## Запуск

### Веб-интерфейс (рекомендуется)

```bash
python main.py
# открой http://127.0.0.1:7860
```

### CLI (без интерфейса)

```bash
python main.py compose \
  --top top_video.mp4 \
  --bottom bottom_video.mp4 \
  --banner banner.png \
  --output result.mp4
```

## Структура проекта

```
content-factory/
├── src/
│   └── content_factory/
│       ├── config/
│       │   └── settings.py          # все параметры шаблона здесь
│       ├── core/
│       │   ├── subtitle_generator.py # Whisper → .ass субтитры
│       │   └── video_composer.py     # FFmpeg композитинг
│       └── ui/
│           └── app.py               # Gradio интерфейс
├── assets/templates/                # кастомные .ass шаблоны (опционально)
├── output/                          # готовые видео
├── tests/
├── main.py
└── pyproject.toml
```

## Тонкая настройка

Все параметры шаблона меняются в `src/content_factory/config/settings.py`:

| Параметр | Описание |
|---|---|
| `WHISPER_MODEL` | tiny / base / small / medium / large |
| `SUBTITLE_FONT_*` | шрифт, размер, цвет субтитров |
| `SUBTITLE_MARGIN_V` | вертикальная позиция субтитров |
| `BANNER_APPEAR_AT_SEC` | когда баннер появляется |
| `BANNER_DURATION_SEC` | как долго баннер виден |
| `OUTPUT_CRF` | качество выходного видео (18 = высокое) |
