#!/bin/bash
set -e
echo "[*] GHOST VPS installer"
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv nginx
id -u ghost >/dev/null 2>&1 || useradd -m -s /bin/bash ghost
mkdir -p /opt/ghost
cp -r . /opt/ghost/
chown -R ghost:ghost /opt/ghost
cd /opt/ghost
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
cat > /etc/systemd/system/ghost.service << 'EOF'
[Unit]
Description=GHOST Jitbit Sender
After=network.target

[Service]
User=ghost
WorkingDirectory=/opt/ghost
ExecStart=/opt/ghost/venv/bin/gunicorn -w 4 -b 127.0.0.1:8080 app:app
Restart=always

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now ghost
cat > /etc/nginx/sites-available/ghost << 'EOF'
server {
    listen 80 default_server;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
EOF
ln -sf /etc/nginx/sites-available/ghost /etc/nginx/sites-enabled/ghost
rm -f /etc/nginx/sites-enabled/default
systemctl reload nginx
echo "[+] GHOST running on port 80"
echo "[+] Open http://$(curl -s ifconfig.me)"
