class DiskSpaceManager:
    def __init__(self, config: dict):
        self.config = config
        self.read_count = 0
        self.write_count = 0

    def log_write(self, *args, **kwargs):
        return None
