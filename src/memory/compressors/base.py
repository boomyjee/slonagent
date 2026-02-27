class BaseCompressor:
    async def compress(self, turns: list) -> list:
        raise NotImplementedError
