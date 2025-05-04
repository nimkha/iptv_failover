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

FUZZY_MATCH_THRESHOLD = 80  # similarity threshold

def auto_reload_m3u(interval=172800):  # every 48 hour
    while True:
        new_config = parse_m3u_files("input/")
        checker.config = new_config
        print("[M3U Reloaded]")
        time.sleep(interval)

def normalize_name(name):
    """Normalize channel names with better case handling and pattern matching"""
    if not name:
        return ""
        
    name = name.lower()
    # Remove resolution/region tags (case-insensitive)
    name = re.sub(r'(?i)\s*\(.*?\)|\[.*?\]', '', name) # Remove things in () or []
    name = re.sub(r'(?i)\b(hd|fhd|uhd|4k|sd|uk|us|ca|au|de|pt|fr)\d*\b', '', name) # Remove quality + digit suffix
    name = re.sub(r'(?i)\b(hd|fhd|uhd|4k|sd|uk|us|ca|au|de|pt|fr)\b', '', name) # Also remove any that don't have digits
    name = re.sub(r'(?i)^(uk:|dstv:|epl\s?:|ss:|us:|pt:|ca:|es:|tr:|lb:)', '', name)
    name = re.sub(r'[^\w\s]', '', name)  # Remove punctuation
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def load_epg_display_names(epg_path="input/guide.xml"):
    """Extract a list of display names from EPG XML."""
    try:
        tree = ET.parse(epg_path)
        root = tree.getroot()
        display_names = set()

        for channel in root.findall("channel"):
            for name in channel.findall("display-name"):
                if name.text:
                    display_names.add(name.text.strip())

        return list(display_names)
    except Exception as e:
        print(f"WARNING: Failed to parse EPG: {e}")
        return []

def parse_m3u_entry(extinf_line):
    """Parse EXTINF line with all attributes"""
    entry = {
        'tvg-id': '',
        'tvg-name': '',
        'tvg-logo': '',
        'group-title': '',
        'name': '',
        'extinf': extinf_line
    }
    
    # Extract attributes
    attr_match = re.search(r'#EXTINF:-1\s+(.*?),', extinf_line)
    if attr_match:
        attrs = attr_match.group(1)
        for attr in attrs.split(' '):
            if '=' in attr:
                key, val = attr.split('=', 1)
                val = val.strip('"')
                if key in entry:
                    entry[key] = val
    
    # Extract display name (after last comma)
    name_match = extinf_line.rsplit(',', 1)
    if len(name_match) > 1:
        entry['name'] = name_match[1].strip()
    
    return entry

def parse_m3u_files(m3u_folder="input/"):
    entries = []  # Will store dicts instead of tuples

    for m3u_file in glob.glob(f"{m3u_folder}/*.m3u"):
        with open(m3u_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        current_entry = None
        for line in lines:
            line = line.strip()
            if line.startswith("#EXTINF"):
                current_entry = parse_m3u_entry(line)
            elif line.startswith("http") and current_entry:
                current_entry['url'] = line
                entries.append(current_entry)
                current_entry = None

    # Grouping logic
    grouped = defaultdict(list)
    epg_names = load_epg_display_names(os.path.join(m3u_folder, "guide.xml"))
    norm_epg_names = {normalize_name(name): name for name in epg_names}

    for entry in entries:
        norm_name = normalize_name(entry['name'])
        best_match = None
        best_score = 0

        # Try EPG matching first
        if norm_epg_names:
            result = process.extractOne(norm_name, norm_epg_names.keys(), 
                                      scorer=fuzz.token_sort_ratio)
            if result and result[1] >= FUZZY_MATCH_THRESHOLD:
                best_match = norm_epg_names[result[0]]

        # Fallback to cleaned version of current name
        if not best_match:
            best_match = " ".join([word.capitalize() for word in norm_name.split()])

        # Preserve all original metadata in the grouped entry
        grouped_entry = entry.copy()
        grouped_entry['canonical_name'] = best_match
        grouped[best_match].append(grouped_entry)

    return {"channels": grouped}

def create_app():
    global checker
    config = parse_m3u_files("input/")
    print("Loaded channels:")
    for name, urls in config["channels"].items():
        print(f"- {name}: {len(urls)} stream(s)")

    checker = StreamChecker(config)

    # Start automatic health monitor
    threading.Thread(target=checker.background_monitor, daemon=True).start()

    flask_app = Flask(__name__)
    
    @flask_app.route("/playlist.m3u")
    def serve_playlist():
        m3u = "#EXTM3U\n"
        active_streams = checker.get_active_streams()
        
        for channel, entries in active_streams.items():
            if not entries:
                continue
                
            # Use first entry's metadata as representative
            entry = entries[0]
            m3u += (f"#EXTINF:-1 tvg-id=\"{entry.get('tvg-id', '')}\" "
                f"tvg-name=\"{entry.get('canonical_name', channel)}\" "
                f"tvg-logo=\"{entry.get('tvg-logo', '')}\" "
                f"group-title=\"{entry.get('group-title', '')}\","
                f"{entry.get('canonical_name', channel)}\n")
            m3u += f"{entry['url']}\n"
        
        return Response(m3u, mimetype="application/x-mpegURL")

    @flask_app.route("/failover/<channel>")
    def failover_channel(channel):
        checker.mark_stream_failed(channel)
        return f"Failover triggered for channel: {channel}\n"

    return flask_app


# Used by Gunicorn
app = create_app()

if __name__ == "__main__":
    threading.Thread(target=auto_reload_m3u, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
