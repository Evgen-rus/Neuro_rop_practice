"""Manage local ROP Assistant users without putting passwords in shell history."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from storage.rop_db import (  # noqa: E402
    DEFAULT_DB_PATH,
    activate_auth_user,
    create_auth_user,
    deactivate_auth_user,
    get_auth_user,
    list_auth_users,
    revoke_auth_user_sessions,
    set_auth_user_password,
    update_auth_user,
)


def _add_user_selector(parser: argparse.ArgumentParser) -> None:
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--user-id", type=int, help="Локальный ID пользователя")
    selector.add_argument("--login", help="Логин пользователя")


def _add_parser_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="Путь к SQLite (по умолчанию reports/rop_assistant/rop_assistant.sqlite)",
    )


def _add_subparser_db_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=argparse.SUPPRESS,
        help="Путь к SQLite",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Управление локальными пользователями Neuro ROP Assistant",
    )
    _add_parser_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Создать пользователя")
    _add_subparser_db_argument(create)
    create.add_argument("--login", required=True)
    create.add_argument("--role", choices=("admin", "rop", "manager"), required=True)
    create.add_argument("--manager-id", default=None)
    create.add_argument(
        "--inactive",
        action="store_true",
        help="Создать неактивного пользователя (полезно для manager без manager_id)",
    )

    list_parser = subparsers.add_parser("list", help="Показать пользователей без password hash")
    _add_subparser_db_argument(list_parser)

    passwd = subparsers.add_parser("passwd", help="Сменить пароль")
    _add_subparser_db_argument(passwd)
    _add_user_selector(passwd)

    activate = subparsers.add_parser("activate", help="Активировать пользователя")
    _add_subparser_db_argument(activate)
    _add_user_selector(activate)

    deactivate = subparsers.add_parser("deactivate", help="Деактивировать пользователя")
    _add_subparser_db_argument(deactivate)
    _add_user_selector(deactivate)

    set_role = subparsers.add_parser("set-role", help="Изменить роль")
    _add_subparser_db_argument(set_role)
    _add_user_selector(set_role)
    set_role.add_argument("--role", choices=("admin", "rop", "manager"), required=True)

    set_manager = subparsers.add_parser("set-manager", help="Изменить manager_id")
    _add_subparser_db_argument(set_manager)
    _add_user_selector(set_manager)
    manager_value = set_manager.add_mutually_exclusive_group(required=True)
    manager_value.add_argument("--manager-id")
    manager_value.add_argument(
        "--clear",
        action="store_true",
        help="Очистить manager_id (для активного manager будет отклонено)",
    )

    revoke = subparsers.add_parser("revoke-sessions", help="Отозвать все сессии")
    _add_subparser_db_argument(revoke)
    _add_user_selector(revoke)

    return parser


def _selected_user(args: argparse.Namespace) -> dict[str, Any]:
    user = get_auth_user(
        args.db,
        user_id=args.user_id,
        login=args.login,
    )
    if user is None:
        raise ValueError("Пользователь не найден")
    return user


def _password_pair() -> str:
    password = getpass.getpass("Новый пароль: ")
    confirmation = getpass.getpass("Повторите пароль: ")
    if not password:
        raise ValueError("Пароль не может быть пустым")
    if password != confirmation:
        raise ValueError("Пароли не совпадают")
    return password


def _display_user(user: dict[str, Any]) -> str:
    return (
        f"id={user['id']} login={user['login']} role={user['role']} "
        f"manager_id={user.get('manager_id') or '-'} active={int(bool(user['is_active']))}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            password = _password_pair()
            user = create_auth_user(
                args.db,
                login=args.login,
                password=password,
                role=args.role,
                manager_id=args.manager_id,
                is_active=not args.inactive,
            )
            print(_display_user(user))
            return 0

        if args.command == "list":
            for user in list_auth_users(args.db):
                print(_display_user(user))
            return 0

        user = _selected_user(args)
        user_id = int(user["id"])
        if args.command == "passwd":
            updated = set_auth_user_password(args.db, user_id=user_id, password=_password_pair())
            print(_display_user(updated))
        elif args.command == "activate":
            print(_display_user(activate_auth_user(args.db, user_id=user_id)))
        elif args.command == "deactivate":
            print(_display_user(deactivate_auth_user(args.db, user_id=user_id)))
        elif args.command == "set-role":
            print(_display_user(update_auth_user(args.db, user_id=user_id, role=args.role)))
        elif args.command == "set-manager":
            manager_id = None if args.clear else args.manager_id
            print(
                _display_user(
                    update_auth_user(
                        args.db,
                        user_id=user_id,
                        manager_id=manager_id,
                    )
                )
            )
        elif args.command == "revoke-sessions":
            print(f"revoked_sessions={revoke_auth_user_sessions(args.db, user_id=user_id)}")
        else:
            parser.error(f"Неизвестная команда: {args.command}")
        return 0
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
