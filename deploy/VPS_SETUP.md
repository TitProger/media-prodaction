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

---

# Авто-деплой через GitHub Actions

После первой ручной установки (выше) обновления катятся автоматически: push в `main`
→ GitHub по SSH заходит на VPS, делает `git reset --hard origin/main`, ставит зависимости
и перезапускает сервис. Логика — в [deploy.sh](deploy.sh), workflow — в
`.github/workflows/deploy.yml`.

### 1. Разрешить деплой-юзеру перезапуск сервиса без пароля

`deploy.sh` вызывает `sudo systemctl restart media-factory`. Чтобы это работало без
интерактивного пароля, добавь правило sudoers (замени `ubuntu` на своего юзера):

```bash
echo 'ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart media-factory, /bin/systemctl status media-factory' \
  | sudo tee /etc/sudoers.d/media-factory
sudo chmod 440 /etc/sudoers.d/media-factory
```

### 2. Создать отдельный SSH-ключ для деплоя

На своём ПК (или на сервере), без пароля на ключ:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/mf_deploy -N "" -C "github-actions-deploy"
# публичный ключ → на сервер, в authorized_keys деплой-юзера:
ssh-copy-id -i ~/.ssh/mf_deploy.pub ubuntu@SERVER_IP
# приватный ключ (его содержимое целиком) пойдёт в GitHub Secret VPS_SSH_KEY:
cat ~/.ssh/mf_deploy
```

### 3. Добавить секреты в GitHub

Репозиторий → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Значение |
|---|---|
| `VPS_HOST` | IP сервера |
| `VPS_USER` | SSH-пользователь (напр. `ubuntu`) |
| `VPS_SSH_KEY` | содержимое приватного ключа `~/.ssh/mf_deploy` (целиком, включая `-----BEGIN/END-----`) |
| `VPS_PROJECT_DIR` | путь к проекту, напр. `/home/ubuntu/media-prodaction` |
| `VPS_PORT` | (необязательно) SSH-порт, если не 22 |

### 4. Включить

Запушить `.github/workflows/deploy.yml` в `main`. Дальше каждый push в `main`
запускает деплой. Можно и вручную: вкладка **Actions → Deploy to VPS → Run workflow**.

> Секреты (`.env`, `youtube_token.json`, `client_secret.json`) остаются на сервере и
> **никогда** не передаются через CI — деплой их не трогает (они в `.gitignore`).
