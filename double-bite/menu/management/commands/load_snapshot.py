"""
Management command: load_snapshot
----------------------------------
Загружает данные из JSON-снапшота обратно в БД.

Важно:
  - Пути к изображениям (поле image у Dish) восстанавливаются как есть.
    Физические файлы должны уже лежать в MEDIA_ROOT/dishes/.
  - Пароли пользователей НЕ восстанавливаются (их нет в снапшоте).
    Существующие пользователи — обновляются (кроме пароля).
    Новые пользователи — создаются с unusable password (set_unusable_password).
  - По умолчанию использует режим update_or_create (данные не удаляются).
    С флагом --replace сначала очищает все таблицы, потом вставляет заново.

Использование:
  python manage.py load_snapshot snapshots/snapshot_20240924_180000_initial.json
  python manage.py load_snapshot snapshots/snapshot_20240924_180000_initial.json --replace
  python manage.py load_snapshot snapshots/snapshot_20240924_180000_initial.json --only menu
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Загружает данные из JSON-снапшота в БД (пути к фото восстанавливаются автоматически)"

    def add_arguments(self, parser):
        parser.add_argument(
            "snapshot",
            type=str,
            help="Путь к файлу снапшота (.json)",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            default=False,
            help="Очистить таблицы перед загрузкой (ОСТОРОЖНО: удаляет существующие данные)",
        )
        parser.add_argument(
            "--only",
            type=str,
            choices=["menu", "orders", "accounts", "all"],
            default="all",
            help="Загрузить только указанную группу (default: all)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Показать что будет загружено, но не сохранять в БД",
        )

    def handle(self, *args, **options):
        from accounts.models import DeliveryAddress, User
        from orders.models import Cart, CartItem, Order, OrderItem

        from menu.models import Category, Dish, DishOption

        snapshot_path = Path(options["snapshot"])
        if not snapshot_path.exists():
            raise CommandError(f"Файл не найден: {snapshot_path}")

        self.stdout.write(f"Читаю снапшот: {snapshot_path}")
        with open(snapshot_path, encoding="utf-8") as f:
            data = json.load(f)

        meta = data.get("_meta", {})
        self.stdout.write(f"  Создан: {meta.get('created_at', '?')}")
        self.stdout.write(f"  Тег:    {meta.get('tag') or '—'}")
        self.stdout.write("")

        only = options["only"]
        replace = options["replace"]
        dry_run = options["dry_run"]

        if dry_run:
            self.stdout.write(self.style.WARNING("⚠  DRY-RUN: данные НЕ будут сохранены\n"))

        if replace and not dry_run:
            self._confirm_replace()

        stats = {}

        with transaction.atomic():
            if dry_run:
                # Просто показываем что будет загружено
                for key, rows in data.items():
                    if key != "_meta" and isinstance(rows, list):
                        self.stdout.write(f"  {key:<22} {len(rows)} записей")
                transaction.set_rollback(True)
                return

            # ── MENU ──────────────────────────────────────────────────
            if only in ("menu", "all"):
                if replace:
                    DishOption.objects.all().delete()
                    Dish.objects.all().delete()
                    Category.objects.all().delete()

                stats["categories"] = self._load_model(
                    Category, data.get("categories", []),
                    update_fields=["name", "slug", "icon", "display_order", "is_active"],
                )
                stats["dishes"] = self._load_dishes(
                    data.get("dishes", []), replace=replace
                )
                stats["dish_options"] = self._load_model(
                    DishOption, data.get("dish_options", []),
                    update_fields=["dish_id", "name", "price_delta"],
                )

            # ── ACCOUNTS ──────────────────────────────────────────────
            if only in ("accounts", "all"):
                if replace:
                    DeliveryAddress.objects.all().delete()
                    User.objects.exclude(is_superuser=True).delete()

                stats["users"] = self._load_users(data.get("users", []))
                stats["delivery_addresses"] = self._load_model(
                    DeliveryAddress, data.get("delivery_addresses", []),
                    update_fields=[
                        "user_id", "title", "city", "street", "building",
                        "apartment", "floor", "intercom", "is_default",
                    ],
                )

            # ── ORDERS ────────────────────────────────────────────────
            if only in ("orders", "all"):
                if replace:
                    OrderItem.objects.all().delete()
                    Order.objects.all().delete()
                    CartItem.objects.all().delete()
                    Cart.objects.all().delete()

                stats["carts"] = self._load_model(
                    Cart, data.get("carts", []),
                    update_fields=["user_id", "session_key"],
                )
                stats["cart_items"] = self._load_model(
                    CartItem, data.get("cart_items", []),
                    update_fields=["cart_id", "dish_id", "quantity", "selected_options"],
                )
                stats["orders"] = self._load_model(
                    Order, data.get("orders", []),
                    update_fields=[
                        "order_number", "user_id", "session_key",
                        "customer_name", "customer_phone", "delivery_address",
                        "status", "payment_method", "payment_status",
                        "stripe_session_id", "stripe_payment_intent_id",
                        "total_amount", "eta_minutes", "notes",
                    ],
                )
                stats["order_items"] = self._load_model(
                    OrderItem, data.get("order_items", []),
                    update_fields=[
                        "order_id", "dish_id", "dish_title",
                        "price", "quantity", "selected_options",
                    ],
                )

        self.stdout.write(self.style.SUCCESS("\n✓ Загрузка завершена:\n"))
        for key, (created, updated) in stats.items():
            self.stdout.write(f"  {key:<22} создано: {created}  обновлено: {updated}")

    # ── Helpers ───────────────────────────────────────────────────────

    def _confirm_replace(self):
        self.stdout.write(self.style.WARNING(
            "\n⚠  ВНИМАНИЕ: флаг --replace удалит существующие данные в таблицах!\n"
        ))
        answer = input("Продолжить? [y/N]: ").strip().lower()
        if answer != "y":
            raise CommandError("Отменено пользователем.")

    def _load_model(self, Model, rows, update_fields):
        """update_or_create по pk для обычных моделей."""
        created = updated = 0
        for row in rows:
            pk = row.pop("id", None)
            if pk is None:
                continue
            _, was_created = Model.objects.update_or_create(
                pk=pk,
                defaults=row,
            )
            if was_created:
                created += 1
            else:
                updated += 1
        return created, updated

    def _load_dishes(self, rows, replace):
        """
        Загружает блюда.
        Поле image сохраняется как строка-путь (например 'dishes/burger.webp').
        Поле embedding не трогаем.
        """
        from menu.models import Dish

        created = updated = 0
        skip_fields = {"embedding", "created_at", "updated_at"}

        for row in rows:
            pk = row.pop("id", None)
            if pk is None:
                continue

            # Убираем поля которые не нужно восстанавливать
            clean = {k: v for k, v in row.items() if k not in skip_fields}

            _obj, was_created = Dish.objects.update_or_create(
                pk=pk,
                defaults=clean,
            )
            if was_created:
                created += 1
            else:
                updated += 1
        return created, updated

    def _load_users(self, rows):
        """
        Загружает пользователей без паролей.
        Суперпользователи не перезаписываются.
        """
        from accounts.models import User

        created = updated = 0
        for row in rows:
            pk = row.pop("id", None)
            if pk is None:
                continue

            is_superuser = row.get("is_superuser", False)

            # Не трогаем существующих суперпользователей
            if is_superuser and User.objects.filter(pk=pk).exists():
                continue

            obj, was_created = User.objects.update_or_create(
                pk=pk,
                defaults=row,
            )
            if was_created:
                obj.set_unusable_password()
                obj.save(update_fields=["password"])
                created += 1
            else:
                updated += 1

        return created, updated
