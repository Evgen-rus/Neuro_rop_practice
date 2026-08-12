# Пользователи пилота

Пользователи хранятся в общей SQLite приложения. Пароли вводятся интерактивно через `getpass`, не передаются аргументом командной строки и не выводятся в терминал.

## Команды

```powershell
# Администратор
.\venv\Scripts\python.exe scripts\manage_user.py create --login admin-1 --role admin

# РОП
.\venv\Scripts\python.exe scripts\manage_user.py create --login rop-1 --role rop

# Менеджер: manager-id совпадает с ответственным в Bitrix
.\venv\Scripts\python.exe scripts\manage_user.py create --login manager-1 --role manager --manager-id 123

# Список без password hash
.\venv\Scripts\python.exe scripts\manage_user.py list

# Смена пароля отзывает активные сессии
.\venv\Scripts\python.exe scripts\manage_user.py passwd --login manager-1

# Отключение и включение
.\venv\Scripts\python.exe scripts\manage_user.py deactivate --login manager-1
.\venv\Scripts\python.exe scripts\manage_user.py activate --login manager-1
```

Доступны также `set-role`, `set-manager` и `revoke-sessions`; точные параметры показывает `--help`. Последнего активного администратора отключить или перевести в другую роль нельзя. Активному менеджеру обязателен уникальный `manager_id`.

## Публикация

Cloudflare quick tunnel и внешний Nginx Basic Auth остаются без изменений. После внешнего Basic Auth основной интерфейс требует личный app-login. Карточка `/review/{token}` не требует app-сессии, но остаётся за внешним Basic Auth.

Не добавляйте реальные логины, пароли, cookie или содержимое SQLite в Git и логи.
