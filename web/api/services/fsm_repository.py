"""
fsm_repository.py
~~~~~~~~~~~~~~~~~
SQLite реализация CursorRepository для llm-nano-vm.
"""
import base64
import logging
import pickle
from datetime import datetime, timezone
from typing import Any, Optional

from ..db import get_db

logger = logging.getLogger(__name__)

class SqliteCursorRepository:
    async def save(self, cursor: Any) -> None:
        """Сохраняет слепок состояния (курсор) в БД."""
        # Используем pickle + base64, чтобы обойтись без прямого импорта 
        # внутренних классов из nano_vm (избегаем ImportError).
        try:
            cursor_b64 = base64.b64encode(pickle.dumps(cursor)).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to serialize cursor: {e}")
            raise
            
        now = datetime.now(timezone.utc).isoformat()
        
        # Безопасное извлечение атрибутов (через getattr, на случай если API изменится)
        trace_id = getattr(cursor, "trace_id", "unknown")
        context = getattr(cursor, "context", {})
        order_id = context.get("order_id", "unknown") if isinstance(context, dict) else "unknown"
        
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO fsm_cursors (trace_id, order_id, cursor_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    cursor_json = excluded.cursor_json,
                    updated_at = excluded.updated_at
                """,
                (trace_id, order_id, cursor_b64, now, now)
            )

    async def load(self, trace_id: str) -> Optional[Any]:
        """Загружает курсор для resume."""
        with get_db() as conn:
            row = conn.execute("SELECT cursor_json FROM fsm_cursors WHERE trace_id = ?", (trace_id,)).fetchone()
            
        if row and row["cursor_json"]:
            try:
                cursor_bytes = base64.b64decode(row["cursor_json"])
                return pickle.loads(cursor_bytes)
            except Exception as e:
                logger.error(f"Failed to deserialize cursor {trace_id}: {e}")
                return None
        return None

    async def get_trace_id_by_order(self, order_id: str) -> Optional[str]:
        """Хелпер: найти trace_id по order_id (вызывается из вебхука)."""
        with get_db() as conn:
            row = conn.execute("SELECT trace_id FROM fsm_cursors WHERE order_id = ?", (order_id,)).fetchone()
            
        return row["trace_id"] if row else None