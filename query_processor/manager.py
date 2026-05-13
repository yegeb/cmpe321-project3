class QueryProcessor:
    def __init__(self, config: dict, file_idx, buffer, disk):
        self.config = config
        self.file_idx = file_idx
        self.buffer = buffer
        self.disk = disk

    def process(self, line: str):
        return None
