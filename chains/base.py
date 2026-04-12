from __future__ import annotations
from abc import ABC, abstractmethod


class ChainReader(ABC):
    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def read_pool_position(self) -> dict: ...
