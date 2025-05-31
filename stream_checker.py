import threading
import time
import requests
import logging

logger = logging.getLogger(__name__)


class StreamChecker:
    def __init__(self, config):
        self.config = config
        self.stream_groups = {}
        self.current_index = {}
        self.lock = threading.Lock()
        self.load_stream_groups()

    def load_stream_groups(self):
        with self.lock:
            for name, urls in self.config["channels"].items():
                self.stream_groups[name] = urls
                self.current_index.setdefault(name, 0)

    def get_active_streams(self):
        """Returns dictionary of {channel: entry_dict}"""
        with self.lock:
            active = {}
            for channel, entries in self.stream_groups.items():
                if not entries:
                    continue
                idx = self.current_index.get(channel, 0)
                active[channel] = entries[idx]  # Return the full entry dictionary
            return active

    def mark_stream_failed(self, channel):
        with self.lock:
            if channel not in self.stream_groups:
                return
            current = self.current_index.get(channel, 0)
            total = len(self.stream_groups[channel])
            if total > 0: # Avoid modulo by zero if a group becomes empty
                self.current_index[channel] = (current + 1) % total
                logger.info(f"[FAILOVER] {channel} → Switched to index {self.current_index[channel]}")
            else:
                logger.warning(f"[FAILOVER] Attempted to failover channel {channel}, but it has no streams.")

    def update_config(self, new_app_config): # new_app_config is {"channels": new_grouped_channels}
        """Reload config, attempting to preserve current stream indexes for existing groups."""
        with self.lock:
            self.config = new_app_config # Update the main config store

            new_stream_groups = {}
            old_current_index = self.current_index.copy() # Preserve old current_index
            self.current_index = {} # Reset current_index to rebuild

            for group_name, entries_list in self.config.get("channels", {}).items():
                new_stream_groups[group_name] = entries_list
                if group_name in old_current_index and old_current_index[group_name] < len(entries_list):
                    self.current_index[group_name] = old_current_index[group_name]
                else:
                    self.current_index[group_name] = 0 # Default to 0 for new or changed/emptied groups
            
            self.stream_groups = new_stream_groups
            logger.info("StreamChecker configuration updated.")

    def background_monitor(self, interval=60):
        """Periodically checks if current streams are working."""
        while True:
            logger.info("[Monitor] Checking active streams...")
            active_streams = self.get_active_streams()
            for channel, entry in active_streams.items():
                if not self._is_stream_working(entry):
                    logger.warning(f"[Monitor] {channel} stream failed (URL: {entry.get('url')}). Advancing...")
                    self.mark_stream_failed(channel)
            time.sleep(interval)

    def _is_stream_working(self, entry):
        """Check if a stream URL is working (accepts either entry dict or raw URL)"""
        try:
            url = entry['url'] if isinstance(entry, dict) else entry
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
            }
            response = requests.get(url, headers=headers, timeout=5, stream=True)
            return response.status_code in [200, 301, 302]
        except Exception as e:
            logger.debug(f"[Monitor] Stream check failed for URL {url}: {str(e)}")
            return False
