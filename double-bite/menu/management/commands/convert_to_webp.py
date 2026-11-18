from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from PIL import Image


class Command(BaseCommand):
    help = 'Конвертує всі зображення страв у media/dishes/ у формат WebP'

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete-originals',
            action='store_true',
            default=False,
            help='Видалити вихідні JPG/PNG файли після успішної конвертації у WebP',
        )

    def handle(self, *args, **options):
        dishes_dir = Path(settings.MEDIA_ROOT) / 'dishes'
        if not dishes_dir.exists():
            self.stdout.write(self.style.WARNING(f'Директорія {dishes_dir} не знайдена.'))
            return

        extensions = ('*.jpg', '*.jpeg', '*.png')
        image_files = []
        for ext in extensions:
            image_files.extend(dishes_dir.glob(ext))

        converted_count = 0
        total_saved_bytes = 0
        delete_originals = bool(options.get('delete_originals'))

        self.stdout.write(f'Знайдено {len(image_files)} зображень для перевірки...')

        for img_path in image_files:
            webp_path = img_path.with_suffix('.webp')
            orig_size = img_path.stat().st_size

            # If webp already exists and is newer than source, skip
            if webp_path.exists() and webp_path.stat().st_mtime >= img_path.stat().st_mtime:
                continue

            try:
                with Image.open(img_path) as img:
                    if img.mode in ('RGBA', 'LA'):
                        img.save(webp_path, 'WEBP', quality=82, method=4)
                    else:
                        rgb = img.convert('RGB')
                        rgb.save(webp_path, 'WEBP', quality=82, method=4)

                webp_size = webp_path.stat().st_size
                saved = orig_size - webp_size
                total_saved_bytes += saved
                converted_count += 1
                if delete_originals and webp_path.exists() and webp_size > 0:
                    img_path.unlink(missing_ok=True)
                self.stdout.write(
                    f'  [OK] {img_path.name} -> {webp_path.name} '
                    f'({orig_size // 1024}KB -> {webp_size // 1024}KB)'
                )
            except (OSError, ValueError, Image.DecompressionBombError) as e:
                self.stdout.write(self.style.ERROR(f'  [ERROR] {img_path.name}: {e}'))

        self.stdout.write(
            self.style.SUCCESS(
                f'Завершено! Сконвертовано {converted_count} нових файлів. '
                f'Заощаджено {total_saved_bytes / (1024 * 1024):.2f} MB.'
            )
        )
