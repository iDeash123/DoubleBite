import pgvector.django.indexes
import pgvector.django.vector
from django.db import migrations
from pgvector.django import VectorExtension


class Migration(migrations.Migration):

    dependencies = [
        ('menu', '0003_alter_dish_image'),
    ]

    operations = [
        VectorExtension(),
        migrations.AddField(
            model_name='dish',
            name='embedding',
            field=pgvector.django.vector.VectorField(blank=True, dimensions=1024, null=True, verbose_name='Векторний ембедінг'),
        ),
        migrations.AddIndex(
            model_name='dish',
            index=pgvector.django.indexes.HnswIndex(ef_construction=64, fields=['embedding'], m=16, name='dish_embedding_hnsw_idx', opclasses=['vector_cosine_ops']),
        ),
    ]
