try:
    from django.db.backends.ddl_references import Statement
    from pgvector.django import HnswIndex, VectorExtension, VectorField

    def is_vector_extension_installed(connection) -> bool:
        if getattr(connection, 'vendor', '') != 'postgresql':
            return False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_extension WHERE extname = %s", ['vector'])
                return bool(cursor.fetchone())
        except Exception:
            return False

    def is_vector_extension_available(connection) -> bool:
        if getattr(connection, 'vendor', '') != 'postgresql':
            return False
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_extension WHERE extname = %s", ['vector'])
                if cursor.fetchone():
                    return True
                cursor.execute("SELECT 1 FROM pg_available_extensions WHERE name = %s", ['vector'])
                return bool(cursor.fetchone())
        except Exception:
            return False

    if not getattr(VectorExtension, '_safe_patched', False):
        _orig_ext_forwards = VectorExtension.database_forwards

        def _safe_ext_forwards(self, app_label, schema_editor, from_state, to_state):
            if not is_vector_extension_available(schema_editor.connection):
                return
            try:
                _orig_ext_forwards(self, app_label, schema_editor, from_state, to_state)
            except Exception:
                pass

        VectorExtension.database_forwards = _safe_ext_forwards
        VectorExtension._safe_patched = True

    if not getattr(HnswIndex, '_safe_patched', False):
        _orig_create_sql = HnswIndex.create_sql
        _orig_remove_sql = HnswIndex.remove_sql

        def _safe_create_sql(self, model, schema_editor, using='', **kwargs):
            if not is_vector_extension_installed(schema_editor.connection):
                return Statement('SELECT 1')
            return _orig_create_sql(self, model, schema_editor, using=using, **kwargs)

        def _safe_remove_sql(self, model, schema_editor, **kwargs):
            if not is_vector_extension_installed(schema_editor.connection):
                return Statement('SELECT 1')
            return _orig_remove_sql(self, model, schema_editor, **kwargs)

        HnswIndex.create_sql = _safe_create_sql
        HnswIndex.remove_sql = _safe_remove_sql
        HnswIndex._safe_patched = True

    if not getattr(VectorField, '_safe_patched', False):
        _orig_db_type = VectorField.db_type

        def _safe_db_type(self, connection):
            if is_vector_extension_installed(connection):
                return _orig_db_type(self, connection)
            return 'text'

        VectorField.db_type = _safe_db_type
        VectorField._safe_patched = True
except ImportError:
    pass
