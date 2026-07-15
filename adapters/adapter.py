from abc import ABC, abstractmethod
from typing import List

class APIAdapter(ABC):
    @abstractmethod
    async def geocode(self, place_name: str) -> dict:
        """Convert a place name to coordinates."""
        pass

    @abstractmethod
    async def reverse_geocode(self, lat: float, lng: float) -> str:
        """Convert coordinates to a readable address."""
        pass

    @abstractmethod
    async def get_directions(
        self, 
        origin: dict, 
        destination: dict,
        option: dict,
        via: List[dict] = None, 
        avoid_roads: List[str] = None,
        avoid_features: List[str] = None,
    ) -> dict:
        """Return routing directions from origin to destination."""
        pass
