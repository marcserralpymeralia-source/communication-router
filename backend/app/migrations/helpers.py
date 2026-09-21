from __future__ import annotations

from hashlib import sha256

from sqlalchemy import inspect, text


def checksum_text(*parts: str) -> str:
    digest = sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def table_exists(engine, table_name: str) -> bool:  # noqa: ANN001
    with engine.connect() as conn:
        return table_name in inspect(conn).get_table_names()


def existing_columns(engine, table_name: str) -> set[str]:  # noqa: ANN001
    with engine.connect() as conn:
        inspector = inspect(conn)
        if table_name not in inspector.get_table_names():
            return set()
        return {column["name"] for column in inspector.get_columns(table_name)}


def ensure_columns(engine, table_name: str, columns: dict[str, str], *, dry_run: bool = False) -> list[str]:  # noqa: ANN001
    with engine.connect() as conn:
        inspector = inspect(conn)
        if table_name not in inspector.get_table_names():
            return []
        current_columns = {column["name"] for column in inspector.get_columns(table_name)}
    actions: list[str] = []
    with engine.begin() as conn:
        for column_name, column_sql in columns.items():
            if column_name in current_columns:
                continue
            statement = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
            actions.append(statement)
            if not dry_run:
                conn.execute(text(statement))
    return actions


def ensure_unique_index(engine, table_name: str, index_name: str, columns: tuple[str, ...], *, dry_run: bool = False) -> list[str]:  # noqa: ANN001
    with engine.connect() as conn:
        inspector = inspect(conn)
        if table_name not in inspector.get_table_names():
            return []
        existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if index_name in existing_indexes:
        return []
    statement = f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table_name} ({', '.join(columns)})"
    if not dry_run:
        with engine.begin() as conn:
            conn.execute(text(statement))
    return [statement]


def ensure_index(engine, table_name: str, index_name: str, columns: tuple[str, ...], *, dry_run: bool = False) -> list[str]:  # noqa: ANN001
    with engine.connect() as conn:
        inspector = inspect(conn)
        if table_name not in inspector.get_table_names():
            return []
        existing = next(
            (index for index in inspector.get_indexes(table_name) if index["name"] == index_name),
            None,
        )
    if existing:
        if tuple(existing.get("column_names") or ()) != columns or existing.get("unique", False):
            raise RuntimeError(
                f"El índice {index_name} existente no coincide con {table_name}({', '.join(columns)})"
            )
        return []
    statement = f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({', '.join(columns)})"
    if not dry_run:
        with engine.begin() as conn:
            conn.execute(text(statement))
    return [statement]


def ensure_postgresql_foreign_key(
    engine,
    table_name: str,
    constrained_columns: tuple[str, ...],
    referred_table: str,
    referred_columns: tuple[str, ...],
    constraint_name: str,
    *,
    ondelete: str,
    dry_run: bool = False,
) -> list[str]:  # noqa: ANN001
    if engine.dialect.name != "postgresql":
        return []
    with engine.connect() as conn:
        inspector = inspect(conn)
        if table_name not in inspector.get_table_names():
            return []
        current_columns = {column["name"] for column in inspector.get_columns(table_name)}
        foreign_keys = inspector.get_foreign_keys(table_name)
    missing_columns = set(constrained_columns) - current_columns
    statement = (
        f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} "
        f"FOREIGN KEY ({', '.join(constrained_columns)}) REFERENCES {referred_table} "
        f"({', '.join(referred_columns)}) ON DELETE {ondelete}"
    )
    if missing_columns:
        if dry_run:
            return [statement]
        raise RuntimeError(
            f"No se puede añadir {constraint_name}: faltan columnas en {table_name}: "
            f"{', '.join(sorted(missing_columns))}"
        )
    expected = {
        "constrained_columns": list(constrained_columns),
        "referred_table": referred_table,
        "referred_columns": list(referred_columns),
        "ondelete": ondelete,
    }
    for foreign_key in foreign_keys:
        same_columns = foreign_key.get("constrained_columns") == expected["constrained_columns"]
        if not same_columns:
            continue
        current = {
            "constrained_columns": foreign_key.get("constrained_columns"),
            "referred_table": foreign_key.get("referred_table"),
            "referred_columns": foreign_key.get("referred_columns"),
            "ondelete": (foreign_key.get("options") or {}).get("ondelete"),
        }
        if current != expected:
            raise RuntimeError(
                f"La FK de {table_name}.{', '.join(constrained_columns)} no coincide con el contrato esperado"
            )
        return []

    with engine.connect() as conn:
        orphan_count = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {table_name} child "
                f"LEFT JOIN {referred_table} parent ON child.{constrained_columns[0]} = parent.{referred_columns[0]} "
                f"WHERE child.{constrained_columns[0]} IS NOT NULL AND parent.{referred_columns[0]} IS NULL"
            )
        ).scalar_one()
    if orphan_count:
        raise RuntimeError(
            f"No se puede añadir {constraint_name}: {orphan_count} valor(es) huérfano(s) en "
            f"{table_name}.{constrained_columns[0]}"
        )

    if not dry_run:
        with engine.begin() as conn:
            conn.execute(text(statement))
    return [statement]


def ensure_postgresql_check_constraint(
    engine,
    table_name: str,
    constraint_name: str,
    expression: str,
    *,
    required_fragments: tuple[str, ...],
    dry_run: bool = False,
) -> list[str]:  # noqa: ANN001
    if engine.dialect.name != "postgresql":
        return []
    with engine.connect() as conn:
        inspector = inspect(conn)
        if table_name not in inspector.get_table_names():
            return []
        constraints = inspector.get_check_constraints(table_name)
    existing = next(
        (constraint for constraint in constraints if constraint.get("name") == constraint_name),
        None,
    )
    if existing:
        sqltext = (existing.get("sqltext") or "").upper()
        if not all(fragment.upper() in sqltext for fragment in required_fragments):
            raise RuntimeError(f"El CHECK {constraint_name} existente no coincide con el contrato esperado")
        return []
    statement = f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} CHECK ({expression})"
    if not dry_run:
        with engine.begin() as conn:
            conn.execute(text(statement))
    return [statement]
