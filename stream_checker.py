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
        """
        Returns a dictionary of {channel_group_name: working_entry_dict}.
        For each channel group, it tries to find the first working stream
        starting from its current_index and cycling through if necessary.
        If no working stream is found for a group, that group is omitted.
        """
        with self.lock:
            active_working_streams = {}
            for group_name, entries in self.stream_groups.items():
                if not entries:
                    logger.debug(f"Channel group '{group_name}' has no streams, skipping.")
                    continue

                num_entries = len(entries)
                # Get the starting index for the search, default to 0 if not set
                start_index = self.current_index.get(group_name, 0)
                if start_index >= num_entries: # Safety check if index is out of bounds
                    start_index = 0
                    self.current_index[group_name] = 0

                found_working_stream_for_group = False
                for i in range(num_entries): # Iterate once through all streams in the group
                    current_stream_index = (start_index + i) % num_entries
                    stream_entry = entries[current_stream_index]
                    
                    logger.debug(f"Checking stream {current_stream_index + 1}/{num_entries} for group '{group_name}': {stream_entry.get('url')}")
                    if self._is_stream_working(stream_entry):
                        logger.info(f"Found working stream for group '{group_name}': {stream_entry.get('url')} (index {current_stream_index})")
                        active_working_streams[group_name] = stream_entry
                        self.current_index[group_name] = current_stream_index # Update current_index to this working one
                        found_working_stream_for_group = True
                        break # Found a working stream for this group, move to the next group
                    else:
                        logger.warning(f"Stream {stream_entry.get('url')} for group '{group_name}' (index {current_stream_index}) is not working during active search.")
                
                if not found_working_stream_for_group:
                    logger.warning(f"No working streams found for channel group '{group_name}' after checking all {num_entries} streams. Group will be omitted from playlist.")
            return active_working_streams

    def mark_stream_failed(self, channel):
        with self.lock:
            if channel not in self.stream_groups:
                return
            current = self.current_index.get(channel, 0)
            total = len(self.stream_groups[channel])
            if total > 0: # Avoid modulo by zero if a group becomes empty
                self.current_index[channel] = (current + 1) % total # Advance to next stream
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
            logger.info("[Monitor] Starting background check of default streams...")
            
            streams_to_check_in_monitor = {}
            with self.lock: # Safely access shared stream_groups and current_index
                for channel_group_name, entries in self.stream_groups.items():
                    if not entries:
                        continue
                    # Get the current default stream for this group
                    idx = self.current_index.get(channel_group_name, 0)
                    if idx >= len(entries): # Safety check
                        idx = 0
                    streams_to_check_in_monitor[channel_group_name] = entries[idx]

            for channel, entry in streams_to_check_in_monitor.items():
                if not self._is_stream_working(entry):
                    logger.warning(f"[Monitor] Default stream for {channel} failed (URL: {entry.get('url')}). Advancing index via mark_stream_failed...")
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
