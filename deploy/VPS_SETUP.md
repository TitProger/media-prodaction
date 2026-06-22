# Развёртывание Content Factory на VPS (2 ГБ RAM · 2 ядра · 30 ГБ SSD)

Домен **не нужен**: Telegram работает через polling, загрузка на YouTube — через
OAuth-токен. Веб-интерфейс доступен по `http://IP:8001`.

## 1. Система (Ubuntu 22.04)

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip ffmpeg git
```

## 2. Код и зависимости

```bash
git clone https://github.com/TitProger/media-prodaction.git
cd media-prodaction
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 3. Файлы конфигурации (копируются с вашего ПК)

```bash
# с локальной машины:
scp .env                      user@SERVER:/home/ubuntu/media-prodaction/
scp storage/client_secret.json user@SERVER:/home/ubuntu/media-prodaction/storage/
scp storage/youtube_token.json user@SERVER:/home/ubuntu/media-prodaction/storage/
```

`youtube_token.json` уже содержит refresh-токен → браузер на сервере не нужен,
загрузка пойдёт автоматически.

### Память: Whisper

В `.env` на сервере поставьте лёгкую модель (2 ГБ RAM):

```
WHISPER_MODEL=tiny
```

Ориентир по RAM (пик во время транскрипции): `tiny ≈ 0.4 ГБ · base ≈ 1 ГБ ·
small ≈ 2 ГБ`. На 2 ГБ — только `tiny` (надёжно) или `base` (впритык, см. swap).
Тяжёлые операции (Whisper/FFmpeg) сериализованы process-wide — два Whisper
одновременно не запустятся, даже если совпадут крон и веб.

## 4. Swap-файл 2 ГБ (страховка от пиков памяти)

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h   # проверить
```

## 5. Автозапуск (systemd)

```bash
sudo cp deploy/media-factory.service /etc/systemd/system/
# отредактируйте пути User / WorkingDirectory / ExecStart под сервер
sudo systemctl daemon-reload
sudo systemctl enable --now media-factory
journalctl -u media-factory -f      # смотреть логи
```

Веб-интерфейс: `http://SERVER_IP:8001` (корень `/` редиректит на `/ui`).
Откройте порт 8001, если включён фаервол: `sudo ufw allow 8001`.

## 6. Диск (30 ГБ)

- `output/` копит рабочие папки задач (`web_*`, `cron_*`). Чистите периодически:
  ```bash
  find output -maxdepth 1 -type d -mtime +2 -exec rm -rf {} +
  ```
  Можно повесить в cron (`crontab -e`):
  ```
  0 4 * * * find /home/ubuntu/media-prodaction/output -maxdepth 1 -type d -mtime +2 -exec rm -rf {} +
  ```
- Модель Whisper кэшируется в `~/.cache/whisper` (tiny ≈ 75 МБ, base ≈ 140 МБ).
- Рядом с исходниками создаются `*.whisper_cache.json` (транскрипты) — мелкие.

## Проверка

```bash
free -h                       # RAM + swap
journalctl -u media-factory -f  # логи бота/крона/API
# во время нарезки в другом окне:
htop                          # убедиться, что RAM не упирается в лимит
```
