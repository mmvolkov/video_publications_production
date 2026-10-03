# 🎬 Reels Studio — материалы → рилсы для Instagram

Сайт, куда вы загружаете материалы (фото, видео, тексты, музыку), а Claude пишет по ним
сценарий и сервис собирает готовый вертикальный ролик **1080×1920, 30 fps, H.264 + AAC**
с титрами, озвучкой диктором, обложкой и подписью к посту.

## Как это работает

1. **Проект и бриф.** Тема/продукт, цель, аудитория, тон, призыв к действию.
2. **Материалы.** Перетащите файлы (можно с телефона) или вставьте текст: тезисы, отзывы, цифры.
   К каждому фото/видео можно добавить комментарий: «это наш бариста», «результат клиента».
3. **«Сделать рилс».** Claude (по умолчанию `claude-opus-5-5`) смотрит фото и кадры из видео,
   читает тексты и бриф и возвращает раскадровку: хук → сцены с текстом на экране → призыв,
   плюс подпись к посту и хэштеги.
4. **Рендер.** ffmpeg собирает видео: плавный зум по фото, размытый фон для горизонтальных кадров,
   плашки с текстом в безопасной зоне Reels, карточки на градиенте, фоновая музыка с затуханием.
5. **Озвучка (по желанию).** Включите «Озвучить диктором», выберите провайдера, голос и темп,
   нажмите «Прослушать». Claude пишет для каждой сцены отдельную фразу диктора; сцена длится
   ровно столько, сколько звучит фраза, музыка приглушается под голос. По желанию —
   караоке-субтитры: слова появляются группами по 2–3, текущее слово подсвечивается жёлтым.
6. **Правки.** Редактор сценария: меняйте тексты, порядок, длительность, кадры — видео
   пересобирается без нового запроса к ИИ (озвучиваются заново только изменённые фразы —
   остальное берётся из кеша). Там же можно сменить голос или выключить озвучку. Кнопка «Новый вариант» просит Claude написать
   сценарий заново.
7. **Публикация.** Скачайте видео и обложку, скопируйте подпись — или отправьте всё в n8n.

Без ключа Anthropic сайт тоже работает: сценарий собирается «в черновом режиме» из ваших
комментариев и текстов (это видно по метке в шапке).

## Запуск

### Попробовать на своём компьютере (Docker)

```bash
git clone https://github.com/mmvolkov/video_publications_production
cd video_publications_production
git checkout claude/confident-fermi-dq2s1a   # пока изменения не влиты в main
cp .env.example .env      # впишите ANTHROPIC_API_KEY, CORP_TTS_API_KEY и др.
docker compose up -d --build
# сайт: http://localhost:8128
```

### На сервере с Traefik (рядом с TTS, домен `reels.cloudsmasters.ru`)

```bash
cd /data/apps && git clone https://github.com/mmvolkov/video_publications_production reels && cd reels
cp .env.example .env
# в .env: ANTHROPIC_API_KEY, APP_PASSWORD (обязательно — сайт публичный),
#         CORP_TTS_BASE_URL=http://tts:8000/v1 и CORP_TTS_API_KEY,
#         PUBLIC_BASE_URL=https://reels.cloudsmasters.ru, при необходимости REELS_DOMAIN
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d --build
```

Нужна DNS-запись `reels.cloudsmasters.ru` на этот сервер (как у `tts.cloudsmasters.ru`).
Контейнер подключается к сети Traefik `dedicated_server_default`, в которой уже есть контейнер
`tts`, поэтому свой TTS вызывается напрямую по `http://tts:8000/v1`.

Материалы и готовые ролики лежат в `./data`.

### Локально

Нужны Python 3.10+ и ffmpeg (`brew install ffmpeg` / `apt install ffmpeg fonts-dejavu-core`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload
```

Тесты: `pytest` (нужен ffmpeg).

## Настройки (`.env`)

| Переменная | Зачем |
|---|---|
| `ANTHROPIC_API_KEY` | Ключ Claude. Без него — черновые сценарии без ИИ |
| `ANTHROPIC_MODEL` | Модель, по умолчанию `claude-opus-5-5` |
| `ANTHROPIC_EFFORT` | Глубина размышления: `low` / `medium` / `high` |
| `APP_USER`, `APP_PASSWORD` | Вход на сайт по паролю (HTTP Basic). **Задайте, если сайт открыт в интернет** |
| `PUBLIC_BASE_URL` | Публичный адрес сайта — для ссылок на видео в вебхуке |
| `N8N_WEBHOOK_URL` | Вебхук n8n, получает событие `reel.ready` |
| `MAX_UPLOAD_MB` | Лимит на один файл (по умолчанию 500 МБ) |
| `FONT_PATH` | Свой шрифт для титров (TTF с кириллицей) |
| `TTS_DEFAULT_PROVIDER`, `CORP_TTS_*`, `EDGE_TTS_*`, `YANDEX_*`, `ELEVENLABS_*` | Озвучка — см. раздел ниже и `.env.example` |

## Озвучка

| Провайдер | Голоса | Нужно |
|---|---|---|
| `corp` — свой TTS (Fun-CosyVoice3, OpenAI-совместимый) | `anastasiya` (деловая диктовка), `dmitry`, `svetlana` | `CORP_TTS_API_KEY`, при необходимости `CORP_TTS_BASE_URL` |
| `edge` — живой Microsoft edge-tts | `ru-RU-SvetlanaNeural`, `ru-RU-DmitryNeural` | ничего; текст уходит в Microsoft |
| `yandex` — Yandex SpeechKit | `alena`, `filipp`, `jane`, `ermil`, `zahar`, `omazh` и эмоциональные `alena+good`, `jane+good`, `ermil+good`, `zahar+good` | `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` |
| `elevenlabs` — ElevenLabs | из вашей библиотеки (`ELEVENLABS_VOICES=id:Имя,…`) | `ELEVENLABS_API_KEY` |

Провайдер без ключа виден в списке, но неактивен. По умолчанию выбирается `TTS_DEFAULT_PROVIDER`
(если он настроен), иначе первый доступный.

Особенности:
- **Свой TTS.** Запрос `POST {base}/audio/speech` с телом
  `{"model":"tts-1","voice":"anastasiya","input":"…","response_format":"wav","speed":1.0}` и
  `Authorization: Bearer <ключ>`. Тело уходит в UTF-8, грабли с кириллицей из Windows-curl тут нет.
  Если Reels Studio запущена в той же docker-сети, что и студия, укажите
  `CORP_TTS_BASE_URL=http://tts:8000/v1` (или просто передайте студийные `TTS_BASE_URL`/`TTS_API_KEY`).
- **Голоса своего TTS** подтягиваются с сервера (`GET /v1/voices`, кеш на минуту): новый пресет
  в `voices/` сразу появляется на сайте. Если сервер недоступен — используется `CORP_TTS_VOICES`
  или встроенный список (anastasiya, dmitry, svetlana). Если `CORP_TTS_VOICES` задан, сервер не опрашивается.
- **Подача (`instruct`)** — только для своего TTS: «как в пресете голоса» (поле не отправляется),
  «без инструкции» (пустая строка) или готовые варианты и своя инструкция. Инструкция пишется
  **по-английски**: русскую CosyVoice зачитывает вслух, поэтому сайт её не пропускает.
- **Замена edge-tts.** Если Microsoft не отвечает (например, 403 для IP дата-центра), фраза
  озвучивается клоном того же голоса на своём TTS: `ru-RU-SvetlanaNeural` → `svetlana`,
  `ru-RU-DmitryNeural` → `dmitry`. После отказа 10 минут edge не дёргается — сразу клон.
  В карточке рилса и при «Прослушать» видно, что сработала замена. Точных таймингов слов у клона
  нет — караоке раскладывается по длине слов. Выключить: `EDGE_FALLBACK=off`; нужен `CORP_TTS_API_KEY`.
- **Эмоции Яндекса.** Голос вида `alena+good` отправляется как `voice=alena&emotion=good`.
- **Edge TTS.** На 401/403 повторов нет (это отказ, а не сбой связи). Отрицательный `rate` сервис отвергает, поэтому темп ниже 1.0 делается через
  ffmpeg `atempo`. Обрывы повторяются до 5 раз с нарастающей паузой.
- **Кеш.** Каждая фраза кешируется в `data/tts_cache/` по (провайдер, голос, темп, текст),
  тайминги слов — рядом в `*.words.json`.

### Караоке-субтитры

- **Edge TTS** отдаёт реальные тайминги каждого слова (`boundary="WordBoundary"`), поэтому
  подсветка идёт точно в такт речи. Слова сервиса сопоставляются с исходным текстом, чтобы
  в субтитрах сохранились знаки препинания.
- **Свой TTS, Яндекс, ElevenLabs** таймингов не отдают: слова раскладываются пропорционально
  длине внутри участка речи (тишина в начале и в конце фразы отрезается по `silencedetect`).
- Субтитры рисуются через libass (ASS) снизу, над интерфейсом Reels. Текст сцены в этом
  режиме переезжает наверх, а крупная карточка — выше центра, чтобы ничего не перекрывалось.
- Группы режутся по концу предложения, по паузам (запятая, тире) и по длине строки.

## Интеграция с n8n

Когда рилс собран, на `N8N_WEBHOOK_URL` уходит POST:

```json
{
  "event": "reel.ready",
  "project_id": "a1b2c3d4e5f6",
  "project_title": "Кофейня «Утро»",
  "reel_id": "0f9e8d7c6b5a",
  "title": "Лучший кофе у метро",
  "duration": 21.5,
  "video_url": "https://reels.example.com/api/projects/.../reels/.../video",
  "cover_url": "https://reels.example.com/api/projects/.../reels/.../cover",
  "caption": "Текст поста…",
  "hashtags": ["#кофе", "#reels"],
  "caption_full": "Текст поста…\n\n#кофе #reels"
}
```

Дальше в n8n можно: отправить ролик на согласование в Telegram, положить в Google Drive
или опубликовать через Instagram Graph API (нода HTTP Request: `POST /{ig-user-id}/media`
с `media_type=REELS`, `video_url`, `caption`, затем `POST /{ig-user-id}/media_publish`).
Для публикации нужен бизнес-аккаунт Instagram и публичный `video_url`. Если включён
`APP_PASSWORD`, Instagram не сможет скачать видео по ссылке — тогда сначала загрузите ролик
в n8n (HTTP Request с Basic Auth) и передайте его через общедоступное хранилище.

API сайта тоже можно вызывать из n8n: `POST /api/projects`, `POST /api/projects/{id}/materials`
(multipart, поле `files`), `POST /api/projects/{id}/notes`, `POST /api/projects/{id}/reels`.
Документация API: `/docs`.

## Устройство

```
app/
  main.py      FastAPI: проекты, материалы, рилсы, отдача файлов, пароль
  ai.py        промпт и запрос к Claude (vision + JSON-схема), черновой режим без ИИ
  render.py    сборка видео: Pillow (кадры, титры, обложка) + ffmpeg (зум, склейка, звук)
  tts.py       озвучка: свой TTS, edge-tts, Yandex SpeechKit, ElevenLabs + кеш и тайминги слов
  subtitles.py караоке-субтитры (ASS для libass)
  jobs.py      фоновая очередь рендера (по одному ролику) и вебхук n8n
  media.py     тип файла, ffprobe, превью
  storage.py   проекты в JSON на диске (data/projects/<id>/)
  static/      интерфейс (HTML/CSS/JS без сборки)
```

## Что можно добавить дальше

- Автосубтитры по голосу из исходных видео.
- Звук из исходных видео вместо/вместе с музыкой.
- Переходы между сценами и фирменные шаблоны (цвета, шрифт, логотип).
- Автопубликация в Instagram прямо с сайта по расписанию.
