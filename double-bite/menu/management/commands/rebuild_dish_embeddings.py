from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from support.vector_search import build_dish_embedding_text, get_embeddings_batch

from menu.models import Dish


class Command(BaseCommand):
    help = 'Пакетна перебудова векторних ембедінгів страв меню.'

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            '--only-missing',
            action='store_true',
            default=False,
            help='Оновити ембедінги лише для страв, де embedding IS NULL.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help='Примусово перебудувати ембедінги для всіх вибраних страв.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=32,
            help='Розмір пакета запитів до API та bulk_update (default: 32).',
        )
        parser.add_argument(
            '--include-unavailable',
            action='store_true',
            default=False,
            help='Включити недоступні страви та страви з неактивних категорій.',
        )

    def handle(self, *args, **options) -> None:
        only_missing = bool(options['only_missing'])
        force = bool(options['force'])
        batch_size = int(options['batch_size'])
        include_unavailable = bool(options['include_unavailable'])

        if batch_size < 1:
            raise CommandError('--batch-size must be at least 1.')

        qs = Dish.objects.select_related('category').order_by('id')
        if not include_unavailable:
            qs = qs.filter(is_available=True, category__is_active=True)
        if only_missing and not force:
            qs = qs.filter(embedding__isnull=True)

        dishes = list(qs)
        total_count = len(dishes)
        if total_count == 0:
            self.stdout.write(
                'Страви для перебудови ембедінгів не знайдено '
                '(total=0, updated=0, skipped=0, failed=0).'
            )
            return

        updated_count = 0
        skipped_count = 0
        failed_count = 0

        for start in range(0, total_count, batch_size):
            batch = dishes[start : start + batch_size]
            valid_dishes: list[Dish] = []
            texts: list[str] = []
            for dish in batch:
                text = build_dish_embedding_text(dish)
                if not text:
                    skipped_count += 1
                    continue
                valid_dishes.append(dish)
                texts.append(text)

            if not texts:
                continue

            vectors = get_embeddings_batch(texts)
            to_update: list[Dish] = []
            for idx, dish in enumerate(valid_dishes):
                vec = vectors[idx] if idx < len(vectors) else None
                if vec is None:
                    failed_count += 1
                    continue
                dish.embedding = vec
                to_update.append(dish)

            if to_update:
                with transaction.atomic():
                    Dish.objects.bulk_update(
                        to_update,
                        ['embedding'],
                        batch_size=batch_size,
                    )
                updated_count += len(to_update)

        summary = (
            f'Перебудова ембедінгів завершена: '
            f'всього={total_count}, оновлено={updated_count}, '
            f'пропущено={skipped_count}, помилок={failed_count}.'
        )
        if failed_count and updated_count == 0:
            self.stdout.write(self.style.WARNING(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
