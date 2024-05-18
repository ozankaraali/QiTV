import json
import requests
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import threading
from io import BytesIO

app = Flask(__name__)
CORS(app)

config_path = 'config.json'

def load_config():
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def save_config(config):
    with open(config_path, 'w') as f:
        json.dump(config, f)

@app.route('/config', methods=['GET', 'POST', 'PUT', 'DELETE'])
def handle_config():
    config = load_config()
    if request.method == 'GET':
        return jsonify(config)
    elif request.method == 'POST':
        new_config = request.json
        save_config(new_config)
        return jsonify(new_config)
    elif request.method == 'PUT':
        new_provider = request.json
        config['data'].append(new_provider)
        save_config(config)
        return jsonify(config)
    elif request.method == 'DELETE':
        index = request.json.get('index')
        config['data'].pop(index)
        save_config(config)
        return jsonify(config)

@app.route('/createLink', methods=['POST'])
def create_link():
    data = request.json
    cmd, type = data.get('cmd'), data.get('type')
    config = load_config()
    selected_provider = config['data'][config['selected']]
    url, options = selected_provider['url'], selected_provider.get('options', {})
    if type == 'STB':
        try:
            fetchurl = f"{url}/server/load.php?type=itv&action=create_link&type=itv&cmd={requests.utils.quote(cmd)}&JsHttpRequest=1-xml"
            response = requests.get(fetchurl, headers=options['headers'])
            result = response.json()
            link = result['js']['cmd'].split(' ')[-1]
            return jsonify({'link': link})
        except Exception as e:
            return jsonify({'error': 'Failed to create link', 'details': str(e)}), 500
    return jsonify({'link': cmd})

@app.route('/proxy', methods=['GET'])
def proxy():
    stream_url = request.args.get('url')
    if stream_url:
        config = load_config()
        selected_provider = config['data'][config['selected']]
        headers = selected_provider.get('options', {}).get('headers', {})
        try:
            r = requests.get(stream_url, headers=headers, stream=True)
            return send_file(BytesIO(r.content), attachment_filename='stream', mimetype='application/octet-stream')
        except requests.RequestException as e:
            return jsonify({'error': str(e)}), 500
    return jsonify({'error': 'Bad request'}), 400

def run_server():
    app.run(port=8000)

if __name__ == '__main__':
    threading.Thread(target=run_server, daemon=True).start()
