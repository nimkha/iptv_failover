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

def auto_reload_m3u(interval=172800):  # every 48 hours
    while True:
        new_config = parse_m3u_files("input/")
        checker.config = new_config
        logger.info("M3U playlist reloaded")
        time.sleep(interval)

def normalize_name(name):
    """Normalize channel names by removing resolution tags, region suffixes, punctuation, and excess spaces."""
    name = name.lower()
    name = re.sub(r'\s*\(.*?\)|\[.*?\]', '', name)  # Remove content within parentheses or brackets 
    name = re.sub(r'\b(hd|fhd|uhd|4k|sd|uk|us|ca|au|de|pt|fr)\b', '', name)
    name = re.sub(r'^(uk:|us:|ca:|pt:|es:|tr:|lb:)', '', name)
    name = re.sub(r'[^\w\s]', '', name)  # Remove punctuation
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def load_epg_map(epg_path="input/guide.xml"):
    """Create mapping from EPG data: {display-name: tvg-id}"""
    epg_map = {}
    try:
        tree = ET.parse(epg_path)
        for channel in tree.findall(".//channel"):
            tvg_id = channel.get("id")
            if not tvg_id:
                continue
            for name in channel.findall("display-name"):
                if name.text:
                    epg_map[name.text.strip()] = tvg_id
        logger.info(f"Loaded {len(epg_map)} EPG mappings")
    except Exception as e:
        logger.error(f"EPG parsing error: {e}")
    return epg_map

# def parse_m3u_entry(extinf_line):
#     """Parse EXTINF line with all attributes"""
#     entry = {
#         'tvg-id': '',
#         'tvg-name': '',
#         'tvg-logo': '',
#         'group-title': '',
#         'name': '',
#         'extinf': extinf_line
#     }
    
#     # Extract attributes
#     attr_match = re.search(r'#EXTINF:-1\s+(.*?),', extinf_line)
#     if attr_match:
#         attrs = attr_match.group(1)
#         for attr in attrs.split(' '):
#             if '=' in attr:
#                 key, val = attr.split('=', 1)
#                 val = val.strip('"')
#                 if key in entry:
#                     entry[key] = val
    
#     # Extract display name (after last comma)
#     name_match = extinf_line.rsplit(',', 1)
#     if len(name_match) > 1:
#         entry['name'] = name_match[1].strip()
    
#     return entry

def parse_m3u_files(m3u_folder="input/"):
    channel_entries = []

    for m3u_file in glob.glob(f"{m3u_folder}/*.m3u"):
        with open(m3u_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXTINF"):
                # Extract attributes from the EXTINF line
                attrs = {}
                match = re.search(r'#EXTINF:-1\s+(.*?)\s*,\s*(.*)', line)
                if match:
                    attr_str, display_name = match.groups()
                    # Extract key="value" pairs
                    for attr_match in re.finditer(r'(\w+?)="(.*?)"', attr_str):
                        key, value = attr_match.groups()
                        attrs[key] = value
                    attrs['display_name'] = display_name

                # The next line should be the URL
                i += 1
                if i < len(lines):
                    url = lines[i].strip()
                    attrs['url'] = url
                    channel_entries.append(attrs)
            i += 1

    return channel_entries

def group_channels(channel_entries):
    grouped = defaultdict(list)
    for entry in channel_entries:
        norm_name = normalize_name(entry.get('display_name', ''))
        grouped[norm_name].append(entry)
    return grouped

def generate_playlist(grouped_channels):
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
        active_streams = checker.get_active_streams()
        
        for channel, entry in active_streams.items():
            if not entry:
                continue
                
            extinf = (f'#EXTINF:-1 tvg-id="{entry["tvg-id"]}" '
                     f'tvg-name="{entry["tvg-name"]}" '
                     f'tvg-logo="{entry.get("tvg-logo", "")}" '
                     f'group-title="{entry["group-title"]}",'
                     f'{entry["canonical_name"]}')
            
            m3u += f"{extinf}\n{entry['url']}\n"
        
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
            name_map = {
                entry['tvg-id']: entry['canonical_name']
                for entries in checker.config["channels"].values()
                for entry in entries
                if 'tvg-id' in entry
            }
            
            # Update display names in EPG
            for channel in root.findall(".//channel"):
                tvg_id = channel.get("id")
                if tvg_id in name_map:
                    for display_name in channel.findall("display-name"):
                        display_name.text = name_map[tvg_id]
            
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
    threading.Thread(target=auto_reload_m3u, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)