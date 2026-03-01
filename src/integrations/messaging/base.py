from abc import ABC, abstractmethod


class MessagingGateway(ABC):
    """Abstract base for all messaging channels."""

    @abstractmethod
    async def send_message(self, text: str) -> None:
        """Send a message to the user."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Start listening for incoming messages."""
        ...
