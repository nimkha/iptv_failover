import threading
import time
import requests
import logging
import concurrent.futures

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
            # Adjust max_workers as needed. More workers can speed up checks for groups
            # with many streams, but too many can also lead to resource issues or
            # getting rate-limited. 10 is a reasonable default.
            max_workers_for_check = 10

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers_for_check) as executor:
                for group_name, entries in self.stream_groups.items():
                    if not entries:
                        logger.debug(f"Channel group '{group_name}' has no streams, skipping.")
                        continue

                    num_entries = len(entries)
                    start_index = self.current_index.get(group_name, 0)
                    if start_index >= num_entries: # Safety check
                        start_index = 0
                        self.current_index[group_name] = 0

                    # Maps a future object to the original index of the stream in the 'entries' list
                    future_to_original_index = {
                        executor.submit(self._is_stream_working, entry): i
                        for i, entry in enumerate(entries)
                    }
                    
                    # Stores the check result (True/False) for each stream by its original index
                    stream_check_results = [False] * num_entries

                    for future in concurrent.futures.as_completed(future_to_original_index):
                        original_idx = future_to_original_index[future]
                        try:
                            is_working = future.result()
                            stream_check_results[original_idx] = is_working
                            if is_working:
                                logger.debug(f"Parallel check: Stream {entries[original_idx].get('url')} for group '{group_name}' (original index {original_idx}) is working.")
                            # else: # No need to log non-working ones here if too verbose, _is_stream_working already logs
                            #     logger.debug(f"Parallel check: Stream {entries[original_idx].get('url')} for group '{group_name}' (original index {original_idx}) is NOT working.")
                        except Exception as exc:
                            logger.error(f"Exception during parallel check for stream {entries[original_idx].get('url')}: {exc}")
                            stream_check_results[original_idx] = False # Mark as not working on error

                    # Now, select the first working stream based on the failover order
                    chosen_stream_entry = None
                    chosen_stream_original_index = -1

                    for i in range(num_entries):
                        # This is the index in 'entries' we'd try according to failover logic
                        idx_to_try = (start_index + i) % num_entries
                        if stream_check_results[idx_to_try]:
                            chosen_stream_entry = entries[idx_to_try]
                            chosen_stream_original_index = idx_to_try
                            break 
                    
                    if chosen_stream_entry:
                        logger.info(f"Selected working stream for group '{group_name}': {chosen_stream_entry.get('url')} (original index {chosen_stream_original_index}) after parallel checks.")
                        active_working_streams[group_name] = chosen_stream_entry
                        self.current_index[group_name] = chosen_stream_original_index
                    else:
                        logger.warning(f"No working streams found for channel group '{group_name}' after checking all {num_entries} streams in parallel. Group will be omitted from playlist.")
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
