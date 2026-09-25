"""
Budget diario de órdenes. Regla #11:
- MAX_ORDERS_PER_DAY=4 hard cap
- Cada símbolo manda 2 órdenes (primary + runner)
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.errors import BudgetExceededError

ORDERS_PER_SYMBOL = 2  # primary + runner


@dataclass
class DailyOrderBudget:
    max_orders: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_orders - self.used)

    def can_send(self, n: int) -> bool:
        return self.used + n <= self.max_orders

    def consume(self, n: int = ORDERS_PER_SYMBOL) -> None:
        if not self.can_send(n):
            raise BudgetExceededError(
                f"Daily budget exceeded: {self.used}/{self.max_orders} used, "
                f"trying to add {n}"
            )
        self.used += n

    def try_consume(self, n: int = ORDERS_PER_SYMBOL) -> bool:
        """Variante que no levanta excepción. Retorna True si consumió."""
        if self.can_send(n):
            self.used += n
            return True
        return False

    def release(self, n: int = 1) -> None:
        """Devuelve al budget cupos reservados que no se llegaron a enviar.

        Sin esto, una orden que se reserva y luego se descarta (duplicado
        detectado en carrera) quemaría cupo del día sin existir en el broker.
        """
        self.used = max(0, self.used - n)
