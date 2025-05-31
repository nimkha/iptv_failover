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
            # Step 1: Safely copy data needed for checks.
            # This minimizes lock holding time during network operations.
            groups_to_process = {}
            for group_name, entries in self.stream_groups.items():
                if not entries:
                    logger.debug(f"Channel group '{group_name}' has no streams, skipping initial setup.")
                    continue
                current_idx_val = self.current_index.get(group_name, 0)
                # Ensure start_index is valid
                start_index = current_idx_val if 0 <= current_idx_val < len(entries) else 0
                if start_index != current_idx_val: # Log if we had to reset it
                    logger.debug(f"Corrected start_index for group '{group_name}' from {current_idx_val} to {start_index}.")

                groups_to_process[group_name] = {
                    "entries": list(entries),  # Shallow copy of the list of stream entry dicts
                    "start_index": start_index,
                    "num_entries": len(entries)
                }

        if not groups_to_process:
            logger.info("No groups to process in get_active_streams.")
            return {}

        active_working_streams = {}
        max_workers_for_check = 10  # Adjust as needed

        # Step 2: Perform stream checks in parallel for all streams across all groups
        # future_to_info: maps a future to (group_name, original_stream_index_in_group_entries)
        future_to_info = {}
        # group_check_results: { group_name: [False, True, ...] }
        group_check_results = {
            gn: [False] * g_data["num_entries"] for gn, g_data in groups_to_process.items()
        }

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers_for_check) as executor:
            for group_name, group_data in groups_to_process.items():
                for original_idx, entry in enumerate(group_data["entries"]):
                    future = executor.submit(self._is_stream_working, entry)
                    future_to_info[future] = (group_name, original_idx)
            
            for future in concurrent.futures.as_completed(future_to_info):
                group_name, original_idx = future_to_info[future]
                entry_for_log = groups_to_process[group_name]["entries"][original_idx]
                try:
                    is_working = future.result()
                    group_check_results[group_name][original_idx] = is_working
                    if is_working:
                        logger.debug(f"Parallel check: Stream {entry_for_log.get('url')} for group '{group_name}' (idx {original_idx}) is working.")
                except Exception as exc:
                    logger.error(f"Exception during parallel check for stream {entry_for_log.get('url', 'N/A')} in group {group_name}: {exc}")
                    # group_check_results[group_name][original_idx] remains False (default)

        # Step 3: Select the best stream for each group and prepare updates for current_index
        new_current_indices = {}
        for group_name, group_data in groups_to_process.items():
            entries = group_data["entries"]
            start_index = group_data["start_index"]
            num_entries = group_data["num_entries"]
            results_for_group = group_check_results[group_name]

            chosen_stream_entry = None
            chosen_stream_original_index = -1

            for i in range(num_entries):
                idx_to_try = (start_index + i) % num_entries
                if results_for_group[idx_to_try]:
                    chosen_stream_entry = entries[idx_to_try]
                    chosen_stream_original_index = idx_to_try
                    break
            
            if chosen_stream_entry:
                logger.info(f"Selected working stream for group '{group_name}': {chosen_stream_entry.get('url')} (original index {chosen_stream_original_index}) after parallel checks.")
                active_working_streams[group_name] = chosen_stream_entry
                new_current_indices[group_name] = chosen_stream_original_index
            else:
                logger.warning(f"No working streams found for channel group '{group_name}' after checking all {num_entries} streams in parallel. Group will be omitted from playlist.")

        # Step 4: Atomically update self.current_index for groups where a working stream was found
        if new_current_indices:
            # Adjust max_workers as needed. More workers can speed up checks for groups
            # with many streams, but too many can also lead to resource issues or
            # getting rate-limited. 10 is a reasonable default.
            with self.lock:
                for group_name, new_idx in new_current_indices.items():
                    # Ensure group still exists in case of concurrent update_config
                    if group_name in self.stream_groups: # Check if group still exists
                         self.current_index[group_name] = new_idx
        
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
                # Using a common, recent User-Agent
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            # Increased timeout to 10 seconds
            response = requests.get(url, headers=headers, timeout=10, stream=True)
            
            if response.status_code in [200, 301, 302]:
                return True
            else:
                logger.debug(f"Stream check for URL {url} returned non-OK status: {response.status_code}")
                return False
        except requests.exceptions.Timeout:
            logger.debug(f"Stream check timed out for URL {url} (10s)")
            return False
        except Exception as e:
            logger.debug(f"Stream check failed for URL {url} with exception: {str(e)}")
            return False
