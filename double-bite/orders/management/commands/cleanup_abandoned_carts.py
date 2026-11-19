from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from orders.models import Cart, CartItem


class Command(BaseCommand):
    help = 'Видаляє старі покинуті анонімні та порожні кошики користувачів.'

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help='Видаляти кошики, які не оновлювались більше вказаної кількості днів (default: 7).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Показати кількість кошиків до видалення без змін у БД.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=500,
            help='Розмір пакета видалення за одну транзакцію (default: 500).',
        )

    def handle(self, *args, **options) -> None:
        days: int = options['days']
        dry_run: bool = options['dry_run']
        batch_size: int = options['batch_size']

        if days < 1:
            raise CommandError('--days must be at least 1.')
        if batch_size < 1:
            raise CommandError('--batch-size must be at least 1.')

        cutoff = timezone.now() - timedelta(days=days)
        has_items = CartItem.objects.filter(cart_id=OuterRef('pk'))
        anon_qs = Cart.objects.filter(user__isnull=True, updated_at__lt=cutoff)
        empty_auth_qs = (
            Cart.objects.filter(user__isnull=False, updated_at__lt=cutoff)
            .annotate(_has_items=Exists(has_items))
            .filter(_has_items=False)
        )

        if dry_run:
            anon_count = anon_qs.count()
            empty_auth_count = empty_auth_qs.count()
            total_count = anon_count + empty_auth_count
            self.stdout.write(
                self.style.WARNING(
                    f'DRY-RUN: буде видалено {total_count} кошиків '
                    f'(анонімних: {anon_count}, порожніх користувачів: {empty_auth_count}) '
                    f'старіших за {days} дн.'
                )
            )
            return

        target_qs = (
            Cart.objects.filter(updated_at__lt=cutoff)
            .annotate(_has_items=Exists(has_items))
            .filter(Q(user__isnull=True) | Q(user__isnull=False, _has_items=False))
        )

        deleted_total = 0
        batches = 0
        while True:
            batch_ids = list(
                target_qs.order_by('id').values_list('id', flat=True)[:batch_size]
            )
            if not batch_ids:
                break
            with transaction.atomic():
                _, deleted_by_model = Cart.objects.filter(id__in=batch_ids).delete()
            deleted_total += deleted_by_model.get(Cart._meta.label, 0)
            batches += 1
            if len(batch_ids) < batch_size:
                break

        self.stdout.write(
            self.style.SUCCESS(
                f'Очищено {deleted_total} покинутих кошиків '
                f'у {batches} батчах (старіших за {days} дн., batch_size={batch_size}).'
            )
        )
