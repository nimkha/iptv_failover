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

def normalize_name(name):
    """Normalize channel names while preserving main channel numbers"""
    if not name:
        return ""
        
    name = name.lower()
    
    # Remove resolution/quality indicators and regional prefixes
    name = re.sub(r'(?i)\s*\(.*?\)', '', name)  # Remove anything in parentheses
    name = re.sub(r'(?i)\b(?:hd|fhd|uhd|4k|sd|uk|us|ca|au|de|pt|fr)\s*\d*\b', '', name)
    name = re.sub(r'(?i)^(?:uk:|dstv:|epl\s?:|ss:|us:|pt:|ca:|es:|tr:|lb:|tnt\s?:|bt\s?:)', '', name)
    
    # Handle channel numbers - preserve main number but remove version numbers
    # Example: "BT Sport 1 HD 2" → "bt sport 1"
    name = re.sub(r'(?i)((?:bt|tnt|sky|ss|supersport)\s+(?:sports?\s+)?(\d+)).*', r'\1', name)
    
    # Clean up remaining special characters and spaces
    name = re.sub(r'[^\w\s]', '', name)
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

        # Try EPG matching first with lower threshold
        if norm_epg_names:
            result = process.extractOne(norm_name, norm_epg_names.keys(), 
                                     scorer=fuzz.token_set_ratio)  # Changed to token_set_ratio
            if result and result[1] >= 70:  # Lowered threshold
                best_match = norm_epg_names[result[0]]

        # Fallback to cleaned version of current name
        if not best_match:
            # Additional cleaning for sports channels
            clean_name = norm_name
            for term in ['live', 'football', 'match', 'stream']:
                clean_name = clean_name.replace(term, '')
            clean_name = re.sub(r'\s+', ' ', clean_name).strip()
            best_match = " ".join([word.capitalize() for word in clean_name.split()])

        # Preserve all original metadata
        grouped_entry = entry.copy()
        grouped_entry['canonical_name'] = best_match if best_match else norm_name
        grouped[grouped_entry['canonical_name']].append(grouped_entry)

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
                
            if isinstance(entry, str):
                m3u += f"#EXTINF:-1,{channel}\n{entry}\n"
            else:
                # Use the canonical name for consistency
                display_name = entry.get('canonical_name', channel)
                extinf = entry.get('extinf')
                if not extinf:
                    extinf = (f'#EXTINF:-1 tvg-id="{entry.get("tvg-id", "")}" '
                            f'tvg-name="{display_name}" '
                            f'tvg-logo="{entry.get("tvg-logo", "")}" '
                            f'group-title="{entry.get("group-title", "")}",'
                            f'{display_name}')
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
