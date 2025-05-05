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
    """Normalize channel names while preserving main channel numbers"""
    if not name:
        return "Unknown", "unknown.uk"
    
    original = name
    name = name.lower()
    
    # Remove resolution/quality indicators and regional prefixes
    name = re.sub(r'(?i)\s*\(.*?\)', '', name)  # Remove anything in parentheses
    name = re.sub(r'(?i)\b(?:hd|fhd|uhd|4k|sd)\s*\d*\b', '', name)
    name = re.sub(r'(?i)^(?:uk\s*:|dstv\s*:|epl\s*:|ss\s*:|us\s*:|pt\s*:|ca\s*:|es\s*:|tr\s*:|lb\s*:)', '', name)
    
    # Handle channel numbers - preserve main number but remove version numbers
    name = re.sub(r'(?i)((?:bt|tnt|sky|ss|supersport)\s+(?:sports?\s+)?(\d+)).*', r'\1\2', name)
    
    # Clean up remaining special characters and spaces
    name = re.sub(r'[^\w\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    
    # Generate tvg-id (lowercase, no spaces)
    tvg_id = re.sub(r'\s+', '', name.lower()) + '.uk'
    
    # Specific common replacements
    replacements = {
        'supersport': 'ss',
        'premier league': 'epl',
        'main event': '',
        'grandstand': '',
        'dstv': '',
        'tnt': 'bt'  # Standardize TNT to BT
    }
    for old, new in replacements.items():
        name = name.replace(old, new)
    
    name = re.sub(r'\s+', ' ', name).strip()
    display_name = " ".join([word.capitalize() for word in name.split()])
    
    return display_name, tvg_id

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
    """Parse M3U files with EPG integration and dynamic channel mapping"""
    entries = []
    epg_map = load_epg_map(os.path.join(m3u_folder, "guide.xml"))
    
    # Parse M3U files
    for m3u_file in glob.glob(f"{m3u_folder}/*.m3u"):
        try:
            with open(m3u_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            logger.info(f"Processing M3U file: {m3u_file}")
        except Exception as e:
            logger.error(f"Error reading {m3u_file}: {e}")
            continue

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
        try:
            original_name = entry['name']
            display_name, tvg_id = normalize_name(original_name)
            
            # Try to match with EPG
            epg_match = None
            for epg_name, epg_tvg_id in epg_map.items():
                if fuzz.ratio(display_name.lower(), epg_name.lower()) > 85:
                    epg_match = (epg_name, epg_tvg_id)
                    break
            
            if epg_match:
                display_name, tvg_id = epg_match
                logger.debug(f"EPG matched '{original_name}' to '{display_name}'")
            
            entry.update({
                'canonical_name': display_name,
                'tvg-id': entry.get('tvg-id', tvg_id),
                'tvg-name': display_name,
                'group-title': display_name
            })
            
            grouped[display_name].append(entry)
            
        except Exception as e:
            logger.error(f"Error processing entry {original_name}: {e}")
            continue
    
    logger.info(f"Successfully processed {len(grouped)} channel groups")
    return {"channels": grouped}

def create_app():
    global checker
    config = parse_m3u_files("input/")
    logger.info("Loaded channels:")
    for name, urls in config["channels"].items():
        logger.info(f"- {name}: {len(urls)} stream(s)")

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