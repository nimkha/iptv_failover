from flask import Flask, Response
import glob
import time
from collections import defaultdict
from rapidfuzz import process, fuzz
from stream_checker import StreamChecker
import threading
import re
import xml.etree.ElementTree as ET
import os
import logging
from logging.handlers import RotatingFileHandler

FUZZY_MATCH_THRESHOLD = 70  # similarity threshold

# Configure logging only once
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler()
        ]
    )
    # File handler only in main process
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        file_handler = RotatingFileHandler(
            'logs/iptv_failover.log',
            maxBytes=1000000,
            backupCount=3
        )
        logging.getLogger().addHandler(file_handler)

logger = logging.getLogger(__name__)

def auto_reload_m3u(interval=172800):  # Default: every 48 hours
    while True:
        time.sleep(interval)
        logger.info("Attempting to reload M3U playlist and EPG data...")
        new_entries = parse_m3u_files("input/")
        new_grouped_channels = group_channels(new_entries)
        checker.update_config({"channels": new_grouped_channels}) # Pass the correct structure
        logger.info("M3U playlist and EPG data reloaded, checker updated.")

def normalize_name(name):
    """Normalize channel names by removing resolution tags, region suffixes, punctuation, and excess spaces."""
    name = name.lower()
    name = re.sub(r'\s*\(.*?\)|\[.*?\]', '', name)  # Remove content within parentheses or brackets 
    name = re.sub(r'\b(hd|fhd|uhd|4k|sd|uk|us|ca|au|de|pt|fr)\b', '', name)
    name = re.sub(r'^(uk:|us:|ca:|pt:|es:|tr:|lb:)', '', name)
    name = re.sub(r'[^\w\s]', '', name)  # Remove punctuation
    name = re.sub(r'\s+', ' ', name).strip()

    # Iteratively remove trailing numbers if they appear to be stream indices
    # following another number. e.g., "channel name 1 2" -> "channel name 1".
    # This handles cases like "bt sport 1 1" -> "bt sport 1".
    previous_name_state = None
    while name != previous_name_state:
        previous_name_state = name
        # Regex: Matches a string ending with "<text><space><digits><space><digits>"
        # and replaces it with "<text><space><digits>"
        name = re.sub(r'^(.*\s\d+)\s\d+$', r'\1', name)
    return name

def load_epg_map(epg_path="input/guide.xml"):
    """Create mapping from EPG data: {normalized_display_name: tvg-id}"""
    epg_map = {}
    try:
        tree = ET.parse(epg_path)
        for channel_node in tree.findall(".//channel"):
            tvg_id = channel_node.get("id")
            if not tvg_id:
                continue
            for name_node in channel_node.findall("display-name"):
                if name_node.text:
                    original_epg_name = name_node.text.strip()
                    normalized_epg_name = normalize_name(original_epg_name)
                    if normalized_epg_name not in epg_map: # First one wins in case of normalization collision
                        epg_map[normalized_epg_name] = tvg_id
                    else:
                        logger.warning(
                            f"EPG name collision: '{original_epg_name}' and other(s) normalize to "
                            f"'{normalized_epg_name}' (tvg-id: {tvg_id}). "
                            f"Keeping tvg-id '{epg_map[normalized_epg_name]}' from first encountered EPG entry."
                        )
        logger.info(f"Loaded {len(epg_map)} EPG mappings (using normalized display names).")
    except Exception as e:
        logger.error(f"EPG parsing error: {e}")
    return epg_map

def parse_m3u_files(m3u_folder="input/"):
    channel_entries = []
    # epg_map is now {normalized_epg_display_name: tvg_id}
    epg_map = load_epg_map()
    normalized_epg_names_for_fuzz = list(epg_map.keys())

    for m3u_file in glob.glob(f"{m3u_folder}/*.m3u"):
        logger.info(f"Parsing M3U file: {m3u_file}")
        with open(m3u_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXTINF"):
                attrs = {}
                match = re.search(r'#EXTINF:-1\s+(.*?)\s*,\s*(.*)', line)
                if match:
                    attr_str, display_name = match.groups()
                    for attr_match in re.finditer(r'(\w+?)="(.*?)"', attr_str):
                        key, value = attr_match.groups()
                        attrs[key] = value
                    attrs['display_name'] = display_name.strip() # Original M3U display name

                    # Normalize M3U display name for grouping and EPG lookup
                    current_channel_normalized_name = normalize_name(attrs['display_name'])
                    attrs['canonical_name'] = current_channel_normalized_name

                    # Attempt to find tvg-id using normalized names
                    tvg_id_from_epg = epg_map.get(current_channel_normalized_name)

                    if not tvg_id_from_epg and normalized_epg_names_for_fuzz: # If no direct match, try fuzzy
                        match_result = process.extractOne(
                            current_channel_normalized_name,
                            normalized_epg_names_for_fuzz,
                            scorer=fuzz.WRatio,
                            score_cutoff=FUZZY_MATCH_THRESHOLD
                        )
                        if match_result:
                            best_match_norm_name, score, _ = match_result
                            tvg_id_from_epg = epg_map.get(best_match_norm_name)
                            logger.info(f"Fuzzy EPG match for M3U: '{attrs['display_name']}' (norm: '{current_channel_normalized_name}') -> EPG norm: '{best_match_norm_name}' (tvg-id: {tvg_id_from_epg}, score: {score})")

                    if tvg_id_from_epg:
                        attrs['tvg-id'] = tvg_id_from_epg # Prioritize EPG-matched ID
                    elif not attrs.get('tvg-id'): # No tvg-id from M3U and no EPG match
                        logger.warning(f"No tvg-id found for M3U channel: '{attrs['display_name']}' (norm: '{current_channel_normalized_name}') after EPG lookup.")

                i += 1
                if i < len(lines):
                    url = lines[i].strip()
                    attrs['url'] = url
                    channel_entries.append(attrs)
            i += 1
    logger.info(f"Parsed {len(channel_entries)} channel entries from M3U files.")
    return channel_entries

def group_channels(channel_entries):
    grouped = defaultdict(list)
    for entry in channel_entries:
        norm_name = normalize_name(entry.get('display_name', ''))
        grouped[norm_name].append(entry)
    return grouped

def generate_playlist(grouped_channels):
    # This function appears to be unused. The active playlist is generated by `serve_playlist`.
    playlist = "#EXTM3U\n"
    for group in grouped_channels.values():
        for channel in group:
            extinf = f'#EXTINF:-1'
            if 'tvg-id' in channel:
                extinf += f' tvg-id="{channel["tvg-id"]}"'
            if 'tvg-name' in channel:
                extinf += f' tvg-name="{channel["tvg-name"]}"'
            if 'tvg-logo' in channel:
                extinf += f' tvg-logo="{channel["tvg-logo"]}"'
            if 'group-title' in channel:
                extinf += f' group-title="{channel["group-title"]}"'
            extinf += f',{channel["display_name"]}\n'
            playlist += f'{extinf}{channel["url"]}\n'
    return playlist

def create_app():
    global checker
    entries = parse_m3u_files("input/")
    grouped_channels = group_channels(entries)

    logger.info("Loaded channels:")
    for name, urls in grouped_channels.items():
        logger.info(f"- {name}: {len(urls)} stream(s)")

    checker = StreamChecker({"channels": grouped_channels})

    # Start automatic health monitor
    threading.Thread(target=checker.background_monitor, daemon=True).start()

    flask_app = Flask(__name__)
    
    @flask_app.route("/playlist.m3u")
    def serve_playlist():
        m3u = "#EXTM3U\n"
        active_streams = checker.get_active_streams() # {group_name: entry_dict}

        # Sort by group_name (canonical_name) for consistent channel numbering
        sorted_channel_groups = sorted(active_streams.items())

        channel_number = 1
        for group_name, entry in sorted_channel_groups: # group_name is the canonical_name
            if not entry:
                continue

            tvg_id = entry.get("tvg-id", "")
            # Use canonical_name (which is group_name) for tvg-name and display,
            # but allow original M3U tvg-name to override if present.
            canonical_name = entry.get('canonical_name', group_name)
            tvg_name_attr = entry.get("tvg-name", canonical_name)
            tvg_logo = entry.get("tvg-logo", "")
            group_title_attr = entry.get("group-title", "")

            extinf_parts = [f'#EXTINF:-1 tvg-chno="{channel_number}"']
            if tvg_id:
                extinf_parts.append(f'tvg-id="{tvg_id}"')
            extinf_parts.append(f'tvg-name="{tvg_name_attr}"')
            if tvg_logo:
                extinf_parts.append(f'tvg-logo="{tvg_logo}"')
            if group_title_attr:
                extinf_parts.append(f'group-title="{group_title_attr}"')

            extinf = " ".join(extinf_parts) + f',{canonical_name}' # Display name after comma
            m3u += f"{extinf}\n{entry['url']}\n"
            channel_number += 1
        return Response(m3u, mimetype="application/x-mpegURL")

    @flask_app.route("/failover/<channel>")
    def failover_channel(channel):
        checker.mark_stream_failed(channel)
        logger.info(f"Failover triggered for channel: {channel}")
        return f"Failover triggered for channel: {channel}\n"
    
    @flask_app.route("/epg.xml")
    def serve_modified_epg():
        """Serve modified EPG with normalized display names"""
        try:
            tree = ET.parse("input/guide.xml")
            root = tree.getroot()
            
            # Create mapping of tvg-id to canonical names
            tvg_id_to_canonical_name_map = {}
            if checker.config and "channels" in checker.config:
                for entries_list in checker.config["channels"].values():
                    for entry in entries_list:
                        if 'tvg-id' in entry and 'canonical_name' in entry:
                            # If multiple groups somehow map to the same tvg-id, last one wins.
                            tvg_id_to_canonical_name_map[entry['tvg-id']] = entry['canonical_name']
            
            # Update display names in EPG
            for channel_node in root.findall(".//channel"):
                tvg_id = channel_node.get("id")
                if tvg_id in tvg_id_to_canonical_name_map:
                    for display_name_node in channel_node.findall("display-name"):
                        display_name_node.text = tvg_id_to_canonical_name_map[tvg_id]
            
            # Return modified EPG
            epg_data = ET.tostring(root, encoding='utf-8', method='xml')
            return Response(epg_data, mimetype="application/xml")
            
        except Exception as e:
            logger.error(f"EPG modification error: {e}")
            return Response("# Error processing EPG", status=500)

    return flask_app

# Used by Gunicorn
app = create_app()

if __name__ == "__main__":
    # Start the M3U auto-reloader thread.
    threading.Thread(target=auto_reload_m3u, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)