import threading

class StreamChecker:
    def __init__(self, config):
        self.config = config
        self.stream_groups = {}  # {channel_name: [url1, url2, ...]}
        self.current_index = {}  # {channel_name: 0}
        self.load_stream_groups()

    def load_stream_groups(self):
        for name, urls in self.config["channels"].items():
            self.stream_groups[name] = urls
            self.current_index[name] = 0

    def get_active_streams(self):
        return {
            channel: urls[self.current_index.get(channel, 0)]
            for channel, urls in self.stream_groups.items()
            if urls
        }

    def mark_stream_failed(self, channel):
        """Advance to the next stream for a channel, with loop fallback."""
        if channel not in self.stream_groups:
            return
        current = self.current_index.get(channel, 0)
        total = len(self.stream_groups[channel])
        self.current_index[channel] = (current + 1) % total  # loop back
        print(f"[FAILOVER] {channel} → Switched to index {self.current_index[channel]}")

    def update_config(self, new_config):
        """Reload config without losing current index if possible."""
        self.config = new_config
        self.load_stream_groups()
