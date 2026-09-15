try:
    from django.db.backends.ddl_references import Statement
    from pgvector.django import HnswIndex

    if not getattr(HnswIndex, '_safe_patched', False):
        _orig_create_sql = HnswIndex.create_sql
        _orig_remove_sql = HnswIndex.remove_sql

        def _safe_create_sql(self, model, schema_editor, using='', **kwargs):
            if schema_editor.connection.vendor != 'postgresql':
                return Statement('SELECT 1')
            return _orig_create_sql(self, model, schema_editor, using=using, **kwargs)

        def _safe_remove_sql(self, model, schema_editor, **kwargs):
            if schema_editor.connection.vendor != 'postgresql':
                return Statement('SELECT 1')
            return _orig_remove_sql(self, model, schema_editor, **kwargs)

        HnswIndex.create_sql = _safe_create_sql
        HnswIndex.remove_sql = _safe_remove_sql
        HnswIndex._safe_patched = True
except ImportError:
    pass
