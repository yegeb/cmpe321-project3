class BufferManager:
    def __init__(self, config: dict, disk):
        self.config = config
        self.disk = disk
        self.requests = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.dirty_writebacks = 0

    def flush(self):
        return None
