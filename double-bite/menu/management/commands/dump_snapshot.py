"""
Management command: dump_snapshot
----------------------------------
Сохраняет слепок данных из БД в папку snapshots/ в корне проекта.

Сохраняемые данные:
  - menu.Category
  - menu.Dish        (без поля embedding)
  - menu.DishOption
  - accounts.User    (без паролей — только профильные данные)
  - accounts.DeliveryAddress
  - orders.Cart + CartItem
  - orders.Order + OrderItem

Использование:
  python manage.py dump_snapshot
  python manage.py dump_snapshot --tag my_backup
  python manage.py dump_snapshot --output /custom/path/
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand


def _default(obj):
    """JSON serializer для типов, которые не сериализуются по умолчанию."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


class Command(BaseCommand):
    help = "Создаёт слепок данных из БД и сохраняет в snapshots/ (корень проекта)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tag",
            type=str,
            default="",
            help="Дополнительная метка в имени файла (например: --tag before_migration)",
        )
        parser.add_argument(
            "--output",
            type=str,
            default="",
            help="Кастомная папка для сохранения (по умолчанию: <project_root>/snapshots/)",
        )
        parser.add_argument(
            "--indent",
            type=int,
            default=2,
            help="Отступ JSON (0 = компактный)",
        )

    def handle(self, *args, **options):
        # Импорты здесь, чтобы не падать при collectstatic и т.д.
        from accounts.models import DeliveryAddress, User
        from orders.models import Cart, CartItem, Order, OrderItem

        from menu.models import Category, Dish, DishOption

        # --- Определяем папку для сохранения ---
        if options["output"]:
            snapshots_dir = Path(options["output"])
        else:
            # Корень проекта = папка выше double-bite/manage.py
            manage_py = Path(__file__).resolve()
            # Поднимаемся: commands -> management -> menu -> double-bite -> project_root
            project_root = manage_py.parents[4]
            snapshots_dir = project_root / "snapshots"

        snapshots_dir.mkdir(parents=True, exist_ok=True)

        # --- Формируем имя файла ---
        ts = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
        tag = f"_{options['tag']}" if options["tag"] else ""
        filename = f"snapshot_{ts}{tag}.json"
        filepath = snapshots_dir / filename

        # --- Собираем данные ---
        self.stdout.write("Собираю данные...")

        categories = list(Category.objects.values())
        dishes = list(Dish.objects.defer("embedding").values())
        dish_options = list(DishOption.objects.values())

        users = list(
            User.objects.values(
                "id", "email", "first_name", "last_name",
                "phone", "role", "is_active", "is_staff",
                "is_superuser", "date_joined", "created_at",
            )
        )
        delivery_addresses = list(DeliveryAddress.objects.values())

        carts = list(Cart.objects.values())
        cart_items = list(CartItem.objects.values())

        orders = list(Order.objects.values())
        order_items = list(OrderItem.objects.values())

        # --- Мета-информация ---
        snapshot = {
            "_meta": {
                "created_at": datetime.now(tz=UTC).isoformat(),
                "tag": options["tag"] or None,
                "counts": {
                    "categories": len(categories),
                    "dishes": len(dishes),
                    "dish_options": len(dish_options),
                    "users": len(users),
                    "delivery_addresses": len(delivery_addresses),
                    "carts": len(carts),
                    "cart_items": len(cart_items),
                    "orders": len(orders),
                    "order_items": len(order_items),
                },
            },
            "categories": categories,
            "dishes": dishes,
            "dish_options": dish_options,
            "users": users,
            "delivery_addresses": delivery_addresses,
            "carts": carts,
            "cart_items": cart_items,
            "orders": orders,
            "order_items": order_items,
        }

        # --- Сохраняем ---
        indent = options["indent"] if options["indent"] > 0 else None
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=indent, default=_default)

        size_kb = filepath.stat().st_size / 1024
        self.stdout.write(self.style.SUCCESS(f"\n✓ Снапшот сохранён: {filepath}"))
        self.stdout.write(f"  Размер: {size_kb:.1f} KB")
        self.stdout.write("\n  Записи:")
        for key, count in snapshot["_meta"]["counts"].items():
            self.stdout.write(f"    {key:<22} {count}")
