#!/usr/bin/env python3
import os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))
port = int(os.environ.get('PORT', sys.argv[1] if len(sys.argv) > 1 else '3000'))
import http.server, socketserver
handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(('', port), handler) as httpd:
    print(f'Serving at http://localhost:{port}', flush=True)
    httpd.serve_forever()
