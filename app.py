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

FUZZY_MATCH_THRESHOLD = 70  # similarity threshold

def auto_reload_m3u(interval=172800):  # every 48 hour
    while True:
        new_config = parse_m3u_files("input/")
        checker.config = new_config
        print("[M3U Reloaded]")
        time.sleep(interval)

def normalize_name(name, epg_map=None):
    """Normalize names with EPG matching priority"""
    if not name:
        return "", "", ""
    
    original = name
    name = name.lower()
    
    # Basic cleaning
    name = re.sub(r'(?i)\s*\(.*?\)', '', name)  # Remove anything in parentheses
    name = re.sub(r'(?i)\b(?:hd|fhd|uhd|4k|sd)\s*\d*\b', '', name)  # Remove quality
    name = re.sub(r'(?i)^(?:uk\s*:|dstv\s*:|epl\s*:|ss\s*:|us\s*:|pt\s*:|ca\s*:|es\s*:|tr\s*:|lb\s*:)', '', name)
    name = re.sub(r'[^\w\s]', '', name)  # Remove punctuation
    name = re.sub(r'\s+', ' ', name).strip()
    
    # Try to match with EPG first
    if epg_map:
        best_match, score, key = process.extractOne(name, epg_map.keys(), scorer=fuzz.token_set_ratio)
        if score >= 70:  # Good enough match
            return epg_map[key], key, name  # (display_name, tvg-id, normalized_name)
    
    # Fallback normalization
    name = re.sub(r'(?i)((?:bt|tnt|sky|ss|supersport)\s+(?:sports?\s+)?(\d+)).*', r'\1\2', name)
    display_name = " ".join([word.capitalize() for word in name.split()])
    tvg_id = re.sub(r'\s+', '', name.lower()) + ".uk"  # Default fallback ID
    
    return display_name, tvg_id, name

def load_epg_map(epg_path="input/guide.xml"):
    """Create mapping from EPG data: {normalized_name: (display_name, tvg-id)}"""
    epg_map = {}
    try:
        tree = ET.parse(epg_path)
        for channel in tree.findall(".//channel"):
            tvg_id = channel.get("id", "")
            for name in channel.findall("display-name"):
                if name.text:
                    norm_name = name.text.lower()
                    norm_name = re.sub(r'[^\w\s]', '', norm_name)
                    epg_map[norm_name] = (name.text.strip(), tvg_id)
    except Exception as e:
        print(f"EPG parsing error: {e}")
    return epg_map

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
    epg_map = load_epg_map(os.path.join(m3u_folder, "guide.xml"))

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

    grouped = defaultdict(list)
    for entry in entries:
        display_name, tvg_id, norm_name = normalize_name(entry['name'], epg_map)
        
        # Update entry with normalized values
        entry.update({
            'canonical_name': display_name,
            'tvg-id': entry.get('tvg-id') or tvg_id,
            'tvg-name': entry.get('tvg-name') or display_name,
            'group-title': entry.get('group-title') or display_name
        })
        
        grouped[display_name].append(entry)
    
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
        
        for channel, entry in active_streams.items():
            if not entry:
                continue
                
            extinf = (f"#EXTINF:-1 tvg-id=\"{entry['tvg-id']}\" "
                    f"tvg-name=\"{entry['tvg-name']}\" "
                    f"tvg-logo=\"{entry.get('tvg-logo', '')}\" "
                    f"group-title=\"{entry['group-title']}\","
                    f"{entry['canonical_name']}")
            
            m3u += f"{extinf}\n{entry['url']}\n"
        
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
