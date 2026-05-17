"""
fsm_repository.py
~~~~~~~~~~~~~~~~~
SQLite реализация CursorRepository для llm-nano-vm.
"""
from typing import Optional
from datetime import datetime, timezone
from nano_vm.vm import Cursor
from ..db import get_db

class SqliteCursorRepository:
    async def save(self, cursor: Cursor) -> None:
        """Сохраняет слепок состояния (курсор) в БД."""
        cursor_json = cursor.model_dump_json()
        now = datetime.now(timezone.utc).isoformat()
        
        # Извлекаем order_id из контекста (сохранен при старте)
        order_id = cursor.context.get("order_id", "unknown")
        
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO fsm_cursors (trace_id, order_id, cursor_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    cursor_json = excluded.cursor_json,
                    updated_at = excluded.updated_at
                """,
                (cursor.trace_id, order_id, cursor_json, now, now)
            )

    async def load(self, trace_id: str) -> Optional[Cursor]:
        """Загружает курсор для resume."""
        with get_db() as conn:
            row = conn.execute("SELECT cursor_json FROM fsm_cursors WHERE trace_id = ?", (trace_id,)).fetchone()
            
        if row:
            return Cursor.model_validate_json(row["cursor_json"])
        return None

    async def get_trace_id_by_order(self, order_id: str) -> Optional[str]:
        """Хелпер: найти trace_id по order_id (вызывается из вебхука)."""
        with get_db() as conn:
            row = conn.execute("SELECT trace_id FROM fsm_cursors WHERE order_id = ?", (order_id,)).fetchone()
            
        return row["trace_id"] if row else None
